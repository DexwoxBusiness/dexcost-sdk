"""Convert durable v1 Python capture into strict attribution-v3 observations."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from dexcost.attribution.convert import (
    _component_and_usage,
    _decimal_detail,
    _evidence_for,
    _positive_quantity,
    _provider_for,
    _resource_for,
    _string_detail,
)
from dexcost.attribution.types import (
    ATTRIBUTION_COMPONENTS,
    ATTRIBUTION_UNIT_BY_METRIC,
    AttributionComponent,
)
from dexcost.attribution.v3_types import (
    AttributionBillingDimension,
    AttributionBillingDimensionValue,
    AttributionEventV3,
    AttributionOperationErrorV3,
    AttributionOperationIdentityV3,
    AttributionOperationStatusV3,
    AttributionResourceV3,
    AttributionUsageLineV3,
)
from dexcost.attribution.v3_validate import validate_attribution_observation_v3
from dexcost.models._serde import iso_canonical
from dexcost.models.event import Event

_log = logging.getLogger(__name__)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
_CANONICAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._{}/*^+-]{0,63}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_INTEGER = re.compile(r"^-?(?:0|[1-9]\d{0,25})$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$")
_ENVIRONMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_NON_CANONICAL_ERROR = re.compile(r"[^a-z0-9._-]")
_LEADING_ERROR_JUNK = re.compile(r"^[^a-z0-9]+")
_MAX_ERROR_TYPE_LENGTH = 127
_MAX_ERROR_CODE_LENGTH = 64
_MAX_LATENCY_MS = 86_400_000
# ``resource.type`` accepted by the v3 contract after the in-place extension.
_V3_RESOURCE_TYPES = frozenset(
    {"model", "sku", "instance", "endpoint", "session", "other", "tool"}
)
_COMPONENTS = set(ATTRIBUTION_COMPONENTS)
_OPERATION_NAMES: dict[str, str] = {
    "llm_call": "llm.call",
    "external_cost": "external.call",
    "compute_cost": "compute.consume",
    "gpu_cost": "gpu.consume",
    "gpu_utilization_signal": "gpu.observe",
    "network": "network.transfer",
    "retry_marker": "retry.attempt",
}


def _number_detail(details: dict[str, Any], *keys: str) -> float | None:
    decimal = _decimal_detail(details, *keys)
    if decimal is None:
        return None
    try:
        return float(decimal)
    except (OverflowError, ValueError):
        return None


def _normalized_environment(value: object) -> str | None:
    """Return the canonical deployment environment, or ``None`` to omit it.

    The SDK never raises into user code: a value that cannot satisfy the
    server charset is dropped with a warning rather than failing the push.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        _log.warning("dexcost: environment=%r dropped — must be a string", value)
        return None
    candidate = value.strip().lower()
    if not candidate:
        return None
    if _ENVIRONMENT.fullmatch(candidate) is None:
        _log.warning(
            "dexcost: environment=%r dropped — must match %s",
            value,
            _ENVIRONMENT.pattern,
        )
        return None
    return candidate


def _latency_ms(event: Event) -> int | None:
    """Return the operation latency in whole milliseconds, clamped to range.

    ``None`` is returned when the event carries no usable latency, so the
    optional wire field is simply omitted.
    """
    raw: object = getattr(event, "latency_ms", None)
    if raw is None:
        raw = _decimal_detail(event.details, "latency_ms")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        latency = raw
    elif isinstance(raw, (float, Decimal)):
        try:
            latency = int(Decimal(str(raw)).to_integral_value(rounding=ROUND_HALF_UP))
        except (ArithmeticError, ValueError):
            return None
    else:
        return None
    return max(0, min(latency, _MAX_LATENCY_MS))


