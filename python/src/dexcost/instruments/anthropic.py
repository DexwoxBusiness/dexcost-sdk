"""Auto-instrumentation for the Anthropic Python SDK.

Monkey-patches ``anthropic.resources.messages.Messages.create`` (sync and async)
using :pypi:`wrapt` so that every call made inside an active
:class:`~dexcost.tracker.CostTracker` task is automatically recorded as an
``llm_call`` event.

Usage::

    from dexcost import CostTracker, instrument_anthropic

    tracker = CostTracker()
    instrument_anthropic(tracker)

    # All subsequent anthropic.messages.create() calls inside a
    # tracked task are captured automatically.

Implements US-013.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
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
    error_details,
    finalize_failed_auto_task,
    record_call_failure,
    requested_model,
)
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)
from dexcost.models.capability import CapabilityIdentity
from dexcost.models.event import Event
from dexcost.models.provider_job import ProviderJobStatus
from dexcost.provider_jobs import (
    AsyncProviderJobStream,
    ProviderJobSession,
    SyncProviderJobStream,
    reconcile_provider_job,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}
_optional_originals: list[tuple[Any, str, Any]] = []


def _patch_optional_method(
    module_name: str,
    class_name: str,
    method_name: str,
    wrapper: Any,
) -> bool:
    """Patch a current-SDK surface while retaining legacy compatibility."""
    try:
        module = importlib.import_module(module_name)
        owner = getattr(module, class_name)
        original = getattr(owner, method_name)
    except (AttributeError, ImportError):
        return False
    _optional_originals.append((owner, method_name, original))
    wrapt.wrap_function_wrapper(
        module_name,
        f"{class_name}.{method_name}",
        provider_capture_wrapper("anthropic", wrapper),
    )
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def instrument_anthropic(tracker: Any) -> None:
    """Monkey-patch the Anthropic SDK to capture LLM calls automatically.

    Patches ``anthropic.resources.messages.Messages.create`` (sync)
    and ``anthropic.resources.messages.AsyncMessages.create`` (async).

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            price calls and persist events.

    Raises:
        ImportError: If the ``anthropic`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched

    if _patched:
        raise RuntimeError(
            "Anthropic instrumentation is already active. "
            "Call uninstrument_anthropic() before re-instrumenting."
        )

    # Verify anthropic is importable
    try:
        import anthropic.resources.messages as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'anthropic' package is required for Anthropic auto-instrumentation. "
            "Install it with: pip install anthropic"
        ) from exc

    _active_tracker = tracker

    # Store originals for uninstrument
    from anthropic.resources.messages import AsyncMessages, Messages

    _originals["sync_create"] = Messages.create
    _originals["async_create"] = AsyncMessages.create

    # Apply monkey-patches via wrapt
    wrapt.wrap_function_wrapper(
        "anthropic.resources.messages",
        "Messages.create",
        provider_capture_wrapper("anthropic", _sync_create_wrapper),
    )
    wrapt.wrap_function_wrapper(
        "anthropic.resources.messages",
        "AsyncMessages.create",
        provider_capture_wrapper("anthropic", _async_create_wrapper),
    )
    _patch_optional_method(
        "anthropic.resources.beta.messages.messages",
        "Messages",
        "create",
        _sync_beta_create_wrapper,
    )
    _patch_optional_method(
        "anthropic.resources.beta.messages.messages",
        "AsyncMessages",
        "create",
        _async_beta_create_wrapper,
    )
    for module_name, beta in (
        ("anthropic.resources.messages", False),
        ("anthropic.resources.beta.messages.messages", True),
    ):
        for class_name, asynchronous in (("Messages", False), ("AsyncMessages", True)):
            _patch_optional_method(
                module_name,
                class_name,
                "count_tokens",
                _count_tokens_wrapper(beta=beta, asynchronous=asynchronous),
            )
    for class_name, asynchronous in (("Completions", False), ("AsyncCompletions", True)):
        _patch_optional_method(
            "anthropic.resources.completions",
            class_name,
            "create",
            _legacy_completion_wrapper(asynchronous=asynchronous),
        )
    for class_name, asynchronous in (("Dreams", False), ("AsyncDreams", True)):
        _patch_optional_method(
            "anthropic.resources.beta.dreams",
            class_name,
            "create",
            _dream_create_wrapper(asynchronous=asynchronous),
        )
        for method_name in ("retrieve", "cancel", "archive"):
            _patch_optional_method(
                "anthropic.resources.beta.dreams",
                class_name,
                method_name,
                _dream_reconcile_wrapper(asynchronous=asynchronous),
            )
    for class_name, asynchronous in (("Sessions", False), ("AsyncSessions", True)):
        _patch_optional_method(
            "anthropic.resources.beta.sessions.sessions",
            class_name,
            "create",
            _managed_session_create_wrapper(asynchronous=asynchronous),
        )
        for method_name in ("retrieve", "update", "archive"):
            _patch_optional_method(
                "anthropic.resources.beta.sessions.sessions",
                class_name,
                method_name,
                _managed_session_reconcile_wrapper(asynchronous=asynchronous),
            )
    for class_name, asynchronous in (("Events", False), ("AsyncEvents", True)):
        _patch_optional_method(
            "anthropic.resources.beta.sessions.events",
            class_name,
            "stream",
            _managed_session_stream_wrapper(asynchronous=asynchronous),
        )
    for module_name, class_name, method_name, operation in (
        (
            "anthropic.resources.beta.deployments",
            "Deployments",
            "run",
            "anthropic.beta.deployments.run",
        ),
        (
            "anthropic.resources.beta.deployments",
            "AsyncDeployments",
            "run",
            "anthropic.beta.deployments.run",
        ),
        (
            "anthropic.resources.beta.deployment_runs",
            "DeploymentRuns",
            "retrieve",
            "anthropic.beta.deployment_runs.retrieve",
        ),
        (
            "anthropic.resources.beta.deployment_runs",
            "AsyncDeploymentRuns",
            "retrieve",
            "anthropic.beta.deployment_runs.retrieve",
        ),
    ):
        _patch_optional_method(
            module_name,
            class_name,
            method_name,
            _deployment_run_wrapper(
                operation=operation,
                asynchronous=class_name.startswith("Async"),
            ),
        )
    for module_name, service_name in (
        ("anthropic.resources.messages.batches", "message_batches"),
        ("anthropic.resources.beta.messages.batches", "beta_message_batches"),
    ):
        for class_name, asynchronous in (("Batches", False), ("AsyncBatches", True)):
            _patch_optional_method(
                module_name,
                class_name,
                "create",
                _batch_create_wrapper(service_name, asynchronous=asynchronous),
            )
            for method_name in ("retrieve", "cancel"):
                _patch_optional_method(
                    module_name,
                    class_name,
                    method_name,
                    _batch_reconcile_wrapper(
                        service_name,
                        method_name,
                        asynchronous=asynchronous,
                    ),
                )
            _patch_optional_method(
                module_name,
                class_name,
                "results",
                _batch_results_wrapper(service_name, asynchronous=asynchronous),
            )

    _patched = True


def uninstrument_anthropic() -> None:
    """Remove Anthropic monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched

    if not _patched:
        return

    from anthropic.resources.messages import AsyncMessages, Messages

    if "sync_create" in _originals:
        Messages.create = _originals["sync_create"]  # type: ignore[method-assign]
    if "async_create" in _originals:
        AsyncMessages.create = _originals["async_create"]  # type: ignore[method-assign]

    for owner, method_name, original in reversed(_optional_originals):
        setattr(owner, method_name, original)

    _originals.clear()
    _optional_originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _record_call_failure(
    exc: BaseException,
    start_time: float,
    kwargs: dict[str, Any],
    auto_task_obj: Any = None,
    task: Any = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
    *,
    service_name: str = "messages",
    operation_name: str = "anthropic.messages.create",
) -> Event | None:
    """Record a raised Anthropic call as a failed operation. Never raises."""
    try:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
    except Exception:  # pragma: no cover - defensive
        latency_ms = None
    event = record_call_failure(
        tracker=_active_tracker,
        exc=exc,
        provider="anthropic",
        model=requested_model(kwargs),
        latency_ms=latency_ms,
        service_name=service_name,
        task=task,
        details={
            "attribution_component": "llm",
            "attribution_operation_name": operation_name,
            "attribution_operation_status": "failed",
            "attribution_resource_type": "model",
            "attribution_resource_id": requested_model(kwargs) or "unknown",
            "attribution_usage_lines": [
                {"metric": "request_count", "quantity": "1", "unit": "Requests"}
            ],
            "provider_usage_privacy": "quantities_only",
        },
        capability=capability,
        idempotency_key=idempotency_key,
    )
    finalize_failed_auto_task(_active_tracker, auto_task_obj, event)
    return event


def _sync_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Messages.create``."""
    return _sync_message_create_wrapper(
        wrapped,
        args,
        kwargs,
        task_type="anthropic.messages",
        service_name="messages",
        operation_name="anthropic.messages.create",
    )


def _sync_beta_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for current ``beta.messages.create``."""
    return _sync_message_create_wrapper(
        wrapped,
        args,
        kwargs,
        task_type="anthropic.beta.messages",
        service_name="beta_messages",
        operation_name="anthropic.beta.messages.create",
    )


def _sync_message_create_wrapper(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    task_type: str,
    service_name: str,
    operation_name: str,
) -> Any:
    """Capture one stable or Beta sync Messages call."""
    task = get_current_task()
    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task(task_type)
        auto_token = set_current_task(auto_task_obj)

    try:
        stream = kwargs.get("stream", False)
        start_time = time.perf_counter()

        if stream:
            try:
                with suppress_network_event():
                    raw_stream = wrapped(*args, **kwargs)
            except Exception as exc:
                _record_call_failure(
                    exc,
                    start_time,
                    kwargs,
                    auto_task_obj,
                    task or auto_task_obj,
                    capability,
                    idempotency_key,
                    service_name=service_name,
                    operation_name=operation_name,
                )
                raise
            return _SyncStreamWrapper(
                raw_stream,
                start_time,
                task or auto_task_obj,
                auto_task_obj,
                requested_model(kwargs),
                capability,
                idempotency_key,
                service_name,
                operation_name,
            )

        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(
                exc,
                start_time,
                kwargs,
                auto_task_obj,
                task or auto_task_obj,
                capability,
                idempotency_key,
                service_name=service_name,
                operation_name=operation_name,
            )
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(
                response,
                latency_ms,
                task or auto_task_obj,
                capability,
                idempotency_key,
                service_name=service_name,
                operation_name=operation_name,
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


def _async_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``AsyncMessages.create``."""
    return _async_message_create_wrapper(
        wrapped,
        args,
        kwargs,
        task_type="anthropic.messages",
        service_name="messages",
        operation_name="anthropic.messages.create",
    )


def _async_beta_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for current async ``beta.messages.create``."""
    return _async_message_create_wrapper(
        wrapped,
        args,
        kwargs,
        task_type="anthropic.beta.messages",
        service_name="beta_messages",
        operation_name="anthropic.beta.messages.create",
    )


def _async_message_create_wrapper(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    task_type: str,
    service_name: str,
    operation_name: str,
) -> Any:
    """Capture one stable or Beta async Messages call."""
    task = get_current_task()
    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task(task_type)

    stream = kwargs.get("stream", False)
    start_time = time.perf_counter()

    if stream:
        return _async_stream_handler(
            wrapped,
            args,
            kwargs,
            start_time,
            auto_task_obj,
            auto_token,
            task,
            capability,
            idempotency_key,
            service_name,
            operation_name,
        )

    return _async_non_stream_handler(
        wrapped,
        args,
        kwargs,
        start_time,
        auto_task_obj,
        auto_token,
        task,
        capability,
        idempotency_key,
        service_name,
        operation_name,
    )


async def _async_non_stream_handler(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    start_time: float,
    auto_task_obj: Any = None,
    auto_token: Any = None,
    task: Any = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
    service_name: str = "messages",
    operation_name: str = "anthropic.messages.create",
) -> Any:
    """Await the async create call and record the response."""
    if auto_task_obj is not None and auto_token is None:
        auto_token = set_current_task(auto_task_obj)
    try:
        try:
            with suppress_network_event():
                response = await wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(
                exc,
                start_time,
                kwargs,
                auto_task_obj,
                task or auto_task_obj,
                capability,
                idempotency_key,
                service_name=service_name,
                operation_name=operation_name,
            )
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(
                response,
                latency_ms,
                task or auto_task_obj,
                capability,
                idempotency_key,
                service_name=service_name,
                operation_name=operation_name,
            )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

        if auto_task_obj is not None and event is not None:
            try:
                finalize_auto_task(auto_task_obj, event, status="success")
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(auto_task_obj)
            except Exception:
                _log.debug("dexcost: failed to finalize auto-task", exc_info=True)

        return response
    finally:
        if auto_token is not None:
            _current_task.reset(auto_token)


async def _async_stream_handler(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    start_time: float,
    auto_task_obj: Any = None,
    auto_token: Any = None,
    task: Any = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
    service_name: str = "messages",
    operation_name: str = "anthropic.messages.create",
) -> Any:
    """Wrap async streaming to capture usage from the final events."""
    if auto_task_obj is not None and auto_token is None:
        auto_token = set_current_task(auto_task_obj)
    try:
        try:
            with suppress_network_event():
                raw_stream = await wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(
                exc,
                start_time,
                kwargs,
                auto_task_obj,
                task or auto_task_obj,
                capability,
                idempotency_key,
                service_name=service_name,
                operation_name=operation_name,
            )
            raise
        return _AsyncStreamWrapper(
            raw_stream,
            start_time,
            task or auto_task_obj,
            auto_task_obj,
            requested_model(kwargs),
            capability,
            idempotency_key,
            service_name,
            operation_name,
        )
    finally:
        if auto_token is not None:
            _current_task.reset(auto_token)


def _operation_session(
    *, task_type: str, service: str, operation: str, model: str | None
) -> ProviderOperationSession | None:
    tracker = _active_tracker
    if tracker is None:
        return None
    return ProviderOperationSession(
        tracker=tracker,
        task_type=task_type,
        provider="anthropic",
        service=service,
        operation=operation,
        component="llm",
        model=model,
        event_type="llm_call",
    )


def _count_tokens_measurement(response: Any, model: str | None) -> OperationMeasurement:
    input_tokens = _non_negative_int(_value(response, "input_tokens"))
    return OperationMeasurement(
        pricing_usage={},
        usage_lines=(ProviderUsageLine("counted_input_tokens", input_tokens, "Tokens"),),
        provider_cost_usd=Decimal(0),
        response_model=model,
        billing_dimensions=(("billing_status", "no_charge"),),
    )


def _count_tokens_wrapper(*, beta: bool, asynchronous: bool) -> Any:
    service = "beta_token_count" if beta else "token_count"
    operation = (
        "anthropic.beta.messages.count_tokens"
        if beta
        else "anthropic.messages.count_tokens"
    )

    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        model = requested_model(kwargs)
        if asynchronous:
            return _async_count_tokens_call(wrapped, args, kwargs, service, operation, model)
        session = _operation_session(
            task_type=f"anthropic.{service}",
            service=service,
            operation=operation,
            model=model,
        )
        if session is None:
            return wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        session.succeed(_count_tokens_measurement(response, model))
        return response

    return wrapper


def _async_count_tokens_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    service: str,
    operation: str,
    model: str | None,
) -> Any:
    async def invoke() -> Any:
        session = _operation_session(
            task_type=f"anthropic.{service}",
            service=service,
            operation=operation,
            model=model,
        )
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                response = await wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        session.succeed(_count_tokens_measurement(response, model))
        return response

    return invoke()


def _legacy_completion_measurement(model: str | None) -> OperationMeasurement:
    return OperationMeasurement(
        pricing_usage={"provider_usage_unreported": 1},
        usage_lines=(
            ProviderUsageLine("provider_usage_unreported", 1, "Operations"),
        ),
        response_model=model,
        billing_dimensions=(("usage_reporting", "unavailable"),),
    )


class _LegacyCompletionMeter:
    def __init__(self, model: str | None) -> None:
        self.model = model

    def observe(self, response: Any) -> None:
        self.model = _bounded_string(_value(response, "model")) or self.model

    def measurement(self) -> OperationMeasurement:
        return _legacy_completion_measurement(self.model)


def _legacy_completion_wrapper(*, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        model = requested_model(kwargs)
        if asynchronous:
            return _async_legacy_completion_call(wrapped, args, kwargs, model)
        session = _operation_session(
            task_type="anthropic.completions",
            service="completions",
            operation="anthropic.completions.create",
            model=model,
        )
        if session is None:
            return wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        meter = _LegacyCompletionMeter(model)
        if kwargs.get("stream") is True:
            return SyncProviderStream(
                response,
                session,
                observe=meter.observe,
                measurement=meter.measurement,
            )
        meter.observe(response)
        session.succeed(meter.measurement())
        return response

    return wrapper


def _async_legacy_completion_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    model: str | None,
) -> Any:
    async def invoke() -> Any:
        session = _operation_session(
            task_type="anthropic.completions",
            service="completions",
            operation="anthropic.completions.create",
            model=model,
        )
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                response = await wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        meter = _LegacyCompletionMeter(model)
        if kwargs.get("stream") is True:
            return AsyncProviderStream(
                response,
                session,
                observe=meter.observe,
                measurement=meter.measurement,
            )
        meter.observe(response)
        session.succeed(meter.measurement())
        return response

    return invoke()


_MANAGED_SESSION_PRICING_METRICS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "cache_write_input_tokens_1h",
        "web_search_calls",
        "managed_agent_active_seconds",
        "fast_mode_usage",
        "non_global_inference_usage",
    }
)


def _optional_non_negative_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() and result >= 0 else None


def _managed_session_model(resource: Any, fallback: str) -> str:
    return (
        _bounded_string(
            _value(_value(_value(resource, "agent"), "model"), "id")
        )
        or fallback
    )


def _managed_session_requested_model(kwargs: dict[str, Any]) -> str:
    agent = kwargs.get("agent")
    return (
        _bounded_string(_value(_value(agent, "model"), "id"))
        or "anthropic-managed-agent"
    )


def _managed_session_provider_cost(usage: Any) -> Decimal | None:
    list_cost = _value(usage, "list_cost")
    if _bounded_string(_value(list_cost, "currency")) != "USD":
        return None
    amount = _bounded_string(_value(list_cost, "amount"))
    if amount is None or not amount.isdigit():
        return None
    return Decimal(amount) / Decimal(100)


def _managed_session_measurement(
    resource_or_usage: Any,
    *,
    model: str,
    include_resource_metadata: bool,
) -> OperationMeasurement:
    usage = (
        _value(resource_or_usage, "usage")
        if include_resource_metadata
        else resource_or_usage
    )
    input_tokens = _optional_non_negative_int(_value(usage, "input_tokens"))
    output_tokens = _optional_non_negative_int(_value(usage, "output_tokens"))
    cached_tokens = _optional_non_negative_int(
        _value(usage, "cache_read_input_tokens")
    )
    cache_creation = _value(usage, "cache_creation")
    cache_write_5m = _optional_non_negative_int(
        _value(cache_creation, "ephemeral_5m_input_tokens")
    )
    cache_write_1h = _optional_non_negative_int(
        _value(cache_creation, "ephemeral_1h_input_tokens")
    )
    server_tools = _value(usage, "server_tool_use")
    web_searches = _optional_non_negative_int(
        _value(server_tools, "web_search_requests")
    )
    web_fetches = _optional_non_negative_int(
        _value(server_tools, "web_fetch_requests")
    )
    active_seconds = _optional_non_negative_decimal(
        _value(usage, "active_seconds")
    )
    provider_cost = _managed_session_provider_cost(usage)

    model_config = (
        _value(_value(resource_or_usage, "agent"), "model")
        if include_resource_metadata
        else None
    )
    speed = _bounded_string(_value(model_config, "speed"))
    inference_geo = _bounded_string(_value(model_config, "inference_geo"))
    pricing_usage: dict[str, Decimal | int | str] = {
        dimension: quantity
        for dimension, quantity in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cache_read_input_tokens", cached_tokens),
            ("cache_write_input_tokens", cache_write_5m),
            ("cache_write_input_tokens_1h", cache_write_1h),
            ("web_search_calls", web_searches),
            (
                "managed_agent_active_seconds",
                active_seconds if provider_cost is None else None,
            ),
            ("fast_mode_usage", 1 if speed == "fast" else None),
            (
                "non_global_inference_usage",
                1 if inference_geo not in {None, "global"} else None,
            ),
        )
        if quantity is not None and Decimal(str(quantity)) > 0
    }
    lines: list[ProviderUsageLine] = []
    for metric, quantity, unit in (
        ("input_tokens", input_tokens, "Tokens"),
        ("output_tokens", output_tokens, "Tokens"),
        ("cache_read_input_tokens", cached_tokens, "Tokens"),
        ("cache_write_input_tokens", cache_write_5m, "Tokens"),
        ("cache_write_input_tokens_1h", cache_write_1h, "Tokens"),
        ("web_search_calls", web_searches, "Calls"),
        ("web_fetch_requests", web_fetches, "Requests"),
        ("managed_agent_active_seconds", active_seconds, "Seconds"),
        ("provider_list_cost_usd", provider_cost, "USD"),
    ):
        if quantity is not None and Decimal(str(quantity)) > 0:
            lines.append(ProviderUsageLine(metric, quantity, unit))
    if speed == "fast":
        lines.append(ProviderUsageLine("fast_mode_usage", 1, "Operations"))
    if inference_geo not in {None, "global"}:
        lines.append(
            ProviderUsageLine("non_global_inference_usage", 1, "Operations")
        )
    if include_resource_metadata:
        outcomes = _value(resource_or_usage, "outcome_evaluations")
        if isinstance(outcomes, Sequence):
            lines.append(
                ProviderUsageLine(
                    "outcome_evaluation_count", len(outcomes), "Evaluations"
                )
            )
    lines.append(ProviderUsageLine("session_usage_checkpoint_count", 1, "Operations"))
    return OperationMeasurement(
        pricing_usage=pricing_usage,
        usage_lines=tuple(lines),
        provider_cost_usd=provider_cost,
        response_model=model,
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
        task_cached_tokens=cached_tokens,
    )


def _managed_session_has_consumption(resource_or_usage: Any) -> bool:
    usage = _value(resource_or_usage, "usage") or resource_or_usage
    cost = _managed_session_provider_cost(usage)
    if cost is not None and cost > 0:
        return True
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
        quantity = _optional_non_negative_int(_value(usage, field))
        if quantity is not None and quantity > 0:
            return True
    active = _optional_non_negative_decimal(_value(usage, "active_seconds"))
    return active is not None and active > 0


def _managed_session_status(resource: Any) -> ProviderJobStatus:
    raw_status = _bounded_string(_value(resource, "status"))
    if raw_status in {"running", "rescheduling"}:
        return "succeeded" if _managed_session_has_consumption(resource) else "running"
    if raw_status in {"idle", "terminated"}:
        return "succeeded"
    return "unknown"


def _managed_session_job(
    model: str,
    *,
    task_type: str = "anthropic.beta.sessions.create",
    operation: str = "anthropic.beta.sessions.create",
) -> ProviderJobSession | None:
    tracker = _active_tracker
    if tracker is None:
        return None
    return ProviderJobSession(
        tracker=tracker,
        task_type=task_type,
        provider="anthropic",
        service="managed_sessions",
        operation=operation,
        component="llm",
        event_type="llm_call",
        resource_type="session",
        resource_id=model,
    )


def _managed_session_pending_measurement(
    resource: Any, status: ProviderJobStatus
) -> OperationMeasurement:
    if status in {"submitted", "running"}:
        return OperationMeasurement(pricing_usage={}, usage_lines=())
    model = _managed_session_model(resource, "anthropic-managed-agent")
    return _managed_session_measurement(
        resource,
        model=model,
        include_resource_metadata=True,
    )


def _submit_managed_session(session: ProviderJobSession, resource: Any) -> None:
    record_id = _bounded_string(_value(resource, "id"))
    if record_id is None:
        raise ValueError("Anthropic managed session response did not include an id")
    session.resource_id = _managed_session_model(resource, session.resource_id)
    status = _managed_session_status(resource)
    session.submit(
        record_id,
        status=status,
        measurement=_managed_session_pending_measurement(resource, status),
    )


def _carry_managed_session_measurement(
    existing: Any, measurement: OperationMeasurement
) -> OperationMeasurement | None:
    lines = {(line.metric, line.unit): line for line in measurement.usage_lines}
    pricing_usage = dict(measurement.pricing_usage)
    for previous in existing.usage:
        key = (previous.metric, previous.unit)
        current = lines.get(key)
        if current is not None and current.quantity < previous.quantity:
            return None
        if current is None:
            carried = ProviderUsageLine(previous.metric, previous.quantity, previous.unit)
            lines[key] = carried
            if previous.metric in _MANAGED_SESSION_PRICING_METRICS:
                pricing_usage[previous.metric] = previous.quantity

    provider_cost = measurement.provider_cost_usd
    if existing.cost_amount is not None:
        if provider_cost is None:
            if existing.cost_source == "provider_reported":
                provider_cost = existing.cost_amount
        elif Decimal(str(provider_cost)) < existing.cost_amount:
            return None
    for current_counter, previous_counter in (
        (measurement.task_input_tokens, existing.task_input_tokens),
        (measurement.task_output_tokens, existing.task_output_tokens),
        (measurement.task_cached_tokens, existing.task_cached_tokens),
    ):
        if (
            current_counter is not None
            and previous_counter is not None
            and current_counter < previous_counter
        ):
            return None
    return OperationMeasurement(
        pricing_usage=pricing_usage,
        usage_lines=tuple(lines.values()),
        provider_cost_usd=provider_cost,
        response_model=measurement.response_model,
        task_input_tokens=(
            measurement.task_input_tokens
            if measurement.task_input_tokens is not None
            else existing.task_input_tokens
        ),
        task_output_tokens=(
            measurement.task_output_tokens
            if measurement.task_output_tokens is not None
            else existing.task_output_tokens
        ),
        task_cached_tokens=(
            measurement.task_cached_tokens
            if measurement.task_cached_tokens is not None
            else existing.task_cached_tokens
        ),
    )


def _observe_or_adopt_managed_session(resource: Any) -> None:
    tracker = _active_tracker
    record_id = _bounded_string(_value(resource, "id"))
    if tracker is None or record_id is None:
        return
    status = _managed_session_status(resource)
    measurement = _managed_session_pending_measurement(resource, status)
    existing = tracker._storage.get_provider_job(
        "anthropic", "managed_sessions", record_id
    )
    if existing is None:
        session = _managed_session_job(
            _managed_session_model(resource, "anthropic-managed-agent")
        )
        if session is not None:
            session.submit(record_id, status=status, measurement=measurement)
        return
    if status in {"submitted", "running"} and existing.terminal:
        return
    if status == "succeeded":
        carried = _carry_managed_session_measurement(existing, measurement)
        if carried is None:
            _log.warning(
                "dexcost: ignored non-monotonic Anthropic session usage for %s",
                record_id,
            )
            return
        measurement = carried
    reconcile_provider_job(
        tracker=tracker,
        provider="anthropic",
        service="managed_sessions",
        provider_record_id=record_id,
        status=status,
        measurement=measurement,
    )


def _managed_session_create_wrapper(*, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        model = _managed_session_requested_model(kwargs)
        if asynchronous:
            return _async_managed_session_create_call(wrapped, args, kwargs, model)
        session = _managed_session_job(model)
        if session is None:
            return wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                resource = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        try:
            _submit_managed_session(session, resource)
        except Exception:
            _log.debug("dexcost: failed to persist Anthropic session", exc_info=True)
        finally:
            session.release_context()
        return resource

    return wrapper


def _async_managed_session_create_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    model: str,
) -> Any:
    async def invoke() -> Any:
        session = _managed_session_job(model)
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                resource = await wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        try:
            _submit_managed_session(session, resource)
        except Exception:
            _log.debug("dexcost: failed to persist async Anthropic session", exc_info=True)
        finally:
            session.release_context()
        return resource

    return invoke()


def _managed_session_reconcile_wrapper(*, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if asynchronous:
            return _async_managed_session_reconcile_call(wrapped, args, kwargs)
        with suppress_network_event():
            resource = wrapped(*args, **kwargs)
        try:
            _observe_or_adopt_managed_session(resource)
        except Exception:
            _log.debug("dexcost: failed to reconcile Anthropic session", exc_info=True)
        return resource

    return wrapper


def _async_managed_session_reconcile_call(
    wrapped: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        try:
            _observe_or_adopt_managed_session(resource)
        except Exception:
            _log.debug("dexcost: failed to reconcile async Anthropic session", exc_info=True)
        return resource

    return invoke()


class _ManagedSessionStreamMeter:
    def __init__(self, record_id: str) -> None:
        self.record_id = record_id

    def observe(self, event: Any) -> None:
        if _bounded_string(_value(event, "type")) != "session.usage":
            return
        tracker = _active_tracker
        if tracker is None:
            return
        existing = tracker._storage.get_provider_job(
            "anthropic", "managed_sessions", self.record_id
        )
        if existing is None:
            session = _managed_session_job("anthropic-managed-agent")
            if session is None:
                return
            session.submit(self.record_id, status="running")
            existing = tracker._storage.get_provider_job(
                "anthropic", "managed_sessions", self.record_id
            )
        if existing is None:
            return
        measurement = _managed_session_measurement(
            _value(event, "usage"),
            model=existing.resource_id,
            include_resource_metadata=False,
        )
        carried = _carry_managed_session_measurement(existing, measurement)
        if carried is None:
            _log.warning(
                "dexcost: ignored non-monotonic Anthropic session stream usage for %s",
                self.record_id,
            )
            return
        reconcile_provider_job(
            tracker=tracker,
            provider="anthropic",
            service="managed_sessions",
            provider_record_id=self.record_id,
            status="succeeded",
            measurement=carried,
        )

    def complete(self) -> None:
        return None


def _tap_managed_session_sse(
    stream: Any, meter: _ManagedSessionStreamMeter, *, asynchronous: bool
) -> None:
    original = getattr(stream, "_iter_events", None)
    if not callable(original):
        return

    def observe_raw(sse: Any) -> None:
        try:
            data = sse.json()
            if _bounded_string(getattr(sse, "event", None)) == "session.usage" or (
                isinstance(data, dict) and data.get("type") == "session.usage"
            ):
                meter.observe(data)
        except Exception:
            _log.debug(
                "dexcost: failed to inspect raw Anthropic session usage SSE",
                exc_info=True,
            )

    if asynchronous:

        async def async_events() -> Any:
            async for sse in original():
                observe_raw(sse)
                yield sse

        stream._iter_events = async_events
    else:

        def sync_events() -> Any:
            for sse in original():
                observe_raw(sse)
                yield sse

        stream._iter_events = sync_events


def _managed_session_record_id(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str | None:
    value = kwargs.get("session_id")
    if not isinstance(value, str) and args:
        value = args[0]
    return _bounded_string(value)


def _managed_session_stream_wrapper(*, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        record_id = _managed_session_record_id(args, kwargs)
        if asynchronous:
            return _async_managed_session_stream_call(wrapped, args, kwargs, record_id)
        with suppress_network_event():
            stream = wrapped(*args, **kwargs)
        if record_id is None or _active_tracker is None:
            return stream
        meter = _ManagedSessionStreamMeter(record_id)
        _tap_managed_session_sse(stream, meter, asynchronous=False)
        return SyncProviderJobStream(
            stream,
            observe=meter.observe,
            complete=meter.complete,
        )

    return wrapper


def _async_managed_session_stream_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    record_id: str | None,
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            stream = await wrapped(*args, **kwargs)
        if record_id is None or _active_tracker is None:
            return stream
        meter = _ManagedSessionStreamMeter(record_id)
        _tap_managed_session_sse(stream, meter, asynchronous=True)
        return AsyncProviderJobStream(
            stream,
            observe=meter.observe,
            complete=meter.complete,
        )

    return invoke()


def _deployment_run_session_id(resource: Any) -> str | None:
    return _bounded_string(_value(resource, "session_id"))


def _deployment_run_error(resource: Any) -> RuntimeError | None:
    error_type = _bounded_string(_value(_value(resource, "error"), "type"))
    if error_type is None:
        return None
    return RuntimeError(f"Anthropic deployment run failed ({error_type})")


def _persist_deployment_session(
    session: ProviderJobSession, resource: Any
) -> None:
    session_id = _deployment_run_session_id(resource)
    if session_id is None:
        error = _deployment_run_error(resource)
        if error is not None:
            session.fail(error)
        else:
            session.release_context()
        return
    tracker = _active_tracker
    if tracker is not None and tracker._storage.get_provider_job(
        "anthropic", "managed_sessions", session_id
    ) is not None:
        session.release_context()
        return
    session.submit(session_id, status="submitted")


def _deployment_run_wrapper(*, operation: str, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if asynchronous:
            return _async_deployment_run_call(
                wrapped, args, kwargs, operation=operation
            )
        session = _managed_session_job(
            "anthropic-managed-agent",
            task_type=operation,
            operation=operation,
        )
        if session is None:
            return wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                resource = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        try:
            _persist_deployment_session(session, resource)
        except Exception:
            _log.debug(
                "dexcost: failed to persist Anthropic deployment session",
                exc_info=True,
            )
            session.release_context()
        return resource

    return wrapper


def _async_deployment_run_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    operation: str,
) -> Any:
    async def invoke() -> Any:
        session = _managed_session_job(
            "anthropic-managed-agent",
            task_type=operation,
            operation=operation,
        )
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                resource = await wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        try:
            _persist_deployment_session(session, resource)
        except Exception:
            _log.debug(
                "dexcost: failed to persist async Anthropic deployment session",
                exc_info=True,
            )
            session.release_context()
        return resource

    return invoke()


def _dream_model(resource: Any, fallback: str = "anthropic-dream") -> str:
    return (
        _bounded_string(_value(_value(resource, "model"), "id"))
        or _bounded_string(_value(resource, "model"))
        or fallback
    )


def _dream_status(resource: Any) -> ProviderJobStatus:
    raw_status = _bounded_string(_value(resource, "status"))
    return {
        "pending": "submitted",
        "running": "running",
        "completed": "succeeded",
        "failed": "failed",
        "canceled": "cancelled",
    }.get(raw_status or "", "unknown")  # type: ignore[return-value]


def _dream_measurement(resource: Any, status: ProviderJobStatus) -> OperationMeasurement:
    if status in {"submitted", "running"}:
        return OperationMeasurement(pricing_usage={}, usage_lines=())
    usage = _value(resource, "usage")
    input_tokens = _non_negative_int(_value(usage, "input_tokens"))
    output_tokens = _non_negative_int(_value(usage, "output_tokens"))
    cached_tokens = _non_negative_int(_value(usage, "cache_read_input_tokens"))
    cache_write_tokens = _non_negative_int(
        _value(usage, "cache_creation_input_tokens")
    )
    speed = _bounded_string(_value(_value(resource, "model"), "speed"))
    pricing_usage = {
        dimension: quantity
        for dimension, quantity in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cache_read_input_tokens", cached_tokens),
            ("cache_write_input_tokens", cache_write_tokens),
            ("fast_mode_usage", 1 if speed == "fast" else 0),
        )
        if quantity > 0
    }
    lines = [
        ProviderUsageLine(dimension, quantity, "Tokens")
        for dimension, quantity in pricing_usage.items()
        if dimension != "fast_mode_usage"
    ]
    if speed == "fast":
        lines.append(ProviderUsageLine("fast_mode_usage", 1, "Operations"))
    if status == "succeeded":
        lines.append(ProviderUsageLine("dream_completed_count", 1, "Operations"))
    return OperationMeasurement(
        pricing_usage=pricing_usage,
        usage_lines=tuple(lines),
        response_model=_dream_model(resource),
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
        task_cached_tokens=cached_tokens,
    )


def _dream_session(model: str) -> ProviderJobSession | None:
    tracker = _active_tracker
    if tracker is None:
        return None
    return ProviderJobSession(
        tracker=tracker,
        task_type="anthropic.beta.dreams.create",
        provider="anthropic",
        service="dreams",
        operation="anthropic.beta.dreams.create",
        component="llm",
        event_type="llm_call",
        resource_type="model",
        resource_id=model,
    )


def _submit_dream(session: ProviderJobSession, resource: Any) -> None:
    record_id = _bounded_string(_value(resource, "id"))
    if record_id is None:
        raise ValueError("Anthropic dream response did not include an id")
    status = _dream_status(resource)
    error_type = _bounded_string(_value(_value(resource, "error"), "type"))
    session.submit(
        record_id,
        status=status,
        measurement=_dream_measurement(resource, status),
        error_type=error_type,
    )


def _dream_create_wrapper(*, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        model = _bounded_string(kwargs.get("model")) or "anthropic-dream"
        if asynchronous:
            return _async_dream_create_call(wrapped, args, kwargs, model)
        session = _dream_session(model)
        if session is None:
            return wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                resource = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        try:
            _submit_dream(session, resource)
        except Exception:
            _log.debug("dexcost: failed to persist Anthropic dream", exc_info=True)
        finally:
            session.release_context()
        return resource

    return wrapper


def _async_dream_create_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    model: str,
) -> Any:
    async def invoke() -> Any:
        session = _dream_session(model)
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            with suppress_network_event():
                resource = await wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        try:
            _submit_dream(session, resource)
        except Exception:
            _log.debug("dexcost: failed to persist async Anthropic dream", exc_info=True)
        finally:
            session.release_context()
        return resource

    return invoke()


def _observe_or_adopt_dream(resource: Any) -> None:
    tracker = _active_tracker
    record_id = _bounded_string(_value(resource, "id"))
    if tracker is None or record_id is None:
        return
    status = _dream_status(resource)
    measurement = _dream_measurement(resource, status)
    error_type = _bounded_string(_value(_value(resource, "error"), "type"))
    existing = tracker._storage.get_provider_job("anthropic", "dreams", record_id)
    if existing is None:
        session = _dream_session(_dream_model(resource))
        if session is not None:
            session.submit(
                record_id,
                status=status,
                measurement=measurement,
                error_type=error_type,
            )
        return
    reconcile_provider_job(
        tracker=tracker,
        provider="anthropic",
        service="dreams",
        provider_record_id=record_id,
        status=status,
        measurement=measurement,
        error_type=error_type,
    )


def _dream_reconcile_wrapper(*, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if asynchronous:
            return _async_dream_reconcile_call(wrapped, args, kwargs)
        with suppress_network_event():
            resource = wrapped(*args, **kwargs)
        try:
            _observe_or_adopt_dream(resource)
        except Exception:
            _log.debug("dexcost: failed to reconcile Anthropic dream", exc_info=True)
        return resource

    return wrapper


def _async_dream_reconcile_call(
    wrapped: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        try:
            _observe_or_adopt_dream(resource)
        except Exception:
            _log.debug("dexcost: failed to reconcile async Anthropic dream", exc_info=True)
        return resource

    return invoke()


def _batch_request_metadata(
    kwargs: dict[str, Any],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    requests = kwargs.get("requests")
    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes, bytearray)):
        return "anthropic-message-batch", ()
    models = {
        model
        for request in requests
        if (
            model := _bounded_string(_value(_value(request, "params"), "model"))
        )
        is not None
    }
    resource = next(iter(models)) if len(models) == 1 else "anthropic-message-batch"
    dimensions: list[tuple[str, str]] = [("batch_request_count", str(len(requests)))]
    if models:
        dimensions.append(("batch_model_count", str(len(models))))
    return resource, tuple(dimensions)


def _batch_record_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    value = kwargs.get("message_batch_id")
    if not isinstance(value, str) and args:
        value = args[0]
    return _bounded_string(value)


def _batch_count_values(resource: Any) -> dict[str, int]:
    counts = _value(resource, "request_counts")
    result: dict[str, int] = {}
    for name in ("processing", "succeeded", "errored", "canceled", "expired"):
        quantity = _optional_non_negative_int(_value(counts, name))
        if quantity is not None:
            result[name] = quantity
    return result


def _batch_status(resource: Any, *, submission: bool = False) -> ProviderJobStatus:
    status = _bounded_string(_value(resource, "processing_status"))
    counts = _batch_count_values(resource)
    if status == "in_progress":
        return "submitted" if submission else "running"
    if status == "canceling" or counts.get("processing", 0) > 0:
        return "running"
    if status != "ended":
        return "unknown"
    if counts.get("succeeded", 0) > 0:
        return "succeeded"
    if counts.get("errored", 0) > 0 or counts.get("expired", 0) > 0:
        return "failed"
    if counts.get("canceled", 0) > 0:
        return "cancelled"
    return "unknown"


def _batch_count_measurement(resource: Any) -> OperationMeasurement:
    lines = tuple(
        ProviderUsageLine(f"batch_{name}_request_count", quantity, "Requests")
        for name, quantity in sorted(_batch_count_values(resource).items())
        if quantity > 0
    )
    return OperationMeasurement(pricing_usage={}, usage_lines=lines)


def _batch_status_measurement(
    resource: Any, status: ProviderJobStatus
) -> OperationMeasurement:
    if status in {"submitted", "running"}:
        return OperationMeasurement(pricing_usage={}, usage_lines=())
    return _batch_count_measurement(resource)


def _batch_session(
    service_name: str,
    resource_id: str,
    billing_dimensions: tuple[tuple[str, str], ...] = (),
) -> ProviderJobSession | None:
    tracker = _active_tracker
    if tracker is None:
        return None
    return ProviderJobSession(
        tracker=tracker,
        task_type=f"anthropic.{service_name}.create",
        provider="anthropic",
        service=service_name,
        operation=f"anthropic.{service_name}.create",
        component="llm",
        event_type="llm_call",
        resource_type="model",
        resource_id=resource_id,
        billing_dimensions=billing_dimensions,
    )


def _submit_batch_job(session: ProviderJobSession, resource: Any) -> None:
    record_id = _bounded_string(_value(resource, "id"))
    if record_id is None:
        raise ValueError("Anthropic batch response did not include an id")
    status = _batch_status(resource, submission=True)
    measurement = _batch_status_measurement(resource, status)
    session.submit(record_id, status=status, measurement=measurement)


def _batch_create_wrapper(service_name: str, *, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        resource_id, dimensions = _batch_request_metadata(kwargs)
        session = _batch_session(service_name, resource_id, dimensions)
        if asynchronous:
            return _async_batch_create_call(wrapped, args, kwargs, session)
        if session is None:
            return wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    resource = wrapped(*args, **kwargs)
            except Exception as exc:
                session.fail(exc)
                raise
            try:
                _submit_batch_job(session, resource)
            except Exception:
                _log.debug("dexcost: failed to persist Anthropic batch", exc_info=True)
            return resource
        finally:
            session.release_context()

    return wrapper


def _async_batch_create_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    session: ProviderJobSession | None,
) -> Any:
    async def invoke() -> Any:
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    resource = await wrapped(*args, **kwargs)
            except Exception as exc:
                session.fail(exc)
                raise
            try:
                _submit_batch_job(session, resource)
            except Exception:
                _log.debug("dexcost: failed to persist async Anthropic batch", exc_info=True)
            return resource
        finally:
            session.release_context()

    return invoke()


def _observe_or_adopt_batch(resource: Any, service_name: str) -> None:
    tracker = _active_tracker
    record_id = _bounded_string(_value(resource, "id"))
    if tracker is None or record_id is None:
        return
    status = _batch_status(resource)
    measurement = _batch_status_measurement(resource, status)
    existing = tracker._storage.get_provider_job("anthropic", service_name, record_id)
    if existing is None:
        session = _batch_session(service_name, "anthropic-message-batch")
        if session is not None:
            session.submit(record_id, status=status, measurement=measurement)
        return
    reconcile_provider_job(
        tracker=tracker,
        provider="anthropic",
        service=service_name,
        provider_record_id=record_id,
        status=status,
        measurement=measurement,
    )


def _batch_reconcile_wrapper(
    service_name: str,
    method_name: str,
    *,
    asynchronous: bool,
) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if asynchronous:
            return _async_batch_reconcile_call(wrapped, args, kwargs, service_name)
        with suppress_network_event():
            resource = wrapped(*args, **kwargs)
        try:
            _observe_or_adopt_batch(resource, service_name)
        except Exception:
            _log.debug(
                "dexcost: failed to reconcile Anthropic batch after %s",
                method_name,
                exc_info=True,
            )
        return resource

    return wrapper


def _async_batch_reconcile_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    service_name: str,
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        try:
            _observe_or_adopt_batch(resource, service_name)
        except Exception:
            _log.debug("dexcost: failed to reconcile async Anthropic batch", exc_info=True)
        return resource

    return invoke()


def _batch_pricing_usage(usage: _UsageSnapshot) -> dict[str, int]:
    return {
        dimension: quantity
        for dimension, quantity in (
            ("anthropic_batch_input_tokens", usage.input_tokens),
            ("anthropic_batch_output_tokens", usage.output_tokens),
            (
                "anthropic_batch_cache_read_input_tokens",
                usage.cache_read_input_tokens,
            ),
            (
                "anthropic_batch_cache_write_input_tokens",
                max(
                    0,
                    usage.cache_creation_input_tokens
                    - usage.cache_creation_input_tokens_1h,
                ),
            ),
            (
                "anthropic_batch_cache_write_input_tokens_1h",
                usage.cache_creation_input_tokens_1h,
            ),
            ("web_search_calls", usage.web_search_requests),
        )
        if quantity > 0
    }


class _BatchResultsMeter:
    """Aggregate a JSONL batch result stream without retaining custom IDs/content."""

    def __init__(self, service_name: str, record_id: str) -> None:
        self._service_name = service_name
        self._record_id = record_id
        self._usage: dict[str, int] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._cost_usd = Decimal(0)
        self._pricing_version: str | None = None
        self._priced_any = False
        self._unknown = False
        self._result_counts = {
            "succeeded": 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        }

    def _add_usage(self, model: str, usage: _UsageSnapshot, stop_reason: str | None) -> None:
        billable = _billable_usage(usage, stop_reason)
        snapshots: list[tuple[str, _UsageSnapshot]] = []
        if usage.iterations:
            for iteration in usage.iterations:
                if (
                    iteration.kind in {"message", "fallback_message"}
                    and iteration.output_tokens == 0
                ):
                    self._usage["batch_unbilled_refusal_input_tokens"] = (
                        self._usage.get("batch_unbilled_refusal_input_tokens", 0)
                        + iteration.input_tokens
                    )
                    continue
                snapshots.append(
                    (
                        iteration.model or model,
                        _UsageSnapshot(
                            input_tokens=iteration.input_tokens,
                            output_tokens=iteration.output_tokens,
                            cache_creation_input_tokens=(
                                iteration.cache_creation_input_tokens
                            ),
                            cache_creation_input_tokens_1h=(
                                iteration.cache_creation_input_tokens_1h
                            ),
                            cache_read_input_tokens=iteration.cache_read_input_tokens,
                            cache_breakdown_inconsistent=(
                                iteration.cache_breakdown_inconsistent
                            ),
                        ),
                    )
                )
        else:
            snapshots.append((model, billable.usage))

        for iteration_model, snapshot in snapshots:
            pricing_usage = _batch_pricing_usage(snapshot)
            tracker = _active_tracker
            if tracker is None:
                self._unknown = True
                continue
            result = tracker._pricing.get_metered_cost(iteration_model, pricing_usage)
            self._cost_usd += result.cost_usd
            self._pricing_version = self._pricing_version or result.pricing_version
            self._priced_any = self._priced_any or bool(result.lines)
            self._unknown = self._unknown or bool(result.unpriced_dimensions)
            for dimension, quantity in pricing_usage.items():
                self._usage[dimension] = self._usage.get(dimension, 0) + quantity

        # Beta iteration usage accounts for sampling tokens, while server-tool
        # request counts remain on the top-level usage object. Price those once
        # against the executor model rather than dropping or duplicating them.
        if usage.iterations and billable.usage.web_search_requests > 0:
            tracker = _active_tracker
            tool_usage = {"web_search_calls": billable.usage.web_search_requests}
            if tracker is None:
                self._unknown = True
            else:
                result = tracker._pricing.get_metered_cost(model, tool_usage)
                self._cost_usd += result.cost_usd
                self._pricing_version = self._pricing_version or result.pricing_version
                self._priced_any = self._priced_any or bool(result.lines)
                self._unknown = self._unknown or bool(result.unpriced_dimensions)
                self._usage["web_search_calls"] = (
                    self._usage.get("web_search_calls", 0)
                    + billable.usage.web_search_requests
                )

        self._input_tokens += billable.usage.input_tokens
        self._output_tokens += billable.usage.output_tokens
        self._cached_tokens += billable.usage.cache_read_input_tokens
        for dimension, quantity in (
            ("batch_reasoning_output_tokens", billable.usage.thinking_tokens),
            ("batch_web_fetch_requests", billable.usage.web_fetch_requests),
        ):
            if quantity > 0:
                self._usage[dimension] = self._usage.get(dimension, 0) + quantity

    def observe(self, item: Any) -> None:
        result = _value(item, "result")
        result_type = _bounded_string(_value(result, "type")) or "errored"
        if result_type not in self._result_counts:
            result_type = "errored"
        self._result_counts[result_type] += 1
        if result_type != "succeeded":
            return
        message = _value(result, "message")
        model = _bounded_string(_value(message, "model")) or "unknown"
        raw_usage = _value(message, "usage")
        if raw_usage is None:
            self._unknown = True
            return
        usage = _extract_usage(raw_usage, fallback_model=model)
        self._add_usage(model, usage, _bounded_string(_value(message, "stop_reason")))

    def complete(self) -> None:
        tracker = _active_tracker
        if tracker is None:
            return
        for result_status, quantity in self._result_counts.items():
            if quantity > 0:
                self._usage[f"batch_{result_status}_request_count"] = quantity
        lines = tuple(
            ProviderUsageLine(
                metric,
                quantity,
                (
                    "Requests"
                    if metric.endswith("request_count") or metric.endswith("requests")
                    else "Calls" if metric.endswith("_calls") else "Tokens"
                ),
            )
            for metric, quantity in sorted(self._usage.items())
            if quantity > 0
        )
        succeeded = self._result_counts["succeeded"]
        if succeeded > 0:
            status: ProviderJobStatus = "succeeded"
        elif self._result_counts["errored"] > 0 or self._result_counts["expired"] > 0:
            status = "failed"
        elif self._result_counts["canceled"] > 0:
            status = "cancelled"
        else:
            status = "unknown"
        computed = self._cost_usd if self._priced_any else None
        measurement = OperationMeasurement(
            pricing_usage={},
            usage_lines=lines,
            computed_cost_usd=computed,
            computed_cost_source="sdk_catalog" if computed is not None else None,
            computed_cost_confidence=(
                "unknown" if self._unknown else "computed"
            )
            if computed is not None
            else None,
            computed_pricing_version=(
                self._pricing_version if computed is not None else None
            ),
            task_input_tokens=self._input_tokens,
            task_output_tokens=self._output_tokens,
            task_cached_tokens=self._cached_tokens,
        )
        try:
            reconcile_provider_job(
                tracker=tracker,
                provider="anthropic",
                service=self._service_name,
                provider_record_id=self._record_id,
                status=status,
                measurement=measurement,
            )
        except Exception:
            _log.debug("dexcost: failed to finalize Anthropic batch results", exc_info=True)


def _ensure_batch_for_results(service_name: str, record_id: str) -> None:
    tracker = _active_tracker
    if tracker is None:
        return
    if tracker._storage.get_provider_job("anthropic", service_name, record_id) is not None:
        return
    session = _batch_session(service_name, "anthropic-message-batch")
    if session is not None:
        session.submit(record_id, status="running")


def _batch_results_wrapper(service_name: str, *, asynchronous: bool) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        record_id = _batch_record_id(args, kwargs)
        if asynchronous:
            return _async_batch_results_call(
                wrapped, args, kwargs, service_name, record_id
            )
        with suppress_network_event():
            stream = wrapped(*args, **kwargs)
        if record_id is None or _active_tracker is None:
            return stream
        _ensure_batch_for_results(service_name, record_id)
        meter = _BatchResultsMeter(service_name, record_id)
        return SyncProviderJobStream(
            stream,
            observe=meter.observe,
            complete=meter.complete,
        )

    return wrapper


def _async_batch_results_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    service_name: str,
    record_id: str | None,
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            stream = await wrapped(*args, **kwargs)
        if record_id is None or _active_tracker is None:
            return stream
        _ensure_batch_for_results(service_name, record_id)
        meter = _BatchResultsMeter(service_name, record_id)
        return AsyncProviderJobStream(
            stream,
            observe=meter.observe,
            complete=meter.complete,
        )

    return invoke()


# ---------------------------------------------------------------------------
# Stream wrappers
# ---------------------------------------------------------------------------


class _SyncStreamWrapper(Iterator[Any]):
    """Wraps a sync Anthropic stream to capture usage on completion.

    Anthropic streaming distributes usage across events:
    - ``message_start``: ``message.model`` and ``message.usage`` (input tokens)
    - ``message_delta``: ``usage.output_tokens``
    - ``message_stop``: signals stream end
    """

    def __init__(
        self,
        stream: Any,
        start_time: float,
        task: Any = None,
        auto_task_obj: Any = None,
        requested: str | None = None,
        capability: CapabilityIdentity | None = None,
        idempotency_key: IdempotencyKey | None = None,
        service_name: str = "messages",
        operation_name: str = "anthropic.messages.create",
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._requested = requested
        self._model: str | None = None
        self._usage = _UsageSnapshot()
        self._stop_reason: str | None = None
        self._tool_calls: int = 0
        self._provider_record_id: str | None = None
        self._finalized: bool = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._capability = capability
        self._idempotency_key = idempotency_key
        self._service_name = service_name
        self._operation_name = operation_name

    def __iter__(self) -> _SyncStreamWrapper:
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._stream)
            self._process_event(event)
            return event
        except StopIteration:
            self._finalize("succeeded")
            raise
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _record_failure(self, exc: BaseException) -> None:
        """Persist a provider error raised while the stream was being consumed.

        Anthropic exposes prompt and delivered-output counters incrementally,
        so a failed stream must retain those already-billed quantities.
        """
        self._finalize("failed", exc)

    def _process_event(self, event: Any) -> None:
        """Extract model and usage info from streaming events."""
        event_type = getattr(event, "type", None)

        if event_type == "message_start":
            message = getattr(event, "message", None)
            if message is not None:
                model = getattr(message, "model", None)
                if model:
                    self._model = model
                self._provider_record_id = _bounded_string(getattr(message, "id", None))
                self._tool_calls += _tool_call_count(message)
                usage = getattr(message, "usage", None)
                if usage is not None:
                    self._usage = _extract_usage(
                        usage,
                        self._usage,
                        fallback_model=self._model or self._requested,
                    )

        elif event_type == "content_block_start":
            self._tool_calls += _tool_call_count(getattr(event, "content_block", None))
        elif event_type == "message_delta":
            delta = getattr(event, "delta", None)
            self._stop_reason = _bounded_string(_value(delta, "stop_reason"))
            usage = getattr(event, "usage", None)
            if usage is not None:
                self._usage = _extract_usage(
                    usage,
                    self._usage,
                    fallback_model=self._model or self._requested,
                )

    def _finalize(self, status: str, error: BaseException | None = None) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_data(
                model=self._model,
                usage=self._usage,
                stop_reason=self._stop_reason,
                latency_ms=latency_ms,
                task=self._task,
                status=status,
                error=error,
                provider_record_id=self._provider_record_id,
                tool_calls=self._tool_calls,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
                service_name=self._service_name,
                operation_name=self._operation_name,
            )
            if self._auto_task_obj is not None and event is not None:
                finalize_auto_task(
                    self._auto_task_obj,
                    event,
                    status="success" if status == "succeeded" else "failed",
                )
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(self._auto_task_obj)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def __del__(self) -> None:
        """Account for an abandoned stream exactly once during collection."""
        with suppress(Exception):
            self._finalize("cancelled")

    # Forward close/context-manager to the underlying stream
    def close(self) -> None:
        try:
            if hasattr(self._stream, "close"):
                self._stream.close()
        except Exception as exc:
            self._record_failure(exc)
            raise
        self._finalize("cancelled")

    def __enter__(self) -> _SyncStreamWrapper:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        try:
            result = self._stream.__exit__(*args) if hasattr(self._stream, "__exit__") else None
        except Exception as exit_exc:
            self._record_failure(exit_exc)
            raise
        if not self._finalized:
            self._finalize("cancelled")
        return result


class _AsyncStreamWrapper:
    """Wraps an async Anthropic stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        task: Any = None,
        auto_task_obj: Any = None,
        requested: str | None = None,
        capability: CapabilityIdentity | None = None,
        idempotency_key: IdempotencyKey | None = None,
        service_name: str = "messages",
        operation_name: str = "anthropic.messages.create",
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._requested = requested
        self._model: str | None = None
        self._usage = _UsageSnapshot()
        self._stop_reason: str | None = None
        self._tool_calls: int = 0
        self._provider_record_id: str | None = None
        self._finalized: bool = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._capability = capability
        self._idempotency_key = idempotency_key
        self._service_name = service_name
        self._operation_name = operation_name

    def __aiter__(self) -> _AsyncStreamWrapper:
        return self

    async def __anext__(self) -> Any:
        try:
            event = await self._stream.__anext__()
            self._process_event(event)
            return event
        except StopAsyncIteration:
            self._finalize("succeeded")
            raise
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _record_failure(self, exc: BaseException) -> None:
        """Persist a provider error raised while the stream was being consumed.

        Anthropic exposes prompt and delivered-output counters incrementally,
        so a failed stream must retain those already-billed quantities.
        """
        self._finalize("failed", exc)

    def _process_event(self, event: Any) -> None:
        """Extract model and usage info from streaming events."""
        event_type = getattr(event, "type", None)

        if event_type == "message_start":
            message = getattr(event, "message", None)
            if message is not None:
                model = getattr(message, "model", None)
                if model:
                    self._model = model
                self._provider_record_id = _bounded_string(getattr(message, "id", None))
                self._tool_calls += _tool_call_count(message)
                usage = getattr(message, "usage", None)
                if usage is not None:
                    self._usage = _extract_usage(
                        usage,
                        self._usage,
                        fallback_model=self._model or self._requested,
                    )

        elif event_type == "content_block_start":
            self._tool_calls += _tool_call_count(getattr(event, "content_block", None))
        elif event_type == "message_delta":
            delta = getattr(event, "delta", None)
            self._stop_reason = _bounded_string(_value(delta, "stop_reason"))
            usage = getattr(event, "usage", None)
            if usage is not None:
                self._usage = _extract_usage(
                    usage,
                    self._usage,
                    fallback_model=self._model or self._requested,
                )

    def _finalize(self, status: str, error: BaseException | None = None) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_data(
                model=self._model,
                usage=self._usage,
                stop_reason=self._stop_reason,
                latency_ms=latency_ms,
                task=self._task,
                status=status,
                error=error,
                provider_record_id=self._provider_record_id,
                tool_calls=self._tool_calls,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
                service_name=self._service_name,
                operation_name=self._operation_name,
            )
            if self._auto_task_obj is not None and event is not None:
                finalize_auto_task(
                    self._auto_task_obj,
                    event,
                    status="success" if status == "succeeded" else "failed",
                )
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(self._auto_task_obj)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def __del__(self) -> None:
        """Account for an abandoned async stream exactly once during collection."""
        with suppress(Exception):
            self._finalize("cancelled")

    async def aclose(self) -> None:
        try:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()
        except Exception as exc:
            self._record_failure(exc)
            raise
        self._finalize("cancelled")

    async def __aenter__(self) -> _AsyncStreamWrapper:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        try:
            result = (
                await self._stream.__aexit__(*args) if hasattr(self._stream, "__aexit__") else None
            )
        except Exception as exit_exc:
            self._record_failure(exit_exc)
            raise
        if not self._finalized:
            self._finalize("cancelled")
        return result


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _bounded_string(value: Any) -> str | None:
    return value.strip()[:256] if isinstance(value, str) and value.strip() else None


def _value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


@dataclass(frozen=True)
class _UsageIteration:
    """One privacy-safe Anthropic sampling iteration."""

    kind: str
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_creation_input_tokens_1h: int
    cache_read_input_tokens: int
    cache_breakdown_inconsistent: bool


@dataclass(frozen=True)
class _UsageSnapshot:
    """Provider-reported usage without prompts, outputs, or tool arguments."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_input_tokens_1h: int = 0
    cache_read_input_tokens: int = 0
    thinking_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    service_tier: str | None = None
    inference_geo: str | None = None
    speed: str | None = None
    fallback_credit_status: str | None = None
    iterations: tuple[_UsageIteration, ...] = ()
    cache_breakdown_inconsistent: bool = False


def _cache_usage(value: Any) -> tuple[int, int, bool]:
    """Return total cache writes, the disjoint 1h bucket, and consistency."""
    total_raw = _value(value, "cache_creation_input_tokens")
    parsed_total = _optional_non_negative_int(total_raw)
    total = parsed_total or 0
    breakdown = _value(value, "cache_creation")
    five_minutes_raw = _optional_non_negative_int(
        _value(breakdown, "ephemeral_5m_input_tokens")
    )
    one_hour_raw = _optional_non_negative_int(
        _value(breakdown, "ephemeral_1h_input_tokens")
    )
    if five_minutes_raw is None and one_hour_raw is None:
        return total, 0, False
    five_minutes = five_minutes_raw or 0
    one_hour = one_hour_raw or 0
    breakdown_total = five_minutes + one_hour
    if parsed_total is None:
        return breakdown_total, one_hour, False
    if total != breakdown_total:
        # Never invent a TTL allocation when provider totals disagree. Retain
        # the authoritative total in the ordinary write bucket and make the
        # resulting lower-bound price explicitly unknown.
        return total, 0, True
    return total, one_hour, False


def _usage_iteration(value: Any, fallback_model: str | None) -> _UsageIteration | None:
    kind = _bounded_string(_value(value, "type"))
    if kind not in {"message", "compaction", "advisor_message", "fallback_message"}:
        return None
    model = _bounded_string(_value(value, "model")) or fallback_model
    cache_total, cache_one_hour, cache_inconsistent = _cache_usage(value)
    return _UsageIteration(
        kind=kind,
        model=model,
        input_tokens=_non_negative_int(_value(value, "input_tokens")),
        output_tokens=_non_negative_int(_value(value, "output_tokens")),
        cache_creation_input_tokens=cache_total,
        cache_creation_input_tokens_1h=cache_one_hour,
        cache_read_input_tokens=_non_negative_int(_value(value, "cache_read_input_tokens")),
        cache_breakdown_inconsistent=cache_inconsistent,
    )


def _fallback_credit_status(value: Any) -> str | None:
    fallback_credit = _value(value, "fallback_credit")
    status = _value(fallback_credit, "status")
    status_type = _bounded_string(_value(status, "type"))
    return status_type if status_type in {"redeemed", "not_applied"} else None


def _extract_usage(
    value: Any,
    previous: _UsageSnapshot | None = None,
    *,
    fallback_model: str | None = None,
) -> _UsageSnapshot:
    """Normalize stable and Beta Messages usage, including cumulative deltas."""
    prior = previous or _UsageSnapshot()

    def cumulative_int(key: str, prior_value: int) -> int:
        parsed = _optional_non_negative_int(_value(value, key))
        return prior_value if parsed is None else parsed

    cache_raw = _value(value, "cache_creation_input_tokens")
    cache_breakdown = _value(value, "cache_creation")
    if (
        _optional_non_negative_int(cache_raw) is None
        and _optional_non_negative_int(
            _value(cache_breakdown, "ephemeral_5m_input_tokens")
        )
        is None
        and _optional_non_negative_int(
            _value(cache_breakdown, "ephemeral_1h_input_tokens")
        )
        is None
    ):
        cache_total = prior.cache_creation_input_tokens
        cache_one_hour = prior.cache_creation_input_tokens_1h
        cache_inconsistent = prior.cache_breakdown_inconsistent
    else:
        cache_total, cache_one_hour, cache_inconsistent = _cache_usage(value)
        if cache_breakdown is None and cache_total == prior.cache_creation_input_tokens:
            # MessageDeltaUsage repeats cumulative totals but omits the TTL
            # object. Preserve the exact split learned from message_start.
            cache_one_hour = prior.cache_creation_input_tokens_1h
            cache_inconsistent = prior.cache_breakdown_inconsistent

    output_details = _value(value, "output_tokens_details")
    thinking_raw = _optional_non_negative_int(_value(output_details, "thinking_tokens"))
    thinking_tokens = (
        prior.thinking_tokens if thinking_raw is None else thinking_raw
    )
    output_tokens = cumulative_int("output_tokens", prior.output_tokens)
    thinking_tokens = min(thinking_tokens, output_tokens)

    server_tools = _value(value, "server_tool_use")
    web_search_requests = prior.web_search_requests
    web_fetch_requests = prior.web_fetch_requests
    if server_tools is not None:
        search_raw = _value(server_tools, "web_search_requests")
        fetch_raw = _value(server_tools, "web_fetch_requests")
        parsed_search = _optional_non_negative_int(search_raw)
        parsed_fetch = _optional_non_negative_int(fetch_raw)
        if parsed_search is not None:
            web_search_requests = parsed_search
        if parsed_fetch is not None:
            web_fetch_requests = parsed_fetch

    iterations_raw = _value(value, "iterations")
    iterations = prior.iterations
    if isinstance(iterations_raw, Sequence) and not isinstance(
        iterations_raw, (str, bytes, bytearray)
    ):
        normalized = tuple(
            iteration
            for raw_iteration in iterations_raw
            if (iteration := _usage_iteration(raw_iteration, fallback_model)) is not None
        )
        iterations = normalized

    service_tier = _bounded_string(_value(value, "service_tier")) or prior.service_tier
    if service_tier not in {None, "standard", "priority", "batch"}:
        service_tier = None
    inference_geo = _bounded_string(_value(value, "inference_geo")) or prior.inference_geo
    speed = _bounded_string(_value(value, "speed")) or prior.speed
    if speed not in {None, "standard", "fast"}:
        speed = None

    return _UsageSnapshot(
        input_tokens=cumulative_int("input_tokens", prior.input_tokens),
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_total,
        cache_creation_input_tokens_1h=cache_one_hour,
        cache_read_input_tokens=cumulative_int(
            "cache_read_input_tokens", prior.cache_read_input_tokens
        ),
        thinking_tokens=thinking_tokens,
        web_search_requests=web_search_requests,
        web_fetch_requests=web_fetch_requests,
        service_tier=service_tier,
        inference_geo=inference_geo,
        speed=speed,
        fallback_credit_status=_fallback_credit_status(value)
        or prior.fallback_credit_status,
        iterations=iterations,
        cache_breakdown_inconsistent=cache_inconsistent,
    )


def _tool_call_count(value: Any) -> int:
    content = value.get("content") if isinstance(value, dict) else getattr(value, "content", None)
    if isinstance(content, list):
        return sum(
            1
            for item in content
            if (
                (isinstance(item, dict) and item.get("type") == "tool_use")
                or getattr(item, "type", None) == "tool_use"
            )
        )
    item_type = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    return 1 if item_type == "tool_use" else 0


def _usage_lines(
    usage: _UsageSnapshot,
    tool_calls: int,
    unbilled_usage: dict[str, int] | None = None,
) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    visible_output_tokens = max(0, usage.output_tokens - usage.thinking_tokens)
    for metric, quantity, unit in (
        ("input_tokens", usage.input_tokens, "Tokens"),
        ("output_tokens", visible_output_tokens, "Tokens"),
        ("reasoning_output_tokens", usage.thinking_tokens, "Tokens"),
        ("cache_write_input_tokens", usage.cache_creation_input_tokens, "Tokens"),
        ("cache_read_input_tokens", usage.cache_read_input_tokens, "Tokens"),
        ("tool_call_count", tool_calls, "Calls"),
        ("web_search_calls", usage.web_search_requests, "Calls"),
        ("web_fetch_requests", usage.web_fetch_requests, "Requests"),
    ):
        if quantity > 0:
            lines.append({"metric": metric, "quantity": str(quantity), "unit": unit})
    for metric, quantity in sorted((unbilled_usage or {}).items()):
        if quantity > 0:
            lines.append({"metric": metric, "quantity": str(quantity), "unit": "Tokens"})
    return lines or [{"metric": "request_count", "quantity": "1", "unit": "Requests"}]


@dataclass(frozen=True)
class _BillableUsage:
    usage: _UsageSnapshot
    unbilled_usage: dict[str, int]
    iterations: tuple[dict[str, Any], ...]


def _billable_usage(usage: _UsageSnapshot, stop_reason: str | None) -> _BillableUsage:
    """Separate chargeable sampling from provider-reported refusal counters."""
    if not usage.iterations:
        if stop_reason == "refusal" and usage.output_tokens == 0:
            refusal_unbilled = {
                "unbilled_refusal_input_tokens": usage.input_tokens,
                "unbilled_refusal_cache_read_input_tokens": usage.cache_read_input_tokens,
                "unbilled_refusal_cache_write_input_tokens": (
                    usage.cache_creation_input_tokens
                ),
            }
            empty = _UsageSnapshot(
                web_search_requests=usage.web_search_requests,
                web_fetch_requests=usage.web_fetch_requests,
                service_tier=usage.service_tier,
                inference_geo=usage.inference_geo,
                speed=usage.speed,
                fallback_credit_status=usage.fallback_credit_status,
            )
            return _BillableUsage(empty, refusal_unbilled, ())
        return _BillableUsage(usage, {}, ())

    input_tokens = 0
    output_tokens = 0
    cache_creation_input_tokens = 0
    cache_creation_input_tokens_1h = 0
    cache_read_input_tokens = 0
    unbilled: dict[str, int] = {}
    iteration_details: list[dict[str, Any]] = []
    inconsistent = usage.cache_breakdown_inconsistent
    for index, iteration in enumerate(usage.iterations):
        # Anthropic does not bill a refusal delivered before any output, even
        # though it reports input counters. A mid-output refusal has positive
        # output usage and is billed normally.
        billed = not (
            iteration.kind in {"message", "fallback_message"}
            and iteration.output_tokens == 0
        )
        detail: dict[str, Any] = {
            "index": index,
            "type": iteration.kind,
            "model": iteration.model or "unknown",
            "billed": billed,
            "input_tokens": iteration.input_tokens,
            "output_tokens": iteration.output_tokens,
            "cache_creation_input_tokens": iteration.cache_creation_input_tokens,
            "cache_read_input_tokens": iteration.cache_read_input_tokens,
        }
        if iteration.cache_creation_input_tokens_1h > 0:
            detail["cache_creation_input_tokens_1h"] = (
                iteration.cache_creation_input_tokens_1h
            )
        iteration_details.append(detail)
        inconsistent = inconsistent or iteration.cache_breakdown_inconsistent
        if not billed:
            unbilled["unbilled_refusal_input_tokens"] = (
                unbilled.get("unbilled_refusal_input_tokens", 0)
                + iteration.input_tokens
            )
            unbilled["unbilled_refusal_cache_read_input_tokens"] = (
                unbilled.get("unbilled_refusal_cache_read_input_tokens", 0)
                + iteration.cache_read_input_tokens
            )
            unbilled["unbilled_refusal_cache_write_input_tokens"] = (
                unbilled.get("unbilled_refusal_cache_write_input_tokens", 0)
                + iteration.cache_creation_input_tokens
            )
            continue
        input_tokens += iteration.input_tokens
        output_tokens += iteration.output_tokens
        cache_creation_input_tokens += iteration.cache_creation_input_tokens
        cache_creation_input_tokens_1h += iteration.cache_creation_input_tokens_1h
        cache_read_input_tokens += iteration.cache_read_input_tokens

    aggregate = _UsageSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_creation_input_tokens_1h=cache_creation_input_tokens_1h,
        cache_read_input_tokens=cache_read_input_tokens,
        thinking_tokens=min(usage.thinking_tokens, output_tokens),
        web_search_requests=usage.web_search_requests,
        web_fetch_requests=usage.web_fetch_requests,
        service_tier=usage.service_tier,
        inference_geo=usage.inference_geo,
        speed=usage.speed,
        fallback_credit_status=usage.fallback_credit_status,
        iterations=usage.iterations,
        cache_breakdown_inconsistent=inconsistent,
    )
    return _BillableUsage(aggregate, unbilled, tuple(iteration_details))


@dataclass(frozen=True)
class _PricingSummary:
    cost_usd: Decimal
    cost_confidence: str
    pricing_source: str
    pricing_version: str | None
    unpriced_dimensions: tuple[str, ...]
    iteration_details: tuple[dict[str, Any], ...]
    server_tool_breakdown: tuple[dict[str, str], ...]


def _price_anthropic_usage(
    tracker: Any,
    model: str,
    provider_usage: _UsageSnapshot,
    billable: _BillableUsage,
    *,
    has_provider_usage: bool,
) -> _PricingSummary:
    """Price native Anthropic usage without double-counting iteration totals."""
    cost_usd = Decimal(0)
    confidences: list[str] = []
    sources: list[str] = []
    pricing_version: str | None = None
    iteration_details = [dict(detail) for detail in billable.iterations]
    unpriced: set[str] = set()

    if provider_usage.iterations:
        for index, iteration in enumerate(provider_usage.iterations):
            if not iteration_details[index]["billed"]:
                iteration_details[index]["cost_usd"] = "0"
                iteration_details[index]["cost_confidence"] = "exact"
                iteration_details[index]["pricing_source"] = "provider_response"
                continue
            iteration_model = iteration.model or model
            result = tracker._pricing.get_cost(
                iteration_model,
                iteration.input_tokens,
                iteration.output_tokens,
                cached_tokens=iteration.cache_read_input_tokens,
                cache_creation_tokens=max(
                    0,
                    iteration.cache_creation_input_tokens
                    - iteration.cache_creation_input_tokens_1h,
                ),
                cache_creation_tokens_1h=iteration.cache_creation_input_tokens_1h,
            )
            cost_usd += result.cost_usd
            confidences.append(result.cost_confidence)
            sources.append(result.pricing_source)
            pricing_version = pricing_version or result.pricing_version
            if (
                result.cost_confidence == "unknown"
                and iteration.cache_creation_input_tokens_1h > 0
            ):
                unpriced.add("cache_creation_input_tokens_1h")
            iteration_details[index]["cost_usd"] = str(result.cost_usd)
            iteration_details[index]["cost_confidence"] = result.cost_confidence
            iteration_details[index]["pricing_source"] = result.pricing_source
    elif any(
        (
            billable.usage.input_tokens,
            billable.usage.output_tokens,
            billable.usage.cache_creation_input_tokens,
            billable.usage.cache_read_input_tokens,
        )
    ):
        result = tracker._pricing.get_cost(
            model,
            billable.usage.input_tokens,
            billable.usage.output_tokens,
            cached_tokens=billable.usage.cache_read_input_tokens,
            cache_creation_tokens=max(
                0,
                billable.usage.cache_creation_input_tokens
                - billable.usage.cache_creation_input_tokens_1h,
            ),
            cache_creation_tokens_1h=billable.usage.cache_creation_input_tokens_1h,
        )
        cost_usd += result.cost_usd
        confidences.append(result.cost_confidence)
        sources.append(result.pricing_source)
        pricing_version = result.pricing_version
        if (
            result.cost_confidence == "unknown"
            and billable.usage.cache_creation_input_tokens_1h > 0
        ):
            unpriced.add("cache_creation_input_tokens_1h")

    server_tool_breakdown: list[dict[str, str]] = []
    if billable.usage.web_search_requests > 0:
        tool_pricing = tracker._pricing.get_metered_cost(
            model,
            {"web_search_calls": billable.usage.web_search_requests},
        )
        cost_usd += tool_pricing.cost_usd
        confidences.append(tool_pricing.cost_confidence)
        sources.append(tool_pricing.pricing_source)
        pricing_version = pricing_version or tool_pricing.pricing_version
        unpriced.update(tool_pricing.unpriced_dimensions)
        server_tool_breakdown.extend(
            {
                "dimension": line.dimension,
                "quantity": str(line.quantity),
                "rate_field": line.rate_field,
                "rate_usd": str(line.rate_usd),
                "cost_usd": str(line.cost_usd),
            }
            for line in tool_pricing.lines
        )

    # These modifiers are returned by the provider but require explicit
    # catalog rates. Keep the standard-rate lower bound and downgrade
    # confidence rather than silently presenting it as the final charge.
    if provider_usage.speed == "fast":
        unpriced.add("speed")
    if provider_usage.inference_geo not in {None, "global"}:
        unpriced.add("inference_geo")
    if provider_usage.service_tier == "batch":
        unpriced.add("service_tier")
    if billable.usage.cache_breakdown_inconsistent:
        unpriced.add("cache_creation_ttl_breakdown")

    if unpriced or "unknown" in confidences:
        confidence = "unknown"
    elif confidences:
        confidence = "computed"
    elif has_provider_usage and billable.unbilled_usage:
        confidence = "exact"
    else:
        confidence = "estimated"

    source = next((item for item in sources if item != "unknown"), "unknown")
    if confidence == "exact" and not sources:
        source = "provider_response"
    return _PricingSummary(
        cost_usd=cost_usd,
        cost_confidence=confidence,
        pricing_source=source,
        pricing_version=pricing_version,
        unpriced_dimensions=tuple(sorted(unpriced)),
        iteration_details=tuple(iteration_details),
        server_tool_breakdown=tuple(server_tool_breakdown),
    )


def _cache_creation_details(usage: _UsageSnapshot) -> dict[str, int]:
    total = max(0, usage.cache_creation_input_tokens)
    one_hour = min(max(0, usage.cache_creation_input_tokens_1h), total)
    if total == 0:
        return {}
    details = {"cache_creation_input_tokens": total}
    if one_hour > 0:
        details["cache_creation_input_tokens_1h"] = one_hour
    five_minutes = total - one_hour
    if five_minutes > 0:
        details["cache_creation_input_tokens_5m"] = five_minutes
    return details


def _record_from_response(
    response: Any,
    latency_ms: int,
    task: Any,
    capability: CapabilityIdentity | None,
    idempotency_key: IdempotencyKey | None,
    *,
    service_name: str = "messages",
    operation_name: str = "anthropic.messages.create",
) -> Event | None:
    """Extract fields from an Anthropic Message response and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    if task is None:
        return None

    model = getattr(response, "model", None) or "unknown"
    usage = getattr(response, "usage", None)
    provider_record_id = _bounded_string(getattr(response, "id", None))
    tool_calls = _tool_call_count(response)

    normalized_usage = (
        _extract_usage(usage, fallback_model=model) if usage is not None else _UsageSnapshot()
    )
    stop_reason = _bounded_string(getattr(response, "stop_reason", None))

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        usage=normalized_usage,
        latency_ms=latency_ms,
        has_usage=usage is not None,
        stop_reason=stop_reason,
        provider_record_id=provider_record_id,
        tool_calls=tool_calls,
        capability=capability,
        idempotency_key=idempotency_key,
        service_name=service_name,
        operation_name=operation_name,
    )


def _record_from_stream_data(
    *,
    model: str | None,
    usage: _UsageSnapshot,
    stop_reason: str | None,
    latency_ms: int,
    task: Any,
    status: str,
    error: BaseException | None = None,
    provider_record_id: str | None = None,
    tool_calls: int = 0,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
    service_name: str = "messages",
    operation_name: str = "anthropic.messages.create",
) -> Event | None:
    """Record an event from accumulated stream data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    if task is None:
        return None

    resolved_model = model or "unknown"
    has_usage = any(
        (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
            usage.web_search_requests,
            usage.web_fetch_requests,
        )
    )

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=resolved_model,
        usage=usage,
        latency_ms=latency_ms,
        has_usage=has_usage,
        stop_reason=stop_reason,
        operation_status=status,
        error=error,
        provider_record_id=provider_record_id,
        tool_calls=tool_calls,
        capability=capability,
        idempotency_key=idempotency_key,
        service_name=service_name,
        operation_name=operation_name,
    )


def _insert_llm_event(
    *,
    tracker: Any,
    task_id: Any,
    model: str,
    usage: _UsageSnapshot,
    latency_ms: int,
    has_usage: bool,
    stop_reason: str | None = None,
    operation_status: str = "succeeded",
    error: BaseException | None = None,
    provider_record_id: str | None = None,
    tool_calls: int = 0,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
    service_name: str = "messages",
    operation_name: str = "anthropic.messages.create",
) -> Event:
    """Create and persist an llm_call Event."""
    billable = _billable_usage(usage, stop_reason)
    pricing = _price_anthropic_usage(
        tracker,
        model,
        usage,
        billable,
        has_provider_usage=has_usage,
    )

    # Store cache_read_input_tokens in the standard cached_tokens field.
    # Store cache_creation_input_tokens in details for full auditability.
    details: dict[str, Any] = {
        "attribution_component": "llm",
        "attribution_operation_name": operation_name,
        "attribution_operation_status": operation_status,
        "attribution_resource_type": "model",
        "attribution_resource_id": model,
        "attribution_usage_lines": _usage_lines(
            billable.usage, tool_calls, billable.unbilled_usage
        ),
        "provider_usage_privacy": "quantities_only",
    }
    if provider_record_id is not None:
        details["provider_record_id"] = provider_record_id
    if error is not None:
        details.update(error_details(error))
    details.update(_cache_creation_details(billable.usage))
    if billable.usage.thinking_tokens > 0:
        details["reasoning_output_tokens"] = billable.usage.thinking_tokens
    dimensions = [
        {"key": key, "value": {"type": "string", "value": value}}
        for key, value in (
            ("service_tier", usage.service_tier),
            ("inference_geo", usage.inference_geo),
            ("inference_speed", usage.speed),
            ("fallback_credit_status", usage.fallback_credit_status),
            ("stop_reason", stop_reason),
        )
        if value is not None
    ]
    if dimensions:
        details["attribution_dimensions"] = dimensions
    if pricing.unpriced_dimensions:
        details["pricing_unpriced_dimensions"] = list(pricing.unpriced_dimensions)
    if pricing.iteration_details:
        details["anthropic_usage_iterations"] = list(pricing.iteration_details)
        details["anthropic_sampling_iteration_count"] = len(pricing.iteration_details)
        details["anthropic_top_level_usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
        }
    if pricing.server_tool_breakdown:
        details["pricing_breakdown"] = list(pricing.server_tool_breakdown)
    if usage.cache_breakdown_inconsistent:
        details["anthropic_cache_ttl_breakdown_inconsistent"] = True

    event = Event(
        task_id=task_id,
        event_type="llm_call",
        cost_usd=pricing.cost_usd,
        cost_confidence=pricing.cost_confidence,
        pricing_source=pricing.pricing_source,
        pricing_version=pricing.pricing_version,
        service_name=service_name,
        provider="anthropic",
        model=model,
        input_tokens=billable.usage.input_tokens,
        output_tokens=billable.usage.output_tokens,
        cached_tokens=billable.usage.cache_read_input_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    apply_event_capability(event, capability)
    apply_event_idempotency(event, idempotency_key)
    tracker._storage.insert_event(event)
    return event
