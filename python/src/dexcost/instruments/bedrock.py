"""Auto-instrumentation for AWS Bedrock.

Monkey-patches ``botocore.client.BaseClient._make_api_call`` using
:pypi:`wrapt` so that native ``InvokeModel`` and ``Converse`` calls to
bedrock-runtime are automatically recorded as ``llm_call`` events, including
their streaming variants.

Token usage extraction handles the varying response formats across model
families (Anthropic on Bedrock, Amazon Titan, Meta Llama).

Usage::

    from dexcost import CostTracker, instrument_bedrock

    tracker = CostTracker()
    instrument_bedrock(tracker)

    # All subsequent bedrock-runtime InvokeModel calls inside a
    # tracked task are captured automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import suppress
from decimal import Decimal
from typing import Any

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.capabilities import apply_event_capability, get_capability
from dexcost.context import (
    _current_task,
    get_current_task,
    set_current_task,
    suppress_network_event,
)
from dexcost.idempotency import (
    IdempotencyKey,
    apply_event_idempotency,
    capture_idempotency_key,
)
from dexcost.instruments._capture import provider_capture_wrapper
from dexcost.instruments._errors import (
    finalize_failed_auto_task,
    record_call_failure,
    record_stream_failure,
)
from dexcost.instruments._provider_metering import (
    OperationMeasurement,
    ProviderOperationSession,
    ProviderUsageLine,
    record_provider_operation,
)
from dexcost.models.capability import CapabilityIdentity
from dexcost.models.event import Event
from dexcost.models.provider_job import ProviderJobStatus
from dexcost.provider_jobs import ProviderJobSession, reconcile_provider_job

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}

_SUPPORTED_OPERATIONS = {
    "ApplyGuardrail",
    "InvokeModel",
    "InvokeModelWithResponseStream",
    "Converse",
    "ConverseStream",
    "CountTokens",
    "StartAsyncInvoke",
    "GetAsyncInvoke",
}
_STREAMING_OPERATIONS = {"InvokeModelWithResponseStream", "ConverseStream"}


def _operation_identity(operation_name: Any) -> str:
    return {
        "InvokeModel": "bedrock.invoke_model",
        "InvokeModelWithResponseStream": "bedrock.invoke_model_stream",
        "Converse": "bedrock.converse",
        "ConverseStream": "bedrock.converse_stream",
        "ApplyGuardrail": "bedrock.apply_guardrail",
        "CountTokens": "bedrock.count_tokens",
        "StartAsyncInvoke": "bedrock.async_invoke.start",
        "GetAsyncInvoke": "bedrock.async_invoke.get",
    }.get(str(operation_name), "bedrock.invoke_model")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def instrument_bedrock(tracker: Any) -> None:
    """Monkey-patch botocore to capture Bedrock InvokeModel calls automatically.

    Patches ``botocore.client.BaseClient._make_api_call``.  Only calls where
    the service is ``bedrock-runtime`` and the operation is ``InvokeModel`` are
    intercepted; all other boto3 calls pass through unmodified.

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            price calls and persist events.

    Raises:
        ImportError: If the ``botocore`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched

    if _patched:
        raise RuntimeError(
            "Bedrock instrumentation is already active. "
            "Call uninstrument_bedrock() before re-instrumenting."
        )

    # Verify botocore is importable
    try:
        import botocore.client as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'botocore' package is required for Bedrock auto-instrumentation. "
            "Install it with: pip install boto3"
        ) from exc

    _active_tracker = tracker

    # Store originals for uninstrument
    from botocore.client import BaseClient

    _originals["_make_api_call"] = BaseClient._make_api_call

    # Apply monkey-patch via wrapt
    wrapt.wrap_function_wrapper(
        "botocore.client",
        "BaseClient._make_api_call",
        provider_capture_wrapper("bedrock", _make_api_call_wrapper),
    )

    # Nova Sonic's official Python 3.12+ path is a separate Smithy client,
    # not boto3. Keep it optional so the ordinary Bedrock extra remains
    # available on every Python version supported by DexCost.
    try:
        from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient

        _originals["smithy_bidi"] = (
            BedrockRuntimeClient.invoke_model_with_bidirectional_stream
        )
        wrapt.wrap_function_wrapper(
            "aws_sdk_bedrock_runtime.client",
            "BedrockRuntimeClient.invoke_model_with_bidirectional_stream",
            provider_capture_wrapper("bedrock", _smithy_bidi_wrapper),
        )
    except ImportError:
        pass

    _patched = True


def uninstrument_bedrock() -> None:
    """Remove Bedrock monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched

    if not _patched:
        return

    try:
        from botocore.client import BaseClient

        if "_make_api_call" in _originals:
            BaseClient._make_api_call = _originals["_make_api_call"]
    except ImportError:
        pass
    try:
        from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient

        if "smithy_bidi" in _originals:
            BedrockRuntimeClient.invoke_model_with_bidirectional_stream = _originals[
                "smithy_bidi"
            ]
    except ImportError:
        pass

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _record_call_failure(
    exc: BaseException,
    start_time: float,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    auto_task_obj: Any = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event | None:
    """Record a raised Bedrock invoke as a failed operation. Never raises."""
    try:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
    except Exception:  # pragma: no cover - defensive
        latency_ms = None
    model: str | None = None
    operation = "bedrock.invoke_model"
    try:
        operation_name = args[0] if args else kwargs.get("operation_name")
        operation = _operation_identity(operation_name)
        api_params = args[1] if len(args) > 1 else kwargs.get("api_params", {})
        if isinstance(api_params, dict):
            raw_model = api_params.get("modelId")
            if isinstance(raw_model, str) and raw_model.strip():
                model = _canonical_model(raw_model.strip())
    except Exception:  # pragma: no cover - defensive
        model = None
    event = record_call_failure(
        tracker=_active_tracker,
        exc=exc,
        provider="aws_bedrock",
        model=model,
        latency_ms=latency_ms,
        service_name="bedrock_runtime",
        details={
            "attribution_component": "llm",
            "attribution_operation_name": operation,
            "attribution_operation_status": "failed",
            "attribution_resource_type": "model",
            "attribution_resource_id": model or "unknown",
            "attribution_usage_lines": [
                {"metric": "request_count", "quantity": "1", "unit": "Requests"}
            ],
            "provider_usage_privacy": "quantities_only",
            **_provider_metadata_details(_response_metadata(getattr(exc, "response", None))),
        },
        capability=capability,
        idempotency_key=idempotency_key,
    )
    finalize_failed_auto_task(_active_tracker, auto_task_obj, event)
    return event


def _provider_session(
    *,
    task_type: str,
    service: str,
    operation: str,
    model: str,
) -> ProviderOperationSession:
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=task_type,
        provider="aws_bedrock",
        service=service,
        operation=operation,
        component="external",
        model=model,
        event_type="external_cost",
    )


def _apply_guardrail_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    api_params: dict[str, Any],
) -> Any:
    session = _provider_session(
        task_type="bedrock.apply_guardrail",
        service="bedrock_guardrails",
        operation="bedrock.apply_guardrail",
        model="bedrock-guardrail",
    )
    try:
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        usage = response.get("usage") if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        guardrail_usage = _guardrail_usage(
            {"invocationMetrics": {"usage": usage}}
        )
        metadata = _response_metadata(response)
        action = response.get("action") if isinstance(response, dict) else None
        dimensions = (
            (("guardrail_action", action.lower()[:256]),)
            if isinstance(action, str) and action
            else ()
        )
        session.succeed(
            OperationMeasurement(
                pricing_usage=guardrail_usage,
                usage_lines=tuple(
                    ProviderUsageLine(metric, quantity, "Units")
                    for metric, quantity in sorted(guardrail_usage.items())
                ),
                provider_record_id=metadata.get("request_id"),
                provider_retry_count=metadata.get("retry_count"),
                response_model="bedrock-guardrail",
                billing_dimensions=dimensions,
            )
        )
        return response
    finally:
        session.release_context()


def _count_tokens_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    api_params: dict[str, Any],
) -> Any:
    raw_model = api_params.get("modelId")
    model = _canonical_model(raw_model) if isinstance(raw_model, str) else "unknown"
    session = _provider_session(
        task_type="bedrock.count_tokens",
        service="bedrock_runtime",
        operation="bedrock.count_tokens",
        model=model,
    )
    try:
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        tokens = (
            _non_negative_int(response.get("inputTokens"))
            if isinstance(response, dict)
            else None
        )
        metadata = _response_metadata(response)
        session.succeed(
            OperationMeasurement(
                # CountTokens is a separate API operation. Its returned model
                # tokens describe the payload but are not an inference charge.
                pricing_usage={},
                usage_lines=(
                    ()
                    if tokens is None
                    else (ProviderUsageLine("input_tokens", tokens, "Tokens"),)
                ),
                provider_record_id=metadata.get("request_id"),
                provider_retry_count=metadata.get("retry_count"),
                response_model=model,
            )
        )
        return response
    finally:
        session.release_context()


def _async_invoke_identity(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _start_async_invoke_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    api_params: dict[str, Any],
) -> Any:
    raw_model = api_params.get("modelId")
    model = _canonical_model(raw_model) if isinstance(raw_model, str) else "unknown"
    session = ProviderJobSession(
        tracker=_active_tracker,
        task_type="bedrock.async_invoke.start",
        provider="aws_bedrock",
        service="bedrock_async_invoke",
        operation="bedrock.async_invoke.start",
        component="external",
        event_type="external_cost",
        resource_type="model",
        resource_id=model,
        billing_dimensions=(("output_destination", "s3"),),
    )
    try:
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        invocation_arn = response.get("invocationArn") if isinstance(response, dict) else None
        record_id = _async_invoke_identity(invocation_arn)
        if record_id is None:
            session.fail(ValueError("Bedrock async invoke response omitted invocationArn"))
            return response
        session.submit(record_id)
        return response
    finally:
        session.release_context()


def _get_async_invoke_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    api_params: dict[str, Any],
) -> Any:
    start_time = time.perf_counter()
    with suppress_network_event():
        response = wrapped(*args, **kwargs)
    record_id = _async_invoke_identity(api_params.get("invocationArn"))
    if record_id is None or _active_tracker is None or not isinstance(response, dict):
        return response
    previous = _active_tracker._storage.get_provider_job(
        "aws_bedrock", "bedrock_async_invoke", record_id
    )
    if previous is None:
        return response
    raw_status = response.get("status")
    status: ProviderJobStatus
    measurement: OperationMeasurement | None = None
    if raw_status == "InProgress":
        status = "running"
    elif raw_status == "Completed":
        status = "succeeded"
        # The result/usage is stored in S3, not returned by GetAsyncInvoke.
        # Record provider-observed completion but assert neither units nor cost
        # that this polling response cannot prove.
        measurement = OperationMeasurement(
            pricing_usage={},
            usage_lines=(ProviderUsageLine("request_count", 1, "Requests"),),
            response_model=previous.resource_id,
        )
    elif raw_status == "Failed":
        status = "failed"
    else:
        status = "unknown"
    try:
        reconcile_provider_job(
            tracker=_active_tracker,
            provider="aws_bedrock",
            service="bedrock_async_invoke",
            provider_record_id=record_id,
            status=status,
            measurement=measurement,
            latency_ms=max(0, int((time.perf_counter() - start_time) * 1000)),
            error_type=("bedrock_async_invoke_failed" if status == "failed" else None),
        )
    except Exception:
        _log.debug("dexcost: failed to reconcile Bedrock async invoke", exc_info=True)
    return response


class _NovaSonicMeter:
    """Accumulate provider-reported cumulative Nova Sonic usage totals."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.input_text = 0
        self.input_audio = 0
        self.output_text = 0
        self.output_audio = 0
        self.tool_calls = 0

    def observe(self, item: Any) -> None:
        value = getattr(item, "value", None)
        raw = getattr(value, "bytes_", None)
        if not isinstance(raw, (str, bytes, bytearray)):
            return
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return
        if not isinstance(decoded, dict):
            return
        envelope = decoded.get("event")
        if not isinstance(envelope, dict):
            return
        usage_event = envelope.get("usageEvent")
        if not isinstance(usage_event, dict):
            usage_event = envelope.get("usage_event")
        if isinstance(usage_event, dict):
            details = usage_event.get("details")
            totals = details.get("total") if isinstance(details, dict) else None
            if isinstance(totals, dict):
                input_usage = totals.get("input")
                output_usage = totals.get("output")
                if isinstance(input_usage, dict):
                    self.input_text = _non_negative_int(
                        input_usage.get("textTokens")
                    ) or 0
                    self.input_audio = _non_negative_int(
                        input_usage.get("speechTokens")
                    ) or 0
                if isinstance(output_usage, dict):
                    self.output_text = _non_negative_int(
                        output_usage.get("textTokens")
                    ) or 0
                    self.output_audio = _non_negative_int(
                        output_usage.get("speechTokens")
                    ) or 0
        if isinstance(envelope.get("toolUse"), dict) or isinstance(
            envelope.get("tool_use"), dict
        ):
            self.tool_calls += 1

    def measurement(self) -> OperationMeasurement:
        values = (
            ("input_tokens", self.input_text, "Tokens"),
            ("input_audio_tokens", self.input_audio, "Tokens"),
            ("output_tokens", self.output_text, "Tokens"),
            ("output_audio_tokens", self.output_audio, "Tokens"),
            ("tool_call_count", self.tool_calls, "Calls"),
        )
        return OperationMeasurement(
            pricing_usage={
                metric: quantity
                for metric, quantity, _unit in values
                if quantity > 0 and metric != "tool_call_count"
            },
            usage_lines=tuple(
                ProviderUsageLine(metric, quantity, unit)
                for metric, quantity, unit in values
                if quantity > 0
            ),
            response_model=self.model,
            billing_dimensions=(("stream_mode", "bidirectional"),),
            task_input_tokens=self.input_text + self.input_audio,
            task_output_tokens=self.output_text + self.output_audio,
        )


class _SmithyOutputReceiver:
    def __init__(
        self,
        receiver: Any,
        session: ProviderOperationSession,
        meter: _NovaSonicMeter,
    ) -> None:
        self._receiver = receiver
        self._session = session
        self._meter = meter

    async def receive(self) -> Any:
        try:
            item = await self._receiver.receive()
        except BaseException as exc:
            self._session.fail(exc, self._meter.measurement())
            raise
        if item is None:
            self._session.succeed(self._meter.measurement())
            return None
        if "exception" in type(item).__name__.lower():
            self._session.fail(
                RuntimeError("bedrock bidirectional stream error"),
                self._meter.measurement(),
            )
            return item
        try:
            self._meter.observe(item)
        except Exception:
            _log.debug("dexcost: failed to observe Nova Sonic output", exc_info=True)
        return item

    def __aiter__(self) -> _SmithyOutputReceiver:
        return self

    async def __anext__(self) -> Any:
        item = await self.receive()
        if item is None:
            await self.close()
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        try:
            await self._receiver.close()
        finally:
            self._session.cancel(self._meter.measurement())

    async def __aenter__(self) -> _SmithyOutputReceiver:
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.close()


class _SmithyDuplexStream:
    def __init__(
        self,
        stream: Any,
        session: ProviderOperationSession,
        meter: _NovaSonicMeter,
    ) -> None:
        self._stream = stream
        self._session = session
        self._meter = meter
        self.input_stream = stream.input_stream
        self.output = None
        self.output_stream: _SmithyOutputReceiver | None = None

    async def await_output(self) -> tuple[Any, _SmithyOutputReceiver]:
        if self.output_stream is not None:
            return self.output, self.output_stream
        output, receiver = await self._stream.await_output()
        self.output = output
        self.output_stream = _SmithyOutputReceiver(receiver, self._session, self._meter)
        return output, self.output_stream

    async def close(self) -> None:
        try:
            await self._stream.close()
        finally:
            self._session.cancel(self._meter.measurement())

    async def __aenter__(self) -> _SmithyDuplexStream:
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _smithy_bidi_wrapper(
    wrapped: Any,
    instance: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        input_value = args[0] if args else kwargs.get("input")
        raw_model = getattr(input_value, "model_id", None)
        model = _canonical_model(raw_model) if isinstance(raw_model, str) else "unknown"
        session = ProviderOperationSession(
            tracker=_active_tracker,
            task_type="bedrock.invoke_model_bidirectional_stream",
            provider="aws_bedrock",
            service="bedrock_runtime",
            operation="bedrock.invoke_model_bidirectional_stream",
            component="voice_platform",
            model=model,
            event_type="external_cost",
        )
        try:
            try:
                with suppress_network_event():
                    stream = await wrapped(*args, **kwargs)
            except BaseException as exc:
                session.fail(exc)
                raise
            meter = _NovaSonicMeter(model)
            session.release_context()
            return _SmithyDuplexStream(stream, session, meter)
        finally:
            session.release_context()

    return invoke()


def _make_api_call_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``BaseClient._make_api_call``.

    Intercepts the native InvokeModel/Converse sync and streaming operations.
    All other calls pass through unmodified.
    """
    # args: (operation_name, api_params)
    operation_name = args[0] if args else kwargs.get("operation_name")

    # Check if this is a bedrock-runtime invoke call
    service_name = _get_service_name(instance)
    if service_name != "bedrock-runtime" or operation_name not in _SUPPORTED_OPERATIONS:
        return wrapped(*args, **kwargs)

    api_params = args[1] if len(args) > 1 else kwargs.get("api_params", {})
    if not isinstance(api_params, dict):
        api_params = {}
    if operation_name == "ApplyGuardrail":
        return _apply_guardrail_call(wrapped, args, kwargs, api_params)
    if operation_name == "CountTokens":
        return _count_tokens_call(wrapped, args, kwargs, api_params)
    if operation_name == "StartAsyncInvoke":
        return _start_async_invoke_call(wrapped, args, kwargs, api_params)
    if operation_name == "GetAsyncInvoke":
        return _get_async_invoke_call(wrapped, args, kwargs, api_params)

    streaming = operation_name in _STREAMING_OPERATIONS

    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_name = (
            "bedrock.converse"
            if operation_name in {"Converse", "ConverseStream"}
            else "bedrock.invoke"
        )
        auto_task_obj = create_auto_task(auto_task_name)
        task = auto_task_obj
        auto_token = set_current_task(auto_task_obj)

    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    try:
        start_time = time.perf_counter()

        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(
                exc,
                start_time,
                args,
                kwargs,
                auto_task_obj,
                capability,
                idempotency_key,
            )
            raise
        if streaming:
            # Streaming: wrap the response body EventStream so usage is
            # captured once the caller fully consumes the stream.
            return _wrap_stream_response(
                response,
                start_time,
                api_params,
                task,
                auto_task_obj,
                capability,
                idempotency_key,
                operation_name=str(operation_name),
            )

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            if operation_name == "Converse":
                event = _record_from_converse_response(
                    response,
                    latency_ms,
                    api_params,
                    task,
                    capability,
                    idempotency_key,
                )
            else:
                event = _record_from_response(
                    response,
                    latency_ms,
                    api_params,
                    task,
                    capability,
                    idempotency_key,
                )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

        if auto and auto_task_obj is not None and event is not None:
            try:
                finalize_auto_task(auto_task_obj, event, status="success")
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(auto_task_obj)
            except Exception:
                _log.debug("dexcost: failed to finalize auto-task", exc_info=True)

        return response
    except Exception:
        if auto and auto_task_obj is not None:
            with suppress(Exception):
                _log.debug("dexcost: auto-task call failed", exc_info=True)
        raise
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _wrap_stream_response(
    response: Any,
    start_time: float,
    api_params: dict[str, Any],
    task: Any = None,
    auto_task_obj: Any = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
    operation_name: str = "InvokeModelWithResponseStream",
) -> Any:
    """Wrap a native Bedrock stream so usage is captured on consume."""
    try:
        model_id = api_params.get("modelId", "unknown") if api_params else "unknown"
        stream_key = "stream" if operation_name == "ConverseStream" else "body"
        body = response.get(stream_key) if isinstance(response, dict) else None
        if body is not None:
            response[stream_key] = _StreamBodyWrapper(
                body,
                start_time,
                model_id,
                task,
                auto_task_obj,
                _response_metadata(response),
                capability,
                idempotency_key,
                operation=_operation_identity(operation_name),
            )
    except Exception:
        _log.debug("dexcost: failed to wrap stream body", exc_info=True)
    return response


def _extract_stream_tokens(payload: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Extract input/output/cache tokens from one decoded stream event.

    Bedrock streaming chunks vary by model family.  The final chunk of an
    ``InvokeModelWithResponseStream`` response carries
    ``amazon-bedrock-invocationMetrics`` with ``inputTokenCount`` and
    ``outputTokenCount`` for every model family.  Anthropic models also
    distribute usage across ``message_start`` / ``message_delta`` events.
    """
    # Universal: invocation metrics on the terminal chunk.
    metrics = payload.get("amazon-bedrock-invocationMetrics")
    if isinstance(metrics, dict):
        return (
            int(metrics.get("inputTokenCount", 0) or 0),
            int(metrics.get("outputTokenCount", 0) or 0),
            int(metrics.get("cacheReadInputTokenCount", 0) or 0),
            int(metrics.get("cacheWriteInputTokenCount", 0) or 0),
            _cache_write_1h_tokens(metrics),
        )

    # ConverseStream emits authoritative usage in its terminal metadata event.
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        usage = metadata.get("usage")
        if isinstance(usage, dict):
            return (
                int(usage.get("inputTokens", 0) or 0),
                int(usage.get("outputTokens", 0) or 0),
                int(usage.get("cacheReadInputTokens", 0) or 0),
                int(usage.get("cacheWriteInputTokens", 0) or 0),
                _cache_write_1h_tokens(usage),
            )

    # Anthropic-on-Bedrock streaming events.
    chunk_type = payload.get("type")
    if chunk_type == "message_start":
        usage = payload.get("message", {}).get("usage", {})
        if isinstance(usage, dict):
            return (
                int(usage.get("input_tokens", 0) or 0),
                0,
                int(usage.get("cache_read_input_tokens", 0) or 0),
                int(usage.get("cache_creation_input_tokens", 0) or 0),
                _cache_write_1h_tokens(usage),
            )
    if chunk_type == "message_delta":
        usage = payload.get("usage", {})
        if isinstance(usage, dict):
            return (0, int(usage.get("output_tokens", 0) or 0), 0, 0, 0)

    # Generic ``usage`` object on a chunk.
    usage = payload.get("usage")
    if isinstance(usage, dict):
        input_t = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_t = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        return (
            int(input_t),
            int(output_t),
            int(usage.get("cache_read_input_tokens", 0) or 0),
            int(usage.get("cache_creation_input_tokens", 0) or 0),
            _cache_write_1h_tokens(usage),
        )

    return (0, 0, 0, 0, 0)


def _extract_tool_call_count(payload: dict[str, Any]) -> int:
    """Count provider-returned tool invocations without retaining arguments."""
    content = payload.get("content")
    if isinstance(content, list):
        return sum(
            1
            for item in content
            if isinstance(item, dict) and item.get("type") in {"tool_use", "tool_call"}
        )

    calls = payload.get("tool_calls")
    if isinstance(calls, list):
        return len(calls)

    # Anthropic-on-Bedrock streaming announces a tool invocation when its
    # content block starts. Arguments arrive later and are deliberately ignored.
    if payload.get("type") == "content_block_start":
        block = payload.get("content_block")
        if isinstance(block, dict) and block.get("type") in {"tool_use", "tool_call"}:
            return 1
    converse_start = payload.get("contentBlockStart")
    if isinstance(converse_start, dict):
        start = converse_start.get("start")
        if isinstance(start, dict) and isinstance(start.get("toolUse"), dict):
            return 1
    return 0


def _decode_stream_event(event: Any) -> dict[str, Any]:
    """Decode one botocore EventStream event into a JSON payload dict.

    Bedrock stream events are shaped as ``{"chunk": {"bytes": b"...json..."}}``.
    """
    if not isinstance(event, dict):
        return {}
    # ConverseStream events are already decoded dictionaries.
    if any(
        key in event
        for key in (
            "messageStart",
            "contentBlockStart",
            "contentBlockDelta",
            "contentBlockStop",
            "messageStop",
            "metadata",
        )
    ):
        return event
    chunk = event.get("chunk")
    if not isinstance(chunk, dict):
        return {}
    raw = chunk.get("bytes")
    if raw is None:
        return {}
    try:
        if isinstance(raw, bytes):
            return dict(json.loads(raw.decode("utf-8")))
        return dict(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return {}


class _StreamBodyWrapper(Iterator[Any]):
    """Wraps a Bedrock ``InvokeModelWithResponseStream`` body EventStream.

    Iterating yields the original events untouched; token usage is
    accumulated from each decoded chunk and an ``llm_call`` event is
    recorded once the stream is fully consumed.
    """

    def __init__(
        self,
        stream: Any,
        start_time: float,
        model_id: str,
        task: Any = None,
        auto_task_obj: Any = None,
        response_metadata: dict[str, Any] | None = None,
        capability: CapabilityIdentity | None = None,
        idempotency_key: IdempotencyKey | None = None,
        operation: str = "bedrock.invoke_model_stream",
    ) -> None:
        self._stream = stream
        # botocore EventStream is iterable; obtain its iterator once.
        self._iter = iter(stream)
        self._start_time = start_time
        self._model_id = model_id
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_read_input_tokens = 0
        self._cache_write_input_tokens = 0
        self._cache_write_input_tokens_1h = 0
        self._tool_calls = 0
        self._dimensions: list[dict[str, Any]] = []
        self._additional_usage: dict[str, int] = {}
        self._unpriced_dimensions: list[str] = []
        self._finalized = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._response_metadata = response_metadata or {}
        self._capability = capability
        self._idempotency_key = idempotency_key
        self._operation = operation

    def __iter__(self) -> _StreamBodyWrapper:
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._iter)
            self._process_event(event)
            return event
        except StopIteration:
            self._finalize()
            raise
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _record_failure(self, exc: BaseException) -> None:
        """Persist a provider error raised while the stream was being consumed.

        Marks the wrapper finalized so the success path can no longer fire: a
        stream that died mid-flight has no trustworthy usage total, and
        recording one would overstate what the provider actually delivered.
        """
        if self._finalized:
            return
        self._finalized = True
        record_stream_failure(
            tracker=_active_tracker,
            exc=exc,
            start_time=self._start_time,
            provider="aws_bedrock",
            model=_canonical_model(self._model_id),
            task=self._task,
            auto_task_obj=self._auto_task_obj,
            service_name="bedrock_runtime",
            details={
                "attribution_component": "llm",
                "attribution_operation_name": self._operation,
                "attribution_operation_status": "failed",
                "attribution_resource_type": "model",
                "attribution_resource_id": _canonical_model(self._model_id),
                "attribution_usage_lines": _usage_lines(
                    self._input_tokens,
                    self._output_tokens,
                    self._tool_calls,
                    self._cache_read_input_tokens,
                    self._cache_write_input_tokens,
                    self._additional_usage,
                ),
                "provider_usage_privacy": "quantities_only",
                **_provider_metadata_details(self._response_metadata),
                **_cache_creation_details(
                    self._cache_write_input_tokens,
                    self._cache_write_input_tokens_1h,
                ),
                **(
                    {"attribution_dimensions": self._dimensions}
                    if self._dimensions
                    else {}
                ),
                **(
                    {"pricing_unpriced_dimensions": self._unpriced_dimensions}
                    if self._unpriced_dimensions
                    else {}
                ),
            },
            capability=self._capability,
            idempotency_key=self._idempotency_key,
            input_tokens=self._input_tokens or None,
            output_tokens=self._output_tokens or None,
            cached_tokens=self._cache_read_input_tokens or None,
        )

    def _process_event(self, event: Any) -> None:
        payload = _decode_stream_event(event)
        if not payload:
            return
        in_t, out_t, cache_read, cache_write, cache_write_1h = (
            _extract_stream_tokens(payload)
        )
        # Invocation metrics and Converse metadata carry final totals;
        # otherwise usage is distributed across model-specific deltas.
        if "amazon-bedrock-invocationMetrics" in payload or "metadata" in payload:
            self._input_tokens = in_t
            self._output_tokens = out_t
            self._cache_read_input_tokens = cache_read
            self._cache_write_input_tokens = cache_write
            self._cache_write_input_tokens_1h = cache_write_1h
            if "metadata" in payload and isinstance(payload["metadata"], dict):
                invoked_model, dimensions, additional_usage, unpriced = (
                    _response_context(payload["metadata"])
                )
                if invoked_model is not None:
                    self._model_id = invoked_model
                self._dimensions = dimensions
                self._additional_usage = additional_usage
                self._unpriced_dimensions = unpriced
        else:
            self._input_tokens += in_t
            self._output_tokens += out_t
            self._cache_read_input_tokens += cache_read
            self._cache_write_input_tokens += cache_write
            self._cache_write_input_tokens_1h += cache_write_1h
        self._tool_calls += _extract_tool_call_count(payload)

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream(
                self._model_id,
                self._input_tokens,
                self._output_tokens,
                latency_ms,
                self._task,
                self._response_metadata,
                "succeeded",
                self._tool_calls,
                self._capability,
                self._idempotency_key,
                self._operation,
                self._cache_read_input_tokens,
                self._cache_write_input_tokens,
                self._cache_write_input_tokens_1h,
                self._dimensions,
                self._additional_usage,
                self._unpriced_dimensions,
            )
            if self._auto_task_obj is not None and event is not None:
                finalize_auto_task(self._auto_task_obj, event, status="success")
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(self._auto_task_obj)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def close(self) -> None:
        try:
            if hasattr(self._stream, "close"):
                self._stream.close()
        finally:
            self._finalize_cancelled()

    def __enter__(self) -> _StreamBodyWrapper:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        try:
            result = None
            if hasattr(self._stream, "__exit__"):
                result = self._stream.__exit__(*args)
        except BaseException as exc:
            self._record_failure(exc)
            raise
        if not self._finalized:
            self._finalize_cancelled()
        return result

    def _finalize_cancelled(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream(
                self._model_id,
                self._input_tokens,
                self._output_tokens,
                latency_ms,
                self._task,
                self._response_metadata,
                "cancelled",
                self._tool_calls,
                self._capability,
                self._idempotency_key,
                self._operation,
                self._cache_read_input_tokens,
                self._cache_write_input_tokens,
                self._cache_write_input_tokens_1h,
                self._dimensions,
                self._additional_usage,
                self._unpriced_dimensions,
            )
            if self._auto_task_obj is not None and event is not None:
                finalize_auto_task(self._auto_task_obj, event, status="failed")
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(self._auto_task_obj)
        except Exception:
            _log.debug("dexcost: failed to record cancelled stream", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_service_name(client_instance: Any) -> str:
    """Extract the AWS service name from a botocore client instance."""
    meta = getattr(client_instance, "_service_model", None)
    if meta is not None:
        name = getattr(meta, "service_name", None)
        if name:
            return str(name)
    # Fallback: try endpoint_prefix or the meta attribute
    meta2 = getattr(client_instance, "meta", None)
    if meta2 is not None:
        name2 = getattr(meta2, "service_model", None)
        if name2 is not None:
            sn = getattr(name2, "service_name", None)
            if sn:
                return str(sn)
    return "unknown"


def _parse_response_body(response: dict[str, Any]) -> dict[str, Any]:
    """Parse the response body from a Bedrock InvokeModel response.

    Bedrock returns the body as a ``StreamingBody``.  We read and parse it
    as JSON.  The body is replaced with the parsed dict so the caller can
    still access it.
    """
    body = response.get("body")
    if body is None:
        return {}

    try:
        if hasattr(body, "read"):
            raw = body.read()
            # Replace body with a readable version for the caller
            import io

            response["body"] = io.BytesIO(raw)
            if isinstance(raw, bytes):
                return dict(json.loads(raw.decode("utf-8")))
            return dict(json.loads(raw))
        elif isinstance(body, (str, bytes)):
            return dict(json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    return {}


def _extract_tokens(body: dict[str, Any], model_id: str) -> tuple[int, int]:
    """Extract input/output token counts from the response body.

    Token location varies by model family:
      - Anthropic on Bedrock: body["usage"]["input_tokens"], ["output_tokens"]
      - Amazon Titan: body["inputTextTokenCount"], body["results"][0]["tokenCount"]
      - Meta Llama: body["prompt_token_count"], body["generation_token_count"]
      - Cohere on Bedrock: body["token_count"]["input_tokens"], ["output_tokens"]
      - AI21: body["usage"]["prompt_tokens"], ["completion_tokens"]
    """
    model_lower = model_id.lower()

    # Anthropic models on Bedrock
    if "anthropic" in model_lower or "claude" in model_lower:
        usage = body.get("usage", {})
        return (
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
        )

    # Amazon Titan models
    if "titan" in model_lower or "amazon" in model_lower:
        input_tokens = int(body.get("inputTextTokenCount", 0))
        results = body.get("results", [])
        output_tokens = 0
        if results and isinstance(results, list):
            output_tokens = int(results[0].get("tokenCount", 0))
        return (input_tokens, output_tokens)

    # Meta Llama models
    if "meta" in model_lower or "llama" in model_lower:
        return (
            int(body.get("prompt_token_count", 0)),
            int(body.get("generation_token_count", 0)),
        )

    # Cohere models on Bedrock
    if "cohere" in model_lower:
        token_count = body.get("token_count", {})
        return (
            int(token_count.get("input_tokens", 0)),
            int(token_count.get("output_tokens", 0)),
        )

    # AI21 models
    if "ai21" in model_lower or "jamba" in model_lower:
        usage = body.get("usage", {})
        return (
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )

    # Generic fallback: try common patterns
    usage = body.get("usage", {})
    if usage:
        input_t = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_t = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        return (int(input_t), int(output_t))

    return (0, 0)


def _cache_write_1h_tokens(usage: dict[str, Any]) -> int:
    """Return cache-creation tokens written with the one-hour TTL."""
    nested = usage.get("cache_creation")
    if isinstance(nested, dict):
        return max(0, int(nested.get("ephemeral_1h_input_tokens", 0) or 0))
    direct = usage.get("cache_creation_input_tokens_1h")
    if direct is not None:
        return max(0, int(direct or 0))
    details = usage.get("cacheDetails")
    if details is None:
        details = usage.get("cache_details")
    if not isinstance(details, list):
        return 0
    total = 0
    for item in details:
        if not isinstance(item, dict) or str(item.get("ttl", "")).lower() != "1h":
            continue
        total += max(
            0,
            int(item.get("inputTokens") or item.get("input_tokens") or 0),
        )
    return total


def _extract_cache_tokens(usage_owner: dict[str, Any]) -> tuple[int, int, int]:
    """Return disjoint cache-read/write buckets from native response usage."""
    usage = usage_owner.get("usage")
    if not isinstance(usage, dict):
        return (0, 0, 0)
    return (
        int(
            usage.get("cacheReadInputTokens")
            or usage.get("cache_read_input_tokens")
            or 0
        ),
        int(
            usage.get("cacheWriteInputTokens")
            or usage.get("cache_creation_input_tokens")
            or 0
        ),
        _cache_write_1h_tokens(usage),
    )


def _extract_converse_tool_call_count(response: dict[str, Any]) -> int:
    output = response.get("output")
    if not isinstance(output, dict):
        return 0
    message = output.get("message")
    if not isinstance(message, dict):
        return 0
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for block in content
        if isinstance(block, dict) and isinstance(block.get("toolUse"), dict)
    )


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _canonical_model(model_id: str) -> str:
    """Return a pricing-safe Bedrock model identity.

    Plain foundation-model and inference-profile IDs are preserved exactly:
    regional prefixes such as ``us.`` and ``eu.`` can select different AWS
    rates.  ARNs are never persisted verbatim because they can contain an AWS
    account ID.  Public foundation-model/profile ARNs retain only their model
    resource ID; account-scoped resource classes degrade to a bounded generic
    identity because their underlying billable model is not encoded there.
    """
    candidate = model_id.strip() or "unknown"
    if not candidate.startswith("arn:"):
        return candidate
    parts = candidate.split(":", 5)
    if len(parts) != 6:
        return "bedrock-resource"
    resource = parts[5]
    resource_type, separator, resource_id = resource.partition("/")
    if not separator:
        resource_type, separator, resource_id = resource.partition(":")
    if separator and resource_type in {"foundation-model", "inference-profile"}:
        return resource_id or f"bedrock-{resource_type}"
    normalized_type = resource_type.strip().lower().replace("_", "-")
    if normalized_type and all(
        char.isalnum() or char in {"-", "."} for char in normalized_type
    ):
        return f"bedrock-{normalized_type}"[:128]
    return "bedrock-resource"


def _usage_lines(
    input_tokens: int,
    output_tokens: int,
    tool_calls: int = 0,
    cache_read_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    additional_usage: Mapping[str, int] | None = None,
) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    for metric, quantity, unit in (
        ("input_tokens", input_tokens, "Tokens"),
        ("output_tokens", output_tokens, "Tokens"),
        ("cache_read_input_tokens", cache_read_input_tokens, "Tokens"),
        ("cache_write_input_tokens", cache_write_input_tokens, "Tokens"),
        ("tool_call_count", tool_calls, "Calls"),
    ):
        if quantity > 0:
            lines.append({"metric": metric, "quantity": str(quantity), "unit": unit})
    for metric, quantity in sorted((additional_usage or {}).items()):
        if quantity <= 0:
            continue
        unit = "Requests" if metric == "intelligent_prompt_routing_requests" else "Units"
        lines.append({"metric": metric, "quantity": str(quantity), "unit": unit})
    return lines or [{"metric": "request_count", "quantity": "1", "unit": "Requests"}]


def _cache_creation_details(total: int, one_hour: int) -> dict[str, int]:
    """Return non-overlapping cache-write detail fields for persisted events."""
    safe_total = max(0, total)
    safe_one_hour = min(max(0, one_hour), safe_total)
    if safe_total == 0:
        return {}
    details = {"cache_creation_input_tokens": safe_total}
    if safe_one_hour > 0:
        details["cache_creation_input_tokens_1h"] = safe_one_hour
    five_minutes = safe_total - safe_one_hour
    if five_minutes > 0:
        details["cache_creation_input_tokens_5m"] = five_minutes
    return details


def _snake_case(value: str) -> str:
    characters: list[str] = []
    for index, character in enumerate(value):
        if character.isupper() and index > 0 and characters[-1] != "_":
            characters.append("_")
        characters.append(character.lower())
    return "".join(characters)


def _guardrail_usage(trace: Any) -> dict[str, int]:
    """Aggregate billing-safe guardrail unit counts without retaining trace content."""
    totals: dict[str, int] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            invocation_metrics = node.get("invocationMetrics")
            if not isinstance(invocation_metrics, dict):
                invocation_metrics = node.get("invocation_metrics")
            if isinstance(invocation_metrics, dict):
                usage = invocation_metrics.get("usage")
                if isinstance(usage, dict):
                    for raw_name, raw_quantity in usage.items():
                        if not isinstance(raw_name, str):
                            continue
                        metric_name = _snake_case(raw_name)
                        if not metric_name.endswith("_units"):
                            continue
                        if (
                            not isinstance(raw_quantity, int)
                            or isinstance(raw_quantity, bool)
                            or raw_quantity <= 0
                        ):
                            continue
                        metric = f"guardrail_{metric_name}"
                        totals[metric] = totals.get(metric, 0) + raw_quantity
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(trace)
    return totals


def _response_context(
    response: Mapping[str, Any],
) -> tuple[str | None, list[dict[str, Any]], dict[str, int], list[str]]:
    """Extract pricing-relevant Converse response metadata only."""
    invoked_model: str | None = None
    dimensions: dict[str, str] = {}
    additional_usage: dict[str, int] = {}
    unpriced: set[str] = set()

    trace = response.get("trace")
    if isinstance(trace, dict):
        prompt_router = trace.get("promptRouter")
        if not isinstance(prompt_router, dict):
            prompt_router = trace.get("prompt_router")
        if isinstance(prompt_router, dict):
            raw_model = prompt_router.get("invokedModelId") or prompt_router.get(
                "invoked_model_id"
            )
            if isinstance(raw_model, str) and raw_model.strip():
                invoked_model = raw_model.strip()
                dimensions["prompt_router_used"] = "true"
                additional_usage["intelligent_prompt_routing_requests"] = 1
                unpriced.add("intelligent_prompt_routing_requests")
        guardrail = _guardrail_usage(trace)
        additional_usage.update(guardrail)
        unpriced.update(
            metric
            for metric, quantity in guardrail.items()
            if quantity > 0 and not metric.endswith("_free_units")
        )

    service_tier = response.get("serviceTier")
    if not isinstance(service_tier, dict):
        service_tier = response.get("service_tier")
    if isinstance(service_tier, dict):
        tier = service_tier.get("type")
        if isinstance(tier, str) and tier in {"default", "priority", "flex", "reserved"}:
            dimensions["service_tier"] = tier
            if tier != "default":
                unpriced.add("service_tier")

    performance = response.get("performanceConfig")
    if not isinstance(performance, dict):
        performance = response.get("performance_config")
    if isinstance(performance, dict):
        latency = performance.get("latency")
        if isinstance(latency, str) and latency in {"standard", "optimized"}:
            dimensions["inference_latency"] = latency
            if latency == "optimized":
                unpriced.add("inference_latency")

    typed_dimensions = [
        {"key": key, "value": {"type": "string", "value": value}}
        for key, value in sorted(dimensions.items())
    ]
    return invoked_model, typed_dimensions, additional_usage, sorted(unpriced)


def _response_metadata(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    raw = response.get("ResponseMetadata")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    request_id = raw.get("RequestId")
    if isinstance(request_id, str) and request_id.strip():
        result["request_id"] = request_id.strip()[:256]
    retry_count = raw.get("RetryAttempts")
    if isinstance(retry_count, int) and not isinstance(retry_count, bool) and retry_count >= 0:
        result["retry_count"] = retry_count
    return result


def _provider_metadata_details(metadata: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    request_id = metadata.get("request_id")
    if isinstance(request_id, str):
        details["provider_record_id"] = request_id
    retry_count = metadata.get("retry_count")
    if isinstance(retry_count, int) and retry_count >= 0:
        details["provider_retry_count"] = retry_count
        details["provider_attempt_count"] = retry_count + 1
    return details


def _request_json(api_params: Mapping[str, Any]) -> dict[str, Any]:
    """Decode an InvokeModel request transiently and return configuration only."""
    raw = api_params.get("body")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, (str, bytes, bytearray)):
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _image_count(body: Mapping[str, Any]) -> int:
    for key in ("images", "artifacts"):
        values = body.get(key)
        if isinstance(values, list):
            return len(values)
    return 0


def _embedding_input_tokens(body: Mapping[str, Any]) -> int | None:
    usage = body.get("usage")
    if isinstance(usage, dict):
        for key in ("inputTokens", "input_tokens", "totalTokens", "total_tokens"):
            count = _non_negative_int(usage.get(key))
            if count is not None:
                return count
    for key in ("inputTextTokenCount", "inputTokenCount", "input_tokens"):
        count = _non_negative_int(body.get(key))
        if count is not None:
            return count
    return None


def _image_measurement(
    body: Mapping[str, Any],
    api_params: Mapping[str, Any],
    model: str,
    metadata: Mapping[str, Any],
) -> OperationMeasurement:
    count = _image_count(body)
    request = _request_json(api_params)
    raw_config = request.get("imageGenerationConfig")
    config = raw_config if isinstance(raw_config, dict) else request
    width = _non_negative_int(config.get("width"))
    height = _non_negative_int(config.get("height"))
    steps = _non_negative_int(config.get("steps"))
    quality = config.get("quality")
    quality = quality.lower() if isinstance(quality, str) else None
    dimensions: list[tuple[str, str]] = []
    for key, value in (("image_width", width), ("image_height", height)):
        if value is not None and value > 0:
            dimensions.append((key, str(value)))
    if steps is not None and steps > 0:
        dimensions.append(("image_steps", str(steps)))
    if quality:
        dimensions.append(("image_quality", quality[:256]))

    pricing_metric = "output_image_count"
    above_1024 = (width or 0) > 1024 or (height or 0) > 1024
    premium = quality == "premium"
    if above_1024 and premium:
        pricing_metric = "output_image_count_above_1024_premium"
    elif above_1024:
        pricing_metric = "output_image_count_above_1024"
    elif premium:
        pricing_metric = "output_image_count_premium"

    candidates: list[str] = []
    if width and height and steps:
        size_steps = f"{width}-x-{height}/{steps}-steps"
        candidates.extend((f"{size_steps}/bedrock/{model}", f"{size_steps}/{model}"))
    request_id = metadata.get("request_id")
    retry_count = metadata.get("retry_count")
    return OperationMeasurement(
        pricing_usage={} if count == 0 else {pricing_metric: count},
        usage_lines=(
            ()
            if count == 0
            else (ProviderUsageLine("output_image_count", count, "Images"),)
        ),
        provider_record_id=request_id if isinstance(request_id, str) else None,
        provider_retry_count=retry_count if isinstance(retry_count, int) else None,
        response_model=model,
        model_candidates=tuple(candidates),
        billing_dimensions=tuple(sorted(dimensions)),
    )


def _metered_invoke_measurement(
    mode: str,
    body: Mapping[str, Any],
    api_params: Mapping[str, Any],
    model: str,
    metadata: Mapping[str, Any],
) -> OperationMeasurement:
    if mode in {"image_generation", "image_edit"}:
        return _image_measurement(body, api_params, model, metadata)
    request_id = metadata.get("request_id")
    retry_count = metadata.get("retry_count")
    if mode == "embedding":
        tokens = _embedding_input_tokens(body)
        return OperationMeasurement(
            pricing_usage={} if tokens is None else {"input_tokens": tokens},
            usage_lines=(
                ()
                if tokens is None
                else (ProviderUsageLine("input_tokens", tokens, "Tokens"),)
            ),
            provider_record_id=request_id if isinstance(request_id, str) else None,
            provider_retry_count=retry_count if isinstance(retry_count, int) else None,
            response_model=model,
            task_input_tokens=tokens,
        )
    if mode == "rerank":
        return OperationMeasurement(
            pricing_usage={"query_count": 1},
            usage_lines=(ProviderUsageLine("query_count", 1, "Queries"),),
            provider_record_id=request_id if isinstance(request_id, str) else None,
            provider_retry_count=retry_count if isinstance(retry_count, int) else None,
            response_model=model,
        )
    raise ValueError(f"unsupported metered Bedrock mode {mode!r}")


def _record_from_response(
    response: dict[str, Any],
    latency_ms: int,
    api_params: dict[str, Any],
    task: Any,
    capability: CapabilityIdentity | None,
    idempotency_key: IdempotencyKey | None,
) -> Event | None:
    """Extract fields from a Bedrock InvokeModel response and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    if task is None:
        return None

    model_id: str = api_params.get("modelId", "unknown") if api_params else "unknown"

    # Parse the response body to extract token counts
    body = _parse_response_body(response)
    canonical_model = _canonical_model(model_id)
    mode = tracker._pricing.model_mode(canonical_model)
    if mode in {"embedding", "image_generation", "image_edit", "rerank"}:
        measurement = _metered_invoke_measurement(
            mode,
            body,
            api_params,
            canonical_model,
            _response_metadata(response),
        )
        return record_provider_operation(
            tracker=tracker,
            task=task,
            provider="aws_bedrock",
            service="bedrock_runtime",
            operation="bedrock.invoke_model",
            component="external",
            event_type="external_cost",
            model=canonical_model,
            measurement=measurement,
            latency_ms=latency_ms,
            capability=capability,
            idempotency_key=idempotency_key,
        )
    input_tokens, output_tokens = _extract_tokens(body, model_id)
    (
        cache_read_input_tokens,
        cache_write_input_tokens,
        cache_write_input_tokens_1h,
    ) = _extract_cache_tokens(body)
    tool_calls = _extract_tool_call_count(body)
    has_usage = any(
        (input_tokens, output_tokens, cache_read_input_tokens, cache_write_input_tokens)
    )

    # Extract a cleaner model name (strip provider prefix if present)
    return _insert_llm_event(
        tracker=tracker,
        task=task,
        model=canonical_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        operation="bedrock.invoke_model",
        operation_status="succeeded",
        response_metadata=_response_metadata(response),
        tool_calls=tool_calls,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        cache_write_input_tokens_1h=cache_write_input_tokens_1h,
        capability=capability,
        idempotency_key=idempotency_key,
    )


def _record_from_converse_response(
    response: dict[str, Any],
    latency_ms: int,
    api_params: dict[str, Any],
    task: Any,
    capability: CapabilityIdentity | None,
    idempotency_key: IdempotencyKey | None,
) -> Event | None:
    """Record the model-independent Bedrock Converse response contract."""
    tracker = _active_tracker
    if tracker is None or task is None:
        return None
    model_id: str = api_params.get("modelId", "unknown") if api_params else "unknown"
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = int(usage.get("inputTokens", 0) or 0)
    output_tokens = int(usage.get("outputTokens", 0) or 0)
    cache_read_input_tokens = int(usage.get("cacheReadInputTokens", 0) or 0)
    cache_write_input_tokens = int(usage.get("cacheWriteInputTokens", 0) or 0)
    cache_write_input_tokens_1h = _cache_write_1h_tokens(usage)
    has_usage = bool(usage)
    invoked_model, dimensions, additional_usage, unpriced_dimensions = (
        _response_context(response)
    )
    return _insert_llm_event(
        tracker=tracker,
        task=task,
        model=_canonical_model(invoked_model or model_id),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        operation="bedrock.converse",
        operation_status="succeeded",
        response_metadata=_response_metadata(response),
        tool_calls=_extract_converse_tool_call_count(response),
        cache_read_input_tokens=cache_read_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        cache_write_input_tokens_1h=cache_write_input_tokens_1h,
        dimensions=dimensions,
        additional_usage=additional_usage,
        unpriced_dimensions=unpriced_dimensions,
        capability=capability,
        idempotency_key=idempotency_key,
    )


def _record_from_stream(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    task: Any,
    response_metadata: dict[str, Any],
    operation_status: str,
    tool_calls: int,
    capability: CapabilityIdentity | None,
    idempotency_key: IdempotencyKey | None,
    operation: str = "bedrock.invoke_model_stream",
    cache_read_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    cache_write_input_tokens_1h: int = 0,
    dimensions: list[dict[str, Any]] | None = None,
    additional_usage: dict[str, int] | None = None,
    unpriced_dimensions: list[str] | None = None,
) -> Event | None:
    """Record an event from accumulated Bedrock stream usage data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    if task is None:
        return None

    has_usage = any(
        (input_tokens, output_tokens, cache_read_input_tokens, cache_write_input_tokens)
    )

    # Strip provider prefix (e.g. "anthropic.claude-v2" -> "claude-v2").
    return _insert_llm_event(
        tracker=tracker,
        task=task,
        model=_canonical_model(model_id),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        operation=operation,
        operation_status=operation_status,
        response_metadata=response_metadata,
        tool_calls=tool_calls,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        cache_write_input_tokens_1h=cache_write_input_tokens_1h,
        dimensions=dimensions,
        additional_usage=additional_usage,
        unpriced_dimensions=unpriced_dimensions,
        capability=capability,
        idempotency_key=idempotency_key,
    )


def _insert_llm_event(
    *,
    tracker: Any,
    task: Any,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    has_usage: bool,
    operation: str,
    operation_status: str,
    response_metadata: dict[str, Any],
    tool_calls: int,
    cache_read_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    cache_write_input_tokens_1h: int = 0,
    dimensions: list[dict[str, Any]] | None = None,
    additional_usage: dict[str, int] | None = None,
    unpriced_dimensions: list[str] | None = None,
    capability: CapabilityIdentity | None,
    idempotency_key: IdempotencyKey | None,
) -> Event:
    """Create and persist an llm_call Event."""
    if has_usage:
        cost_result = tracker._pricing.get_cost(
            model,
            input_tokens,
            output_tokens,
            cached_tokens=cache_read_input_tokens,
            cache_creation_tokens=max(
                0, cache_write_input_tokens - cache_write_input_tokens_1h
            ),
            cache_creation_tokens_1h=cache_write_input_tokens_1h,
        )
        cost_usd = cost_result.cost_usd
        cost_confidence = cost_result.cost_confidence
        pricing_source = cost_result.pricing_source
        pricing_version = cost_result.pricing_version
    else:
        cost_usd = Decimal("0")
        cost_confidence = "estimated"
        pricing_source = "unknown"
        pricing_version = None
    if unpriced_dimensions:
        cost_confidence = "unknown"

    details: dict[str, Any] = {
        "attribution_component": "llm",
        "attribution_operation_name": operation,
        "attribution_operation_status": operation_status,
        "attribution_resource_type": "model",
        "attribution_resource_id": model,
        "attribution_usage_lines": _usage_lines(
            input_tokens,
            output_tokens,
            tool_calls,
            cache_read_input_tokens,
            cache_write_input_tokens,
            additional_usage,
        ),
        "provider_usage_privacy": "quantities_only",
        **_provider_metadata_details(response_metadata),
    }
    if dimensions:
        details["attribution_dimensions"] = dimensions
    if unpriced_dimensions:
        details["pricing_unpriced_dimensions"] = sorted(set(unpriced_dimensions))
    details.update(
        _cache_creation_details(
            cache_write_input_tokens,
            cache_write_input_tokens_1h,
        )
    )
    event = Event(
        task_id=task.task_id,
        event_type="llm_call",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        service_name="bedrock_runtime",
        provider="aws_bedrock",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cache_read_input_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    apply_event_capability(event, capability)
    apply_event_idempotency(event, idempotency_key)
    tracker._storage.insert_event(event)
    return event
