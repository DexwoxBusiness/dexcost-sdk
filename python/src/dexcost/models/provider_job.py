"""Immutable revisions for asynchronous provider jobs.

Provider jobs outlive the request that creates them.  This model keeps their
local persistence aligned with attribution-v3's append-only event revision
ledger: pending revisions never assert consumption, while terminal revisions
are complete provider-observed usage snapshots.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypeAlias, cast

from dexcost.attribution.v3_types import AttributionEventV3
from dexcost.attribution.v3_validate import assert_attribution_observation_v3
from dexcost.models._serde import canonical_decimal, iso_canonical, parse_canonical
from dexcost.models.capability import CapabilityIdentity

ProviderJobStatus: TypeAlias = Literal[
    "submitted", "running", "succeeded", "failed", "cancelled", "unknown"
]
ProviderJobEventType: TypeAlias = Literal["llm_call", "external_cost", "compute_cost"]
ProviderJobCostSource: TypeAlias = Literal[
    "provider_reported", "sdk_catalog", "sdk_rate_registry", "manual"
]
ProviderJobCostConfidence: TypeAlias = Literal[
    "exact", "computed", "estimated", "unknown"
]
ProviderJobOperationStatus: TypeAlias = Literal[
    "in_progress", "succeeded", "failed", "cancelled", "unknown"
]

_CANONICAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._{}/*^+-]{0,63}$")
_ERROR_CODE_MAX = 64
_RESOURCE_TYPES = frozenset(
    {"model", "sku", "instance", "endpoint", "session", "other", "tool"}
)
_STATUSES = frozenset(
    {"submitted", "running", "succeeded", "failed", "cancelled", "unknown"}
)
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "unknown"})
_EVENT_TYPES = frozenset({"llm_call", "external_cost", "compute_cost"})
_COST_SOURCES = frozenset(
    {"provider_reported", "sdk_catalog", "sdk_rate_registry", "manual"}
)
_COST_CONFIDENCES = frozenset({"exact", "computed", "estimated", "unknown"})


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _bounded(value: str, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return value


def _canonical(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_NAME.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical lowercase identifier")
    return value


def _decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{field_name} must be an integer, Decimal, or decimal string")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a plain decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return parsed


def _optional_counter(value: int | None, field_name: str) -> int | None:
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value < 0
    ):
        raise ValueError(f"{field_name} must be a non-negative integer or None")
    return value


def _deterministic_uuid(namespace: str, *parts: str) -> uuid.UUID:
    digest = bytearray(
        hashlib.sha256("\0".join((namespace, *parts)).encode("utf-8")).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(digest))


def provider_job_event_id(provider: str, service: str, provider_record_id: str) -> uuid.UUID:
    """Return the stable observation identity for one provider-owned job."""
    return _deterministic_uuid(
        "dexcost:provider-job:v1", provider, service, provider_record_id
    )


@dataclass(frozen=True)
class ProviderJobUsageLine:
    """One provider-native quantity in a terminal job snapshot."""

    metric: str
    quantity: Decimal | int | str
    unit: str

    def __post_init__(self) -> None:
        _canonical(self.metric, "provider job usage metric")
        if not isinstance(self.unit, str) or _UNIT.fullmatch(self.unit) is None:
            raise ValueError("provider job usage unit is invalid")
        quantity = _decimal(self.quantity, self.metric)
        if quantity <= 0:
            raise ValueError("provider job usage quantities must be positive")
        object.__setattr__(self, "quantity", quantity)

    def to_dict(self) -> dict[str, str]:
        return {
            "metric": self.metric,
            "quantity": canonical_decimal(cast(Decimal, self.quantity)),
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> ProviderJobUsageLine:
        return cls(
            metric=str(value.get("metric")),
            quantity=str(value.get("quantity")),
            unit=str(value.get("unit")),
        )


@dataclass(frozen=True)
class ProviderJobRevision:
    """One immutable full snapshot of an asynchronous provider job."""

    event_id: uuid.UUID
    revision: int
    task_id: uuid.UUID
    provider: str
    service: str
    provider_record_id: str
    operation: str
    component: str
    event_type: ProviderJobEventType
    resource_type: str
    resource_id: str
    status: ProviderJobStatus
    submitted_at: datetime
    observed_at: datetime
    owns_task: bool = False
    billing_dimensions: tuple[tuple[str, str], ...] = ()
    usage: tuple[ProviderJobUsageLine, ...] = ()
    cost_amount: Decimal | None = None
    cost_source: ProviderJobCostSource | None = None
    cost_confidence: ProviderJobCostConfidence | None = None
    pricing_version: str | None = None
    latency_ms: int | None = None
    error_type: str | None = None
    error_code: str | None = None
    task_input_tokens: int | None = None
    task_output_tokens: int | None = None
    task_cached_tokens: int | None = None
    capability: CapabilityIdentity | None = None
    schema_version: str = field(default="1", kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, uuid.UUID):
            raise ValueError("provider job event_id must be a UUID")
        if not isinstance(self.task_id, uuid.UUID):
            raise ValueError("provider job task_id must be a UUID")
        if isinstance(self.revision, bool) or not 1 <= self.revision <= 2_147_483_647:
            raise ValueError("provider job revision must be between 1 and 2147483647")
        _canonical(self.provider, "provider")
        _canonical(self.service, "service")
        _bounded(self.provider_record_id, "provider_record_id")
        _canonical(self.operation, "operation")
        _canonical(self.component, "component")
        if self.event_type not in _EVENT_TYPES:
            raise ValueError(f"unsupported provider job event_type {self.event_type!r}")
        if self.resource_type not in _RESOURCE_TYPES:
            raise ValueError(f"unsupported provider job resource_type {self.resource_type!r}")
        _bounded(self.resource_id, "resource_id")
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported provider job status {self.status!r}")
        submitted = _aware(self.submitted_at, "submitted_at")
        observed = _aware(self.observed_at, "observed_at")
        if observed < submitted:
            raise ValueError("provider job observed_at cannot precede submitted_at")
        object.__setattr__(self, "submitted_at", submitted)
        object.__setattr__(self, "observed_at", observed)
        if not isinstance(self.owns_task, bool):
            raise TypeError("owns_task must be a bool")
        if len(self.billing_dimensions) > 24:
            raise ValueError("provider job supports at most 24 billing dimensions")
        dimension_keys: set[str] = set()
        for dimension in self.billing_dimensions:
            if not isinstance(dimension, tuple) or len(dimension) != 2:
                raise TypeError("provider job billing dimensions must be key/value tuples")
            key, value = dimension
            _canonical(key, "billing dimension key")
            _bounded(value, "billing dimension value")
            if key in dimension_keys:
                raise ValueError(f"duplicate provider job billing dimension {key!r}")
            dimension_keys.add(key)
        if self.schema_version != "1":
            raise ValueError("unsupported provider job schema version")

        identities: set[tuple[str, str]] = set()
        for line in self.usage:
            if not isinstance(line, ProviderJobUsageLine):
                raise TypeError("provider job usage must contain ProviderJobUsageLine")
            identity = (line.metric, line.unit)
            if identity in identities:
                raise ValueError(f"duplicate provider job usage line {identity!r}")
            identities.add(identity)

        pending = self.status in {"submitted", "running"}
        if pending and self.usage:
            raise ValueError("pending provider jobs cannot assert usage")
        if pending and any(
            value is not None
            for value in (
                self.cost_amount,
                self.cost_source,
                self.cost_confidence,
                self.pricing_version,
            )
        ):
            raise ValueError("pending provider jobs cannot assert cost evidence")
        if self.status == "succeeded" and not self.usage:
            raise ValueError("successful provider jobs require provider-observed usage")

        cost_fields = (self.cost_amount, self.cost_source, self.cost_confidence)
        if any(value is not None for value in cost_fields):
            if not all(value is not None for value in cost_fields):
                raise ValueError("provider job cost amount, source, and confidence are atomic")
            amount = _decimal(cast(Decimal, self.cost_amount), "cost_amount")
            if amount <= 0:
                raise ValueError("provider job cost evidence must be positive")
            object.__setattr__(self, "cost_amount", amount)
            if self.cost_source not in _COST_SOURCES:
                raise ValueError(f"unsupported provider job cost source {self.cost_source!r}")
            if self.cost_confidence not in _COST_CONFIDENCES:
                raise ValueError(
                    f"unsupported provider job cost confidence {self.cost_confidence!r}"
                )
            if self.cost_source == "provider_reported" and self.cost_confidence not in {
                "exact",
                "estimated",
            }:
                raise ValueError("provider-reported cost must be exact or estimated")
            if self.cost_source in {
                "sdk_catalog",
                "sdk_rate_registry",
            } and (self.cost_confidence == "exact" or not self.pricing_version):
                raise ValueError(
                    "SDK provider-job cost requires non-exact confidence and pricing_version"
                )
        elif self.pricing_version is not None:
            raise ValueError("pricing_version requires cost evidence")

        if self.latency_ms is not None and (
            not isinstance(self.latency_ms, int)
            or isinstance(self.latency_ms, bool)
            or not 0 <= self.latency_ms <= 86_400_000
        ):
            raise ValueError("latency_ms must be between 0 and 86400000")
        if self.error_type is not None:
            _canonical(self.error_type, "error_type")
            if self.status == "succeeded":
                raise ValueError("successful provider jobs cannot carry an error")
        if self.error_code is not None:
            _bounded(self.error_code, "error_code", _ERROR_CODE_MAX)
            if self.error_type is None:
                raise ValueError("error_code requires error_type")
        for name in (
            "task_input_tokens",
            "task_output_tokens",
            "task_cached_tokens",
        ):
            _optional_counter(getattr(self, name), name)

        expected = provider_job_event_id(
            self.provider, self.service, self.provider_record_id
        )
        if self.event_id != expected:
            raise ValueError("provider job event_id does not match its provider identity")

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def lifecycle_state(self) -> Literal["pending", "final"]:
        return "final" if self.terminal else "pending"

    @property
    def operation_status(self) -> ProviderJobOperationStatus:
        if not self.terminal:
            return "in_progress"
        return cast(ProviderJobOperationStatus, self.status)

    def economic_snapshot(self) -> dict[str, Any]:
        """Return fields that make repeated provider polls idempotent."""
        value = self.to_dict()
        value.pop("revision")
        value.pop("observed_at")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": str(self.event_id),
            "revision": self.revision,
            "task_id": str(self.task_id),
            "provider": self.provider,
            "service": self.service,
            "provider_record_id": self.provider_record_id,
            "operation": self.operation,
            "component": self.component,
            "event_type": self.event_type,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "status": self.status,
            "submitted_at": iso_canonical(self.submitted_at),
            "observed_at": iso_canonical(self.observed_at),
            "owns_task": self.owns_task,
            "billing_dimensions": [
                {"key": key, "value": value}
                for key, value in self.billing_dimensions
            ],
            "usage": [line.to_dict() for line in self.usage],
            "cost_amount": (
                canonical_decimal(self.cost_amount) if self.cost_amount is not None else None
            ),
            "cost_source": self.cost_source,
            "cost_confidence": self.cost_confidence,
            "pricing_version": self.pricing_version,
            "latency_ms": self.latency_ms,
            "error_type": self.error_type,
            "error_code": self.error_code,
            "task_input_tokens": self.task_input_tokens,
            "task_output_tokens": self.task_output_tokens,
            "task_cached_tokens": self.task_cached_tokens,
            "capability": self.capability.to_dict() if self.capability is not None else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProviderJobRevision:
        raw_capability = value.get("capability")
        return cls(
            schema_version=str(value.get("schema_version")),
            event_id=uuid.UUID(str(value.get("event_id"))),
            revision=int(value.get("revision", 0)),
            task_id=uuid.UUID(str(value.get("task_id"))),
            provider=str(value.get("provider")),
            service=str(value.get("service")),
            provider_record_id=str(value.get("provider_record_id")),
            operation=str(value.get("operation")),
            component=str(value.get("component")),
            event_type=cast(ProviderJobEventType, value.get("event_type")),
            resource_type=str(value.get("resource_type")),
            resource_id=str(value.get("resource_id")),
            status=cast(ProviderJobStatus, value.get("status")),
            submitted_at=parse_canonical(str(value.get("submitted_at"))),
            observed_at=parse_canonical(str(value.get("observed_at"))),
            owns_task=value.get("owns_task", False),
            billing_dimensions=tuple(
                (str(dimension["key"]), str(dimension["value"]))
                for dimension in value.get("billing_dimensions", [])
            ),
            usage=tuple(
                ProviderJobUsageLine.from_dict(cast(dict[str, object], line))
                for line in value.get("usage", [])
            ),
            cost_amount=(
                Decimal(str(value["cost_amount"]))
                if value.get("cost_amount") is not None
                else None
            ),
            cost_source=cast(ProviderJobCostSource | None, value.get("cost_source")),
            cost_confidence=cast(
                ProviderJobCostConfidence | None, value.get("cost_confidence")
            ),
            pricing_version=cast(str | None, value.get("pricing_version")),
            latency_ms=cast(int | None, value.get("latency_ms")),
            error_type=cast(str | None, value.get("error_type")),
            error_code=cast(str | None, value.get("error_code")),
            task_input_tokens=cast(int | None, value.get("task_input_tokens")),
            task_output_tokens=cast(int | None, value.get("task_output_tokens")),
            task_cached_tokens=cast(int | None, value.get("task_cached_tokens")),
            capability=(
                CapabilityIdentity.from_dict(cast(dict[str, Any], raw_capability))
                if raw_capability is not None
                else None
            ),
        )

    def to_attribution_observation(
        self, *, environment: str | None = None
    ) -> AttributionEventV3:
        """Convert this local revision into the strict server observation contract."""
        operation: dict[str, Any] = {
            "id": str(self.event_id),
            "name": self.operation,
            "status": self.operation_status,
            "attempt": {"id": str(self.event_id), "number": 1},
        }
        if self.latency_ms is not None:
            operation["latency_ms"] = self.latency_ms
        if self.error_type is not None:
            error = {"type": self.error_type}
            if self.error_code is not None:
                error["code"] = self.error_code
            operation["error"] = error

        dimensions = [
            {"key": key, "value": {"type": "string", "value": value}}
            for key, value in self.billing_dimensions
        ]
        stable_dimensions = json.dumps(
            dimensions, ensure_ascii=False, separators=(",", ":")
        )
        usage = [
            {
                "line_id": str(
                    _deterministic_uuid(
                        "dexcost:provider-job-usage-line:v1",
                        str(self.event_id),
                        line.metric,
                        line.unit,
                        stable_dimensions,
                    )
                ),
                **line.to_dict(),
                "dimensions": dimensions,
            }
            for line in self.usage
        ]
        result: dict[str, Any] = {
            "schema_version": "3",
            "event_id": str(self.event_id),
            "task_id": str(self.task_id),
            "occurred_at": iso_canonical(self.submitted_at),
            "observed_at": iso_canonical(self.observed_at),
            "component": self.component,
            "provider": {
                "name": self.provider,
                "service": self.service,
                "record_id": self.provider_record_id,
            },
            "resource": {"type": self.resource_type, "id": self.resource_id},
            "operation": operation,
            "lifecycle": {"state": self.lifecycle_state, "revision": self.revision},
            "usage_snapshot": "full",
            "usage_period": {
                "start_at": iso_canonical(self.submitted_at),
                **(
                    {"end_at": iso_canonical(self.observed_at)}
                    if self.terminal
                    else {}
                ),
            },
            "usage": usage,
        }
        if environment is not None:
            result["environment"] = environment
        if self.capability is not None:
            result["capability"] = self.capability.to_dict()
        if self.cost_amount is not None:
            evidence: dict[str, Any] = {
                "amount": canonical_decimal(self.cost_amount),
                "currency": "USD",
                "source": self.cost_source,
                "confidence": self.cost_confidence,
            }
            if self.pricing_version is not None:
                evidence["pricing_version"] = self.pricing_version
            result["cost_evidence"] = evidence
        assert_attribution_observation_v3(result)
        return cast(AttributionEventV3, result)


__all__ = [
    "ProviderJobCostConfidence",
    "ProviderJobCostSource",
    "ProviderJobEventType",
    "ProviderJobOperationStatus",
    "ProviderJobRevision",
    "ProviderJobStatus",
    "ProviderJobUsageLine",
    "provider_job_event_id",
]