def _deterministic_uuid(namespace: str, *parts: str) -> str:
    digest = bytearray(
        hashlib.sha256("\0".join((namespace, *parts)).encode("utf-8")).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _parse_dimension_value(value: object) -> AttributionBillingDimensionValue | None:
    if not isinstance(value, dict):
        return None
    value_type = value.get("type")
    raw_value = value.get("value")
    if value_type == "string" and isinstance(raw_value, str) and 1 <= len(raw_value) <= 256:
        return {"type": "string", "value": raw_value}
    if value_type == "boolean" and isinstance(raw_value, bool):
        return {"type": "boolean", "value": raw_value}
    if value_type == "integer" and isinstance(raw_value, str) and _INTEGER.fullmatch(raw_value):
        return {"type": "integer", "value": raw_value}
    if value_type == "decimal" and isinstance(raw_value, str) and _DECIMAL.fullmatch(raw_value):
        return {"type": "decimal", "value": raw_value}
    return None


def _explicit_dimensions(
    details: dict[str, Any],
) -> list[AttributionBillingDimension] | None:
    raw = details.get("attribution_dimensions")
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 24:
        return None
    dimensions: list[AttributionBillingDimension] = []
    for candidate in raw:
        if not isinstance(candidate, dict):
            return None
        key = candidate.get("key")
        if not isinstance(key, str) or _CANONICAL_NAME.fullmatch(key) is None:
            return None
        value = _parse_dimension_value(candidate.get("value"))
        if value is None:
            return None
        dimensions.append({"key": key, "value": value})
    dimensions.sort(key=lambda dimension: dimension["key"])
    return dimensions


def _gpu_signal_usage(
    event: Event,
) -> tuple[
    AttributionComponent,
    list[dict[str, str]],
    Decimal | None,
    list[AttributionBillingDimension],
]:
    details = event.details
    candidates: tuple[tuple[str, object, str], ...] = (
        ("gpu.sm_utilization_percent", details.get("sm_util_pct"), "Percent"),
        ("gpu.memory_utilization_percent", details.get("mem_util_pct"), "Percent"),
        ("gpu.vram_peak_bytes", details.get("vram_used_peak_bytes"), "Bytes"),
        ("gpu.vram_capacity_bytes", details.get("vram_total_bytes"), "Bytes"),
        ("gpu.process_count", details.get("process_count"), "Processes"),
        ("gpu.sample_count", details.get("sample_count"), "Samples"),
    )
    usage: list[dict[str, str]] = []
    for metric, raw_quantity, unit in candidates:
        quantity = _positive_quantity(raw_quantity)
        if quantity is not None:
            usage.append({"metric": metric, "quantity": quantity, "unit": unit})
    dimensions: list[AttributionBillingDimension] = []
    gpu_index = _number_detail(details, "gpu_index")
    if gpu_index is not None and gpu_index.is_integer() and gpu_index >= 0:
        dimensions.append(
            {
                "key": "gpu_index",
                "value": {"type": "integer", "value": str(int(gpu_index))},
            }
        )
    sku = _string_detail(details, "gpu_sku")
    if sku is not None:
        dimensions.append({"key": "gpu_sku", "value": {"type": "string", "value": sku[:256]}})
    duration_ms = _decimal_detail(details, "task_duration_ms")
    duration_seconds = (
        duration_ms / Decimal(1000) if duration_ms is not None and duration_ms > 0 else None
    )
    return "gpu", usage, duration_seconds, dimensions


def _selected_component(
    event: Event, fallback: AttributionComponent
) -> AttributionComponent | None:
    explicit = _string_detail(event.details, "attribution_component")
    if explicit is None:
        return fallback
    if explicit not in _COMPONENTS:
        return None
    return cast(AttributionComponent, explicit)


def _operation_name(event: Event) -> str:
    explicit = _string_detail(event.details, "attribution_operation_name")
    if explicit is not None and _CANONICAL_NAME.fullmatch(explicit):
        return cast(str, explicit)
    return _OPERATION_NAMES.get(event.event_type, "external.call")


def _operation_status(event: Event) -> AttributionOperationStatusV3:
    explicit = _string_detail(event.details, "attribution_operation_status")
    if explicit in {"in_progress", "succeeded", "failed", "cancelled", "unknown"}:
        return cast(AttributionOperationStatusV3, explicit)
    if event.event_type == "gpu_utilization_signal":
        return "unknown"
    if event.event_type == "retry_marker" or _string_detail(event.details, "error_type"):
        return "failed"
    return "succeeded"


def _valid_uuid(value: str | None) -> bool:
    return value is not None and _UUID.fullmatch(value) is not None


def _error_code(details: dict[str, Any]) -> str | None:
    """Return the optional provider error code, bounded to the wire limit."""
    for key in ("attribution_error_code", "error_code"):
        value = details.get(key)
        if value is None or isinstance(value, bool):
            continue
        if not isinstance(value, (str, int)):
            continue
        code = str(value).strip()
        if code:
            return code[:_MAX_ERROR_CODE_LENGTH]
    return None


def _operation_error(event: Event) -> AttributionOperationErrorV3 | None:
    """Return ``operation.error`` for a non-succeeded operation, if identifiable.

    The error type is canonicalised here rather than at the call sites so that
    *any* producer of ``details["error_type"]`` — auto-instruments, the manual
    ``record_llm_call(error_type=...)`` API, integrations — lands on a value the
    server accepts.
    """
    raw_type = _string_detail(event.details, "attribution_error_type", "error_type")
    if raw_type is None:
        return None
    error_type = _NON_CANONICAL_ERROR.sub("_", raw_type.strip().lower())
    error_type = _LEADING_ERROR_JUNK.sub("", error_type)[:_MAX_ERROR_TYPE_LENGTH]
    if not error_type or _CANONICAL_NAME.fullmatch(error_type) is None:
        return None
    error: AttributionOperationErrorV3 = {"type": error_type}
    code = _error_code(event.details)
    if code is not None:
        error["code"] = code
    return error


def _operation_for(event: Event) -> AttributionOperationIdentityV3 | None:
    explicit_operation_id = _string_detail(event.details, "attribution_operation_id")
    retry_of = str(event.retry_of).lower() if event.retry_of is not None else None
    has_valid_operation_id = _valid_uuid(explicit_operation_id)
    operation_id = (
        cast(str, explicit_operation_id).lower()
        if has_valid_operation_id
        else str(event.event_id).lower()
    )
    explicit_attempt_id = _string_detail(event.details, "attribution_attempt_id")
    attempt_id = (
        cast(str, explicit_attempt_id).lower()
        if _valid_uuid(explicit_attempt_id)
        else str(event.event_id).lower()
    )
    explicit_attempt = _decimal_detail(event.details, "attribution_attempt_number")
    has_valid_attempt = (
        explicit_attempt is not None
        and explicit_attempt == explicit_attempt.to_integral_value()
        and explicit_attempt > 0
        and explicit_attempt <= 2_147_483_647
    )
    if retry_of is not None and (
        not _valid_uuid(retry_of)
        or not has_valid_operation_id
        or not has_valid_attempt
        or cast(Decimal, explicit_attempt) <= 1
    ):
        return None
    attempt_number = int(cast(Decimal, explicit_attempt)) if has_valid_attempt else 1
    operation: AttributionOperationIdentityV3 = {
        "id": operation_id,
        "name": _operation_name(event),
        "status": _operation_status(event),
        "attempt": {"id": attempt_id, "number": attempt_number},
    }
    if retry_of is not None:
        operation["attempt"]["retry_of"] = retry_of
    # The server rejects an observation that carries an error on a succeeded
    # operation, so the guard lives here rather than at the call sites.
    if operation["status"] != "succeeded":
        error = _operation_error(event)
        if error is not None:
            operation["error"] = error
    latency_ms = _latency_ms(event)
    if latency_ms is not None:
        operation["latency_ms"] = latency_ms
    trace_id = _string_detail(event.details, "trace_id")
    span_id = _string_detail(event.details, "span_id")
    if trace_id is not None and span_id is not None:
        trace_id = trace_id.lower()
        span_id = span_id.lower()
        if _TRACE_ID.fullmatch(trace_id) and _SPAN_ID.fullmatch(span_id):
            operation["trace"] = {"trace_id": trace_id, "span_id": span_id}
    return operation


def _resource_for_v3(event: Event) -> AttributionResourceV3 | None:
    """Return the v3 resource identity.

    Identical to v2 apart from the ``"tool"`` type added by the in-place v3
    extension, which the shared v2 helper cannot emit.
    """
    explicit_type = _string_detail(event.details, "attribution_resource_type")
    explicit_id = _string_detail(event.details, "attribution_resource_id")
    if explicit_id and explicit_type == "tool":
        return {"type": cast(Any, "tool"), "id": explicit_id[:256]}
    resource = _resource_for(event)
    if resource is not None and resource.get("type") not in _V3_RESOURCE_TYPES:
        return None
    return cast("AttributionResourceV3 | None", resource)


def _unknown_explicit_usage(
    event: Event,
) -> tuple[AttributionComponent, list[dict[str, str]], Decimal | None] | None:
    if event.event_type != "external_cost":
        return None
    metric = _string_detail(event.details, "attribution_usage_metric")
    if (
        metric is None
        or metric in ATTRIBUTION_UNIT_BY_METRIC
        or _CANONICAL_NAME.fullmatch(metric) is None
    ):
        return None
    unit = _string_detail(event.details, "attribution_usage_unit")
    quantity = _positive_quantity(event.details.get("attribution_usage_quantity"))
    if unit is None or _UNIT.fullmatch(unit) is None or quantity is None:
        return None
    return (
        "external",
        [{"metric": metric, "quantity": quantity, "unit": unit}],
        _decimal_detail(event.details, "attribution_usage_duration_seconds"),
    )


def to_attribution_observation_v3(
    event: Event,
    *,
    environment: str | None = None,
) -> AttributionEventV3 | None:
    """Convert one durable SDK event into a strict, details-free v3 observation.

    ``environment`` is the configured deployment environment
    (``dexcost.init(environment=...)`` / ``DEXCOST_ENV``). It is normalised
    to lower case and dropped — never raised — when it cannot satisfy the
    server charset.
    """
    explicit = _unknown_explicit_usage(event)
    gpu_signal = _gpu_signal_usage(event) if event.event_type == "gpu_utilization_signal" else None
    legacy = _component_and_usage(event) if explicit is None and gpu_signal is None else None
    mapped = explicit or gpu_signal or legacy
    if mapped is None:
        return None

    component, mapped_usage, duration_seconds = mapped[:3]
    explicit_dimensions = _explicit_dimensions(event.details)
    if explicit_dimensions is None:
        _log.warning("Event %s has invalid attribution_dimensions", event.event_id)
        return None
    dimensions = list(explicit_dimensions)
    if gpu_signal is not None:
        dimensions.extend(gpu_signal[3])
        dimensions.sort(key=lambda dimension: dimension["key"])
    stable_dimensions = json.dumps(dimensions, ensure_ascii=False, separators=(",", ":"))
    event_id = str(event.event_id).lower()
    usage: list[AttributionUsageLineV3] = [
        {
            "line_id": _deterministic_uuid(
                "dexcost:attribution-usage-line:v3",
                event_id,
                str(line["metric"]),
                str(line["unit"]),
                stable_dimensions,
            ),
            "metric": str(line["metric"]),
            "quantity": str(line["quantity"]),
            "unit": str(line["unit"]),
            "dimensions": dimensions,
        }
        for line in mapped_usage
    ]
    selected_component = _selected_component(event, component)
    if selected_component is None:
        _log.warning("Event %s has an invalid attribution_component", event.event_id)
        return None
    operation = _operation_for(event)
    if operation is None:
        _log.warning("Event %s has invalid or incomplete retry lineage", event.event_id)
        return None
    occurred_at = iso_canonical(event.occurred_at)
    converted: AttributionEventV3 = {
        "schema_version": "3",
        "event_id": event_id,
        "task_id": str(event.task_id).lower(),
        "occurred_at": occurred_at,
        "observed_at": occurred_at,
        "component": selected_component,
        "provider": _provider_for(event),
        "operation": operation,
        "lifecycle": {"state": "final", "revision": 1},
        "usage_snapshot": "full",
        "usage": usage,
    }
    normalized_environment = _normalized_environment(environment)
    if normalized_environment is not None:
        converted["environment"] = normalized_environment
    resource = _resource_for_v3(event)
    if resource is not None:
        converted["resource"] = cast(Any, resource)
    if event.event_type != "gpu_utilization_signal":
        evidence = _evidence_for(event)
        if evidence is not None:
            converted["cost_evidence"] = evidence
    has_time_based_usage = any(
        line["metric"] in ATTRIBUTION_UNIT_BY_METRIC
        and line["unit"] == ATTRIBUTION_UNIT_BY_METRIC[cast(Any, line["metric"])]
        and line["unit"].endswith("Seconds")
        for line in usage
    )
    if has_time_based_usage or (duration_seconds is not None and duration_seconds > 0):
        offset_microseconds = 0
        if duration_seconds is not None and duration_seconds > 0:
            offset_microseconds = int(
                (duration_seconds * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP)
            )
        converted["usage_period"] = {
            "start_at": iso_canonical(
                event.occurred_at - timedelta(microseconds=offset_microseconds)
            ),
            "end_at": occurred_at,
        }

    validation = validate_attribution_observation_v3(converted)
    if not validation.success:
        _log.warning(
            "Event %s cannot be represented by attribution v3: %s",
            event.event_id,
            ", ".join(issue.path or "<root>" for issue in validation.issues),
        )
        return None
    return converted


to_attribution_event_v3 = to_attribution_observation_v3
