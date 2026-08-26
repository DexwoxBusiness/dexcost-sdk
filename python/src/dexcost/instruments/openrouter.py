"""Privacy-safe instrumentation for the official OpenRouter Python SDK.

The official SDK exposes synchronous and asynchronous methods from the same
resource classes.  This adapter covers every current inference surface while
leaving catalog, account, and other read-only administration calls untouched.
It records only usage quantities and bounded provider identifiers; prompts,
documents, audio, images, generated media, and error messages are never kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from decimal import Decimal, InvalidOperation
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
from dexcost.instruments.openai_usage import OpenAIUsageError, normalize_openai_usage
from dexcost.models.provider_job import ProviderJobStatus
from dexcost.provider_jobs import ProviderJobSession, reconcile_provider_job

_active_tracker: Any = None
_patched = False
_originals: dict[str, tuple[Any, str, Any]] = {}

_Kind = Literal[
    "chat",
    "responses",
    "embeddings",
    "images",
    "stt",
    "tts",
    "rerank",
]

_METHODS: tuple[tuple[str, str, str, _Kind, bool], ...] = (
    ("openrouter.chat", "Chat", "send", "chat", False),
    ("openrouter.chat", "Chat", "send_async", "chat", True),
    ("openrouter.responses", "Responses", "send", "responses", False),
    ("openrouter.responses", "Responses", "send_async", "responses", True),
    ("openrouter.embeddings", "Embeddings", "generate", "embeddings", False),
    ("openrouter.embeddings", "Embeddings", "generate_async", "embeddings", True),
    ("openrouter.images", "Images", "generate", "images", False),
    ("openrouter.images", "Images", "generate_async", "images", True),
    ("openrouter.stt", "STT", "create_transcription", "stt", False),
    ("openrouter.stt", "STT", "create_transcription_async", "stt", True),
    (
        "openrouter.stt",
        "STT",
        "create_transcription_multipart",
        "stt",
        False,
    ),
    (
        "openrouter.stt",
        "STT",
        "create_transcription_multipart_async",
        "stt",
        True,
    ),
    ("openrouter.tts", "TTS", "create_speech", "tts", False),
    ("openrouter.tts", "TTS", "create_speech_async", "tts", True),
    ("openrouter.rerank", "Rerank", "rerank", "rerank", False),
    ("openrouter.rerank", "Rerank", "rerank_async", "rerank", True),
)


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return attributes.get(name, default)
    return getattr(value, name, default)


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _non_negative_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _positive_line(
    metric: str,
    quantity: int | Decimal | None,
    unit: str,
) -> ProviderUsageLine | None:
    if quantity is None or quantity <= 0:
        return None
    return ProviderUsageLine(metric, quantity, unit)


def _canonical_model(model: Any) -> str:
    if not isinstance(model, str) or not model:
        return "openrouter/unknown"
    return model if model.startswith("openrouter/") else f"openrouter/{model}"


def _requested_model(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    model = kwargs.get("model")
    if not isinstance(model, str):
        # Current generated SDK methods are keyword-only, but retaining this
        # fallback keeps the adapter compatible with hand-written test doubles.
        for value in args:
            if isinstance(value, str) and "/" in value:
                model = value
                break
    return _canonical_model(model)


def _record_id(response: Any) -> str | None:
    value = _value(response, "id")
    if not isinstance(value, str):
        value = _value(response, "generation_id")
    if isinstance(value, str) and value:
        return value[:256]
    headers = _value(response, "headers")
    if isinstance(headers, Mapping):
        for name in ("x-generation-id", "X-Generation-Id"):
            candidate = headers.get(name)
            if isinstance(candidate, str) and candidate:
                return candidate[:256]
    return None


def _upstream_provider(response: Any) -> str | None:
    direct = _value(response, "provider") or _value(response, "provider_name")
    if isinstance(direct, str) and direct:
        return direct[:256]
    metadata = _value(response, "openrouter_metadata")
    attempts = _value(metadata, "attempts")
    if isinstance(attempts, (list, tuple)):
        for attempt in reversed(attempts):
            status = _non_negative_int(_value(attempt, "status"))
            provider = _value(attempt, "provider")
            if status is not None and 200 <= status < 300 and isinstance(provider, str):
                return provider[:256]
    return None


def _dimensions(response: Any, usage: Any) -> tuple[tuple[str, str], ...]:
    dimensions: list[tuple[str, str]] = []
    provider = _upstream_provider(response)
    if provider:
        dimensions.append(("upstream_provider", provider))
    is_byok = _value(usage, "is_byok")
    if not isinstance(is_byok, bool):
        is_byok = _value(_value(response, "openrouter_metadata"), "is_byok")
    if isinstance(is_byok, bool):
        dimensions.append(("is_byok", "true" if is_byok else "false"))
    service_tier = _value(response, "service_tier")
    if isinstance(service_tier, str) and service_tier:
        dimensions.append(("service_tier", service_tier[:256]))
    return tuple(dimensions)


def _costs(usage: Any) -> tuple[Decimal | None, Decimal | None]:
    cost = _non_negative_decimal(_value(usage, "cost"))
    cost_details = _value(usage, "cost_details")
    upstream = _non_negative_decimal(_value(cost_details, "upstream_inference_cost"))
    return cost, upstream


def _server_tool_lines(usage: Any) -> tuple[ProviderUsageLine, ...]:
    details = _value(usage, "server_tool_use_details")
    if details is None:
        details = _value(usage, "server_tool_use")
    return tuple(
        line
        for line in (
            _positive_line(
                "server_tool_calls_requested",
                _non_negative_int(_value(details, "tool_calls_requested")),
                "Calls",
            ),
            _positive_line(
                "server_tool_calls_executed",
                _non_negative_int(_value(details, "tool_calls_executed")),
                "Calls",
            ),
            _positive_line(
                "web_search_requests",
                _non_negative_int(_value(details, "web_search_requests")),
                "Requests",
            ),
        )
        if line is not None
    )


def _token_measurement(
    response: Any,
    requested_model: str,
    *,
    output_images: int = 0,
) -> OperationMeasurement:
    usage = _value(response, "usage")
    response_model = _canonical_model(_value(response, "model") or requested_model)
    pricing: dict[str, int] = {}
    lines: list[ProviderUsageLine] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    try:
        normalized = normalize_openai_usage(usage)
    except OpenAIUsageError:
        normalized = None
    if normalized is not None:
        input_tokens = normalized.total_input_tokens
        output_tokens = normalized.total_output_tokens
        cached_tokens = normalized.cache_read_input_tokens
        for metric, quantity in (
            ("input_tokens", normalized.input_tokens),
            ("cache_read_input_tokens", normalized.cache_read_input_tokens),
            ("cache_write_input_tokens", normalized.cache_write_input_tokens),
            ("output_tokens", normalized.output_tokens),
            ("reasoning_output_tokens", normalized.reasoning_output_tokens),
        ):
            pricing[metric] = quantity
            line = _positive_line(metric, quantity, "Tokens")
            if line is not None:
                lines.append(line)
    else:
        input_tokens = _non_negative_int(
            _value(usage, "prompt_tokens") or _value(usage, "input_tokens")
        )
        output_tokens = _non_negative_int(
            _value(usage, "completion_tokens") or _value(usage, "output_tokens")
        )
        if input_tokens is not None:
            pricing["input_tokens"] = input_tokens
            line = _positive_line("input_tokens", input_tokens, "Tokens")
            if line is not None:
                lines.append(line)
        if output_tokens is not None:
            pricing["output_tokens"] = output_tokens
            line = _positive_line("output_tokens", output_tokens, "Tokens")
            if line is not None:
                lines.append(line)
    if output_images > 0:
        pricing["output_image_count"] = output_images
        lines.append(ProviderUsageLine("output_image_count", output_images, "Images"))
    lines.extend(_server_tool_lines(usage))
    cost, upstream = _costs(usage)
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        provider_record_id=_record_id(response),
        provider_cost_usd=cost,
        provider_upstream_cost_usd=upstream,
        response_model=response_model,
        model_candidates=(response_model,),
        billing_dimensions=_dimensions(response, usage),
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
        task_cached_tokens=cached_tokens,
    )


def _embedding_measurement(response: Any, model: str) -> OperationMeasurement:
    usage = _value(response, "usage")
    input_tokens = _non_negative_int(_value(usage, "prompt_tokens"))
    data = _value(response, "data")
    embedding_count = len(data) if isinstance(data, (list, tuple)) else None
    lines = tuple(
        line
        for line in (
            _positive_line("input_tokens", input_tokens, "Tokens"),
            _positive_line("embedding_count", embedding_count, "Embeddings"),
        )
        if line is not None
    )
    cost, upstream = _costs(usage)
    response_model = _canonical_model(_value(response, "model") or model)
    return OperationMeasurement(
        pricing_usage={} if input_tokens is None else {"input_tokens": input_tokens},
        usage_lines=lines,
        provider_record_id=_record_id(response),
        provider_cost_usd=cost,
        provider_upstream_cost_usd=upstream,
        response_model=response_model,
        model_candidates=(response_model,),
        billing_dimensions=_dimensions(response, usage),
        task_input_tokens=input_tokens,
    )


def _stt_measurement(response: Any, model: str) -> OperationMeasurement:
    usage = _value(response, "usage")
    input_tokens = _non_negative_int(_value(usage, "input_tokens"))
    output_tokens = _non_negative_int(_value(usage, "output_tokens"))
    seconds = _non_negative_decimal(_value(usage, "seconds"))
    lines = tuple(
        line
        for line in (
            _positive_line("input_tokens", input_tokens, "Tokens"),
            _positive_line("output_tokens", output_tokens, "Tokens"),
            _positive_line("audio_seconds", seconds, "Seconds"),
        )
        if line is not None
    )
    pricing: dict[str, int | Decimal] = {}
    if input_tokens is not None:
        pricing["input_tokens"] = input_tokens
    if output_tokens is not None:
        pricing["output_tokens"] = output_tokens
    if seconds is not None:
        pricing["input_audio_seconds"] = seconds
    cost, upstream = _costs(usage)
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=lines,
        provider_record_id=_record_id(response),
        provider_cost_usd=cost,
        provider_upstream_cost_usd=upstream,
        response_model=model,
        model_candidates=(model,),
        billing_dimensions=_dimensions(response, usage),
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
    )


def _tts_measurement(response: Any, model: str, kwargs: Mapping[str, Any]) -> OperationMeasurement:
    input_text = kwargs.get("input")
    characters = len(input_text) if isinstance(input_text, str) else 0
    return OperationMeasurement(
        pricing_usage={} if characters == 0 else {"characters": characters},
        usage_lines=(
            () if characters == 0 else (ProviderUsageLine("characters", characters, "Characters"),)
        ),
        provider_record_id=_record_id(response),
        response_model=model,
        model_candidates=(model,),
    )


def _rerank_measurement(response: Any, model: str) -> OperationMeasurement:
    usage = _value(response, "usage")
    total_tokens = _non_negative_int(_value(usage, "total_tokens"))
    search_units = _non_negative_int(_value(usage, "search_units"))
    results = _value(response, "results")
    result_count = len(results) if isinstance(results, (list, tuple)) else None
    lines = tuple(
        line
        for line in (
            _positive_line("total_tokens", total_tokens, "Tokens"),
            _positive_line("search_units", search_units, "Units"),
            _positive_line("result_count", result_count, "Results"),
        )
        if line is not None
    )
    cost = _non_negative_decimal(_value(usage, "cost"))
    response_model = _canonical_model(_value(response, "model") or model)
    provider = _value(response, "provider")
    dimensions = (
        (("upstream_provider", provider[:256]),) if isinstance(provider, str) and provider else ()
    )
    return OperationMeasurement(
        pricing_usage={} if total_tokens is None else {"input_tokens": total_tokens},
        usage_lines=lines,
        provider_record_id=_record_id(response),
        provider_cost_usd=cost,
        response_model=response_model,
        model_candidates=(response_model,),
        billing_dimensions=dimensions,
        task_input_tokens=total_tokens,
    )


def _image_count(response: Any) -> int:
    data = _value(response, "data")
    if isinstance(data, (list, tuple)):
        return len(data)
    return 1 if _value(response, "b64_json") not in (None, "") else 0


def _measurement(
    kind: _Kind,
    response: Any,
    model: str,
    kwargs: Mapping[str, Any],
) -> OperationMeasurement:
    if kind == "embeddings":
        return _embedding_measurement(response, model)
    if kind == "stt":
        return _stt_measurement(response, model)
    if kind == "tts":
        return _tts_measurement(response, model, kwargs)
    if kind == "rerank":
        return _rerank_measurement(response, model)
    return _token_measurement(
        response,
        model,
        output_images=_image_count(response) if kind == "images" else 0,
    )


def _status(response: Any, kind: _Kind, *, require_terminal: bool = False) -> OperationStatus:
    event_type = _value(response, "type")
    if event_type in {"error", "response.failed"} or _value(response, "error") not in (
        None,
        "",
    ):
        return "failed"
    if event_type == "response.incomplete":
        return "unknown"
    if event_type in {"response.completed", "image_generation.completed"}:
        return "succeeded"
    status = _value(response, "status")
    if status in {"failed", "error"}:
        return "failed"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"completed", "succeeded"}:
        return "succeeded"
    if kind == "chat" and _value(response, "usage") is not None:
        return "succeeded"
    if not require_terminal:
        return "succeeded"
    return "unknown"


class _StreamMeter:
    def __init__(self, kind: _Kind, model: str, kwargs: Mapping[str, Any]) -> None:
        self.kind = kind
        self.model = model
        self.kwargs = kwargs
        self.latest: Any = None
        self.terminal: Any = None

    def observe(self, chunk: Any) -> None:
        self.latest = chunk
        nested = _value(chunk, "response")
        candidate = nested if nested is not None else chunk
        if _value(candidate, "usage") is not None:
            self.terminal = candidate
        if _status(chunk, self.kind, require_terminal=True) in {
            "failed",
            "cancelled",
            "succeeded",
        }:
            self.terminal = candidate

    def measurement(self) -> OperationMeasurement:
        response = self.terminal or self.latest
        return _measurement(self.kind, response, self.model, self.kwargs)

    def completion_status(self) -> OperationStatus:
        if self.terminal is None:
            return "unknown"
        return _status(self.terminal, self.kind, require_terminal=True)


def _session(kind: _Kind, model: str, method_name: str) -> ProviderOperationSession:
    component = {
        "stt": "speech_to_text",
        "tts": "text_to_speech",
    }.get(kind, "llm")
    event_type = "external_cost" if kind in {"stt", "tts"} else "llm_call"
    service = {
        "stt": "speech_to_text",
        "tts": "text_to_speech",
        "images": "image_generation",
    }.get(kind, kind)
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=f"openrouter.{service}.{method_name}",
        provider="openrouter",
        service=service,
        operation=f"openrouter.{service}.{method_name}",
        component=component,
        model=model,
        event_type=event_type,
    )


def _sync_call(
    original: Any,
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    kind: _Kind,
    method_name: str,
) -> Any:
    model = _requested_model(args, kwargs)
    session = _session(kind, model, method_name)
    try:
        result = original(self, *args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise
    if kwargs.get("stream") is True and hasattr(result, "__next__"):
        meter = _StreamMeter(kind, model, kwargs)
        session.release_context()
        return SyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
            completion_status=meter.completion_status,
        )
    session.finish(
        _measurement(kind, result, model, kwargs),
        _status(result, kind),
    )
    return result


async def _async_call(
    original: Any,
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    kind: _Kind,
    method_name: str,
) -> Any:
    model = _requested_model(args, kwargs)
    session = _session(kind, model, method_name)
    try:
        result = await original(self, *args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise
    if kwargs.get("stream") is True and hasattr(result, "__anext__"):
        meter = _StreamMeter(kind, model, kwargs)
        session.release_context()
        return AsyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
            completion_status=meter.completion_status,
        )
    session.finish(
        _measurement(kind, result, model, kwargs),
        _status(result, kind),
    )
    return result


def _sync_wrapper(key: str, kind: _Kind, method_name: str) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _sync_call(
            _originals[key][2],
            self,
            args,
            kwargs,
            kind=kind,
            method_name=method_name,
        )

    return wrapper


def _async_wrapper(key: str, kind: _Kind, method_name: str) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await _async_call(
            _originals[key][2],
            self,
            args,
            kwargs,
            kind=kind,
            method_name=method_name,
        )

    return wrapper


def _video_measurement(response: Any, model: str) -> OperationMeasurement:
    usage = _value(response, "usage")
    cost = _non_negative_decimal(_value(usage, "cost"))
    return OperationMeasurement(
        pricing_usage={},
        usage_lines=(ProviderUsageLine("request_count", 1, "Requests"),),
        provider_record_id=_record_id(response),
        provider_cost_usd=cost,
        response_model=model,
        model_candidates=(model,),
        billing_dimensions=_dimensions(response, usage),
    )


def _job_status(response: Any) -> ProviderJobStatus:
    status = _value(response, "status")
    statuses: dict[Any, ProviderJobStatus] = {
        "pending": "submitted",
        "in_progress": "running",
        "completed": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "expired": "failed",
    }
    return statuses.get(status, "running")


def _video_dimensions(kwargs: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    dimensions: list[tuple[str, str]] = []
    for key in ("duration", "resolution", "aspect_ratio"):
        value = kwargs.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            dimensions.append((key, str(value)[:256]))
    return tuple(dimensions)


def _sync_video_generate(key: str) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        model = _requested_model(args, kwargs)
        session = ProviderJobSession(
            tracker=_active_tracker,
            task_type="openrouter.video_generation.generate",
            provider="openrouter",
            service="video_generation",
            operation="openrouter.video_generation.generate",
            component="external",
            event_type="external_cost",
            resource_type="model",
            resource_id=model,
            billing_dimensions=_video_dimensions(kwargs),
        )
        try:
            result = _originals[key][2](self, *args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        record_id = _record_id(result)
        if record_id is None:
            session.fail(ValueError("OpenRouter video response omitted its job id"))
            return result
        status = _job_status(result)
        measurement = _video_measurement(result, model) if status == "succeeded" else None
        session.submit(record_id, status=status, measurement=measurement)
        return result

    return wrapper


def _async_video_generate(key: str) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        model = _requested_model(args, kwargs)
        session = ProviderJobSession(
            tracker=_active_tracker,
            task_type="openrouter.video_generation.generate",
            provider="openrouter",
            service="video_generation",
            operation="openrouter.video_generation.generate",
            component="external",
            event_type="external_cost",
            resource_type="model",
            resource_id=model,
            billing_dimensions=_video_dimensions(kwargs),
        )
        try:
            result = await _originals[key][2](self, *args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        record_id = _record_id(result)
        if record_id is None:
            session.fail(ValueError("OpenRouter video response omitted its job id"))
            return result
        status = _job_status(result)
        measurement = _video_measurement(result, model) if status == "succeeded" else None
        session.submit(record_id, status=status, measurement=measurement)
        return result

    return wrapper


def _reconcile_video(result: Any, job_id: str) -> None:
    previous = _active_tracker._storage.get_provider_job("openrouter", "video_generation", job_id)
    if previous is None:
        return
    status = _job_status(result)
    measurement = (
        _video_measurement(result, previous.resource_id) if status == "succeeded" else None
    )
    reconcile_provider_job(
        tracker=_active_tracker,
        provider="openrouter",
        service="video_generation",
        provider_record_id=job_id,
        status=status,
        measurement=measurement,
        error_type="openrouter.video.failed" if status == "failed" else None,
    )


def _sync_video_get(key: str) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = _originals[key][2](self, *args, **kwargs)
        job_id = kwargs.get("job_id") or (args[0] if args else None)
        if isinstance(job_id, str):
            with suppress(Exception):
                _reconcile_video(result, job_id)
        return result

    return wrapper


def _async_video_get(key: str) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await _originals[key][2](self, *args, **kwargs)
        job_id = kwargs.get("job_id") or (args[0] if args else None)
        if isinstance(job_id, str):
            with suppress(Exception):
                _reconcile_video(result, job_id)
        return result

    return wrapper


def _generation_measurement(data: Any) -> OperationMeasurement:
    model = _canonical_model(_value(data, "model"))
    total_input_tokens = _non_negative_int(
        _value(data, "native_tokens_prompt") or _value(data, "tokens_prompt")
    )
    total_output_tokens = _non_negative_int(
        _value(data, "native_tokens_completion") or _value(data, "tokens_completion")
    )
    cached = _non_negative_int(_value(data, "native_tokens_cached"))
    reasoning = _non_negative_int(_value(data, "native_tokens_reasoning"))
    # OpenRouter reports cached tokens within the prompt total and reasoning
    # tokens within the completion total.  Attribution usage lines must be
    # disjoint so catalog pricing cannot charge those subsets twice.
    input_tokens = total_input_tokens
    if total_input_tokens is not None and cached is not None:
        if cached <= total_input_tokens:
            input_tokens = total_input_tokens - cached
        else:
            cached = None
    output_tokens = total_output_tokens
    if total_output_tokens is not None and reasoning is not None:
        if reasoning <= total_output_tokens:
            output_tokens = total_output_tokens - reasoning
        else:
            reasoning = None
    media_prompt = _non_negative_int(_value(data, "num_media_prompt"))
    media_completion = _non_negative_int(_value(data, "num_media_completion"))
    search_results = _non_negative_int(_value(data, "num_search_results"))
    fetches = _non_negative_int(_value(data, "num_fetches"))
    lines = tuple(
        line
        for line in (
            _positive_line("input_tokens", input_tokens, "Tokens"),
            _positive_line("output_tokens", output_tokens, "Tokens"),
            _positive_line("cache_read_input_tokens", cached, "Tokens"),
            _positive_line("reasoning_output_tokens", reasoning, "Tokens"),
            _positive_line("input_media_count", media_prompt, "Media"),
            _positive_line("output_media_count", media_completion, "Media"),
            _positive_line("web_search_result_count", search_results, "Results"),
            _positive_line("web_fetch_count", fetches, "Requests"),
        )
        if line is not None
    )
    pricing: dict[str, int] = {}
    if input_tokens is not None:
        pricing["input_tokens"] = input_tokens
    if output_tokens is not None:
        pricing["output_tokens"] = output_tokens
    if cached is not None:
        pricing["cache_read_input_tokens"] = cached
    if reasoning is not None:
        pricing["reasoning_output_tokens"] = reasoning
    provider = _value(data, "provider_name")
    dimensions: list[tuple[str, str]] = []
    if isinstance(provider, str) and provider:
        dimensions.append(("upstream_provider", provider[:256]))
    for key in ("data_region", "service_tier", "web_search_engine"):
        value = _value(data, key)
        if isinstance(value, str) and value:
            dimensions.append((key, value[:256]))
    is_byok = _value(data, "is_byok")
    if isinstance(is_byok, bool):
        dimensions.append(("is_byok", "true" if is_byok else "false"))
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=lines,
        provider_record_id=_record_id(data),
        provider_cost_usd=_non_negative_decimal(_value(data, "total_cost")),
        provider_upstream_cost_usd=_non_negative_decimal(_value(data, "upstream_inference_cost")),
        response_model=model,
        model_candidates=(model,),
        billing_dimensions=tuple(dimensions),
        task_input_tokens=total_input_tokens,
        task_output_tokens=total_output_tokens,
        task_cached_tokens=cached,
    )


def _reconcile_generation(result: Any) -> None:
    data = _value(result, "data")
    record_id = _record_id(data)
    if record_id is None:
        return
    measurement = _generation_measurement(data)
    for event in _active_tracker._storage.query_events():
        if event.provider != "openrouter":
            continue
        if event.details.get("provider_record_id") != record_id:
            continue
        event.model = measurement.response_model or event.model
        event.input_tokens = measurement.task_input_tokens
        event.output_tokens = measurement.task_output_tokens
        event.cached_tokens = measurement.task_cached_tokens
        if measurement.provider_cost_usd is not None:
            event.cost_usd = measurement.provider_cost_usd
            event.cost_confidence = "exact"
            event.pricing_source = "provider_response"
            event.pricing_version = None
            event.details["provider_reported_cost_usd"] = str(measurement.provider_cost_usd)
        if measurement.provider_upstream_cost_usd is not None:
            event.details["provider_upstream_cost_usd"] = str(
                measurement.provider_upstream_cost_usd
            )
        event.details["attribution_usage_lines"] = [
            {"metric": line.metric, "quantity": str(line.quantity), "unit": line.unit}
            for line in measurement.usage_lines
            if Decimal(str(line.quantity)) > 0
        ]
        if measurement.billing_dimensions:
            event.details["attribution_dimensions"] = [
                {"key": key, "value": {"type": "string", "value": value}}
                for key, value in measurement.billing_dimensions
            ]
        _active_tracker._storage.update_event(event)
        task = _active_tracker._storage.get_task(str(event.task_id))
        if task is not None:
            _active_tracker._aggregate_costs(task)
            _active_tracker._storage.update_task(task)
        return


def _sync_generation_get(key: str) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = _originals[key][2](self, *args, **kwargs)
        with suppress(Exception):
            _reconcile_generation(result)
        return result

    return wrapper


def _async_generation_get(key: str) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await _originals[key][2](self, *args, **kwargs)
        with suppress(Exception):
            _reconcile_generation(result)
        return result

    return wrapper


def _patch(owner: Any, name: str, replacement: Any, key: str) -> None:
    original = getattr(owner, name, None)
    if not callable(original):
        return
    _originals[key] = (owner, name, original)
    setattr(
        owner,
        name,
        provider_capture_callable("openrouter", replacement, original),
    )


def _restore_all() -> None:
    for owner, name, original in tuple(_originals.values()):
        with suppress(Exception):
            setattr(owner, name, original)
    _originals.clear()


def instrument_openrouter(tracker: Any) -> None:
    """Instrument every current cost-generating official OpenRouter SDK method."""
    global _active_tracker, _patched
    if _patched:
        raise RuntimeError("OpenRouter instrumentation is already active")
    try:
        import openrouter  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "OpenRouter instrumentation requires the 'openrouter' package; "
            "install dexcost[openrouter]"
        ) from exc

    _active_tracker = tracker
    try:
        for module_name, class_name, method_name, kind, is_async in _METHODS:
            owner = getattr(import_module(module_name), class_name)
            key = f"{module_name}:{class_name}:{method_name}"
            replacement = (
                _async_wrapper(key, kind, method_name)
                if is_async
                else _sync_wrapper(key, kind, method_name)
            )
            _patch(owner, method_name, replacement, key)

        video = import_module("openrouter.video_generation").VideoGeneration
        for method_name, factory in (
            ("generate", _sync_video_generate),
            ("generate_async", _async_video_generate),
            ("get_generation", _sync_video_get),
            ("get_generation_async", _async_video_get),
        ):
            key = f"openrouter.video_generation:VideoGeneration:{method_name}"
            _patch(video, method_name, factory(key), key)

        generations = import_module("openrouter.generations").Generations
        for method_name, factory in (
            ("get_generation", _sync_generation_get),
            ("get_generation_async", _async_generation_get),
        ):
            key = f"openrouter.generations:Generations:{method_name}"
            _patch(generations, method_name, factory(key), key)
    except Exception:
        _restore_all()
        _active_tracker = None
        raise
    _patched = True


def uninstrument_openrouter() -> None:
    """Restore the exact official SDK class methods captured at patch time."""
    global _active_tracker, _patched
    if not _patched:
        return
    _restore_all()
    _active_tracker = None
    _patched = False


__all__ = ["instrument_openrouter", "uninstrument_openrouter"]
