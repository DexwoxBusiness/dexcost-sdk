"""Convert durable v1 SDK capture into strict attribution-v2 wire records."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, cast

from dexcost.attribution.types import (
    ATTRIBUTION_UNIT_BY_METRIC,
    AttributionComponent,
    AttributionCostEvidenceSource,
    AttributionCostEvidenceV2,
    AttributionEventV2,
    AttributionProviderIdentityV2,
    AttributionResourceV2,
    AttributionTaskIngestV1,
    AttributionUsageLineV2,
    AttributionUsageMetric,
)
from dexcost.attribution.validate import validate_attribution_event_v2
from dexcost.models._serde import canonical_decimal, iso_canonical
from dexcost.models.event import Event
from dexcost.models.task import Task

_log = logging.getLogger(__name__)
_GIB = Decimal(1024) ** 3
_TWELVE_PLACES = Decimal("0.000000000001")


def _decimal_detail(details: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = details.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float, Decimal, str)):
            try:
                parsed = Decimal(str(value).strip())
                if parsed.is_finite():
                    return parsed
            except (InvalidOperation, ValueError):
                continue
    return None


def _string_detail(details: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _canonical_name(value: str | None, fallback: str) -> str:
    normalized = (value or "").strip().lower()
    normalized = re.sub(r"^https?://", "", normalized)
    normalized = re.sub(r"[^a-z0-9._-]+", "_", normalized)
    normalized = re.sub(r"^[_\-.]+|[_\-.]+$", "", normalized)[:128]
    return normalized or fallback


def _positive_quantity(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
        if not decimal.is_finite() or decimal <= 0:
            return None
        exponent = cast(int, decimal.as_tuple().exponent)
        if exponent < -12:
            decimal = decimal.quantize(_TWELVE_PLACES, rounding=ROUND_HALF_UP)
        if decimal <= 0:
            return None
        return str(canonical_decimal(decimal))
    except (InvalidOperation, ValueError):
        return None


def _usage_line(metric: AttributionUsageMetric, quantity: object) -> AttributionUsageLineV2 | None:
    normalized = _positive_quantity(quantity)
    if normalized is None:
        return None
    return {
        "metric": metric,
        "quantity": normalized,
        "unit": ATTRIBUTION_UNIT_BY_METRIC[metric],
    }


def _compact_usage(
    lines: list[AttributionUsageLineV2 | None],
) -> list[AttributionUsageLineV2]:
    return [line for line in lines if line is not None]


def _provider_for(event: Event) -> AttributionProviderIdentityV2:
    raw = (event.provider or "").lower()
    name = _canonical_name(event.provider, "unknown")
    service = "api"
    if "openai" in raw:
        name, service = "openai", "responses"
    elif "anthropic" in raw:
        name, service = "anthropic", "messages"
    elif "bedrock" in raw:
        name, service = "aws", "bedrock"
    elif "gemini" in raw or raw == "google":
        name, service = "google", "generate_content"
    elif "cohere" in raw:
        name, service = "cohere", "chat"
    elif "vercel" in raw:
        name, service = "vercel", "ai_sdk"
    elif "langchain" in raw:
        name, service = "langchain", "chat"

    if event.event_type != "llm_call":
        billing_model = _string_detail(event.details, "billing_model")
        service_name = event.service_name
        if event.event_type == "compute_cost":
            if billing_model and billing_model.startswith("azure"):
                name = "azure"
            elif billing_model in {"gce", "cloud_functions"} or (
                billing_model and billing_model.startswith("cloud_")
            ):
                name = "google_cloud"
            elif billing_model == "vercel_fluid":
                name = "vercel"
            elif billing_model == "k8s_pod":
                name = "kubernetes"
            elif billing_model in {"lambda", "fargate", "ec2"}:
                name = "aws"
            else:
                name = _canonical_name(event.provider, "runtime")
            service = _canonical_name(billing_model or service_name, "compute")
        elif event.event_type == "gpu_cost":
            name = _canonical_name(
                _string_detail(event.details, "cloud_provider") or event.provider, "runtime"
            )
            service = _canonical_name(billing_model, "gpu")
        elif event.event_type == "network":
            name = _canonical_name(
                _string_detail(event.details, "cloud_provider") or event.provider, "internet"
            )
            service = "egress"
        else:
            raw_service = service_name or "external"
            if raw_service.startswith("mcp:"):
                name = "mcp"
                service = _canonical_name(raw_service[4:], "tool")
            elif "." in raw_service:
                name = _canonical_name(raw_service, "external")
                service = "http_api"
            else:
                name = _canonical_name(event.provider, _canonical_name(raw_service, "external"))
                service = _canonical_name(raw_service, "api")

    provider: AttributionProviderIdentityV2 = {"name": name, "service": service}
    record_id = _string_detail(event.details, "provider_record_id", "request_id", "call_sid")
    region = _string_detail(event.details, "region", "cloud_region")
    if record_id is not None and len(record_id) <= 256:
        provider["record_id"] = record_id
    if region is not None:
        provider["region"] = _canonical_name(region, "unknown")
    return provider


def _resource_for(event: Event) -> AttributionResourceV2 | None:
    if event.model:
        return {"type": "model", "id": event.model[:256]}
    if event.event_type == "gpu_cost":
        sku = _string_detail(event.details, "gpu_sku", "instance_type")
        if sku:
            return {"type": "sku", "id": sku[:256]}
    if event.event_type == "compute_cost":
        instance = _string_detail(event.details, "instance_type", "architecture")
        if instance:
            return {"type": "instance", "id": instance[:256]}
    return None


def _evidence_for(event: Event) -> AttributionCostEvidenceV2 | None:
    amount = _positive_quantity(event.cost_usd)
    if amount is None:
        return None
    source = event.pricing_source
    if source == "provider_response":
        return {
            "amount": amount,
            "currency": "USD",
            "source": "provider_reported",
            "confidence": "exact" if event.cost_confidence == "exact" else "estimated",
        }
    if source in {"manual", "custom", "rate_per_minute"}:
        return {
            "amount": amount,
            "currency": "USD",
            "source": "manual",
            "confidence": cast(Any, event.cost_confidence),
        }
    is_sdk_catalog = source in {"service_catalog", "litellm", "tokencost"} or bool(
        source and source.startswith(("compute_catalog:", "gpu_catalog:", "egress_catalog:"))
    )
    mapped: AttributionCostEvidenceSource | None
    if source == "rate_registry":
        mapped = "sdk_rate_registry"
    elif is_sdk_catalog:
        mapped = "sdk_catalog"
    else:
        mapped = None
    if mapped is None or not event.pricing_version:
        return None
    return {
        "amount": amount,
        "currency": "USD",
        "source": mapped,
        "confidence": cast(
            Any, "computed" if event.cost_confidence == "exact" else event.cost_confidence
        ),
        "pricing_version": event.pricing_version,
    }


def _component_and_usage(
    event: Event,
) -> tuple[AttributionComponent, list[AttributionUsageLineV2], Decimal | None] | None:
    details = event.details
    if event.event_type in {"retry_marker", "gpu_utilization_signal"}:
        return None
    if event.event_type == "llm_call":
        cached = event.cached_tokens or 0
        provider = (event.provider or "").lower()
        cache_counters_are_disjoint = (
            "anthropic" in provider or "bedrock" in provider or provider == "aws"
        )
        input_tokens = event.input_tokens or 0
        if not cache_counters_are_disjoint:
            input_tokens = max(0, input_tokens - cached)
        cache_write = _decimal_detail(details, "cache_creation_input_tokens")
        reasoning = _decimal_detail(details, "reasoning_output_tokens", "reasoning_tokens")
        output_tokens = Decimal(event.output_tokens or 0)
        if reasoning is not None:
            output_tokens = max(Decimal(0), output_tokens - reasoning)
        usage = _compact_usage(
            [
                _usage_line("input_tokens", input_tokens),
                _usage_line("cache_read_input_tokens", cached),
                _usage_line("cache_write_input_tokens", cache_write),
                _usage_line("output_tokens", output_tokens),
                _usage_line("reasoning_output_tokens", reasoning),
            ]
        )
        if not usage:
            usage.append(cast(AttributionUsageLineV2, _usage_line("request_count", 1)))
        return "llm", usage, None
    if event.event_type == "compute_cost":
        duration_ms = _decimal_detail(details, "duration_ms") or Decimal(0)
        duration_seconds = duration_ms / Decimal(1000)
        if duration_seconds == 0:
            duration_seconds = _decimal_detail(details, "wall_clock_seconds") or Decimal(0)
        memory_bytes = _decimal_detail(details, "memory_bytes_limit", "memory_bytes_peak")
        memory_gib_seconds = (
            None if memory_bytes is None else memory_bytes / _GIB * duration_seconds
        )
        return (
            "compute",
            _compact_usage(
                [
                    _usage_line("compute_seconds", duration_seconds),
                    _usage_line("vcpu_seconds", _decimal_detail(details, "vcpu_seconds_used")),
                    _usage_line("memory_gib_seconds", memory_gib_seconds),
                    _usage_line("request_count", _decimal_detail(details, "invocation_count")),
                ]
            ),
            duration_seconds,
        )
    if event.event_type == "gpu_cost":
        duration_seconds = (_decimal_detail(details, "duration_ms") or Decimal(0)) / Decimal(1000)
        measured = _decimal_detail(details, "gpu_seconds_used")
        gpu_count = _decimal_detail(details, "gpu_count") or Decimal(1)
        billing_model = _string_detail(details, "billing_model") or ""
        billed_seconds = (
            measured if billing_model == "per_gpu_second_active" else duration_seconds * gpu_count
        )
        if billed_seconds is None:
            billed_seconds = measured
        return (
            "gpu",
            _compact_usage([_usage_line("gpu_seconds", billed_seconds)]),
            duration_seconds,
        )
    if event.event_type == "network":
        return (
            "network",
            _compact_usage(
                [
                    _usage_line("bytes_out", _decimal_detail(details, "request_bytes")),
                    _usage_line("bytes_in", _decimal_detail(details, "response_bytes")),
                ]
            ),
            None,
        )
    if event.event_type != "external_cost":
        return None
    explicit_quantity = _decimal_detail(details, "attribution_usage_quantity")
    explicit_metric = _string_detail(details, "attribution_usage_metric")
    per = _canonical_name(_string_detail(details, "attribution_usage_per"), "request")
    inferred_metric: AttributionUsageMetric
    if "page" in per:
        inferred_metric = "page_count"
    elif "credit" in per:
        inferred_metric = "credit_count"
    elif "image" in per:
        inferred_metric = "image_count"
    elif "call" in per:
        inferred_metric = "call_count"
    elif "character" in per:
        inferred_metric = "characters"
    else:
        inferred_metric = "request_count"
    metric = (
        cast(AttributionUsageMetric, explicit_metric)
        if explicit_metric in ATTRIBUTION_UNIT_BY_METRIC
        else inferred_metric
    )
    return "external", _compact_usage([_usage_line(metric, explicit_quantity or 1)]), None


def to_attribution_event_v2(event: Event) -> AttributionEventV2 | None:
    """Convert a durable SDK event into a strict, details-free v2 wire event."""
    mapped = _component_and_usage(event)
    if mapped is None:
        return None
    component, usage, duration_seconds = mapped
    if not usage:
        usage.append(cast(AttributionUsageLineV2, _usage_line("request_count", 1)))
    occurred_at = iso_canonical(event.occurred_at)
    converted: AttributionEventV2 = {
        "schema_version": "2",
        "event_id": str(event.event_id),
        "task_id": str(event.task_id),
        "occurred_at": occurred_at,
        "observed_at": occurred_at,
        "component": component,
        "provider": _provider_for(event),
        "lifecycle": {"state": "final", "revision": 1},
        "usage": usage,
    }
    resource = _resource_for(event)
    if resource is not None:
        converted["resource"] = resource
    evidence = _evidence_for(event)
    if evidence is not None:
        converted["cost_evidence"] = evidence
    if event.is_retry and event.retry_of:
        converted["retry_of"] = str(event.retry_of)
    has_time_based_usage = any(line["unit"].endswith("Seconds") for line in usage)
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

    validation = validate_attribution_event_v2(converted)
    if not validation.success:
        _log.warning(
            "Event %s cannot be represented by attribution v2: %s",
            event.event_id,
            ", ".join(issue.path for issue in validation.issues),
        )
        return None
    return converted


def to_attribution_task_ingest_v1(task: Task) -> AttributionTaskIngestV1:
    """Serialize only the task fields accepted by the ingestion boundary."""
    return {
        "task_id": str(task.task_id),
        "task_type": task.task_type,
        "status": cast(Any, task.status),
        "started_at": iso_canonical(task.started_at),
        "ended_at": iso_canonical(task.ended_at) if task.ended_at else None,
        "metadata": cast(dict[str, object], task.metadata),
        "customer_id": task.customer_id,
        "project_id": task.project_id,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "experiment_id": task.experiment_id,
        "variant": task.variant,
        "schema_version": "1",
    }
