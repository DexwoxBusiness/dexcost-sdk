"""First-class metering for the official Perplexity Python SDK."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from decimal import Decimal
from importlib import import_module
from typing import Any, Literal

from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    OperationStatus,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)
from dexcost.models.provider_job import ProviderJobStatus
from dexcost.provider_jobs import ProviderJobSession, reconcile_provider_job

_log = logging.getLogger(__name__)
_active_tracker: Any | None = None
_patched = False
_originals: dict[str, tuple[Any, str, Any]] = {}

_Kind = Literal["responses", "chat", "search", "embeddings", "contextualized_embeddings"]
_METHODS: tuple[tuple[str, str, _Kind, bool], ...] = (
    ("ResponsesResource", "create", "responses", False),
    ("AsyncClientResponsesResource", "create", "responses", True),
    ("ChatCompletionsResource", "create", "chat", False),
    ("AsyncClientChatCompletionsResource", "create", "chat", True),
    ("SearchResource", "create", "search", False),
    ("AsyncClientSearchResource", "create", "search", True),
    ("EmbeddingsResource", "create", "embeddings", False),
    ("AsyncClientEmbeddingsResource", "create", "embeddings", True),
    (
        "ContextualizedEmbeddingsResource",
        "create",
        "contextualized_embeddings",
        False,
    ),
    (
        "AsyncClientContextualizedEmbeddingsResource",
        "create",
        "contextualized_embeddings",
        True,
    ),
)


def _value(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict) and name in attributes:
        return attributes[name]
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, Mapping) and name in extra:
        return extra[name]
    return getattr(value, name, None)


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _line(metric: str, quantity: Any, unit: str) -> ProviderUsageLine | None:
    parsed = _decimal(quantity)
    return ProviderUsageLine(metric, parsed, unit) if parsed is not None and parsed > 0 else None


def _model(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "perplexity/unknown"
    return value if value.startswith("perplexity/") else f"perplexity/{value}"


def _requested_model(kind: _Kind, kwargs: Mapping[str, Any]) -> str:
    if kind == "search":
        return "perplexity/search"
    selected = kwargs.get("model")
    if not isinstance(selected, str):
        preset = kwargs.get("preset")
        selected = f"preset/{preset}" if isinstance(preset, str) else "unknown"
    return _model(selected)


def _record_id(response: Any) -> str | None:
    value = _value(response, "id") or _value(response, "response_id")
    return value[:256] if isinstance(value, str) and value else None


def _cost(usage: Any) -> Decimal | None:
    return _decimal(_value(_value(usage, "cost"), "total_cost"))


def _dimensions(usage: Any) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = [("gateway", "perplexity")]
    context = _value(usage, "search_context_size")
    if isinstance(context, str) and context:
        result.append(("search_context_size", context[:256]))
    return tuple(result)


def _token_measurement(response: Any, requested: str) -> OperationMeasurement:
    usage = _value(response, "usage")
    prompt = _integer(_value(usage, "prompt_tokens"))
    if prompt is None:
        prompt = _integer(_value(usage, "input_tokens"))
    completion = _integer(_value(usage, "completion_tokens"))
    if completion is None:
        completion = _integer(_value(usage, "output_tokens"))
    input_details = _value(usage, "input_tokens_details")
    cache_read = _integer(_value(input_details, "cache_read_input_tokens")) or 0
    cache_write = _integer(_value(input_details, "cache_creation_input_tokens")) or 0
    ordinary_input: int | None
    if prompt is not None and cache_read + cache_write <= prompt:
        ordinary_input = prompt - cache_read - cache_write
    else:
        ordinary_input = prompt
        cache_read = cache_write = 0
    reasoning = _integer(_value(usage, "reasoning_tokens"))
    output_details = _value(usage, "output_tokens_details")
    if reasoning is None:
        reasoning = _integer(_value(output_details, "reasoning_tokens"))
    reasoning = reasoning or 0
    pricing: dict[str, int] = {}
    lines: list[ProviderUsageLine] = []
    for metric, quantity in (
        ("input_tokens", ordinary_input),
        ("cache_read_input_tokens", cache_read),
        ("cache_write_input_tokens", cache_write),
        ("output_tokens", completion),
        ("reasoning_output_tokens", reasoning),
        ("citation_tokens", _integer(_value(usage, "citation_tokens"))),
        ("query_count", _integer(_value(usage, "num_search_queries"))),
    ):
        if quantity is not None:
            pricing[metric] = quantity
            item = _line(metric, quantity, "Tokens" if "token" in metric else "Queries")
            if item is not None:
                lines.append(item)
    tool_calls = _value(usage, "tool_calls_details")
    if isinstance(tool_calls, Mapping):
        for raw_name, details in tool_calls.items():
            name = re.sub(r"[^a-z0-9_]+", "_", str(raw_name).lower()).strip("_")
            count = _integer(_value(details, "invocation"))
            if not name or count is None:
                continue
            item = _line(f"tool_{name}_invocation_count", count, "Calls")
            if item is not None:
                lines.append(item)
            if name in {"search_web", "web_search"}:
                pricing["web_search_calls"] = count
    response_model = _model(_value(response, "model") or requested)
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        provider_record_id=_record_id(response),
        provider_cost_usd=_cost(usage),
        response_model=response_model,
        model_candidates=(response_model,),
        billing_dimensions=_dimensions(usage),
        task_input_tokens=prompt,
        task_output_tokens=completion,
        task_cached_tokens=cache_read,
    )


def _search_measurement(response: Any) -> OperationMeasurement:
    results = _value(response, "results")
    result_count = (
        len(results)
        if isinstance(results, Sequence) and not isinstance(results, (str, bytes, bytearray))
        else None
    )
    lines = tuple(
        item
        for item in (
            _line("query_count", 1, "Queries"),
            _line("result_count", result_count, "Results"),
        )
        if item is not None
    )
    return OperationMeasurement(
        pricing_usage={"query_count": 1},
        usage_lines=lines,
        provider_record_id=_record_id(response),
        response_model="perplexity/search",
        model_candidates=("perplexity/search",),
        billing_dimensions=(("gateway", "perplexity"),),
    )


def _embedding_measurement(response: Any, requested: str) -> OperationMeasurement:
    usage = _value(response, "usage")
    tokens = _integer(_value(usage, "prompt_tokens"))
    data = _value(response, "data")
    data_items = (
        data
        if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray))
        else None
    )
    count = len(data_items) if data_items is not None else None
    nested = _value(data_items[0], "data") if data_items else None
    if (
        data_items
        and isinstance(nested, Sequence)
        and not isinstance(nested, (str, bytes, bytearray))
    ):
        count = sum(
            len(item_data)
            for item in data_items
            if isinstance((item_data := _value(item, "data")), Sequence)
            and not isinstance(item_data, (str, bytes, bytearray))
        )
    lines = tuple(
        item
        for item in (
            _line("input_tokens", tokens, "Tokens"),
            _line("embedding_count", count, "Embeddings"),
        )
        if item is not None
    )
    response_model = _model(_value(response, "model") or requested)
    return OperationMeasurement(
        pricing_usage={} if tokens is None else {"input_tokens": tokens},
        usage_lines=lines,
        provider_record_id=_record_id(response),
        provider_cost_usd=_cost(usage),
        response_model=response_model,
        model_candidates=(response_model,),
        billing_dimensions=(("gateway", "perplexity"),),
        task_input_tokens=tokens,
    )


def _measurement(kind: _Kind, response: Any, requested: str) -> OperationMeasurement:
    if kind == "search":
        return _search_measurement(response)
    if kind in {"embeddings", "contextualized_embeddings"}:
        return _embedding_measurement(response, requested)
    return _token_measurement(response, requested)


def _status(response: Any, *, terminal_required: bool = False) -> OperationStatus:
    value = _value(response, "status")
    if isinstance(value, str):
        normalized = value.lower()
        if normalized in {"completed", "succeeded"}:
            return "succeeded"
        if normalized in {"failed", "error"}:
            return "failed"
        if normalized in {"cancelled", "canceled"}:
            return "cancelled"
        if terminal_required:
            return "unknown"
    return "succeeded"


class _StreamMeter:
    def __init__(self, kind: _Kind, requested: str) -> None:
        self.kind = kind
        self.requested = requested
        self.latest: Any = None
        self.terminal: Any = None
        self.failed = False

    def observe(self, item: Any) -> None:
        self.latest = item
        event_type = _value(item, "type")
        if event_type == "response.failed":
            self.failed = True
        nested = _value(item, "response")
        candidate = nested if nested is not None else item
        if _value(candidate, "usage") is not None:
            self.terminal = candidate

    def measurement(self) -> OperationMeasurement:
        return _measurement(self.kind, self.terminal or self.latest, self.requested)

    def completion_status(self) -> OperationStatus:
        if self.failed:
            return "failed"
        if self.terminal is None:
            return "unknown"
        return _status(self.terminal, terminal_required=True)


def _session(kind: _Kind, requested: str) -> ProviderOperationSession:
    event_type = "external_cost" if kind == "search" else "llm_call"
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=f"perplexity.{kind}.create",
        provider="perplexity",
        service=kind,
        operation=f"perplexity.{kind}.create",
        component="external" if kind == "search" else "llm",
        model=requested,
        event_type=event_type,
    )


def _job_status(response: Any) -> ProviderJobStatus:
    value = str(_value(response, "status") or "").lower()
    statuses: dict[str, ProviderJobStatus] = {
        "queued": "submitted",
        "in_progress": "running",
        "requires_action": "running",
        "cancelling": "running",
        "completed": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    return statuses.get(value, "running")


def _sync_call(
    original: Any,
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    kind: _Kind,
) -> Any:
    requested = _requested_model(kind, kwargs)
    if kind == "responses" and kwargs.get("background") is True:
        job = ProviderJobSession(
            tracker=_active_tracker,
            task_type="perplexity.responses.create",
            provider="perplexity",
            service="responses",
            operation="perplexity.responses.create",
            component="llm",
            event_type="llm_call",
            resource_type="model",
            resource_id=requested,
            billing_dimensions=(("gateway", "perplexity"),),
        )
        try:
            result = original(self, *args, **kwargs)
        except BaseException as exc:
            job.fail(exc)
            raise
        record_id = _record_id(result)
        if record_id is None:
            job.fail(ValueError("Perplexity background response omitted its id"))
            return result
        status = _job_status(result)
        measurement = _measurement(kind, result, requested) if status == "succeeded" else None
        job.submit(record_id, status=status, measurement=measurement)
        return result
    session = _session(kind, requested)
    try:
        result = original(self, *args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise
    if kwargs.get("stream") is True and hasattr(result, "__next__"):
        meter = _StreamMeter(kind, requested)
        session.release_context()
        return SyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
            completion_status=meter.completion_status,
        )
    session.finish(_measurement(kind, result, requested), _status(result))
    return result


async def _async_call(
    original: Any,
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    kind: _Kind,
) -> Any:
    requested = _requested_model(kind, kwargs)
    if kind == "responses" and kwargs.get("background") is True:
        job = ProviderJobSession(
            tracker=_active_tracker,
            task_type="perplexity.responses.create",
            provider="perplexity",
            service="responses",
            operation="perplexity.responses.create",
            component="llm",
            event_type="llm_call",
            resource_type="model",
            resource_id=requested,
            billing_dimensions=(("gateway", "perplexity"),),
        )
        try:
            result = await original(self, *args, **kwargs)
        except BaseException as exc:
            job.fail(exc)
            raise
        record_id = _record_id(result)
        if record_id is None:
            job.fail(ValueError("Perplexity background response omitted its id"))
            return result
        status = _job_status(result)
        measurement = _measurement(kind, result, requested) if status == "succeeded" else None
        job.submit(record_id, status=status, measurement=measurement)
        return result
    session = _session(kind, requested)
    try:
        result = await original(self, *args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise
    if kwargs.get("stream") is True and hasattr(result, "__anext__"):
        meter = _StreamMeter(kind, requested)
        session.release_context()
        return AsyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
            completion_status=meter.completion_status,
        )
    session.finish(_measurement(kind, result, requested), _status(result))
    return result


def _sync_wrapper(key: str, kind: _Kind) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _sync_call(_originals[key][2], self, args, kwargs, kind)

    return wrapper


def _async_wrapper(key: str, kind: _Kind) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await _async_call(_originals[key][2], self, args, kwargs, kind)

    return wrapper


def _response_id(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str | None:
    value = kwargs.get("response_id")
    if not isinstance(value, str) and args:
        value = args[0]
    return value if isinstance(value, str) and value else None


def _reconcile_response(result: Any, response_id: str, *, cancelled: bool = False) -> None:
    tracker = _active_tracker
    if tracker is None:
        return
    previous = tracker._storage.get_provider_job("perplexity", "responses", response_id)
    if previous is None:
        return
    status = "cancelled" if cancelled else _job_status(result)
    measurement = (
        _measurement("responses", result, previous.resource_id) if status == "succeeded" else None
    )
    reconcile_provider_job(
        tracker=tracker,
        provider="perplexity",
        service="responses",
        provider_record_id=response_id,
        status=status,
        measurement=measurement,
    )


def _sync_reconcile(key: str, *, cancelled: bool = False) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = _originals[key][2](self, *args, **kwargs)
        response_id = _response_id(args, kwargs)
        if response_id is not None:
            with suppress(Exception):
                _reconcile_response(result, response_id, cancelled=cancelled)
        return result

    return wrapper


def _async_reconcile(key: str, *, cancelled: bool = False) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await _originals[key][2](self, *args, **kwargs)
        response_id = _response_id(args, kwargs)
        if response_id is not None:
            with suppress(Exception):
                _reconcile_response(result, response_id, cancelled=cancelled)
        return result

    return wrapper


def _patch(owner: Any, name: str, replacement: Any, key: str) -> None:
    original = getattr(owner, name, None)
    if callable(original):
        _originals[key] = (owner, name, original)
        setattr(
            owner,
            name,
            provider_capture_callable("perplexity", replacement, original),
        )


def _restore_all() -> None:
    for owner, name, original in tuple(_originals.values()):
        with suppress(Exception):
            setattr(owner, name, original)
    _originals.clear()


def instrument_perplexity(tracker: Any) -> None:
    """Instrument all current billable official Perplexity API resources."""
    global _active_tracker, _patched
    if _patched:
        raise RuntimeError("Perplexity instrumentation is already active")
    try:
        api = import_module("perplexity.generated.api")
    except ImportError as exc:
        raise ImportError(
            "Perplexity instrumentation requires 'perplexityai'; " "install dexcost[perplexity]"
        ) from exc
    _active_tracker = tracker
    try:
        for class_name, method_name, kind, is_async in _METHODS:
            owner = getattr(api, class_name)
            key = f"perplexity.generated.api:{class_name}:{method_name}"
            replacement = _async_wrapper(key, kind) if is_async else _sync_wrapper(key, kind)
            _patch(owner, method_name, replacement, key)
        for class_name, is_async in (
            ("ResponsesResource", False),
            ("AsyncClientResponsesResource", True),
        ):
            owner = getattr(api, class_name)
            for method_name in ("retrieve", "cancel"):
                key = f"perplexity.generated.api:{class_name}:{method_name}"
                replacement = (
                    _async_reconcile(key, cancelled=method_name == "cancel")
                    if is_async
                    else _sync_reconcile(key, cancelled=method_name == "cancel")
                )
                _patch(owner, method_name, replacement, key)
    except Exception:
        _restore_all()
        _active_tracker = None
        raise
    _patched = True


def uninstrument_perplexity() -> None:
    """Restore the exact official Perplexity SDK methods captured at patch time."""
    global _active_tracker, _patched
    if not _patched:
        return
    _restore_all()
    _active_tracker = None
    _patched = False


__all__ = ["instrument_perplexity", "uninstrument_perplexity"]
