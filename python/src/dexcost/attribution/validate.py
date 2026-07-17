"""Non-throwing runtime validation for the control-plane attribution-v2 contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from dexcost.attribution.types import (
    ATTRIBUTION_COMPONENTS,
    ATTRIBUTION_UNIT_BY_METRIC,
    ATTRIBUTION_USAGE_METRICS,
    ATTRIBUTION_USAGE_UNITS,
    AttributionUsageMetric,
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_CANONICAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_POSITIVE_DECIMAL = re.compile(r"^(?=.*[1-9])(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,6}))?(Z|[+-](\d{2}):(\d{2}))$"
)
_RESOURCE_TYPES = {"model", "sku", "instance", "endpoint", "session", "other"}
_LIFECYCLE_STATES = {"pending", "provisional", "final", "voided"}
_EVIDENCE_SOURCES = {"provider_reported", "sdk_catalog", "sdk_rate_registry", "manual"}
_CONFIDENCES = {"exact", "computed", "estimated", "unknown"}
_COMPONENTS = set(ATTRIBUTION_COMPONENTS)
_METRICS = set(ATTRIBUTION_USAGE_METRICS)
_UNITS = set(ATTRIBUTION_USAGE_UNITS)


@dataclass(frozen=True)
class AttributionV2ValidationIssue:
    path: str
    message: str


@dataclass(frozen=True)
class AttributionV2ValidationResult:
    success: bool
    issues: tuple[AttributionV2ValidationIssue, ...]


def _is_record(value: object) -> bool:
    return isinstance(value, dict)


def _add_unknown_keys(
    value: dict[str, Any],
    allowed: set[str],
    prefix: str,
    issues: list[AttributionV2ValidationIssue],
) -> None:
    for key in value:
        if key not in allowed:
            path = f"{prefix}.{key}" if prefix else key
            issues.append(AttributionV2ValidationIssue(path, "Unknown field"))


def _validate_string(
    value: object,
    path: str,
    issues: list[AttributionV2ValidationIssue],
    pattern: re.Pattern[str] | None = None,
) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or (pattern is not None and not pattern.fullmatch(value))
    ):
        issues.append(AttributionV2ValidationIssue(path, "Invalid string value"))
        return False
    return True


def _parse_timestamp(
    value: object, path: str, issues: list[AttributionV2ValidationIssue]
) -> datetime | None:
    if not _validate_string(value, path, issues, _TIMESTAMP):
        return None
    assert isinstance(value, str)
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            raise ValueError("timezone offset required")
        return parsed.astimezone(timezone.utc)
    except ValueError:
        issues.append(
            AttributionV2ValidationIssue(
                path, "Timestamp must be a valid ISO 8601 calendar instant"
            )
        )
        return None


def validate_attribution_event_v2(value: object) -> AttributionV2ValidationResult:
    """Validate an attribution-v2 JSON value without raising."""
    issues: list[AttributionV2ValidationIssue] = []
    if not _is_record(value):
        return AttributionV2ValidationResult(
            False, (AttributionV2ValidationIssue("", "Event must be an object"),)
        )
    event = value
    assert isinstance(event, dict)
    _add_unknown_keys(
        event,
        {
            "schema_version",
            "event_id",
            "task_id",
            "occurred_at",
            "observed_at",
            "component",
            "provider",
            "resource",
            "lifecycle",
            "usage_period",
            "usage",
            "cost_evidence",
            "retry_of",
        },
        "",
        issues,
    )

    if event.get("schema_version") != "2":
        issues.append(AttributionV2ValidationIssue("schema_version", "Must equal 2"))
    _validate_string(event.get("event_id"), "event_id", issues, _UUID)
    _validate_string(event.get("task_id"), "task_id", issues, _UUID)
    _parse_timestamp(event.get("occurred_at"), "occurred_at", issues)
    _parse_timestamp(event.get("observed_at"), "observed_at", issues)
    component = event.get("component")
    if not isinstance(component, str) or component not in _COMPONENTS:
        issues.append(AttributionV2ValidationIssue("component", "Unknown attribution component"))
    if "retry_of" in event:
        _validate_string(event["retry_of"], "retry_of", issues, _UUID)

    provider = event.get("provider")
    if not _is_record(provider):
        issues.append(AttributionV2ValidationIssue("provider", "Provider must be an object"))
    else:
        assert isinstance(provider, dict)
        _add_unknown_keys(provider, {"name", "service", "record_id", "region"}, "provider", issues)
        _validate_string(provider.get("name"), "provider.name", issues, _CANONICAL_NAME)
        _validate_string(provider.get("service"), "provider.service", issues, _CANONICAL_NAME)
        if "record_id" in provider:
            record_id = provider["record_id"]
            if not isinstance(record_id, str) or not 1 <= len(record_id) <= 256:
                issues.append(
                    AttributionV2ValidationIssue(
                        "provider.record_id", "Invalid provider record ID"
                    )
                )
        if "region" in provider:
            _validate_string(provider["region"], "provider.region", issues, _CANONICAL_NAME)

    resource = event.get("resource")
    if "resource" in event:
        if not _is_record(resource):
            issues.append(AttributionV2ValidationIssue("resource", "Resource must be an object"))
        else:
            assert isinstance(resource, dict)
            _add_unknown_keys(resource, {"type", "id"}, "resource", issues)
            if resource.get("type") not in _RESOURCE_TYPES:
                issues.append(
                    AttributionV2ValidationIssue("resource.type", "Invalid resource type")
                )
            resource_id = resource.get("id")
            if not isinstance(resource_id, str) or not 1 <= len(resource_id) <= 256:
                issues.append(AttributionV2ValidationIssue("resource.id", "Invalid resource ID"))

    lifecycle_state: str | None = None
    lifecycle_revision: int | None = None
    lifecycle = event.get("lifecycle")
    if not _is_record(lifecycle):
        issues.append(AttributionV2ValidationIssue("lifecycle", "Lifecycle must be an object"))
    else:
        assert isinstance(lifecycle, dict)
        _add_unknown_keys(lifecycle, {"state", "revision"}, "lifecycle", issues)
        state = lifecycle.get("state")
        if not isinstance(state, str) or state not in _LIFECYCLE_STATES:
            issues.append(
                AttributionV2ValidationIssue("lifecycle.state", "Invalid lifecycle state")
            )
        else:
            lifecycle_state = state
        revision = lifecycle.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or revision > 2_147_483_647
        ):
            issues.append(
                AttributionV2ValidationIssue(
                    "lifecycle.revision", "Revision must be a positive integer"
                )
            )
        else:
            lifecycle_revision = revision

    usage_period: dict[str, Any] | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    if "usage_period" in event:
        raw_period = event["usage_period"]
        if not _is_record(raw_period):
            issues.append(
                AttributionV2ValidationIssue("usage_period", "Usage period must be an object")
            )
        else:
            usage_period = raw_period
            assert isinstance(usage_period, dict)
            _add_unknown_keys(usage_period, {"start_at", "end_at"}, "usage_period", issues)
            start_at = _parse_timestamp(
                usage_period.get("start_at"), "usage_period.start_at", issues
            )
            if "end_at" in usage_period:
                end_at = _parse_timestamp(usage_period["end_at"], "usage_period.end_at", issues)
            if start_at is not None and end_at is not None and end_at < start_at:
                issues.append(
                    AttributionV2ValidationIssue("usage_period.end_at", "End cannot precede start")
                )

    raw_usage = event.get("usage")
    usage = raw_usage if isinstance(raw_usage, list) else None
    seen_metrics: set[str] = set()
    has_time_based_usage = False
    if usage is None:
        issues.append(AttributionV2ValidationIssue("usage", "Usage must be an array"))
    else:
        if len(usage) > 32:
            issues.append(
                AttributionV2ValidationIssue("usage", "At most 32 usage lines are allowed")
            )
        for index, raw_line in enumerate(usage):
            prefix = f"usage.{index}"
            if not _is_record(raw_line):
                issues.append(AttributionV2ValidationIssue(prefix, "Usage line must be an object"))
                continue
            line = raw_line
            assert isinstance(line, dict)
            _add_unknown_keys(line, {"metric", "quantity", "unit"}, prefix, issues)
            metric = line.get("metric")
            if not isinstance(metric, str) or metric not in _METRICS:
                issues.append(
                    AttributionV2ValidationIssue(f"{prefix}.metric", "Invalid usage metric")
                )
            else:
                if metric in seen_metrics:
                    issues.append(
                        AttributionV2ValidationIssue(f"{prefix}.metric", "Duplicate usage metric")
                    )
                seen_metrics.add(metric)
            quantity = line.get("quantity")
            if not isinstance(quantity, str) or not _POSITIVE_DECIMAL.fullmatch(quantity):
                issues.append(
                    AttributionV2ValidationIssue(
                        f"{prefix}.quantity", "Must be a positive plain decimal string"
                    )
                )
            unit = line.get("unit")
            if not isinstance(unit, str) or unit not in _UNITS:
                issues.append(AttributionV2ValidationIssue(f"{prefix}.unit", "Invalid usage unit"))
            else:
                has_time_based_usage = has_time_based_usage or unit.endswith("Seconds")
                if (
                    isinstance(metric, str)
                    and metric in ATTRIBUTION_UNIT_BY_METRIC
                    and unit != ATTRIBUTION_UNIT_BY_METRIC[cast(AttributionUsageMetric, metric)]
                ):
                    issues.append(
                        AttributionV2ValidationIssue(
                            f"{prefix}.unit", "Metric must use its canonical unit"
                        )
                    )

    cost = event.get("cost_evidence")
    if "cost_evidence" in event:
        if not _is_record(cost):
            issues.append(
                AttributionV2ValidationIssue("cost_evidence", "Cost evidence must be an object")
            )
        else:
            assert isinstance(cost, dict)
            _add_unknown_keys(
                cost,
                {"amount", "currency", "source", "confidence", "pricing_version"},
                "cost_evidence",
                issues,
            )
            amount = cost.get("amount")
            if not isinstance(amount, str) or not _POSITIVE_DECIMAL.fullmatch(amount):
                issues.append(
                    AttributionV2ValidationIssue(
                        "cost_evidence.amount", "Must be a positive plain decimal string"
                    )
                )
            currency = cost.get("currency")
            if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
                issues.append(
                    AttributionV2ValidationIssue("cost_evidence.currency", "Invalid currency")
                )
            source = cost.get("source")
            if not isinstance(source, str) or source not in _EVIDENCE_SOURCES:
                issues.append(
                    AttributionV2ValidationIssue("cost_evidence.source", "Invalid evidence source")
                )
                source = None
            confidence = cost.get("confidence")
            if not isinstance(confidence, str) or confidence not in _CONFIDENCES:
                issues.append(
                    AttributionV2ValidationIssue("cost_evidence.confidence", "Invalid confidence")
                )
                confidence = None
            pricing_version = cost.get("pricing_version")
            if "pricing_version" in cost and (
                not isinstance(pricing_version, str) or not 1 <= len(pricing_version) <= 128
            ):
                issues.append(
                    AttributionV2ValidationIssue(
                        "cost_evidence.pricing_version", "Invalid pricing version"
                    )
                )
            if source == "provider_reported" and confidence not in {None, "exact", "estimated"}:
                issues.append(
                    AttributionV2ValidationIssue(
                        "cost_evidence.confidence",
                        "Provider-reported cost must be exact or estimated",
                    )
                )
            if source in {"sdk_catalog", "sdk_rate_registry"} and confidence == "exact":
                issues.append(
                    AttributionV2ValidationIssue(
                        "cost_evidence.confidence", "SDK-derived cost cannot be exact"
                    )
                )
            if source in {"sdk_catalog", "sdk_rate_registry"} and "pricing_version" not in cost:
                issues.append(
                    AttributionV2ValidationIssue(
                        "cost_evidence.pricing_version",
                        "SDK-derived cost requires pricing_version",
                    )
                )

    if has_time_based_usage and lifecycle_state in {"provisional", "final"} and end_at is None:
        issues.append(
            AttributionV2ValidationIssue(
                "usage_period.end_at", "Finalized time-based usage requires a closed usage period"
            )
        )
    usage_length = len(usage) if usage is not None else 0
    if lifecycle_state == "pending":
        if usage_length != 0:
            issues.append(
                AttributionV2ValidationIssue("usage", "Pending events cannot assert usage")
            )
        if "cost_evidence" in event:
            issues.append(
                AttributionV2ValidationIssue("cost_evidence", "Pending events cannot assert cost")
            )
        if usage_period is not None and "end_at" in usage_period:
            issues.append(
                AttributionV2ValidationIssue(
                    "usage_period.end_at", "Pending events cannot close usage"
                )
            )
    elif lifecycle_state == "provisional":
        if usage_length == 0:
            issues.append(
                AttributionV2ValidationIssue("usage", "Provisional events require usage")
            )
        if isinstance(cost, dict) and cost.get("confidence") == "exact":
            issues.append(
                AttributionV2ValidationIssue(
                    "cost_evidence.confidence", "Provisional cost cannot be exact"
                )
            )
    elif lifecycle_state == "final":
        if usage_length == 0:
            issues.append(AttributionV2ValidationIssue("usage", "Final events require usage"))
    elif lifecycle_state == "voided":
        if lifecycle_revision == 1:
            issues.append(
                AttributionV2ValidationIssue(
                    "lifecycle.revision", "Voided events must supersede an earlier revision"
                )
            )
        if usage_length != 0 or "cost_evidence" in event:
            issues.append(
                AttributionV2ValidationIssue("usage", "Voided events must be tombstones")
            )

    return AttributionV2ValidationResult(not issues, tuple(issues))


def assert_attribution_event_v2(value: object) -> None:
    """Raise :class:`ValueError` when *value* violates attribution v2."""
    result = validate_attribution_event_v2(value)
    if not result.success:
        raise ValueError("; ".join(f"{issue.path}: {issue.message}" for issue in result.issues))
