"""Auto-instrumentation for AWS Bedrock.

Monkey-patches ``botocore.client.BaseClient._make_api_call`` using
:pypi:`wrapt` so that every ``InvokeModel`` call to bedrock-runtime made
inside an active :class:`~dexcost.tracker.CostTracker` task is automatically
recorded as an ``llm_call`` event.

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

import json
import logging
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.context import _current_task, get_current_task, set_current_task
from dexcost.models.event import Event

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}


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
        _make_api_call_wrapper,
    )

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

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _make_api_call_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for ``BaseClient._make_api_call``.

    Intercepts bedrock-runtime ``InvokeModel`` (non-streaming) and
    ``InvokeModelWithResponseStream`` (streaming) calls.  All other calls
    pass through unmodified.
    """
    # args: (operation_name, api_params)
    operation_name = args[0] if args else kwargs.get("operation_name")

    # Check if this is a bedrock-runtime invoke call
    service_name = _get_service_name(instance)
    if service_name != "bedrock-runtime" or operation_name not in (
        "InvokeModel",
        "InvokeModelWithResponseStream",
    ):
        return wrapped(*args, **kwargs)

    streaming = operation_name == "InvokeModelWithResponseStream"

    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("bedrock.invoke")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()

        response = wrapped(*args, **kwargs)
        api_params = args[1] if len(args) > 1 else kwargs.get("api_params", {})

        if streaming:
            # Streaming: wrap the response body EventStream so usage is
            # captured once the caller fully consumes the stream.
            return _wrap_stream_response(response, start_time, api_params)

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(response, latency_ms, api_params)
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
            try:
                _log.debug("dexcost: auto-task call failed", exc_info=True)
            except Exception:
                pass
        raise
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _wrap_stream_response(
    response: Any, start_time: float, api_params: dict[str, Any]
) -> Any:
    """Wrap a streaming InvokeModel response so usage is captured on consume."""
    try:
        model_id = api_params.get("modelId", "unknown") if api_params else "unknown"
        body = response.get("body") if isinstance(response, dict) else None
        if body is not None:
            response["body"] = _StreamBodyWrapper(body, start_time, model_id)
    except Exception:
        _log.debug("dexcost: failed to wrap stream body", exc_info=True)
    return response


def _extract_stream_tokens(payload: dict[str, Any]) -> tuple[int, int]:
    """Extract input/output tokens from a single decoded stream chunk payload.

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
        )

    # Anthropic-on-Bedrock streaming events.
    chunk_type = payload.get("type")
    if chunk_type == "message_start":
        usage = payload.get("message", {}).get("usage", {})
        if isinstance(usage, dict):
            return (int(usage.get("input_tokens", 0) or 0), 0)
    if chunk_type == "message_delta":
        usage = payload.get("usage", {})
        if isinstance(usage, dict):
            return (0, int(usage.get("output_tokens", 0) or 0))

    # Generic ``usage`` object on a chunk.
    usage = payload.get("usage")
    if isinstance(usage, dict):
        input_t = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_t = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        return (int(input_t), int(output_t))

    return (0, 0)


def _decode_stream_event(event: Any) -> dict[str, Any]:
    """Decode one botocore EventStream event into a JSON payload dict.

    Bedrock stream events are shaped as ``{"chunk": {"bytes": b"...json..."}}``.
    """
    if not isinstance(event, dict):
        return {}
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

    def __init__(self, stream: Any, start_time: float, model_id: str) -> None:
        self._stream = stream
        # botocore EventStream is iterable; obtain its iterator once.
        self._iter = iter(stream)
        self._start_time = start_time
        self._model_id = model_id
        self._input_tokens = 0
        self._output_tokens = 0
        self._finalized = False

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

    def _process_event(self, event: Any) -> None:
        payload = _decode_stream_event(event)
        if not payload:
            return
        in_t, out_t = _extract_stream_tokens(payload)
        # invocationMetrics carry final totals; otherwise accumulate deltas.
        if "amazon-bedrock-invocationMetrics" in payload:
            self._input_tokens = in_t
            self._output_tokens = out_t
        else:
            self._input_tokens += in_t
            self._output_tokens += out_t

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            _record_from_stream(
                self._model_id,
                self._input_tokens,
                self._output_tokens,
                latency_ms,
            )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def close(self) -> None:
        if hasattr(self._stream, "close"):
            self._stream.close()

    def __enter__(self) -> _StreamBodyWrapper:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._finalize()
        if hasattr(self._stream, "__exit__"):
            self._stream.__exit__(*args)


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


def _extract_tokens(
    body: dict[str, Any], model_id: str
) -> tuple[int, int]:
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


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _record_from_response(
    response: dict[str, Any], latency_ms: int, api_params: dict[str, Any]
) -> Event | None:
    """Extract fields from a Bedrock InvokeModel response and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    model_id: str = api_params.get("modelId", "unknown") if api_params else "unknown"

    # Parse the response body to extract token counts
    body = _parse_response_body(response)
    input_tokens, output_tokens = _extract_tokens(body, model_id)
    has_usage = input_tokens > 0 or output_tokens > 0

    # Extract a cleaner model name (strip provider prefix if present)
    model = model_id
    if "." in model:
        # e.g. "anthropic.claude-v2" -> "claude-v2"
        model = model.split(".", 1)[1]

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
    )


def _record_from_stream(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
) -> Event | None:
    """Record an event from accumulated Bedrock stream usage data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    has_usage = input_tokens > 0 or output_tokens > 0

    # Strip provider prefix (e.g. "anthropic.claude-v2" -> "claude-v2").
    model = model_id
    if "." in model:
        model = model.split(".", 1)[1]

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
    )


def _insert_llm_event(
    *,
    tracker: Any,
    task_id: Any,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    has_usage: bool,
) -> Event:
    """Create and persist an llm_call Event."""
    if has_usage:
        cost_result = tracker._pricing.get_cost(model, input_tokens, output_tokens)
        cost_usd = cost_result.cost_usd
        cost_confidence = "exact"
        pricing_source = cost_result.pricing_source
        pricing_version = cost_result.pricing_version
    else:
        cost_usd = Decimal("0")
        cost_confidence = "estimated"
        pricing_source = "unknown"
        pricing_version = None

    event = Event(
        task_id=task_id,
        event_type="llm_call",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        provider="aws_bedrock",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )
    tracker._storage.insert_event(event)
    return event
