"""First-class, privacy-safe metering for the official Groq Python SDK."""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from typing import Any

from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)
from dexcost.instruments.openai import (
    _groq_pricing_lane,
    _groq_tool_execution_blocks_static_pricing,
)
from dexcost.instruments.openai_usage import normalize_openai_usage

_active_tracker: Any | None = None
_patched = False
_originals: dict[str, tuple[Any, str, Any]] = {}


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _usage(response: Any) -> Any:
    direct = _value(response, "usage")
    if direct is not None:
        return direct
    return _value(_value(response, "x_groq"), "usage")


def _request_value(args: tuple[Any, ...], kwargs: Mapping[str, Any], name: str) -> Any:
    value = kwargs.get(name)
    if value is None and args and isinstance(args[0], Mapping):
        value = args[0].get(name)
    return value


def _requested_model(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    value = _request_value(args, kwargs, "model")
    return value if isinstance(value, str) and value else "unknown"


def _measurement(
    response: Any,
    requested: str,
    *,
    service_tier: object = None,
    tool_execution_seen: bool = False,
) -> OperationMeasurement:
    usage = _usage(response)
    lines: list[ProviderUsageLine] = []
    pricing_usage: dict[str, int] = {}
    input_total = output_total = cached = reasoning = 0
    if usage is not None:
        normalized = normalize_openai_usage(usage)
        input_total = normalized.total_input_tokens
        output_total = normalized.total_output_tokens
        cached = normalized.cache_read_input_tokens
        cache_write = normalized.cache_write_input_tokens
        reasoning = normalized.reasoning_output_tokens
        for metric, quantity in (
            ("input_tokens", max(0, input_total - cached - cache_write)),
            ("cache_read_input_tokens", cached),
            ("cache_write_input_tokens", cache_write),
            ("output_tokens", max(0, output_total - reasoning)),
            ("reasoning_output_tokens", reasoning),
        ):
            if quantity > 0:
                lines.append(ProviderUsageLine(metric, quantity, "Tokens"))
                pricing_usage[metric] = quantity
    model = _value(response, "model")
    resolved_model = model if isinstance(model, str) and model else requested
    record_id = _value(response, "id")
    dimensions: list[tuple[str, str]] = [("gateway", "groq")]
    lane = _groq_pricing_lane(
        response if response is not None else {"usage": usage},
        service_tier,
        "groq",
        tool_execution_seen=tool_execution_seen,
    )
    if lane is not None:
        dimensions.append(("groq_pricing_lane", lane))
    return OperationMeasurement(
        pricing_usage=pricing_usage,
        usage_lines=tuple(lines),
        provider_record_id=record_id if isinstance(record_id, str) else None,
        response_model=resolved_model,
        billing_dimensions=tuple(dimensions),
        task_input_tokens=input_total,
        task_output_tokens=output_total,
        task_cached_tokens=cached,
    )


class _StreamMeter:
    def __init__(self, requested: str, service_tier: object) -> None:
        self.requested = requested
        self.latest: Any = None
        self.terminal: Any = None
        self.service_tier = service_tier
        self.tool_execution_seen = False

    def observe(self, item: Any) -> None:
        self.latest = item
        if _value(item, "service_tier") is not None:
            self.service_tier = _value(item, "service_tier")
        if _groq_tool_execution_blocks_static_pricing(item):
            self.tool_execution_seen = True
        if _usage(item) is not None:
            self.terminal = item

    def measurement(self) -> OperationMeasurement:
        return _measurement(
            self.terminal or self.latest,
            self.requested,
            service_tier=self.service_tier,
            tool_execution_seen=self.tool_execution_seen,
        )


def _session(requested: str) -> ProviderOperationSession:
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type="groq.chat.completions.create",
        provider="groq",
        service="chat",
        operation="groq.chat.completions.create",
        component="llm",
        model=requested,
        event_type="llm_call",
    )


def _sync_call(
    original: Any,
    instance: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    requested = _requested_model(args, kwargs)
    service_tier = _request_value(args, kwargs, "service_tier")
    session = _session(requested)
    try:
        result = original(instance, *args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise
    if kwargs.get("stream") is True and hasattr(result, "__next__"):
        meter = _StreamMeter(requested, service_tier)
        session.release_context()
        return SyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
        )
    session.succeed(_measurement(result, requested, service_tier=service_tier))
    return result


async def _async_call(
    original: Any,
    instance: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    requested = _requested_model(args, kwargs)
    service_tier = _request_value(args, kwargs, "service_tier")
    session = _session(requested)
    try:
        result = await original(instance, *args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise
    if kwargs.get("stream") is True and hasattr(result, "__anext__"):
        meter = _StreamMeter(requested, service_tier)
        session.release_context()
        return AsyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
        )
    session.succeed(_measurement(result, requested, service_tier=service_tier))
    return result


def _patch(owner: Any, name: str, *, is_async: bool) -> None:
    original = getattr(owner, name)
    key = f"{owner.__module__}:{owner.__name__}:{name}"

    if is_async:
        async def async_replacement(self: Any, *args: Any, **kwargs: Any) -> Any:
            return await _async_call(original, self, args, kwargs)

        replacement = async_replacement
    else:
        def sync_replacement(self: Any, *args: Any, **kwargs: Any) -> Any:
            return _sync_call(original, self, args, kwargs)

        replacement = sync_replacement

    setattr(owner, name, provider_capture_callable("groq", replacement, original))
    _originals[key] = (owner, name, original)


def instrument_groq(tracker: Any) -> None:
    """Instrument official Groq chat completions without retaining content."""
    global _active_tracker, _patched
    if _patched:
        raise RuntimeError("Groq instrumentation is already active")
    try:
        module = import_module("groq.resources.chat.completions")
        sync_owner = module.Completions
        async_owner = module.AsyncCompletions
    except (AttributeError, ImportError) as exc:
        raise ImportError(
            "Groq instrumentation requires the 'groq' package; install dexcost[groq]"
        ) from exc
    _active_tracker = tracker
    try:
        _patch(sync_owner, "create", is_async=False)
        _patch(async_owner, "create", is_async=True)
    except Exception:
        uninstrument_groq()
        raise
    _patched = True


def uninstrument_groq() -> None:
    """Restore the exact official SDK methods captured at patch time."""
    global _active_tracker, _patched
    for owner, name, original in reversed(list(_originals.values())):
        setattr(owner, name, original)
    _originals.clear()
    _active_tracker = None
    _patched = False


__all__ = ["instrument_groq", "uninstrument_groq"]
