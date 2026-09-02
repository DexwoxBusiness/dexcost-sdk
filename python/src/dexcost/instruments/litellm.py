"""Auto-instrumentation for LiteLLM — a unified LLM gateway.

Monkey-patches ``litellm.completion`` and ``litellm.acompletion`` using
:pypi:`wrapt` so that every call made inside an active
:class:`~dexcost.tracker.CostTracker` task is automatically recorded as an
``llm_call`` event.

Usage::

    from dexcost import CostTracker, instrument_litellm

    tracker = CostTracker()
    instrument_litellm(tracker)

    # All subsequent litellm.completion() / litellm.acompletion() calls
    # inside a tracked task are captured automatically.

Implements US-014.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from inspect import isawaitable
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
    requested_model,
)
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    OperationStatus,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)
from dexcost.instruments.openai_usage import OpenAIUsageError, normalize_openai_usage
from dexcost.models._serde import canonical_decimal
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
_patched_owner: Any | None = None


@dataclass(frozen=True)
class _OperationSpec:
    """One public LiteLLM operation that has independent billing evidence."""

    name: str
    async_call: bool
    component: str
    call_type: str
    model_position: int | None
    supports_stream: bool = False


_OPERATION_SPECS: tuple[_OperationSpec, ...] = (
    _OperationSpec("anthropic_messages", True, "llm", "anthropic_messages", 2, True),
    _OperationSpec("agenerate_content", True, "llm", "agenerate_content", 0),
    _OperationSpec("text_completion", False, "llm", "text_completion", 1, True),
    _OperationSpec("atext_completion", True, "llm", "atext_completion", 1, True),
    _OperationSpec("responses", False, "llm", "responses", 1, True),
    _OperationSpec("aresponses", True, "llm", "aresponses", 1, True),
    _OperationSpec("embedding", False, "llm", "embedding", 0),
    _OperationSpec("aembedding", True, "llm", "aembedding", 0),
    _OperationSpec("image_generation", False, "image", "image_generation", 1),
    _OperationSpec("aimage_generation", True, "image", "aimage_generation", 1),
    _OperationSpec("image_edit", False, "image", "image_edit", 2),
    _OperationSpec("aimage_edit", True, "image", "aimage_edit", 1),
    _OperationSpec("image_variation", False, "image", "image_generation", 1),
    _OperationSpec("aimage_variation", True, "image", "aimage_generation", 1),
    _OperationSpec("transcription", False, "speech_to_text", "transcription", 0),
    _OperationSpec("atranscription", True, "speech_to_text", "atranscription", 0),
    _OperationSpec("speech", False, "text_to_speech", "speech", 0),
    _OperationSpec("aspeech", True, "text_to_speech", "aspeech", 0),
    _OperationSpec("rerank", False, "rerank", "rerank", 0),
    _OperationSpec("arerank", True, "rerank", "arerank", 0),
    _OperationSpec("moderation", False, "moderation", "moderation", 1),
    _OperationSpec("amoderation", True, "moderation", "amoderation", 1),
    _OperationSpec("search", False, "search", "search", None),
    _OperationSpec("asearch", True, "search", "asearch", None),
    _OperationSpec("ocr", False, "ocr", "ocr", 0),
    _OperationSpec("aocr", True, "ocr", "aocr", 0),
)


@dataclass(frozen=True)
class _JobSpec:
    """One LiteLLM delayed-work submission or reconciliation entry point."""

    name: str
    async_call: bool
    kind: str
    phase: str
    model_position: int | None = None
    record_id_name: str | None = None


_JOB_SPECS: tuple[_JobSpec, ...] = (
    _JobSpec("video_generation", False, "video", "submit", 1),
    _JobSpec("avideo_generation", True, "video", "submit", 1),
    _JobSpec("video_edit", False, "video", "submit"),
    _JobSpec("avideo_edit", True, "video", "submit"),
    _JobSpec("video_remix", False, "video", "submit"),
    _JobSpec("avideo_remix", True, "video", "submit"),
    _JobSpec("video_extension", False, "video", "submit"),
    _JobSpec("avideo_extension", True, "video", "submit"),
    _JobSpec("video_status", False, "video", "reconcile", record_id_name="video_id"),
    _JobSpec("avideo_status", True, "video", "reconcile", record_id_name="video_id"),
    _JobSpec("create_batch", False, "batch", "submit"),
    _JobSpec("acreate_batch", True, "batch", "submit"),
    _JobSpec("retrieve_batch", False, "batch", "reconcile", record_id_name="batch_id"),
    _JobSpec("aretrieve_batch", True, "batch", "reconcile", record_id_name="batch_id"),
    _JobSpec("cancel_batch", False, "batch", "reconcile", record_id_name="batch_id"),
    _JobSpec("acancel_batch", True, "batch", "reconcile", record_id_name="batch_id"),
    _JobSpec("create_fine_tuning_job", False, "fine_tuning", "submit", 0),
    _JobSpec("acreate_fine_tuning_job", True, "fine_tuning", "submit", 0),
    _JobSpec(
        "retrieve_fine_tuning_job",
        False,
        "fine_tuning",
        "reconcile",
        record_id_name="fine_tuning_job_id",
    ),
    _JobSpec(
        "aretrieve_fine_tuning_job",
        True,
        "fine_tuning",
        "reconcile",
        record_id_name="fine_tuning_job_id",
    ),
    _JobSpec(
        "cancel_fine_tuning_job",
        False,
        "fine_tuning",
        "reconcile",
        record_id_name="fine_tuning_job_id",
    ),
    _JobSpec(
        "acancel_fine_tuning_job",
        True,
        "fine_tuning",
        "reconcile",
        record_id_name="fine_tuning_job_id",
    ),
    _JobSpec("get_responses", False, "response", "reconcile", record_id_name="response_id"),
    _JobSpec("aget_responses", True, "response", "reconcile", record_id_name="response_id"),
    _JobSpec(
        "cancel_responses", False, "response", "reconcile", record_id_name="response_id"
    ),
    _JobSpec(
        "acancel_responses", True, "response", "reconcile", record_id_name="response_id"
    ),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def instrument_litellm(tracker: Any) -> None:
    """Monkey-patch LiteLLM to capture LLM calls automatically.

    Patches LiteLLM's public inference operations, including chat/text and
    Responses calls, embeddings, images, audio, rerank, moderation, search,
    and OCR. Optional entry points are patched only when the installed
    LiteLLM version exposes them.

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            price calls and persist events.

    Raises:
        ImportError: If the ``litellm`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched, _patched_owner

    # Verify litellm is importable
    try:
        import litellm as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'litellm' package is required for LiteLLM auto-instrumentation. "
            "Install it with: pip install litellm"
        ) from exc

    import litellm

    if _patched:
        if _patched_owner is litellm:
            raise RuntimeError(
                "LiteLLM instrumentation is already active. "
                "Call uninstrument_litellm() before re-instrumenting."
            )
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None

    _active_tracker = tracker
    _patched_owner = litellm

    # Store originals for uninstrument

    _originals["completion"] = litellm.completion
    _originals["acompletion"] = litellm.acompletion

    # Apply monkey-patches via wrapt
    wrapt.wrap_function_wrapper(
        "litellm",
        "completion",
        provider_capture_wrapper("litellm", _sync_completion_wrapper),
    )
    wrapt.wrap_function_wrapper(
        "litellm",
        "acompletion",
        provider_capture_wrapper("litellm", _async_completion_wrapper),
    )
    for spec in _OPERATION_SPECS:
        candidate = getattr(litellm, spec.name, None)
        if not callable(candidate):
            continue
        _originals[spec.name] = candidate
        wrapt.wrap_function_wrapper(
            "litellm",
            spec.name,
            provider_capture_wrapper("litellm", _operation_wrapper(spec)),
        )
    for job_spec in _JOB_SPECS:
        candidate = getattr(litellm, job_spec.name, None)
        if not callable(candidate):
            continue
        _originals[job_spec.name] = candidate
        wrapt.wrap_function_wrapper(
            "litellm",
            job_spec.name,
            provider_capture_wrapper("litellm", _job_wrapper(job_spec)),
        )

    _patched = True


def uninstrument_litellm() -> None:
    """Remove LiteLLM monkey-patches and restore original functions.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched, _patched_owner

    if not _patched:
        return

    try:
        import litellm
    except ImportError:
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None
        return

    if _patched_owner is not litellm:
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None
        return

    for name, original in _originals.items():
        setattr(litellm, name, original)

    _originals.clear()
    _active_tracker = None
    _patched = False
    _patched_owner = None


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _call_argument(
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    name: str,
    position: int | None,
) -> Any:
    value = kwargs.get(name)
    if value is not None:
        return value
    if position is not None and position < len(args):
        return args[position]
    return None


def _operation_request_model(
    spec: _OperationSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> str | None:
    if spec.name in {"search", "asearch"}:
        provider = _call_argument(args, kwargs, "search_provider", 1)
        return f"{provider}/search" if isinstance(provider, str) and provider else None
    model = _call_argument(args, kwargs, "model", spec.model_position)
    return model if isinstance(model, str) and model else None


def _operation_provider(
    spec: _OperationSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    response: Any = None,
) -> str:
    explicit = kwargs.get("custom_llm_provider")
    if spec.name == "anthropic_messages" and not isinstance(explicit, str):
        explicit = "anthropic"
    if spec.name in {"search", "asearch"}:
        explicit = _call_argument(args, kwargs, "search_provider", 1)
    if isinstance(explicit, str) and explicit:
        return _canonical_provider(explicit)
    return _resolve_provider(
        response=response,
        request_model=_operation_request_model(spec, args, kwargs),
    )


def _operation_session(
    spec: _OperationSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> ProviderOperationSession:
    request_model = _operation_request_model(spec, args, kwargs)
    provider = _operation_provider(spec, args, kwargs)
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=f"litellm.{spec.name}",
        provider=provider,
        service="litellm",
        operation=f"litellm.{spec.name}",
        component=spec.component,
        model=_canonical_model(request_model, provider, request_model),
        event_type="llm_call" if spec.component == "llm" else "external_cost",
    )


def _operation_wrapper(spec: _OperationSpec) -> Any:
    return _async_operation_wrapper(spec) if spec.async_call else _sync_operation_wrapper(spec)


def _sync_operation_wrapper(spec: _OperationSpec) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if spec.name == "responses" and kwargs.get("background") is True:
            return _sync_background_response(wrapped, args, kwargs, spec)
        session = _operation_session(spec, args, kwargs)
        try:
            try:
                with suppress_network_event():
                    response = wrapped(*args, **kwargs)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    session.cancel()
                else:
                    session.fail(exc)
                raise

            if isawaitable(response):

                async def await_response() -> Any:
                    try:
                        resolved = await response
                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            session.cancel()
                        else:
                            session.fail(exc)
                        raise
                    return _finish_operation(spec, args, kwargs, resolved, session)

                session.release_context()
                return await_response()
            return _finish_operation(spec, args, kwargs, response, session)
        finally:
            session.release_context()

    return wrapper


def _async_operation_wrapper(spec: _OperationSpec) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if spec.name == "aresponses" and kwargs.get("background") is True:
            return _async_background_response(wrapped, args, kwargs, spec)

        async def invoke() -> Any:
            session = _operation_session(spec, args, kwargs)
            try:
                try:
                    with suppress_network_event():
                        response = await wrapped(*args, **kwargs)
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        session.cancel()
                    else:
                        session.fail(exc)
                    raise
                return _finish_operation(spec, args, kwargs, response, session)
            finally:
                session.release_context()

        return invoke()

    return wrapper


def _finish_operation(
    spec: _OperationSpec,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    response: Any,
    session: ProviderOperationSession,
) -> Any:
    provider = _operation_provider(spec, args, kwargs, response)
    request_model = _operation_request_model(spec, args, kwargs)
    response_model = _response_model(response) or request_model
    session.provider = provider
    session.model = _canonical_model(response_model, provider, request_model)
    if spec.supports_stream and kwargs.get("stream") is True:
        session.release_context()
        meter = _OperationStreamMeter(spec, args, kwargs, provider)
        if hasattr(response, "__aiter__"):
            return AsyncProviderStream(
                response,
                session,
                observe=meter.observe,
                measurement=meter.measurement,
                completion_status=meter.status,
            )
        return SyncProviderStream(
            response,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
            completion_status=meter.status,
        )
    measurement = _operation_measurement(spec, args, kwargs, response, provider)
    session.finish(measurement, _operation_status(response))
    return response


class _OperationStreamMeter:
    """Accumulate only billing metadata from a LiteLLM operation stream."""

    def __init__(
        self,
        spec: _OperationSpec,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        provider: str,
    ) -> None:
        self.spec = spec
        self.args = args
        self.kwargs = kwargs
        self.provider = provider
        self.final: Any = None
        self.usage: Any = None
        self.model: Any = None
        self.hidden_params: dict[str, Any] = {}
        self.record_id: str | None = None
        self.terminal_status: OperationStatus = "succeeded"

    def observe(self, chunk: Any) -> None:
        nested = _field(chunk, "response")
        candidate = nested if nested is not None else chunk
        usage = _field(candidate, "usage")
        if usage is not None:
            self.usage = usage
            self.final = candidate
        model = _response_model(candidate)
        if model is not None:
            self.model = model
        hidden = _response_hidden_params(candidate)
        if hidden:
            self.hidden_params = hidden
        record_id = _provider_record_id(candidate)
        if record_id is not None:
            self.record_id = record_id
        status = _operation_status(candidate)
        if status != "succeeded" or nested is not None:
            self.terminal_status = status

    def measurement(self) -> OperationMeasurement:
        response = self.final
        if response is None:
            response = {
                "usage": self.usage,
                "model": self.model,
                "id": self.record_id,
                "_hidden_params": self.hidden_params,
            }
        return _operation_measurement(
            self.spec,
            self.args,
            self.kwargs,
            response,
            self.provider,
        )

    def status(self) -> OperationStatus:
        return self.terminal_status


def _operation_status(response: Any) -> OperationStatus:
    raw = _field(response, "status")
    if not isinstance(raw, str):
        return "succeeded"
    normalized = raw.strip().lower().replace("-", "_")
    if normalized in {"completed", "complete", "succeeded", "success"}:
        return "succeeded"
    if normalized in {"failed", "error", "expired", "incomplete"}:
        return "failed"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    return "unknown"


def _response_model(response: Any) -> str | None:
    direct = _field(response, "model")
    if isinstance(direct, str) and direct:
        return direct
    hidden = _response_hidden_params(response)
    for key in ("model", "litellm_model_name"):
        candidate = hidden.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _append_usage_line(
    lines: list[ProviderUsageLine],
    metric: str,
    quantity: Decimal | int,
    unit: str,
) -> None:
    if quantity <= 0 or any(line.metric == metric for line in lines):
        return
    lines.append(ProviderUsageLine(metric, quantity, unit))


def _operation_measurement(
    spec: _OperationSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    response: Any,
    provider: str,
) -> OperationMeasurement:
    usage = _field(response, "usage")
    pricing, raw_lines, input_tokens, output_tokens, cached_tokens = _usage_measurement(usage)
    pricing_usage: dict[str, Decimal | int | str] = dict(pricing)
    lines = [
        ProviderUsageLine(line["metric"], line["quantity"], line["unit"])
        for line in raw_lines
    ]
    hidden = _response_hidden_params(response)

    usage_metadata = _field(response, "usage_metadata")
    if usage is None and usage_metadata is not None:
        prompt_total = _non_negative_int(_field(usage_metadata, "prompt_token_count")) or 0
        cache_total = (
            _non_negative_int(_field(usage_metadata, "cached_content_token_count")) or 0
        )
        output_visible = (
            _non_negative_int(_field(usage_metadata, "candidates_token_count")) or 0
        )
        reasoning = _non_negative_int(_field(usage_metadata, "thoughts_token_count")) or 0
        tool_input = (
            _non_negative_int(_field(usage_metadata, "tool_use_prompt_token_count")) or 0
        )
        for metric, quantity in (
            ("input_tokens", max(0, prompt_total - cache_total)),
            ("cache_read_input_tokens", cache_total),
            ("output_tokens", output_visible),
            ("reasoning_output_tokens", reasoning),
            ("tool_input_tokens", tool_input),
        ):
            if quantity > 0:
                pricing_usage[metric] = quantity
                _append_usage_line(lines, metric, quantity, "Tokens")
        input_tokens = prompt_total + tool_input
        output_tokens = output_visible + reasoning
        cached_tokens = cache_total

    if spec.component == "image":
        data = _field(response, "data")
        image_count = len(data) if isinstance(data, list) else 0
        if image_count == 0:
            image_count = _non_negative_int(kwargs.get("n")) or 1
        _append_usage_line(lines, "image_count", image_count, "Images")
        if not pricing_usage:
            pricing_usage["image_count"] = image_count

    if spec.component == "speech_to_text":
        usage_type = _field(usage, "type")
        seconds = _non_negative_decimal(_field(usage, "seconds"))
        if seconds is None:
            seconds = _non_negative_decimal(hidden.get("audio_transcription_duration"))
        if seconds is None:
            seconds = _non_negative_decimal(_field(response, "duration"))
        if seconds is not None and (usage_type == "duration" or not pricing_usage):
            pricing_usage["input_audio_seconds"] = seconds
            _append_usage_line(lines, "audio_seconds", seconds, "Seconds")

    if spec.component == "text_to_speech":
        input_text = _call_argument(args, kwargs, "input", 1)
        characters = len(input_text) if isinstance(input_text, str) else 0
        if characters:
            pricing_usage["characters"] = characters
            _append_usage_line(lines, "characters", characters, "Characters")

    if spec.component == "rerank":
        meta = _field(response, "meta")
        billed = _field(meta, "billed_units")
        search_units = _non_negative_decimal(_field(billed, "search_units"))
        total_tokens = _non_negative_int(_field(billed, "total_tokens"))
        if search_units is not None:
            pricing_usage["query_count"] = search_units
            _append_usage_line(lines, "search_units", search_units, "SearchUnits")
        if total_tokens is not None:
            pricing_usage.setdefault("input_tokens", total_tokens)
            _append_usage_line(lines, "input_tokens", total_tokens, "Tokens")
            input_tokens = max(input_tokens, total_tokens)

    if spec.component == "search":
        query = _call_argument(args, kwargs, "query", 0)
        query_count = len(query) if isinstance(query, list) else 1
        pricing_usage["query_count"] = query_count
        _append_usage_line(lines, "query_count", query_count, "Queries")

    if spec.component == "ocr":
        usage_info = _field(response, "usage_info")
        pages = _non_negative_int(_field(usage_info, "pages_processed"))
        document_bytes = _non_negative_int(_field(usage_info, "doc_size_bytes"))
        if pages is not None:
            pricing_usage["page_count"] = pages
            _append_usage_line(lines, "page_count", pages, "Pages")
        if document_bytes is not None:
            _append_usage_line(lines, "document_bytes", document_bytes, "Bytes")

    request_model = _operation_request_model(spec, args, kwargs)
    response_model = _response_model(response) or request_model
    model = _canonical_model(response_model, provider, request_model)
    provider_cost = _provider_response_cost(provider, usage, hidden)
    gateway_cost = None
    if provider_cost is None:
        gateway_cost = _try_litellm_operation_cost(
            spec,
            response,
            request_model,
            kwargs,
        )
    pricing_version = _litellm_pricing_version() if gateway_cost is not None else None
    return OperationMeasurement(
        pricing_usage=pricing_usage,
        usage_lines=tuple(lines),
        provider_record_id=_provider_record_id(response),
        provider_cost_usd=provider_cost,
        provider_upstream_cost_usd=_provider_upstream_cost(usage),
        computed_cost_usd=gateway_cost,
        computed_cost_source="litellm" if gateway_cost is not None else None,
        computed_cost_confidence="computed" if gateway_cost is not None else None,
        computed_pricing_version=pricing_version,
        response_model=model,
        model_candidates=(model,),
        billing_dimensions=(("gateway", "litellm"),),
        task_input_tokens=input_tokens or None,
        task_output_tokens=output_tokens or None,
        task_cached_tokens=cached_tokens or None,
    )


def _litellm_pricing_version() -> str:
    try:
        return f"litellm:{version('litellm')}"
    except PackageNotFoundError:  # pragma: no cover - fake test module
        return "litellm:unknown"


def _try_litellm_operation_cost(
    spec: _OperationSpec,
    response: Any,
    request_model: str | None,
    kwargs: Mapping[str, Any],
) -> Decimal | None:
    hidden_cost = _try_hidden_litellm_cost(_response_hidden_params(response))
    if hidden_cost is not None and hidden_cost > 0:
        return hidden_cost
    try:
        import litellm

        cost_kwargs: dict[str, Any] = {
            "completion_response": response,
            "call_type": spec.call_type,
        }
        if request_model is not None:
            cost_kwargs["model"] = request_model
        if spec.component == "image":
            for name in ("size", "quality", "n"):
                value = kwargs.get(name)
                if value is not None:
                    cost_kwargs[name] = value
        elif spec.component == "search":
            query = kwargs.get("query")
            query_count = len(query) if isinstance(query, list) else 1
            cost_kwargs["optional_params"] = {"query": [""] * query_count}
        cost = litellm.completion_cost(**cost_kwargs)
        parsed = _non_negative_decimal(cost)
        return parsed if parsed is not None and parsed > 0 else None
    except Exception:
        return None


_BACKGROUND_RESPONSE_JOB = _JobSpec(
    "responses",
    False,
    "response",
    "submit",
    1,
)
_ASYNC_BACKGROUND_RESPONSE_JOB = _JobSpec(
    "aresponses",
    True,
    "response",
    "submit",
    1,
)


def _job_wrapper(spec: _JobSpec) -> Any:
    return _async_job_wrapper(spec) if spec.async_call else _sync_job_wrapper(spec)


def _sync_background_response(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    operation_spec: _OperationSpec,
) -> Any:
    return _sync_job_call(
        wrapped,
        args,
        kwargs,
        _BACKGROUND_RESPONSE_JOB,
        operation_spec,
    )


def _async_background_response(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    operation_spec: _OperationSpec,
) -> Any:
    return _async_job_call(
        wrapped,
        args,
        kwargs,
        _ASYNC_BACKGROUND_RESPONSE_JOB,
        operation_spec,
    )


def _sync_job_wrapper(spec: _JobSpec) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        return _sync_job_call(wrapped, args, kwargs, spec)

    return wrapper


def _async_job_wrapper(spec: _JobSpec) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        return _async_job_call(wrapped, args, kwargs, spec)

    return wrapper


def _sync_job_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    spec: _JobSpec,
    operation_spec: _OperationSpec | None = None,
) -> Any:
    session = _new_job_session(spec, args, kwargs) if spec.phase == "submit" else None
    try:
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except BaseException as exc:
            if session is not None:
                session.fail(exc)
            raise
        if isawaitable(response):

            async def await_response() -> Any:
                try:
                    resolved = await response
                except BaseException as exc:
                    if session is not None:
                        session.fail(exc)
                    raise
                _finish_job_call(spec, args, kwargs, resolved, session, operation_spec)
                return resolved

            if session is not None:
                session.release_context()
            return await_response()
        _finish_job_call(spec, args, kwargs, response, session, operation_spec)
        return response
    finally:
        if session is not None:
            session.release_context()


def _async_job_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    spec: _JobSpec,
    operation_spec: _OperationSpec | None = None,
) -> Any:
    async def invoke() -> Any:
        session = _new_job_session(spec, args, kwargs) if spec.phase == "submit" else None
        try:
            try:
                with suppress_network_event():
                    response = await wrapped(*args, **kwargs)
            except BaseException as exc:
                if session is not None:
                    session.fail(exc)
                raise
            _finish_job_call(spec, args, kwargs, response, session, operation_spec)
            return response
        finally:
            if session is not None:
                session.release_context()

    return invoke()


def _job_provider(
    spec: _JobSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    response: Any = None,
) -> str:
    explicit = kwargs.get("custom_llm_provider")
    if isinstance(explicit, str) and explicit:
        return _canonical_provider(explicit)
    request_model = _job_request_model(spec, args, kwargs)
    resolved = _resolve_provider(response=response, request_model=request_model)
    return "openai" if resolved == "unknown" else resolved


def _job_request_model(
    spec: _JobSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> str | None:
    model = _call_argument(args, kwargs, "model", spec.model_position)
    return model if isinstance(model, str) and model else None


def _job_resource_id(
    spec: _JobSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    response: Any = None,
) -> str:
    model = _response_model(response) or _job_request_model(spec, args, kwargs)
    if model is not None:
        provider = _job_provider(spec, args, kwargs, response)
        return _canonical_model(model, provider, model)[:256]
    if spec.kind == "batch":
        endpoint = _call_argument(args, kwargs, "endpoint", 1)
        if isinstance(endpoint, str) and endpoint:
            return f"batch:{endpoint}"[:256]
        return "batch:unknown"
    return {
        "response": "response:unknown",
        "video": "video:unknown",
        "fine_tuning": "fine-tuning:unknown",
    }.get(spec.kind, f"{spec.kind}:unknown")


def _job_service(kind: str) -> str:
    return {
        "response": "litellm.responses",
        "video": "litellm.videos",
        "batch": "litellm.batches",
        "fine_tuning": "litellm.fine_tuning",
    }[kind]


def _job_component(kind: str) -> str:
    return {
        "response": "llm",
        "batch": "llm",
        "video": "video",
        "fine_tuning": "fine_tuning",
    }[kind]


def _job_dimensions(spec: _JobSpec, kwargs: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    dimensions: list[tuple[str, str]] = [("gateway", "litellm")]
    if spec.kind == "video":
        for field, key in (("seconds", "requested_video_seconds"), ("size", "video_size")):
            value = kwargs.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                dimensions.append((key, str(value)[:256]))
    elif spec.kind == "batch":
        for field, key in (
            ("endpoint", "batch_endpoint"),
            ("completion_window", "batch_completion_window"),
        ):
            value = kwargs.get(field)
            if isinstance(value, str) and value:
                dimensions.append((key, value[:256]))
    elif spec.kind == "response":
        value = kwargs.get("service_tier")
        if isinstance(value, str) and value:
            dimensions.append(("service_tier", value[:256]))
    return tuple(sorted(dimensions))


def _new_job_session(
    spec: _JobSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> ProviderJobSession:
    return ProviderJobSession(
        tracker=_active_tracker,
        task_type=f"litellm.{spec.name}",
        provider=_job_provider(spec, args, kwargs),
        service=_job_service(spec.kind),
        operation=f"litellm.{spec.name}",
        component=_job_component(spec.kind),
        event_type="llm_call" if spec.kind in {"response", "batch"} else "external_cost",
        resource_type="model",
        resource_id=_job_resource_id(spec, args, kwargs),
        billing_dimensions=_job_dimensions(spec, kwargs),
    )


def _job_record_id(
    spec: _JobSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    response: Any,
) -> str | None:
    response_id = _provider_record_id(response)
    if response_id is not None:
        return response_id
    if spec.record_id_name is None:
        return None
    candidate = _call_argument(args, kwargs, spec.record_id_name, 0)
    return candidate[:256] if isinstance(candidate, str) and candidate else None


def _finish_job_call(
    spec: _JobSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    response: Any,
    session: ProviderJobSession | None,
    operation_spec: _OperationSpec | None,
) -> None:
    record_id = _job_record_id(spec, args, kwargs, response)
    if record_id is None:
        _log.debug("dexcost: LiteLLM %s response has no durable job ID", spec.name)
        return
    provider = _job_provider(spec, args, kwargs, response)
    resource_id = _job_resource_id(spec, args, kwargs, response)
    status = _job_status(spec.kind, response, submission=spec.phase == "submit")
    measurement = (
        _job_measurement(spec, args, kwargs, response, provider, operation_spec)
        if status not in {"submitted", "running"}
        else None
    )
    if status == "succeeded" and (measurement is None or not measurement.usage_lines):
        status = "unknown"
    if session is not None:
        session.provider = provider
        session.resource_id = resource_id
        try:
            session.submit(record_id, status=status, measurement=measurement)
        except Exception:
            _log.debug("dexcost: failed to submit LiteLLM provider job", exc_info=True)
        return
    try:
        reconcile_provider_job(
            tracker=_active_tracker,
            provider=provider,
            service=_job_service(spec.kind),
            provider_record_id=record_id,
            status=status,
            measurement=measurement,
        )
    except Exception:
        _log.debug("dexcost: failed to reconcile LiteLLM provider job", exc_info=True)


def _job_status(kind: str, response: Any, *, submission: bool) -> ProviderJobStatus:
    raw = _field(response, "status")
    normalized = raw.strip().lower().replace("-", "_") if isinstance(raw, str) else ""
    if normalized in {"completed", "succeeded", "success"}:
        return "succeeded"
    if normalized in {"failed", "error", "expired"}:
        return "failed"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized == "incomplete":
        return "unknown"
    return "submitted" if submission else "running"


def _job_measurement(
    spec: _JobSpec,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    response: Any,
    provider: str,
    operation_spec: _OperationSpec | None,
) -> OperationMeasurement | None:
    if spec.kind == "response":
        response_spec = operation_spec or _OperationSpec(
            spec.name,
            spec.async_call,
            "llm",
            "aresponses" if spec.async_call else "responses",
            1,
        )
        measurement = _operation_measurement(response_spec, args, kwargs, response, provider)
        return measurement if measurement.usage_lines else None

    request_model = _job_request_model(spec, args, kwargs)
    response_model = _response_model(response) or request_model or _job_resource_id(
        spec, args, kwargs, response
    )
    model = _canonical_model(response_model, provider, request_model)
    hidden = _response_hidden_params(response)
    usage = _field(response, "usage")
    provider_cost = _provider_response_cost(provider, usage, hidden)
    gateway_cost = _try_hidden_litellm_cost(hidden) if provider_cost is None else None
    pricing_usage: dict[str, Decimal | int | str] = {}
    lines: list[ProviderUsageLine] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None

    if spec.kind == "video":
        seconds = _non_negative_decimal(_field(response, "seconds"))
        if seconds is None:
            seconds = _non_negative_decimal(_field(usage, "duration_seconds"))
        if seconds is not None and seconds > 0:
            pricing_usage.update({"output_video_count": 1, "output_video_seconds": seconds})
            lines.extend(
                (
                    ProviderUsageLine("output_video_count", 1, "Videos"),
                    ProviderUsageLine("output_video_seconds", seconds, "Seconds"),
                )
            )
            if provider_cost is None and gateway_cost is None:
                video_spec = _OperationSpec(
                    spec.name,
                    spec.async_call,
                    "video",
                    "acreate_video" if spec.async_call else "create_video",
                    spec.model_position,
                )
                gateway_cost = _try_litellm_operation_cost(
                    video_spec,
                    response,
                    request_model,
                    kwargs,
                )
    elif spec.kind == "batch":
        _, _, raw_input, raw_output, raw_cached = _usage_measurement(usage)
        input_tokens = raw_input or None
        output_tokens = raw_output or None
        cached_tokens = raw_cached or None
        for metric, quantity in (
            ("batch_input_tokens", raw_input),
            ("batch_output_tokens", raw_output),
            ("batch_cache_read_input_tokens", raw_cached),
        ):
            if quantity > 0:
                lines.append(ProviderUsageLine(metric, quantity, "Tokens"))
        counts = _field(response, "request_counts")
        for metric, field in (
            ("batch_request_count", "total"),
            ("batch_successful_request_count", "completed"),
            ("batch_failed_request_count", "failed"),
        ):
            count_quantity = _non_negative_int(_field(counts, field))
            if count_quantity is not None and count_quantity > 0:
                lines.append(ProviderUsageLine(metric, count_quantity, "Requests"))
    else:
        trained_tokens = _non_negative_int(_field(response, "trained_tokens"))
        if trained_tokens is not None and trained_tokens > 0:
            lines.append(
                ProviderUsageLine("training_billable_tokens", trained_tokens, "Tokens")
            )

    if not lines:
        return None
    pricing_version = _litellm_pricing_version() if gateway_cost is not None else None
    return OperationMeasurement(
        pricing_usage=pricing_usage,
        usage_lines=tuple(lines),
        provider_record_id=_provider_record_id(response),
        provider_cost_usd=provider_cost,
        computed_cost_usd=gateway_cost,
        computed_cost_source="litellm" if gateway_cost is not None else None,
        computed_cost_confidence="computed" if gateway_cost is not None else None,
        computed_pricing_version=pricing_version,
        response_model=model,
        model_candidates=(model,),
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
        task_cached_tokens=cached_tokens,
    )


def _record_call_failure(
    exc: BaseException,
    start_time: float,
    kwargs: dict[str, Any],
    auto_task_obj: Any = None,
    task: Any = None,
    capability: Any = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event | None:
    """Record a raised LiteLLM call as a failed operation. Never raises.

    No response exists, so the provider is resolved from the requested model
    string prefix alone (``"openai/gpt-4o"`` -> ``"openai"``).
    """
    try:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
    except Exception:  # pragma: no cover - defensive
        latency_ms = None
    model = requested_model(kwargs)
    try:
        provider = _resolve_provider(request_model=model)
    except Exception:  # pragma: no cover - defensive
        provider = "unknown"
    model = _canonical_model(model, provider, model)
    event = record_call_failure(
        tracker=_active_tracker,
        exc=exc,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        service_name="litellm",
        details={
            "attribution_component": "llm",
            "attribution_operation_name": "litellm.completion",
            "attribution_operation_status": "failed",
            "attribution_resource_type": "model",
            "attribution_resource_id": model or "unknown",
            "provider_usage_privacy": "quantities_only",
        },
        task=task,
        capability=capability,
        idempotency_key=idempotency_key,
    )
    finalize_failed_auto_task(_active_tracker, auto_task_obj, event)
    return event


def _sync_completion_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``litellm.completion``."""
    task = get_current_task()
    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("litellm.completion")
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
                )
                raise
            return _SyncStreamWrapper(
                raw_stream,
                start_time,
                kwargs.get("model"),
                task,
                auto_task_obj,
                capability,
                idempotency_key,
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
            )
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(
                response,
                latency_ms,
                task=task or auto_task_obj,
                request_model=kwargs.get("model"),
                capability=capability,
                idempotency_key=idempotency_key,
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


def _async_completion_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``litellm.acompletion``."""
    task = get_current_task()
    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("litellm.completion")

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
    )


async def _async_non_stream_handler(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    start_time: float,
    auto_task_obj: Any = None,
    auto_token: Any = None,
    task: Any = None,
    capability: Any = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Any:
    """Await the async acompletion call and record the response."""
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
            )
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(
                response,
                latency_ms,
                task=task or auto_task_obj,
                request_model=kwargs.get("model"),
                capability=capability,
                idempotency_key=idempotency_key,
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
    capability: Any = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Any:
    """Wrap async streaming to capture usage from the final chunk."""
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
            )
            raise
        return _AsyncStreamWrapper(
            raw_stream,
            start_time,
            kwargs.get("model"),
            task,
            auto_task_obj,
            capability,
            idempotency_key,
        )
    finally:
        if auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Stream wrappers
# ---------------------------------------------------------------------------


class _SyncStreamWrapper(Iterator[Any]):
    """Wraps a sync LiteLLM stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        request_model: Any = None,
        task: Any = None,
        auto_task_obj: Any = None,
        capability: Any = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._request_model = request_model
        self._model: str | None = None
        self._usage: Any | None = None
        self._hidden_params: dict[str, Any] | None = None
        self._provider_record_id: str | None = None
        self._finalized: bool = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._capability = capability
        self._idempotency_key = idempotency_key

    def __iter__(self) -> _SyncStreamWrapper:
        return self

    def __next__(self) -> Any:
        try:
            chunk = next(self._stream)
            self._process_chunk(chunk)
            return chunk
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
        provider = _resolve_provider(
            hidden_params=self._hidden_params,
            request_model=self._request_model,
        )
        model = _canonical_model(self._model, provider, self._request_model)
        _, _, input_tokens, output_tokens, _ = _usage_measurement(self._usage)
        details: dict[str, Any] = {
            "attribution_component": "llm",
            "attribution_operation_name": "litellm.completion",
            "attribution_operation_status": "failed",
            "attribution_resource_type": "model",
            "attribution_resource_id": model,
            "provider_usage_privacy": "quantities_only",
        }
        if self._provider_record_id is not None:
            details["provider_record_id"] = self._provider_record_id
        record_stream_failure(
            tracker=_active_tracker,
            exc=exc,
            start_time=self._start_time,
            provider=provider,
            model=model,
            task=self._task,
            auto_task_obj=self._auto_task_obj,
            service_name="litellm",
            input_tokens=input_tokens if self._usage is not None else None,
            output_tokens=output_tokens if self._usage is not None else None,
            details=details,
            capability=self._capability,
            idempotency_key=self._idempotency_key,
        )

    def _process_chunk(self, chunk: Any) -> None:
        """Extract model and usage info from streaming chunks."""
        if hasattr(chunk, "model") and chunk.model:
            self._model = chunk.model
        if hasattr(chunk, "usage") and chunk.usage is not None:
            self._usage = chunk.usage
        if hasattr(chunk, "_hidden_params") and chunk._hidden_params:
            self._hidden_params = chunk._hidden_params
        provider_record_id = _provider_record_id(chunk)
        if provider_record_id is not None:
            self._provider_record_id = provider_record_id

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                provider_record_id=self._provider_record_id,
                request_model=self._request_model,
                latency_ms=latency_ms,
                task=self._task or self._auto_task_obj,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            self._finalize_auto_task(event, "success")
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def _cancel(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                provider_record_id=self._provider_record_id,
                request_model=self._request_model,
                latency_ms=latency_ms,
                task=self._task or self._auto_task_obj,
                operation_status="cancelled",
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            self._finalize_auto_task(event, "failed")
        except Exception:
            _log.debug("dexcost: failed to record stream cancellation", exc_info=True)

    def _finalize_auto_task(self, event: Event | None, status: str) -> None:
        if self._auto_task_obj is None or event is None:
            return
        finalize_auto_task(self._auto_task_obj, event, status=status)
        if _active_tracker is not None:
            _active_tracker._storage.insert_task(self._auto_task_obj)

    # Forward close/context-manager to the underlying stream
    def close(self) -> None:
        try:
            if hasattr(self._stream, "close"):
                self._stream.close()
        except BaseException as exc:
            self._record_failure(exc)
            raise
        self._cancel()

    def __enter__(self) -> _SyncStreamWrapper:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        try:
            result = self._stream.__exit__(*args) if hasattr(self._stream, "__exit__") else None
        except BaseException as exc:
            self._record_failure(exc)
            raise
        self._cancel()
        return result


class _AsyncStreamWrapper:
    """Wraps an async LiteLLM stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        request_model: Any = None,
        task: Any = None,
        auto_task_obj: Any = None,
        capability: Any = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._request_model = request_model
        self._model: str | None = None
        self._usage: Any | None = None
        self._hidden_params: dict[str, Any] | None = None
        self._provider_record_id: str | None = None
        self._finalized: bool = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._capability = capability
        self._idempotency_key = idempotency_key

    def __aiter__(self) -> _AsyncStreamWrapper:
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._stream.__anext__()
            self._process_chunk(chunk)
            return chunk
        except StopAsyncIteration:
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
        provider = _resolve_provider(
            hidden_params=self._hidden_params,
            request_model=self._request_model,
        )
        model = _canonical_model(self._model, provider, self._request_model)
        _, _, input_tokens, output_tokens, _ = _usage_measurement(self._usage)
        details: dict[str, Any] = {
            "attribution_component": "llm",
            "attribution_operation_name": "litellm.completion",
            "attribution_operation_status": "failed",
            "attribution_resource_type": "model",
            "attribution_resource_id": model,
            "provider_usage_privacy": "quantities_only",
        }
        if self._provider_record_id is not None:
            details["provider_record_id"] = self._provider_record_id
        record_stream_failure(
            tracker=_active_tracker,
            exc=exc,
            start_time=self._start_time,
            provider=provider,
            model=model,
            task=self._task,
            auto_task_obj=self._auto_task_obj,
            service_name="litellm",
            input_tokens=input_tokens if self._usage is not None else None,
            output_tokens=output_tokens if self._usage is not None else None,
            details=details,
            capability=self._capability,
            idempotency_key=self._idempotency_key,
        )

    def _process_chunk(self, chunk: Any) -> None:
        """Extract model and usage info from streaming chunks."""
        if hasattr(chunk, "model") and chunk.model:
            self._model = chunk.model
        if hasattr(chunk, "usage") and chunk.usage is not None:
            self._usage = chunk.usage
        if hasattr(chunk, "_hidden_params") and chunk._hidden_params:
            self._hidden_params = chunk._hidden_params
        provider_record_id = _provider_record_id(chunk)
        if provider_record_id is not None:
            self._provider_record_id = provider_record_id

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                provider_record_id=self._provider_record_id,
                request_model=self._request_model,
                latency_ms=latency_ms,
                task=self._task or self._auto_task_obj,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            self._finalize_auto_task(event, "success")
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def _cancel(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                provider_record_id=self._provider_record_id,
                request_model=self._request_model,
                latency_ms=latency_ms,
                task=self._task or self._auto_task_obj,
                operation_status="cancelled",
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            self._finalize_auto_task(event, "failed")
        except Exception:
            _log.debug("dexcost: failed to record stream cancellation", exc_info=True)

    def _finalize_auto_task(self, event: Event | None, status: str) -> None:
        if self._auto_task_obj is None or event is None:
            return
        finalize_auto_task(self._auto_task_obj, event, status=status)
        if _active_tracker is not None:
            _active_tracker._storage.insert_task(self._auto_task_obj)

    async def aclose(self) -> None:
        try:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()
        except BaseException as exc:
            self._record_failure(exc)
            raise
        self._cancel()

    async def __aenter__(self) -> _AsyncStreamWrapper:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        try:
            result = (
                await self._stream.__aexit__(*args) if hasattr(self._stream, "__aexit__") else None
            )
        except BaseException as exc:
            self._record_failure(exc)
            raise
        self._cancel()
        return result


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def _resolve_provider(
    response: Any = None,
    hidden_params: dict[str, Any] | None = None,
    request_model: Any = None,
) -> str:
    """Resolve the actual LLM provider from LiteLLM response data.

    Resolution order:
    1. ``_hidden_params["custom_llm_provider"]`` from the response
    2. Prefix of the model string (e.g. ``"openai/gpt-4"`` -> ``"openai"``)
    3. ``"unknown"``
    """
    hp = _response_hidden_params(response, hidden_params)

    if hp and isinstance(hp, dict):
        provider = hp.get("custom_llm_provider")
        if provider and isinstance(provider, str):
            return _canonical_provider(provider)

    # Try extracting from model string prefix (e.g. "openai/gpt-4")
    model_str: str | None = None
    if response is not None:
        raw = _field(response, "model")
        if raw is not None:
            model_str = str(raw)
    if not model_str and request_model is not None:
        model_str = str(request_model)

    if model_str and "/" in model_str:
        prefix = model_str.split("/", 1)[0]
        if prefix:
            return _canonical_provider(prefix)

    return "unknown"


def _field(value: Any, key: str) -> Any:
    """Read one SDK field without materialising dynamic mock attributes."""
    if isinstance(value, Mapping):
        return value.get(key)
    if value is None:
        return None
    model_extra = getattr(value, "model_extra", None)
    if isinstance(model_extra, Mapping) and key in model_extra:
        return model_extra.get(key)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        if key in attributes:
            return attributes.get(key)
        if isinstance(getattr(type(value), "model_fields", None), Mapping):
            return getattr(value, key, None)
        return None
    return getattr(value, key, None)


def _response_hidden_params(
    response: Any,
    explicit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(explicit, dict):
        return explicit
    candidate = _field(response, "_hidden_params")
    return candidate if isinstance(candidate, dict) else {}


def _provider_record_id(response: Any) -> str | None:
    """Return LiteLLM's bounded provider/gateway response identity."""
    for key in ("id", "response_id", "generation_id"):
        candidate = _field(response, key)
        if isinstance(candidate, str) and candidate:
            return candidate[:256]
    return None


def _canonical_provider(provider: str) -> str:
    normalized = provider.strip().lower().replace("-", "_")
    aliases = {
        "open_router": "openrouter",
        "openrouter_ai": "openrouter",
        "azure": "azure_openai",
        "azure_text": "azure_openai",
        "azure_openai": "azure_openai",
        "google_ai_studio": "google",
        "gemini": "google",
        "palm": "google",
        "vertex": "google",
        "vertex_ai": "google",
        "aws_bedrock": "bedrock",
        "bedrock_converse": "bedrock",
        "hugging_face": "huggingface",
        "huggingface_hub": "huggingface",
        "together_ai": "together",
        "fal": "fal_ai",
        "perplexity_ai": "perplexity",
    }
    return aliases.get(normalized, normalized) or "unknown"


def _canonical_model(model: Any, provider: str, request_model: Any = None) -> str:
    selected = model if isinstance(model, str) and model.strip() else request_model
    name = selected.strip() if isinstance(selected, str) and selected.strip() else "unknown"
    request_name = request_model.strip() if isinstance(request_model, str) else ""
    if provider == "together":
        # Preserve Together's provider-published API model ID for the
        # authoritative server catalog. LiteLLM's routing prefixes identify
        # the gateway, not a distinct billable model.
        for routed_prefix in ("together_ai/", "together/"):
            if name.startswith(routed_prefix):
                return name[len(routed_prefix) :]
        return name
    prefix = {
        "openrouter": "openrouter",
        "azure_openai": "azure",
        "azure_ai": "azure_ai",
        "bedrock": "bedrock",
        "fal_ai": "fal_ai",
        "groq": "groq",
        "huggingface": "huggingface",
        "mistral": "mistral",
        "ollama": "ollama",
        "perplexity": "perplexity",
    }.get(provider)
    if provider == "google":
        prefix = "vertex_ai" if request_name.startswith("vertex_ai/") else "gemini"
    if (
        prefix is None
        and provider not in {"openai", "anthropic", "cohere"}
        and "/" in request_name
    ):
        request_prefix = request_name.split("/", 1)[0]
        if _canonical_provider(request_prefix) == provider:
            prefix = request_prefix
    if prefix is not None and not name.startswith(f"{prefix}/"):
        return f"{prefix}/{name}"
    return name


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _non_negative_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _provider_response_cost(
    provider: str,
    usage: Any,
    hidden_params: dict[str, Any],
) -> Decimal | None:
    """Return provider-authoritative USD cost, never a LiteLLM estimate."""
    if provider == "perplexity":
        return _non_negative_decimal(_field(_field(usage, "cost"), "total_cost"))
    if provider != "openrouter":
        return None
    direct = _non_negative_decimal(_field(usage, "cost"))
    if direct is not None:
        return direct
    headers = hidden_params.get("additional_headers")
    if isinstance(headers, Mapping):
        return _non_negative_decimal(headers.get("llm_provider-x-litellm-response-cost"))
    return None


def _provider_upstream_cost(usage: Any) -> Decimal | None:
    cost_details = _field(usage, "cost_details")
    return _non_negative_decimal(
        _field(cost_details, "upstream_inference_cost") or _field(cost_details, "upstream_cost")
    )


def _try_hidden_litellm_cost(hidden_params: dict[str, Any]) -> Decimal | None:
    return _non_negative_decimal(hidden_params.get("response_cost"))


def _usage_measurement(usage: Any) -> tuple[dict[str, int], list[dict[str, str]], int, int, int]:
    """Normalize LiteLLM usage into mutually exclusive billing buckets."""
    if usage is None:
        return {}, [], 0, 0, 0

    try:
        normalized = normalize_openai_usage(usage)
    except OpenAIUsageError:
        normalized = None

    pricing: dict[str, int] = {}
    input_total = (
        _non_negative_int(_field(usage, "prompt_tokens") or _field(usage, "input_tokens")) or 0
    )
    output_total = (
        _non_negative_int(_field(usage, "completion_tokens") or _field(usage, "output_tokens"))
        or 0
    )
    cached_total = 0
    if normalized is not None:
        input_total = normalized.total_input_tokens
        output_total = normalized.total_output_tokens
        cached_total = normalized.cache_read_input_tokens
        pricing.update(
            {
                "input_tokens": normalized.input_tokens,
                "cache_read_input_tokens": normalized.cache_read_input_tokens,
                "cache_write_input_tokens": normalized.cache_write_input_tokens,
                "output_tokens": normalized.output_tokens,
                "reasoning_output_tokens": normalized.reasoning_output_tokens,
            }
        )
    else:
        pricing["input_tokens"] = input_total
        pricing["output_tokens"] = output_total
        for metric, field_name in (
            ("cache_read_input_tokens", "cache_read_input_tokens"),
            ("cache_write_input_tokens", "cache_creation_input_tokens"),
        ):
            quantity = _non_negative_int(_field(usage, field_name)) or 0
            if quantity:
                pricing[metric] = quantity
                if metric == "cache_read_input_tokens":
                    cached_total = quantity
        output_details = _field(usage, "completion_tokens_details") or _field(
            usage, "output_tokens_details"
        )
        reasoning = _non_negative_int(_field(output_details, "reasoning_tokens")) or 0
        if reasoning:
            pricing["reasoning_output_tokens"] = reasoning

    server_tools = _field(usage, "server_tool_use") or _field(usage, "server_tool_use_details")
    for metric, field_name, _unit in (
        ("server_tool_calls_requested", "tool_calls_requested", "Calls"),
        ("server_tool_calls_executed", "tool_calls_executed", "Calls"),
        ("web_search_requests", "web_search_requests", "Requests"),
    ):
        quantity = _non_negative_int(_field(server_tools, field_name)) or 0
        if quantity:
            pricing[metric] = quantity

    units = {
        "server_tool_calls_requested": "Calls",
        "server_tool_calls_executed": "Calls",
        "web_search_requests": "Requests",
    }
    lines = [
        {
            "metric": metric,
            "quantity": str(quantity),
            "unit": units.get(metric, "Tokens"),
        }
        for metric, quantity in pricing.items()
        if quantity > 0
    ]
    return pricing, lines, input_total, output_total, cached_total


# ---------------------------------------------------------------------------
# LiteLLM cost calculation
# ---------------------------------------------------------------------------


def _try_litellm_cost(response: Any) -> Decimal | None:
    """Attempt to use LiteLLM's own ``completion_cost`` for cost calculation.

    Returns the cost as a :class:`Decimal`, or ``None`` if LiteLLM cost
    calculation is unavailable or fails.
    """
    try:
        import litellm

        cost = litellm.completion_cost(completion_response=response)
        parsed = _non_negative_decimal(cost)
        if parsed is not None and parsed > 0:
            return parsed
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _record_from_response(
    response: Any,
    latency_ms: int,
    *,
    task: Any = None,
    request_model: Any = None,
    capability: Any = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event | None:
    """Extract fields from a LiteLLM ModelResponse and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    resolved_task = task or get_current_task()
    if resolved_task is None:
        return None

    response_model = _field(response, "model")
    usage = _field(response, "usage")
    hidden_params = _response_hidden_params(response)
    provider = _resolve_provider(response)
    model = _canonical_model(response_model, provider, request_model)

    return _insert_llm_event(
        tracker=tracker,
        task_id=resolved_task.task_id,
        model=model,
        provider=provider,
        latency_ms=latency_ms,
        usage=usage,
        hidden_params=hidden_params,
        response=response,
        provider_record_id=_provider_record_id(response),
        capability=capability,
        idempotency_key=idempotency_key,
    )


def _record_from_stream_usage(
    *,
    model: str | None,
    usage: Any | None,
    hidden_params: dict[str, Any] | None,
    provider_record_id: str | None,
    request_model: Any,
    latency_ms: int,
    task: Any = None,
    operation_status: str = "succeeded",
    capability: Any = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event | None:
    """Record an event from accumulated stream data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    resolved_task = task or get_current_task()
    if resolved_task is None:
        return None

    provider = _resolve_provider(hidden_params=hidden_params, request_model=request_model)
    resolved_model = _canonical_model(model, provider, request_model)

    return _insert_llm_event(
        tracker=tracker,
        task_id=resolved_task.task_id,
        model=resolved_model,
        provider=provider,
        latency_ms=latency_ms,
        usage=usage,
        hidden_params=hidden_params or {},
        response=None,
        provider_record_id=provider_record_id,
        operation_status=operation_status,
        capability=capability,
        idempotency_key=idempotency_key,
    )


def _insert_llm_event(
    *,
    tracker: Any,
    task_id: Any,
    model: str,
    provider: str,
    latency_ms: int,
    usage: Any,
    hidden_params: dict[str, Any],
    response: Any | None,
    provider_record_id: str | None = None,
    operation_status: str = "succeeded",
    capability: Any = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event:
    """Create and persist an llm_call Event.

    Tries LiteLLM's own ``completion_cost`` first; falls back to
    the dexcost pricing engine.
    """
    pricing_usage, usage_lines, input_tokens, output_tokens, cached_tokens = _usage_measurement(
        usage
    )
    provider_cost = _provider_response_cost(provider, usage, hidden_params)
    upstream_cost = _provider_upstream_cost(usage)
    litellm_cost = (
        _try_litellm_cost(response)
        if response is not None
        else _try_hidden_litellm_cost(hidden_params)
    )
    cost_result = tracker._pricing.get_metered_cost(
        model,
        pricing_usage,
        model_candidates=(model,),
    )
    if provider_cost is not None:
        cost_usd = provider_cost
        cost_confidence = "exact"
        pricing_source = "provider_response"
        pricing_version: str | None = None
    elif litellm_cost is not None:
        cost_usd = litellm_cost
        cost_confidence = "computed"
        pricing_source = "litellm"
        pricing_version = None
    elif usage is not None:
        cost_usd = cost_result.cost_usd
        cost_confidence = cost_result.cost_confidence
        pricing_source = cost_result.pricing_source
        pricing_version = cost_result.pricing_version
    else:
        cost_usd = Decimal("0")
        cost_confidence = "unknown"
        pricing_source = "unknown"
        pricing_version = None

    details: dict[str, Any] = {
        "attribution_component": "llm",
        "attribution_operation_name": "litellm.completion",
        "attribution_operation_status": operation_status,
        "attribution_resource_type": "model",
        "attribution_resource_id": model,
        "attribution_usage_lines": usage_lines
        or [{"metric": "request_count", "quantity": "1", "unit": "Requests"}],
        "attribution_dimensions": [
            {"key": "gateway", "value": {"type": "string", "value": "litellm"}}
        ],
        "provider_usage_privacy": "quantities_only",
    }
    if provider_cost is not None:
        details["provider_reported_cost_usd"] = canonical_decimal(provider_cost)
    if provider_record_id is not None:
        details["provider_record_id"] = provider_record_id
    if upstream_cost is not None:
        details["provider_upstream_cost_usd"] = canonical_decimal(upstream_cost)
    if pricing_source == "litellm":
        details["gateway_calculated_cost_usd"] = canonical_decimal(cost_usd)
    reasoning_tokens = pricing_usage.get("reasoning_output_tokens", 0)
    cache_write_tokens = pricing_usage.get("cache_write_input_tokens", 0)
    if reasoning_tokens:
        details["reasoning_output_tokens"] = reasoning_tokens
    if cache_write_tokens:
        details["cache_creation_input_tokens"] = cache_write_tokens
    if cost_result.resolved_model is not None:
        details["pricing_resolved_model"] = cost_result.resolved_model
    if cost_result.unpriced_dimensions:
        details["pricing_unpriced_dimensions"] = list(cost_result.unpriced_dimensions)

    event = Event(
        task_id=task_id,
        event_type="llm_call",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        service_name="litellm",
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    apply_event_capability(event, capability)
    apply_event_idempotency(event, idempotency_key)
    tracker._storage.insert_event(event)
    return event
