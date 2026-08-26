"""Privacy-safe instrumentation for the current Google Gen AI Python SDK.

The integration patches the public ``Models`` and ``AsyncModels`` inference
methods. It preserves the provider's native response and stream objects while
recording only provider-reported quantities and bounded opaque response IDs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import wrapt

from dexcost.context import suppress_network_event
from dexcost.instruments._capture import provider_capture_wrapper
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    OperationStatus,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)
from dexcost.models.provider_job import ProviderJobEventType, ProviderJobStatus
from dexcost.provider_jobs import (
    AsyncProviderJobStream,
    ProviderJobSession,
    SyncProviderJobStream,
    reconcile_provider_job,
)

_log = logging.getLogger(__name__)

_active_tracker: Any | None = None
_patched = False
_originals: dict[str, Any] = {}
_extra_originals: list[tuple[Any, str, Any]] = []


def _value(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _decimal_count(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _model_name(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    model = kwargs.get("model")
    if not isinstance(model, str) and args and isinstance(args[0], str):
        model = args[0]
    if not isinstance(model, str) or not model:
        return "gemini-unknown"
    for prefix in ("models/", "publishers/google/models/"):
        if model.startswith(prefix):
            return model[len(prefix) :]
    vertex_marker = "/publishers/google/models/"
    if vertex_marker in model:
        return model.rsplit(vertex_marker, 1)[-1]
    return model


def _response_model(response: object, fallback: str) -> str:
    value = _value(response, "model_version")
    if not isinstance(value, str) or not value:
        return fallback
    return _model_name((), {"model": value})


def _is_vertex(instance: object) -> bool:
    if bool(getattr(instance, "vertexai", False)):
        return True
    api_client = getattr(instance, "_api_client", None)
    return bool(getattr(api_client, "vertexai", False))


def _service(instance: object) -> str:
    return "vertex_ai" if _is_vertex(instance) else "gemini"


def _model_candidates(model: str, vertex: bool) -> tuple[str, ...]:
    primary = f"vertex_ai/{model}" if vertex else f"gemini/{model}"
    secondary = f"gemini/{model}" if vertex else f"vertex_ai/{model}"
    return (primary, secondary)


def _unknown_measurement(model: str, *, vertex: bool) -> OperationMeasurement:
    return OperationMeasurement(
        pricing_usage={},
        usage_lines=(),
        response_model=model,
        model_candidates=_model_candidates(model, vertex),
    )


def _session(
    instance: object,
    *,
    operation: str,
    component: str,
    model: str,
    event_type: str = "external_cost",
) -> ProviderOperationSession | None:
    if _active_tracker is None:
        return None
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=operation,
        provider="google",
        service=_service(instance),
        operation=operation,
        component=component,
        model=model,
        event_type=event_type,
    )


def _job_session(
    instance: object,
    *,
    operation: str,
    model: str,
    event_type: ProviderJobEventType = "llm_call",
    resource_type: str = "model",
    billing_dimensions: tuple[tuple[str, str], ...] = (),
) -> ProviderJobSession | None:
    if _active_tracker is None:
        return None
    return ProviderJobSession(
        tracker=_active_tracker,
        task_type=operation,
        provider="google",
        service=_service(instance),
        operation=operation,
        component="external",
        event_type=event_type,
        resource_type=resource_type,
        resource_id=model,
        billing_dimensions=billing_dimensions,
    )


def _enum_name(value: object) -> str:
    enum_value = getattr(value, "value", value)
    if not isinstance(enum_value, str):
        return "UNSPECIFIED"
    return enum_value.upper().rsplit(".", 1)[-1]


def _detail_counts(details: object, total: int | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(details, Sequence) and not isinstance(details, (str, bytes, bytearray)):
        for detail in details:
            count = _count(_value(detail, "token_count"))
            if count is None:
                count = _count(_value(detail, "tokens"))
            if count is None:
                continue
            modality = _enum_name(_value(detail, "modality"))
            counts[modality] = counts.get(modality, 0) + count
    if not counts and total is not None:
        counts["UNSPECIFIED"] = total
    elif total is not None:
        allocated = sum(counts.values())
        if total > allocated:
            counts["UNSPECIFIED"] = counts.get("UNSPECIFIED", 0) + total - allocated
    return counts


_MODAL_METRICS: dict[str, dict[str, str]] = {
    "input": {
        "TEXT": "input_tokens",
        "DOCUMENT": "input_tokens",
        "IMAGE": "input_image_tokens",
        "AUDIO": "input_audio_tokens",
        "VIDEO": "input_video_tokens",
        "UNSPECIFIED": "input_tokens",
    },
    "cache": {
        "TEXT": "cache_read_input_tokens",
        "DOCUMENT": "cache_read_input_tokens",
        "IMAGE": "cache_read_input_image_tokens",
        "AUDIO": "cache_read_input_audio_tokens",
        "VIDEO": "cache_read_input_video_tokens",
        "UNSPECIFIED": "cache_read_input_tokens",
    },
    "output": {
        "TEXT": "output_tokens",
        "DOCUMENT": "output_tokens",
        "IMAGE": "output_image_tokens",
        "AUDIO": "output_audio_tokens",
        "VIDEO": "output_video_tokens",
        "UNSPECIFIED": "output_tokens",
    },
    "tool": {
        "TEXT": "tool_input_tokens",
        "DOCUMENT": "tool_input_tokens",
        "IMAGE": "tool_input_image_tokens",
        "AUDIO": "tool_input_audio_tokens",
        "VIDEO": "tool_input_video_tokens",
        "UNSPECIFIED": "tool_input_tokens",
    },
}


def _add_quantity(values: dict[str, Decimal], name: str, quantity: int | Decimal) -> None:
    parsed = quantity if isinstance(quantity, Decimal) else Decimal(quantity)
    if parsed > 0:
        values[name] = values.get(name, Decimal(0)) + parsed


def _add_modal_counts(values: dict[str, Decimal], phase: str, counts: Mapping[str, int]) -> None:
    metrics = _MODAL_METRICS[phase]
    for modality, count in counts.items():
        metric = metrics.get(modality, f"unallocated_{phase}_tokens")
        _add_quantity(values, metric, count)


def _split_prompt_and_cache(
    usage: object, values: dict[str, Decimal]
) -> tuple[int | None, int | None]:
    prompt_total = _count(_value(usage, "prompt_token_count"))
    cache_total = _count(_value(usage, "cached_content_token_count")) or 0
    prompt_counts = _detail_counts(_value(usage, "prompt_tokens_details"), prompt_total)
    cache_counts = _detail_counts(_value(usage, "cache_tokens_details"), None)

    if cache_total == 0:
        _add_modal_counts(values, "input", prompt_counts)
        return prompt_total, 0

    if cache_counts:
        uncached: dict[str, int] = {}
        for modality in set(prompt_counts) | set(cache_counts):
            prompt_count = prompt_counts.get(modality, 0)
            cached_count = cache_counts.get(modality, 0)
            if cached_count > prompt_count:
                _add_quantity(
                    values,
                    "unallocated_cache_read_input_tokens",
                    cached_count - prompt_count,
                )
            if prompt_count > cached_count:
                uncached[modality] = prompt_count - cached_count
        allocated_cache = sum(cache_counts.values())
        if cache_total > allocated_cache:
            _add_quantity(
                values,
                "unallocated_cache_read_input_tokens",
                cache_total - allocated_cache,
            )
        _add_modal_counts(values, "input", uncached)
        _add_modal_counts(values, "cache", cache_counts)
        return prompt_total, cache_total

    named_modalities = [name for name, count in prompt_counts.items() if count > 0]
    if len(named_modalities) == 1:
        modality = named_modalities[0]
        uncached_count = max(0, prompt_counts[modality] - cache_total)
        _add_modal_counts(values, "input", {modality: uncached_count})
        _add_modal_counts(values, "cache", {modality: cache_total})
    else:
        if prompt_total is not None and prompt_total > cache_total:
            _add_quantity(values, "unallocated_input_tokens", prompt_total - cache_total)
        _add_quantity(values, "unallocated_cache_read_input_tokens", cache_total)
    return prompt_total, cache_total


def _token_lines(values: Mapping[str, Decimal]) -> tuple[ProviderUsageLine, ...]:
    return tuple(
        ProviderUsageLine(metric, quantity, "Tokens")
        for metric, quantity in sorted(values.items())
        if quantity > 0
    )


def _measurement_from_usage(
    usage: object,
    *,
    model: str,
    provider_record_id: str | None,
    vertex: bool,
) -> OperationMeasurement:
    values: dict[str, Decimal] = {}
    prompt_total, cache_total = _split_prompt_and_cache(usage, values)

    output_total = _count(_value(usage, "candidates_token_count"))
    output_counts = _detail_counts(_value(usage, "candidates_tokens_details"), output_total)
    _add_modal_counts(values, "output", output_counts)

    thoughts = _count(_value(usage, "thoughts_token_count"))
    if thoughts is not None:
        _add_quantity(values, "reasoning_output_tokens", thoughts)

    tool_total = _count(_value(usage, "tool_use_prompt_token_count"))
    tool_counts = _detail_counts(_value(usage, "tool_use_prompt_tokens_details"), tool_total)
    _add_modal_counts(values, "tool", tool_counts)

    task_output = None
    if output_total is not None or thoughts is not None:
        task_output = (output_total or 0) + (thoughts or 0)
    task_input = None
    if prompt_total is not None or tool_total is not None:
        task_input = (prompt_total or 0) + (tool_total or 0)
    return OperationMeasurement(
        pricing_usage=values,
        usage_lines=_token_lines(values),
        provider_record_id=provider_record_id,
        response_model=model,
        model_candidates=_model_candidates(model, vertex),
        task_input_tokens=task_input,
        task_output_tokens=task_output,
        task_cached_tokens=cache_total,
    )


def _content_measurement(
    response: object, kwargs: dict[str, Any], *, vertex: bool
) -> OperationMeasurement:
    requested = _model_name((), kwargs)
    model = _response_model(response, requested)
    usage = _value(response, "usage_metadata")
    if usage is None:
        return _unknown_measurement(model, vertex=vertex)
    provider_record_id = _value(response, "response_id")
    if not isinstance(provider_record_id, str):
        provider_record_id = None
    return _measurement_from_usage(
        usage,
        model=model,
        provider_record_id=provider_record_id,
        vertex=vertex,
    )


def _embedding_measurement(
    response: object, kwargs: dict[str, Any], *, vertex: bool
) -> OperationMeasurement:
    model = _model_name((), kwargs)
    token_total = Decimal(0)
    token_evidence = False
    embeddings = _value(response, "embeddings")
    if isinstance(embeddings, Sequence):
        for embedding in embeddings:
            statistics = _value(embedding, "statistics")
            count = _decimal_count(_value(statistics, "token_count"))
            if count is not None:
                token_total += count
                token_evidence = True

    pricing: dict[str, Decimal] = {}
    lines: list[ProviderUsageLine] = []
    task_input: int | None = None
    if token_evidence:
        pricing["input_tokens"] = token_total
        lines.append(ProviderUsageLine("input_tokens", token_total, "Tokens"))
        if token_total == token_total.to_integral_value():
            task_input = int(token_total)

    metadata = _value(response, "metadata")
    characters = _count(_value(metadata, "billable_character_count"))
    if characters is not None and characters > 0:
        lines.append(ProviderUsageLine("characters", characters, "Characters"))
        if not token_evidence:
            pricing["characters"] = Decimal(characters)

    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        response_model=model,
        model_candidates=_model_candidates(model, vertex),
        task_input_tokens=task_input,
    )


def _image_count(response: object, operation: str) -> int:
    field = "generated_masks" if operation == "segment_image" else "generated_images"
    values = _value(response, field)
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        return len(values)
    return 0


def _image_measurement(
    response: object,
    kwargs: dict[str, Any],
    *,
    operation: str,
    vertex: bool,
) -> OperationMeasurement:
    model = _model_name((), kwargs)
    count = _image_count(response, operation)
    pricing: dict[str, Decimal] = {}
    lines: tuple[ProviderUsageLine, ...] = ()
    if count > 0:
        pricing["output_image_count"] = Decimal(count)
        lines = (ProviderUsageLine("output_image_count", count, "Images"),)
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=lines,
        response_model=model,
        model_candidates=_model_candidates(model, vertex),
    )


def _interaction_model(response: object, kwargs: dict[str, Any]) -> str:
    response_model = _value(response, "model")
    if isinstance(response_model, str) and response_model:
        return response_model
    requested = kwargs.get("model")
    if isinstance(requested, str) and requested:
        return requested
    agent = kwargs.get("agent")
    if isinstance(agent, str) and agent:
        return f"agent:{agent}"
    return "gemini-interaction-unknown"


def _interaction_measurement(
    response: object,
    kwargs: dict[str, Any],
) -> OperationMeasurement:
    model = _interaction_model(response, kwargs)
    usage = _value(response, "usage")
    if usage is None:
        return _unknown_measurement(model, vertex=False)
    normalized_usage = {
        "prompt_token_count": _value(usage, "total_input_tokens"),
        "cached_content_token_count": _value(usage, "total_cached_tokens"),
        "prompt_tokens_details": _value(usage, "input_tokens_by_modality"),
        "cache_tokens_details": _value(usage, "cached_tokens_by_modality"),
        "candidates_token_count": _value(usage, "total_output_tokens"),
        "candidates_tokens_details": _value(usage, "output_tokens_by_modality"),
        "thoughts_token_count": _value(usage, "total_thought_tokens"),
        "tool_use_prompt_token_count": _value(usage, "total_tool_use_tokens"),
        "tool_use_prompt_tokens_details": _value(usage, "tool_use_tokens_by_modality"),
    }
    provider_record_id = _value(response, "id")
    if not isinstance(provider_record_id, str):
        provider_record_id = None
    return _measurement_from_usage(
        normalized_usage,
        model=model,
        provider_record_id=provider_record_id,
        vertex=False,
    )


def _interaction_status(response: object) -> OperationStatus:
    status = _value(response, "status")
    if status == "completed":
        return "succeeded"
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "unknown"


def _interaction_job_status(response: object, *, submission: bool = False) -> ProviderJobStatus:
    status = _value(response, "status")
    if status == "completed":
        return "succeeded"
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status == "incomplete":
        return "unknown"
    if submission:
        return "submitted"
    return "running"


def _interaction_id(
    response: object | None,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> str | None:
    response_id = _value(response, "id") if response is not None else None
    if isinstance(response_id, str) and response_id:
        return response_id
    values = kwargs or {}
    keyword_id = values.get("id")
    if isinstance(keyword_id, str) and keyword_id:
        return keyword_id
    if args and isinstance(args[0], str) and args[0]:
        return args[0]
    return None


def _terminal_job_measurement(
    response: object, kwargs: dict[str, Any], status: ProviderJobStatus
) -> OperationMeasurement | None:
    if status in {"submitted", "running"}:
        return None
    if _value(response, "usage") is None:
        return None
    return _interaction_measurement(response, kwargs)


def _video_billing_dimensions(kwargs: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    config = kwargs.get("config")
    dimensions: list[tuple[str, str]] = []
    duration = _decimal_count(_value(config, "duration_seconds"))
    if duration is not None and duration > 0:
        dimensions.append(("video_duration_seconds", str(duration.normalize())))
    resolution = _value(config, "resolution")
    if isinstance(resolution, str) and resolution:
        dimensions.append(("video_resolution", resolution.lower()))
    generate_audio = _value(config, "generate_audio")
    if isinstance(generate_audio, bool):
        dimensions.append(("video_audio", "true" if generate_audio else "false"))
    return tuple(sorted(dimensions))


def _video_response(operation: object) -> object | None:
    return cast(object | None, _value(operation, "response") or _value(operation, "result"))


def _video_operation_status(operation: object, *, submission: bool = False) -> ProviderJobStatus:
    if _value(operation, "done") is not True:
        return "submitted" if submission else "running"
    error = _value(operation, "error")
    if error:
        error_text = str(error).lower()
        return "cancelled" if "cancel" in error_text else "failed"
    response = _video_response(operation)
    generated = _value(response, "generated_videos")
    if (
        isinstance(generated, Sequence)
        and not isinstance(generated, (str, bytes, bytearray))
        and any(_value(item, "video") is not None for item in generated)
    ):
        return "succeeded"
    return "unknown"


def _video_measurement(
    operation: object,
    *,
    model: str,
    billing_dimensions: tuple[tuple[str, str], ...],
    vertex: bool,
) -> OperationMeasurement | None:
    response = _video_response(operation)
    generated = _value(response, "generated_videos")
    if not isinstance(generated, Sequence) or isinstance(generated, (str, bytes, bytearray)):
        return None
    count = sum(1 for item in generated if _value(item, "video") is not None)
    if count <= 0:
        return None
    pricing: dict[str, Decimal] = {"output_video_count": Decimal(count)}
    lines: list[ProviderUsageLine] = [ProviderUsageLine("output_video_count", count, "Videos")]
    context = dict(billing_dimensions)
    duration = _decimal_count(context.get("video_duration_seconds"))
    if duration is not None and duration > 0:
        output_seconds = duration * Decimal(count)
        pricing["output_video_seconds"] = output_seconds
        lines.append(ProviderUsageLine("output_video_seconds", output_seconds, "Seconds"))
    record_id = _value(operation, "name")
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        provider_record_id=record_id if isinstance(record_id, str) else None,
        response_model=model,
        model_candidates=_model_candidates(model, vertex),
    )


def _operation_error_identity(operation: object) -> tuple[str | None, str | None]:
    error = _value(operation, "error")
    if not error:
        return None, None
    raw_code = _value(error, "code")
    raw_status = _value(error, "status") or _value(error, "type")
    error_type = (
        f"google.operation.{str(raw_status).lower()}"
        if raw_status is not None
        else "google.operation.failed"
    )
    return error_type, str(raw_code) if raw_code is not None else None


def _resource_error_identity(resource: object, *, namespace: str) -> tuple[str | None, str | None]:
    error = _value(resource, "error")
    if not error:
        return None, None
    raw_code = _value(error, "code")
    raw_status = _value(error, "status") or _value(error, "type")
    suffix = str(raw_status).lower() if raw_status is not None else "failed"
    return f"google.{namespace}.{suffix}", (str(raw_code) if raw_code is not None else None)


def _job_name(
    resource: object | None,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> str | None:
    name = _value(resource, "name") if resource is not None else None
    if isinstance(name, str) and name:
        return name
    candidate = (kwargs or {}).get("name")
    if isinstance(candidate, str) and candidate:
        return candidate
    if args and isinstance(args[0], str) and args[0]:
        return args[0]
    return None


def _bounded_source_kind(value: object) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "inline"
    if isinstance(value, str):
        lowered = value.lower()
        if lowered.startswith("gs://"):
            return "gcs"
        if lowered.startswith("bq://"):
            return "bigquery"
        if lowered.startswith("files/"):
            return "file"
        return "provider_resource"
    for field, kind in (
        ("inlined_requests", "inline"),
        ("inlined_responses", "inline"),
        ("inlined_embed_content_responses", "inline_embedding"),
        ("gcs_uri", "gcs"),
        ("bigquery_uri", "bigquery"),
        ("file_name", "file"),
        ("vertex_dataset", "vertex_dataset"),
    ):
        if _value(value, field) is not None:
            return kind
    raw_format = _value(value, "format")
    if raw_format is not None:
        normalized = _enum_name(raw_format).lower()
        if normalized != "unspecified":
            return normalized
    return "unknown"


def _batch_billing_dimensions(
    kwargs: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    dimensions = [("batch_source", _bounded_source_kind(kwargs.get("src")))]
    config = kwargs.get("config")
    destination = _value(config, "dest")
    if destination is not None:
        dimensions.append(("batch_destination", _bounded_source_kind(destination)))
    return tuple(sorted(dimensions))


def _tuning_billing_dimensions(
    kwargs: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    config = kwargs.get("config")
    dimensions: list[tuple[str, str]] = []
    for field, key in (
        ("method", "tuning_method"),
        ("adapter_size", "tuning_adapter_size"),
        ("tuning_mode", "tuning_mode"),
    ):
        value = _value(config, field)
        if value is not None:
            normalized = _enum_name(value).lower()
            if normalized != "unspecified":
                dimensions.append((key, normalized))
    epochs = _count(_value(config, "epoch_count"))
    if epochs is not None and epochs > 0:
        dimensions.append(("requested_epoch_count", str(epochs)))
    return tuple(sorted(dimensions))


def _batch_status(resource: object, *, submission: bool = False) -> ProviderJobStatus:
    state = _enum_name(_value(resource, "state"))
    if state == "JOB_STATE_SUCCEEDED":
        return "succeeded"
    if state == "JOB_STATE_PARTIALLY_SUCCEEDED":
        return "unknown"
    if state in {"JOB_STATE_FAILED", "JOB_STATE_EXPIRED", "FAILED"}:
        return "failed"
    if state == "JOB_STATE_CANCELLED":
        return "cancelled"
    if submission:
        return "submitted"
    return "running"


def _tuning_status(resource: object, *, submission: bool = False) -> ProviderJobStatus:
    state = _enum_name(_value(resource, "state"))
    if state in {"JOB_STATE_SUCCEEDED", "ACTIVE"}:
        return "succeeded"
    if state in {"JOB_STATE_FAILED", "JOB_STATE_EXPIRED", "FAILED"}:
        return "failed"
    if state == "JOB_STATE_CANCELLED":
        return "cancelled"
    if submission:
        return "submitted"
    return "running"


def _batch_metric(metric: str) -> str:
    if metric == "reasoning_output_tokens":
        return "batch_reasoning_output_tokens"
    return f"batch_{metric}"


def _batch_measurement(
    resource: object, *, model: str, vertex: bool
) -> OperationMeasurement | None:
    pricing: dict[str, Decimal] = {}
    units: dict[str, str] = {}
    task_input = 0
    task_output = 0
    task_cached = 0
    has_task_input = False
    has_task_output = False
    has_task_cached = False
    derived_success = 0
    derived_failed = 0

    def add_measurement(measurement: OperationMeasurement) -> None:
        nonlocal task_input, task_output, task_cached
        nonlocal has_task_input, has_task_output, has_task_cached
        for line in measurement.usage_lines:
            metric = _batch_metric(line.metric)
            quantity = Decimal(str(line.quantity))
            pricing[metric] = pricing.get(metric, Decimal(0)) + quantity
            units.setdefault(metric, line.unit)
        if measurement.task_input_tokens is not None:
            has_task_input = True
            task_input += measurement.task_input_tokens
        if measurement.task_output_tokens is not None:
            has_task_output = True
            task_output += measurement.task_output_tokens
        if measurement.task_cached_tokens is not None:
            has_task_cached = True
            task_cached += measurement.task_cached_tokens

    destination = _value(resource, "dest")
    inlined = _value(destination, "inlined_responses")
    if isinstance(inlined, Sequence) and not isinstance(inlined, (str, bytes, bytearray)):
        for item in inlined:
            response = _value(item, "response")
            if response is not None:
                derived_success += 1
                add_measurement(_content_measurement(response, {"model": model}, vertex=vertex))
            elif _value(item, "error") is not None:
                derived_failed += 1

    embedded = _value(destination, "inlined_embed_content_responses")
    if isinstance(embedded, Sequence) and not isinstance(embedded, (str, bytes, bytearray)):
        for item in embedded:
            response = _value(item, "response")
            if response is not None:
                derived_success += 1
                add_measurement(_embedding_measurement(response, {"model": model}, vertex=vertex))
            elif _value(item, "error") is not None:
                derived_failed += 1

    lines = [
        ProviderUsageLine(metric, quantity, units.get(metric, "Units"))
        for metric, quantity in sorted(pricing.items())
        if quantity > 0
    ]
    stats = _value(resource, "completion_stats")
    counts = (
        (
            "batch_successful_request_count",
            _count(_value(stats, "successful_count")),
            derived_success,
        ),
        (
            "batch_failed_request_count",
            _count(_value(stats, "failed_count")),
            derived_failed,
        ),
        (
            "batch_incomplete_request_count",
            _count(_value(stats, "incomplete_count")),
            0,
        ),
    )
    for metric, reported, derived in counts:
        quantity = reported if reported is not None else derived
        if quantity > 0:
            lines.append(ProviderUsageLine(metric, quantity, "Requests"))

    if not lines:
        return None
    record_id = _job_name(resource)
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        provider_record_id=record_id,
        response_model=model,
        model_candidates=_model_candidates(model, vertex),
        task_input_tokens=task_input if has_task_input else None,
        task_output_tokens=task_output if has_task_output else None,
        task_cached_tokens=task_cached if has_task_cached else None,
    )


def _tuning_measurement(
    resource: object, *, model: str, vertex: bool
) -> OperationMeasurement | None:
    stats = _value(resource, "tuning_data_stats")
    selected = None
    for field in (
        "supervised_tuning_data_stats",
        "preference_optimization_data_stats",
        "reinforcement_tuning_data_stats",
    ):
        candidate = _value(stats, field)
        if candidate is not None:
            selected = candidate
            break
    if selected is None:
        distillation = _value(stats, "distillation_data_stats")
        selected = _value(distillation, "training_dataset_stats")

    values: list[tuple[str, int | None, str]] = [
        (
            "training_billable_tokens",
            _count(_value(selected, "total_billable_token_count")),
            "Tokens",
        ),
        (
            "training_billable_characters",
            _count(_value(selected, "total_billable_character_count")),
            "Characters",
        ),
        (
            "training_example_count",
            _count(_value(selected, "tuning_dataset_example_count")),
            "Examples",
        ),
    ]
    metadata = _value(resource, "tuning_job_metadata")
    completed_steps = _count(_value(metadata, "completed_step_count"))
    if completed_steps is None:
        completed_steps = _count(_value(selected, "tuning_step_count"))
    values.extend(
        (
            ("training_step_count", completed_steps, "Steps"),
            (
                "training_epoch_count",
                _count(_value(metadata, "completed_epoch_count")),
                "Epochs",
            ),
        )
    )
    lines = tuple(
        ProviderUsageLine(metric, quantity, unit)
        for metric, quantity, unit in values
        if quantity is not None and quantity > 0
    )
    if not lines:
        return None
    return OperationMeasurement(
        # Google reports consumption counters but not a training charge in the
        # job resource. Keep the cost unknown until an authoritative catalog or
        # provider billing record supplies a matching training rate.
        pricing_usage={},
        usage_lines=lines,
        provider_record_id=_job_name(resource),
        response_model=model,
        model_candidates=_model_candidates(model, vertex),
    )


def _terminal_status_with_evidence(
    status: ProviderJobStatus, measurement: OperationMeasurement | None
) -> ProviderJobStatus:
    if status == "succeeded" and (measurement is None or not measurement.usage_lines):
        return "unknown"
    return status


def _resource_model(resource: object, fallback: str, *, tuning: bool = False) -> str:
    field = "base_model" if tuning else "model"
    model = _value(resource, field)
    if not isinstance(model, str) or not model:
        return fallback
    return _model_name((), {"model": model})


def _submit_batch_job(
    session: ProviderJobSession, resource: object, *, model: str, vertex: bool
) -> None:
    record_id = _job_name(resource)
    if record_id is None:
        return
    resolved_model = _resource_model(resource, model)
    raw_status = _batch_status(resource, submission=True)
    measurement = (
        _batch_measurement(resource, model=resolved_model, vertex=vertex)
        if raw_status not in {"submitted", "running"}
        else None
    )
    status = _terminal_status_with_evidence(raw_status, measurement)
    error_type, error_code = _resource_error_identity(resource, namespace="batch")
    session.submit(
        record_id,
        status=status,
        measurement=measurement,
        error_type=error_type,
        error_code=error_code,
    )


def _submit_tuning_job(
    session: ProviderJobSession, resource: object, *, model: str, vertex: bool
) -> None:
    record_id = _job_name(resource)
    if record_id is None:
        return
    resolved_model = _resource_model(resource, model, tuning=True)
    raw_status = _tuning_status(resource, submission=True)
    measurement = (
        _tuning_measurement(resource, model=resolved_model, vertex=vertex)
        if raw_status not in {"submitted", "running"}
        else None
    )
    status = _terminal_status_with_evidence(raw_status, measurement)
    error_type, error_code = _resource_error_identity(resource, namespace="tuning")
    session.submit(
        record_id,
        status=status,
        measurement=measurement,
        error_type=error_type,
        error_code=error_code,
    )


def _reconcile_batch_job(instance: object, resource: object) -> None:
    if _active_tracker is None:
        return
    record_id = _job_name(resource)
    if record_id is None:
        return
    service = _service(instance)
    previous = _active_tracker._storage.get_provider_job("google", service, record_id)
    if previous is None or previous.operation != "google.genai.batches.create":
        return
    raw_status = _batch_status(resource)
    measurement = (
        _batch_measurement(
            resource,
            model=previous.resource_id,
            vertex=_is_vertex(instance),
        )
        if raw_status not in {"submitted", "running"}
        else None
    )
    status = _terminal_status_with_evidence(raw_status, measurement)
    error_type, error_code = _resource_error_identity(resource, namespace="batch")
    try:
        reconcile_provider_job(
            tracker=_active_tracker,
            provider="google",
            service=service,
            provider_record_id=record_id,
            status=status,
            measurement=measurement,
            error_type=error_type,
            error_code=error_code,
        )
    except Exception:
        _log.debug("dexcost: failed to reconcile Google batch job", exc_info=True)


def _reconcile_tuning_job(instance: object, resource: object) -> None:
    if _active_tracker is None:
        return
    record_id = _job_name(resource)
    if record_id is None:
        return
    service = _service(instance)
    previous = _active_tracker._storage.get_provider_job("google", service, record_id)
    if previous is None or previous.operation != "google.genai.tunings.tune":
        return
    raw_status = _tuning_status(resource)
    measurement = (
        _tuning_measurement(
            resource,
            model=previous.resource_id,
            vertex=_is_vertex(instance),
        )
        if raw_status not in {"submitted", "running"}
        else None
    )
    status = _terminal_status_with_evidence(raw_status, measurement)
    error_type, error_code = _resource_error_identity(resource, namespace="tuning")
    try:
        reconcile_provider_job(
            tracker=_active_tracker,
            provider="google",
            service=service,
            provider_record_id=record_id,
            status=status,
            measurement=measurement,
            error_type=error_type,
            error_code=error_code,
        )
    except Exception:
        _log.debug("dexcost: failed to reconcile Google tuning job", exc_info=True)


def _submit_video_operation(
    session: ProviderJobSession,
    operation: object,
    *,
    model: str,
    vertex: bool,
) -> None:
    record_id = _value(operation, "name")
    if not isinstance(record_id, str) or not record_id:
        return
    status = _video_operation_status(operation, submission=True)
    error_type, error_code = _operation_error_identity(operation)
    session.submit(
        record_id,
        status=status,
        measurement=(
            _video_measurement(
                operation,
                model=model,
                billing_dimensions=session.billing_dimensions,
                vertex=vertex,
            )
            if status == "succeeded"
            else None
        ),
        error_type=error_type,
        error_code=error_code,
    )


def _reconcile_video_operation(instance: object, operation: object) -> None:
    if _active_tracker is None:
        return
    record_id = _value(operation, "name")
    if not isinstance(record_id, str) or not record_id:
        return
    service = _service(instance)
    previous = _active_tracker._storage.get_provider_job("google", service, record_id)
    if previous is None or previous.operation != "google.genai.models.generate_videos":
        return
    status = _video_operation_status(operation)
    error_type, error_code = _operation_error_identity(operation)
    try:
        reconcile_provider_job(
            tracker=_active_tracker,
            provider="google",
            service=service,
            provider_record_id=record_id,
            status=status,
            measurement=(
                _video_measurement(
                    operation,
                    model=previous.resource_id,
                    billing_dimensions=previous.billing_dimensions,
                    vertex=_is_vertex(instance),
                )
                if status == "succeeded"
                else None
            ),
            error_type=error_type,
            error_code=error_code,
        )
    except Exception:
        _log.debug("dexcost: failed to reconcile Google video job", exc_info=True)


def _reconcile_interaction(
    instance: object,
    response: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    if _active_tracker is None:
        return
    record_id = _interaction_id(response, args, kwargs)
    if record_id is None:
        return
    status = _interaction_job_status(response)
    try:
        reconcile_provider_job(
            tracker=_active_tracker,
            provider="google",
            service=_service(instance),
            provider_record_id=record_id,
            status=status,
            measurement=_terminal_job_measurement(response, kwargs, status),
        )
    except LookupError:
        # The interaction may have been created before instrumentation or in
        # another local buffer. Its native response must remain unaffected.
        return
    except Exception:
        _log.debug("dexcost: failed to reconcile Google Interaction", exc_info=True)


def _submit_background_interaction(
    session: ProviderJobSession,
    response: object,
    kwargs: dict[str, Any],
) -> None:
    record_id = _interaction_id(response)
    if record_id is None:
        return
    status = _interaction_job_status(response, submission=True)
    try:
        session.submit(
            record_id,
            status=status,
            measurement=_terminal_job_measurement(response, kwargs, status),
        )
    except Exception:
        _log.debug(
            "dexcost: failed to persist Google background Interaction",
            exc_info=True,
        )


MeasurementExtractor = Callable[[object, dict[str, Any], bool], OperationMeasurement]


def _sync_direct_call(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    operation: str,
    component: str,
    event_type: str,
    extract: MeasurementExtractor,
) -> Any:
    model = _model_name(args, kwargs)
    vertex = _is_vertex(instance)
    session = _session(
        instance,
        operation=operation,
        component=component,
        model=model,
        event_type=event_type,
    )
    if session is None:
        return wrapped(*args, **kwargs)
    try:
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except Exception as exc:
            session.fail(exc)
            raise
        try:
            measurement = extract(response, kwargs, vertex)
        except Exception:
            _log.debug("dexcost: failed to extract Google provider usage", exc_info=True)
            measurement = _unknown_measurement(model, vertex=vertex)
        session.succeed(measurement)
        return response
    finally:
        session.release_context()


def _async_direct_call(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    operation: str,
    component: str,
    event_type: str,
    extract: MeasurementExtractor,
) -> Any:
    async def invoke() -> Any:
        model = _model_name(args, kwargs)
        vertex = _is_vertex(instance)
        session = _session(
            instance,
            operation=operation,
            component=component,
            model=model,
            event_type=event_type,
        )
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    response = await wrapped(*args, **kwargs)
            except Exception as exc:
                session.fail(exc)
                raise
            try:
                measurement = extract(response, kwargs, vertex)
            except Exception:
                _log.debug(
                    "dexcost: failed to extract async Google provider usage",
                    exc_info=True,
                )
                measurement = _unknown_measurement(model, vertex=vertex)
            session.succeed(measurement)
            return response
        finally:
            session.release_context()

    return invoke()


class _ContentStreamMeter:
    def __init__(self, kwargs: dict[str, Any], *, vertex: bool) -> None:
        self.kwargs = kwargs
        self.vertex = vertex
        self.terminal: object | None = None

    def observe(self, chunk: object) -> None:
        if _value(chunk, "usage_metadata") is not None:
            self.terminal = chunk

    def measurement(self) -> OperationMeasurement:
        model = _model_name((), self.kwargs)
        if self.terminal is None:
            return _unknown_measurement(model, vertex=self.vertex)
        return _content_measurement(self.terminal, self.kwargs, vertex=self.vertex)


class _InteractionStreamMeter:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.terminal: object | None = None
        self.saw_error = False

    def observe(self, event: object) -> None:
        interaction = _value(event, "interaction")
        if interaction is not None:
            self.terminal = interaction
        event_type = _value(event, "event_type") or _value(event, "type")
        if event_type in {"error", "interaction.error"}:
            self.saw_error = True

    def measurement(self) -> OperationMeasurement:
        if self.terminal is None:
            return _unknown_measurement(
                _interaction_model({}, self.kwargs),
                vertex=False,
            )
        return _interaction_measurement(self.terminal, self.kwargs)

    def status(self) -> OperationStatus:
        if self.saw_error:
            return "failed"
        if self.terminal is None:
            return "unknown"
        return _interaction_status(self.terminal)


def _sync_stream_call(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    model = _model_name(args, kwargs)
    vertex = _is_vertex(instance)
    session = _session(
        instance,
        operation="google.genai.models.generate_content_stream",
        component="external",
        model=model,
        event_type="llm_call",
    )
    if session is None:
        return wrapped(*args, **kwargs)
    try:
        try:
            with suppress_network_event():
                stream = wrapped(*args, **kwargs)
        except Exception as exc:
            session.fail(exc)
            raise
        meter = _ContentStreamMeter(kwargs, vertex=vertex)
        session.release_context()
        return SyncProviderStream(
            stream,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
        )
    finally:
        session.release_context()


def _async_stream_call(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        model = _model_name(args, kwargs)
        vertex = _is_vertex(instance)
        session = _session(
            instance,
            operation="google.genai.models.generate_content_stream",
            component="external",
            model=model,
            event_type="llm_call",
        )
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    stream = await wrapped(*args, **kwargs)
            except Exception as exc:
                session.fail(exc)
                raise
            meter = _ContentStreamMeter(kwargs, vertex=vertex)
            session.release_context()
            return AsyncProviderStream(
                stream,
                session,
                observe=meter.observe,
                measurement=meter.measurement,
            )
        finally:
            session.release_context()

    return invoke()


def _sync_interaction_create(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    if kwargs.get("background") is True:
        model = _interaction_model({}, kwargs)
        job_session = _job_session(
            instance,
            operation="google.genai.interactions.create",
            model=model,
        )
        if job_session is None:
            return wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    response = wrapped(*args, **kwargs)
            except Exception as exc:
                job_session.fail(exc)
                raise
            if kwargs.get("stream") is True:
                meter = _InteractionStreamMeter(kwargs)

                def complete() -> None:
                    if meter.terminal is not None:
                        _submit_background_interaction(job_session, meter.terminal, kwargs)

                job_session.release_context()
                return SyncProviderJobStream(
                    response,
                    observe=meter.observe,
                    complete=complete,
                )
            _submit_background_interaction(job_session, response, kwargs)
            return response
        finally:
            job_session.release_context()
    model = _interaction_model({}, kwargs)
    session = _session(
        instance,
        operation="google.genai.interactions.create",
        component="external",
        model=model,
        event_type="llm_call",
    )
    if session is None:
        return wrapped(*args, **kwargs)
    try:
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except Exception as exc:
            session.fail(exc)
            raise
        if kwargs.get("stream") is True:
            meter = _InteractionStreamMeter(kwargs)
            session.release_context()
            return SyncProviderStream(
                response,
                session,
                observe=meter.observe,
                measurement=meter.measurement,
                completion_status=meter.status,
            )
        try:
            measurement = _interaction_measurement(response, kwargs)
        except Exception:
            _log.debug(
                "dexcost: failed to extract Google Interaction usage",
                exc_info=True,
            )
            measurement = _unknown_measurement(model, vertex=False)
        session.finish(measurement, _interaction_status(response))
        return response
    finally:
        session.release_context()


def _async_interaction_create(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        if kwargs.get("background") is True:
            model = _interaction_model({}, kwargs)
            job_session = _job_session(
                instance,
                operation="google.genai.interactions.create",
                model=model,
            )
            if job_session is None:
                return await wrapped(*args, **kwargs)
            try:
                try:
                    with suppress_network_event():
                        response = await wrapped(*args, **kwargs)
                except Exception as exc:
                    job_session.fail(exc)
                    raise
                if kwargs.get("stream") is True:
                    meter = _InteractionStreamMeter(kwargs)

                    def complete() -> None:
                        if meter.terminal is not None:
                            _submit_background_interaction(job_session, meter.terminal, kwargs)

                    job_session.release_context()
                    return AsyncProviderJobStream(
                        response,
                        observe=meter.observe,
                        complete=complete,
                    )
                _submit_background_interaction(job_session, response, kwargs)
                return response
            finally:
                job_session.release_context()
        model = _interaction_model({}, kwargs)
        session = _session(
            instance,
            operation="google.genai.interactions.create",
            component="external",
            model=model,
            event_type="llm_call",
        )
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    response = await wrapped(*args, **kwargs)
            except Exception as exc:
                session.fail(exc)
                raise
            if kwargs.get("stream") is True:
                meter = _InteractionStreamMeter(kwargs)
                session.release_context()
                return AsyncProviderStream(
                    response,
                    session,
                    observe=meter.observe,
                    measurement=meter.measurement,
                    completion_status=meter.status,
                )
            try:
                measurement = _interaction_measurement(response, kwargs)
            except Exception:
                _log.debug(
                    "dexcost: failed to extract async Google Interaction usage",
                    exc_info=True,
                )
                measurement = _unknown_measurement(model, vertex=False)
            session.finish(measurement, _interaction_status(response))
            return response
        finally:
            session.release_context()

    return invoke()


def _sync_interaction_get(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    with suppress_network_event():
        response = wrapped(*args, **kwargs)
    if kwargs.get("stream") is True:
        meter = _InteractionStreamMeter(kwargs)
        return SyncProviderJobStream(
            response,
            observe=meter.observe,
            complete=lambda: (
                _reconcile_interaction(instance, meter.terminal, args, kwargs)
                if meter.terminal is not None
                else None
            ),
        )
    _reconcile_interaction(instance, response, args, kwargs)
    return response


def _async_interaction_get(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            response = await wrapped(*args, **kwargs)
        if kwargs.get("stream") is True:
            meter = _InteractionStreamMeter(kwargs)
            return AsyncProviderJobStream(
                response,
                observe=meter.observe,
                complete=lambda: (
                    _reconcile_interaction(instance, meter.terminal, args, kwargs)
                    if meter.terminal is not None
                    else None
                ),
            )
        _reconcile_interaction(instance, response, args, kwargs)
        return response

    return invoke()


def _sync_interaction_cancel(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    with suppress_network_event():
        response = wrapped(*args, **kwargs)
    _reconcile_interaction(instance, response, args, kwargs)
    return response


def _async_interaction_cancel(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            response = await wrapped(*args, **kwargs)
        _reconcile_interaction(instance, response, args, kwargs)
        return response

    return invoke()


def _sync_generate_videos(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    model = _model_name(args, kwargs)
    session = _job_session(
        instance,
        operation="google.genai.models.generate_videos",
        model=model,
        billing_dimensions=_video_billing_dimensions(kwargs),
    )
    if session is None:
        return wrapped(*args, **kwargs)
    # Only bounded billing controls survive; prompts and source media never do.
    try:
        try:
            with suppress_network_event():
                operation = wrapped(*args, **kwargs)
        except Exception as exc:
            session.fail(exc)
            raise
        try:
            _submit_video_operation(
                session,
                operation,
                model=model,
                vertex=_is_vertex(instance),
            )
        except Exception:
            _log.debug("dexcost: failed to persist Google video job", exc_info=True)
        return operation
    finally:
        session.release_context()


def _async_generate_videos(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        model = _model_name(args, kwargs)
        session = _job_session(
            instance,
            operation="google.genai.models.generate_videos",
            model=model,
            billing_dimensions=_video_billing_dimensions(kwargs),
        )
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    operation = await wrapped(*args, **kwargs)
            except Exception as exc:
                session.fail(exc)
                raise
            try:
                _submit_video_operation(
                    session,
                    operation,
                    model=model,
                    vertex=_is_vertex(instance),
                )
            except Exception:
                _log.debug(
                    "dexcost: failed to persist async Google video job",
                    exc_info=True,
                )
            return operation
        finally:
            session.release_context()

    return invoke()


def _sync_operation_get(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    with suppress_network_event():
        operation = wrapped(*args, **kwargs)
    _reconcile_video_operation(instance, operation)
    return operation


def _async_operation_get(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            operation = await wrapped(*args, **kwargs)
        _reconcile_video_operation(instance, operation)
        return operation

    return invoke()


def _sync_batch_create(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    model = _model_name(args, kwargs)
    session = _job_session(
        instance,
        operation="google.genai.batches.create",
        model=model,
        billing_dimensions=_batch_billing_dimensions(kwargs),
    )
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
            _submit_batch_job(
                session,
                resource,
                model=model,
                vertex=_is_vertex(instance),
            )
        except Exception:
            _log.debug("dexcost: failed to persist Google batch job", exc_info=True)
        return resource
    finally:
        session.release_context()


def _async_batch_create(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        model = _model_name(args, kwargs)
        session = _job_session(
            instance,
            operation="google.genai.batches.create",
            model=model,
            billing_dimensions=_batch_billing_dimensions(kwargs),
        )
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
                _submit_batch_job(
                    session,
                    resource,
                    model=model,
                    vertex=_is_vertex(instance),
                )
            except Exception:
                _log.debug(
                    "dexcost: failed to persist async Google batch job",
                    exc_info=True,
                )
            return resource
        finally:
            session.release_context()

    return invoke()


def _sync_tuning_create(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    model = _model_name(args, {"model": kwargs.get("base_model")})
    session = _job_session(
        instance,
        operation="google.genai.tunings.tune",
        model=model,
        event_type="external_cost",
        billing_dimensions=_tuning_billing_dimensions(kwargs),
    )
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
            _submit_tuning_job(
                session,
                resource,
                model=model,
                vertex=_is_vertex(instance),
            )
        except Exception:
            _log.debug("dexcost: failed to persist Google tuning job", exc_info=True)
        return resource
    finally:
        session.release_context()


def _async_tuning_create(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        model = _model_name(args, {"model": kwargs.get("base_model")})
        session = _job_session(
            instance,
            operation="google.genai.tunings.tune",
            model=model,
            event_type="external_cost",
            billing_dimensions=_tuning_billing_dimensions(kwargs),
        )
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
                _submit_tuning_job(
                    session,
                    resource,
                    model=model,
                    vertex=_is_vertex(instance),
                )
            except Exception:
                _log.debug(
                    "dexcost: failed to persist async Google tuning job",
                    exc_info=True,
                )
            return resource
        finally:
            session.release_context()

    return invoke()


def _sync_batch_get(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    with suppress_network_event():
        resource = wrapped(*args, **kwargs)
    _reconcile_batch_job(instance, resource)
    return resource


def _async_batch_get(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        _reconcile_batch_job(instance, resource)
        return resource

    return invoke()


def _sync_tuning_get(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    with suppress_network_event():
        resource = wrapped(*args, **kwargs)
    _reconcile_tuning_job(instance, resource)
    return resource


def _async_tuning_get(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        _reconcile_tuning_job(instance, resource)
        return resource

    return invoke()


def _sync_job_control(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    # Batch/tuning cancellation is best effort. The acknowledgement is not a
    # terminal provider state; a later get() response performs reconciliation.
    with suppress_network_event():
        return wrapped(*args, **kwargs)


def _async_job_control(
    wrapped: Any,
    instance: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            return await wrapped(*args, **kwargs)

    return invoke()


def _content_extract(
    response: object, kwargs: dict[str, Any], vertex: bool
) -> OperationMeasurement:
    return _content_measurement(response, kwargs, vertex=vertex)


def _embedding_extract(
    response: object, kwargs: dict[str, Any], vertex: bool
) -> OperationMeasurement:
    return _embedding_measurement(response, kwargs, vertex=vertex)


def _image_extract(operation: str) -> MeasurementExtractor:
    def extract(response: object, kwargs: dict[str, Any], vertex: bool) -> OperationMeasurement:
        return _image_measurement(response, kwargs, operation=operation, vertex=vertex)

    return extract


def _sync_direct_wrapper(
    operation: str, *, event_type: str, extract: MeasurementExtractor
) -> Callable[..., Any]:
    def wrapper(
        wrapped: Any,
        instance: object,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        return _sync_direct_call(
            wrapped,
            instance,
            args,
            kwargs,
            operation=operation,
            component="external",
            event_type=event_type,
            extract=extract,
        )

    return wrapper


def _async_direct_wrapper(
    operation: str, *, event_type: str, extract: MeasurementExtractor
) -> Callable[..., Any]:
    def wrapper(
        wrapped: Any,
        instance: object,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        return _async_direct_call(
            wrapped,
            instance,
            args,
            kwargs,
            operation=operation,
            component="external",
            event_type=event_type,
            extract=extract,
        )

    return wrapper


_DIRECT_METHODS: tuple[tuple[str, str, str, MeasurementExtractor], ...] = (
    (
        "generate_content",
        "google.genai.models.generate_content",
        "llm_call",
        _content_extract,
    ),
    (
        "embed_content",
        "google.genai.models.embed_content",
        "external_cost",
        _embedding_extract,
    ),
    (
        "generate_images",
        "google.genai.models.generate_images",
        "external_cost",
        _image_extract("generate_images"),
    ),
    (
        "upscale_image",
        "google.genai.models.upscale_image",
        "external_cost",
        _image_extract("upscale_image"),
    ),
    (
        "edit_image",
        "google.genai.models.edit_image",
        "external_cost",
        _image_extract("edit_image"),
    ),
    (
        "recontext_image",
        "google.genai.models.recontext_image",
        "external_cost",
        _image_extract("recontext_image"),
    ),
    (
        "segment_image",
        "google.genai.models.segment_image",
        "external_cost",
        _image_extract("segment_image"),
    ),
)


def _patch_interactions() -> None:
    """Patch Google Interactions foreground and durable background methods."""
    resources: list[tuple[str, Any, str, str]] = []
    try:
        from google.genai._interactions.resources import interactions
    except ImportError:
        pass
    else:
        resources.extend(
            (
                (
                    "google.genai._interactions.resources.interactions",
                    interactions,
                    "InteractionsResource",
                    "sync",
                ),
                (
                    "google.genai._interactions.resources.interactions",
                    interactions,
                    "AsyncInteractionsResource",
                    "async",
                ),
            )
        )
    try:
        from google.genai._gaos import google_genai
    except ImportError:
        pass
    else:
        resources.extend(
            (
                (
                    "google.genai._gaos.google_genai",
                    google_genai,
                    "GeminiNextGenInteractions",
                    "sync",
                ),
                (
                    "google.genai._gaos.google_genai",
                    google_genai,
                    "AsyncGeminiNextGenInteractions",
                    "async",
                ),
            )
        )

    sync_wrappers = {
        "create": _sync_interaction_create,
        "get": _sync_interaction_get,
        "cancel": _sync_interaction_cancel,
    }
    async_wrappers = {
        "create": _async_interaction_create,
        "get": _async_interaction_get,
        "cancel": _async_interaction_cancel,
    }
    for module_name, module, class_name, mode in resources:
        owner = getattr(module, class_name, None)
        if owner is None:
            continue
        wrappers = sync_wrappers if mode == "sync" else async_wrappers
        for method, wrapper in wrappers.items():
            if not hasattr(owner, method):
                continue
            original = getattr(owner, method)
            _extra_originals.append((owner, method, original))
            wrapt.wrap_function_wrapper(
                module_name,
                f"{class_name}.{method}",
                provider_capture_wrapper("google_genai", wrapper),
            )


def _patch_operations() -> None:
    """Patch the official operation poller used by Google long-running jobs."""
    try:
        from google.genai import operations
    except ImportError:
        return
    for class_name, wrapper in (
        ("Operations", _sync_operation_get),
        ("AsyncOperations", _async_operation_get),
    ):
        owner = getattr(operations, class_name, None)
        if owner is None or not hasattr(owner, "get"):
            continue
        original = owner.get
        _extra_originals.append((owner, "get", original))
        wrapt.wrap_function_wrapper(
            "google.genai.operations",
            f"{class_name}.get",
            provider_capture_wrapper("google_genai", wrapper),
        )


def _patch_durable_resources() -> None:
    """Patch official batch and tuning lifecycle resource methods."""
    try:
        from google.genai import batches, tunings
    except ImportError:
        return
    resources: tuple[tuple[str, Any, str, tuple[tuple[str, Callable[..., Any]], ...]], ...] = (
        (
            "google.genai.batches",
            batches,
            "Batches",
            (
                ("create", _sync_batch_create),
                ("get", _sync_batch_get),
                ("cancel", _sync_job_control),
            ),
        ),
        (
            "google.genai.batches",
            batches,
            "AsyncBatches",
            (
                ("create", _async_batch_create),
                ("get", _async_batch_get),
                ("cancel", _async_job_control),
            ),
        ),
        (
            "google.genai.tunings",
            tunings,
            "Tunings",
            (
                ("tune", _sync_tuning_create),
                ("get", _sync_tuning_get),
                ("cancel", _sync_job_control),
            ),
        ),
        (
            "google.genai.tunings",
            tunings,
            "AsyncTunings",
            (
                ("tune", _async_tuning_create),
                ("get", _async_tuning_get),
                ("cancel", _async_job_control),
            ),
        ),
    )
    for module_name, module, class_name, methods in resources:
        owner = getattr(module, class_name, None)
        if owner is None:
            continue
        for method, wrapper in methods:
            if not hasattr(owner, method):
                continue
            original = getattr(owner, method)
            _extra_originals.append((owner, method, original))
            wrapt.wrap_function_wrapper(
                module_name,
                f"{class_name}.{method}",
                provider_capture_wrapper("google_genai", wrapper),
            )


def instrument_gemini(tracker: Any) -> None:
    """Instrument current sync and async Google Gen AI inference methods."""
    global _active_tracker, _patched
    if _patched:
        raise RuntimeError(
            "Gemini instrumentation is already active. "
            "Call uninstrument_gemini() before re-instrumenting."
        )
    try:
        from google.genai import models
    except ImportError as exc:
        raise ImportError(
            "The 'google-genai' package is required for Gemini auto-instrumentation. "
            "Install it with: pip install google-genai"
        ) from exc

    _active_tracker = tracker
    try:
        for class_name in ("Models", "AsyncModels"):
            owner = getattr(models, class_name, None)
            if owner is None:
                continue
            async_owner = class_name == "AsyncModels"
            if hasattr(owner, "generate_videos"):
                video_key = f"{class_name}.generate_videos"
                _originals[video_key] = owner.generate_videos
                wrapt.wrap_function_wrapper(
                    "google.genai.models",
                    video_key,
                    provider_capture_wrapper(
                        "google_genai",
                        _async_generate_videos if async_owner else _sync_generate_videos,
                    ),
                )
            for method, operation, event_type, extract in _DIRECT_METHODS:
                if not hasattr(owner, method):
                    continue
                key = f"{class_name}.{method}"
                _originals[key] = getattr(owner, method)
                wrapper = (
                    _async_direct_wrapper(operation, event_type=event_type, extract=extract)
                    if async_owner
                    else _sync_direct_wrapper(operation, event_type=event_type, extract=extract)
                )
                wrapt.wrap_function_wrapper(
                    "google.genai.models",
                    key,
                    provider_capture_wrapper("google_genai", wrapper),
                )

            stream_method = "generate_content_stream"
            if hasattr(owner, stream_method):
                key = f"{class_name}.{stream_method}"
                _originals[key] = getattr(owner, stream_method)
                wrapt.wrap_function_wrapper(
                    "google.genai.models",
                    key,
                    provider_capture_wrapper(
                        "google_genai",
                        _async_stream_call if async_owner else _sync_stream_call,
                    ),
                )
        _patch_interactions()
        _patch_operations()
        _patch_durable_resources()
    except Exception:
        _restore_originals(models)
        _active_tracker = None
        raise
    _patched = True


def _restore_originals(models: Any) -> None:
    for key, original in _originals.items():
        class_name, method = key.split(".", 1)
        owner = getattr(models, class_name, None)
        if owner is not None:
            setattr(owner, method, original)
    _originals.clear()
    while _extra_originals:
        owner, method, original = _extra_originals.pop()
        setattr(owner, method, original)


def uninstrument_gemini() -> None:
    """Restore every Google Gen AI method patched by :func:`instrument_gemini`."""
    global _active_tracker, _patched
    if not _patched and not _originals and not _extra_originals:
        return
    try:
        from google.genai import models
    except ImportError:
        _originals.clear()
        _extra_originals.clear()
    else:
        _restore_originals(models)
    _active_tracker = None
    _patched = False


__all__ = ["instrument_gemini", "uninstrument_gemini"]
