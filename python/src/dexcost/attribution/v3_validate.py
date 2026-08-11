"""Non-throwing validation for the attribution-v3 observation contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from dexcost.attribution.types import ATTRIBUTION_UNIT_BY_METRIC

_TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,6}))?(?:Z|[+-](\d{2}):(\d{2}))$"
)
_MISSING_PROPERTY = re.compile(r"^'([^']+)' is a required property$")
_EXTRA_PROPERTY = re.compile(
    r"^Additional properties are not allowed \('([^']+)' was unexpected\)$"
)
_TIME_METRICS = {
    "audio_seconds",
    "connected_seconds",
    "recording_seconds",
    "agent_seconds",
    "compute_seconds",
    "vcpu_seconds",
    "memory_gib_seconds",
    "gpu_seconds",
}


def _load_schema() -> dict[str, Any]:
    schema_path = files("dexcost.attribution").joinpath("attribution-v3-schema.json")
    document = json.loads(schema_path.read_text(encoding="utf-8"))
    return {
        "$schema": document["$schema"],
        "$id": document["$id"],
        "$ref": "#/components/schemas/AttributionObservation",
        "components": document["components"],
    }


_SCHEMA_VALIDATOR = Draft202012Validator(_load_schema(), format_checker=FormatChecker())


@dataclass(frozen=True)
class AttributionV3ValidationIssue:
    path: str
    message: str


@dataclass(frozen=True)
class AttributionV3ValidationResult:
    success: bool
    issues: tuple[AttributionV3ValidationIssue, ...]


def _is_record(value: object) -> bool:
    return isinstance(value, dict)


def _schema_error_path(error: ValidationError) -> str:
    parts = [str(part) for part in error.absolute_path]
    if error.validator == "required":
        match = _MISSING_PROPERTY.fullmatch(error.message)
        if match is not None:
            parts.append(match.group(1))
    elif error.validator == "additionalProperties":
        match = _EXTRA_PROPERTY.fullmatch(error.message)
        if match is not None:
            parts.append(match.group(1))
    return ".".join(parts)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _add_timestamp_issue(
    value: object,
    path: str,
    issues: list[AttributionV3ValidationIssue],
) -> None:
    if _timestamp(value) is None:
        issues.append(
            AttributionV3ValidationIssue(path, "Must be a valid offset-aware ISO 8601 instant")
        )


def _semantic_issues(value: object) -> list[AttributionV3ValidationIssue]:
    if not _is_record(value):
        return []
    event = value
    assert isinstance(event, dict)
    issues: list[AttributionV3ValidationIssue] = []
    _add_timestamp_issue(event.get("occurred_at"), "occurred_at", issues)
    _add_timestamp_issue(event.get("observed_at"), "observed_at", issues)

    raw_period = event.get("usage_period")
    usage_period = raw_period if isinstance(raw_period, dict) else None
    if usage_period is not None:
        _add_timestamp_issue(usage_period.get("start_at"), "usage_period.start_at", issues)
        if "end_at" in usage_period:
            _add_timestamp_issue(usage_period.get("end_at"), "usage_period.end_at", issues)
            start = _timestamp(usage_period.get("start_at"))
            end = _timestamp(usage_period.get("end_at"))
            if start is not None and end is not None and end < start:
                issues.append(
                    AttributionV3ValidationIssue("usage_period.end_at", "Cannot precede start_at")
                )

    raw_usage = event.get("usage")
    usage = raw_usage if isinstance(raw_usage, list) else []
    line_ids: set[object] = set()
    for line_index, raw_line in enumerate(usage):
        if not isinstance(raw_line, dict):
            continue
        line_id = raw_line.get("line_id")
        try:
            duplicate_line = line_id in line_ids
            line_ids.add(line_id)
        except TypeError:
            duplicate_line = False
        if duplicate_line:
            issues.append(
                AttributionV3ValidationIssue(
                    f"usage.{line_index}.line_id", "Must be unique in a full snapshot"
                )
            )
        metric = raw_line.get("metric")
        canonical_unit = (
            ATTRIBUTION_UNIT_BY_METRIC.get(metric) if isinstance(metric, str) else None
        )
        if canonical_unit is not None and raw_line.get("unit") != canonical_unit:
            issues.append(
                AttributionV3ValidationIssue(
                    f"usage.{line_index}.unit", f"Must be {canonical_unit}"
                )
            )
        raw_dimensions = raw_line.get("dimensions")
        dimensions = raw_dimensions if isinstance(raw_dimensions, list) else []
        dimension_keys: set[object] = set()
        for dimension_index, raw_dimension in enumerate(dimensions):
            if not isinstance(raw_dimension, dict):
                continue
            key = raw_dimension.get("key")
            try:
                duplicate_dimension = key in dimension_keys
                dimension_keys.add(key)
            except TypeError:
                duplicate_dimension = False
            if duplicate_dimension:
                issues.append(
                    AttributionV3ValidationIssue(
                        f"usage.{line_index}.dimensions.{dimension_index}.key",
                        "Must be unique within the usage line",
                    )
                )

    raw_operation = event.get("operation")
    operation = raw_operation if isinstance(raw_operation, dict) else None
    raw_attempt = operation.get("attempt") if operation is not None else None
    attempt = raw_attempt if isinstance(raw_attempt, dict) else None
    if attempt is not None:
        attempt_number = attempt.get("number")
        if attempt_number == 1 and "retry_of" in attempt:
            issues.append(
                AttributionV3ValidationIssue(
                    "operation.attempt.retry_of", "Attempt 1 cannot retry another attempt"
                )
            )
        if (
            isinstance(attempt_number, int)
            and not isinstance(attempt_number, bool)
            and attempt_number > 1
            and "retry_of" not in attempt
        ):
            issues.append(
                AttributionV3ValidationIssue(
                    "operation.attempt.retry_of", "Later attempts require retry_of"
                )
            )
        if attempt.get("id") is not None and attempt.get("id") == attempt.get("retry_of"):
            issues.append(
                AttributionV3ValidationIssue(
                    "operation.attempt.retry_of", "Attempt cannot retry itself"
                )
            )

    raw_lifecycle = event.get("lifecycle")
    lifecycle = raw_lifecycle if isinstance(raw_lifecycle, dict) else None
    state = lifecycle.get("state") if lifecycle is not None else None
    raw_cost = event.get("cost_evidence")
    cost = raw_cost if isinstance(raw_cost, dict) else None
    if state == "pending":
        if usage:
            issues.append(AttributionV3ValidationIssue("usage", "Pending cannot assert usage"))
        if "cost_evidence" in event:
            issues.append(
                AttributionV3ValidationIssue(
                    "cost_evidence", "Pending cannot assert cost evidence"
                )
            )
        if usage_period is not None and "end_at" in usage_period:
            issues.append(
                AttributionV3ValidationIssue("usage_period.end_at", "Pending cannot close usage")
            )
    elif state == "provisional":
        if not usage:
            issues.append(AttributionV3ValidationIssue("usage", "Provisional requires usage"))
        if cost is not None and cost.get("confidence") == "exact":
            issues.append(
                AttributionV3ValidationIssue(
                    "cost_evidence.confidence", "Provisional cost cannot be exact"
                )
            )
    elif state == "final":
        if operation is not None and operation.get("status") == "in_progress":
            issues.append(
                AttributionV3ValidationIssue(
                    "operation.status", "Final operation cannot be in progress"
                )
            )
        if operation is not None and operation.get("status") == "succeeded" and not usage:
            issues.append(
                AttributionV3ValidationIssue("usage", "Successful final operation requires usage")
            )
    elif state == "voided":
        revision = lifecycle.get("revision") if lifecycle is not None else None
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 1:
            issues.append(
                AttributionV3ValidationIssue("lifecycle.revision", "Voided revision must exceed 1")
            )
        if usage:
            issues.append(AttributionV3ValidationIssue("usage", "Voided cannot assert usage"))
        if "cost_evidence" in event:
            issues.append(
                AttributionV3ValidationIssue("cost_evidence", "Voided cannot assert cost evidence")
            )

    if (
        state in {"provisional", "final"}
        and any(
            isinstance(raw_line, dict) and raw_line.get("metric") in _TIME_METRICS
            for raw_line in usage
        )
        and (usage_period is None or "end_at" not in usage_period)
    ):
        issues.append(
            AttributionV3ValidationIssue(
                "usage_period.end_at", "Time-based usage requires a closed period"
            )
        )

    if (
        cost is not None
        and cost.get("source") == "provider_reported"
        and cost.get("confidence") not in {"exact", "estimated"}
    ):
        issues.append(
            AttributionV3ValidationIssue(
                "cost_evidence.confidence",
                "Provider-reported evidence must be exact or estimated",
            )
        )
    if cost is not None and cost.get("source") in {"sdk_catalog", "sdk_rate_registry"}:
        if cost.get("confidence") == "exact":
            issues.append(
                AttributionV3ValidationIssue(
                    "cost_evidence.confidence", "SDK evidence cannot be exact"
                )
            )
        pricing_version = cost.get("pricing_version")
        if not isinstance(pricing_version, str) or not pricing_version:
            issues.append(
                AttributionV3ValidationIssue(
                    "cost_evidence.pricing_version",
                    "SDK evidence requires a pricing version",
                )
            )
    return issues


def validate_attribution_observation_v3(value: object) -> AttributionV3ValidationResult:
    """Validate the complete schema and cross-field invariants without raising."""
    issues = [
        AttributionV3ValidationIssue(_schema_error_path(error), error.message)
        for error in sorted(
            _SCHEMA_VALIDATOR.iter_errors(value),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
    ]
    issues.extend(_semantic_issues(value))
    unique = tuple(dict.fromkeys(issues))
    return AttributionV3ValidationResult(not unique, unique)


def assert_attribution_observation_v3(value: object) -> None:
    """Raise :class:`ValueError` when *value* violates attribution v3."""
    result = validate_attribution_observation_v3(value)
    if not result.success:
        raise ValueError("; ".join(f"{issue.path}: {issue.message}" for issue in result.issues))
