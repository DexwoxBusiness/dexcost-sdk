"""Immutable task/outcome-linked revenue revision models."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias

from dexcost.models._serde import canonical_decimal, iso_canonical, parse_canonical

RevenueState: TypeAlias = Literal["pending", "provisional", "recognized", "voided"]
RevenueSourceType: TypeAlias = Literal["sdk", "workspace_api", "import", "manual"]
RevenueInput: TypeAlias = Decimal | str | int

_AMOUNT = re.compile(r"^(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_STATES = frozenset({"pending", "provisional", "recognized", "voided"})
_SOURCE_TYPES = frozenset({"sdk", "workspace_api", "import", "manual"})


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _uuid(value: uuid.UUID | str, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc


@dataclass(frozen=True)
class RevenueAmount:
    """Exact non-negative money in one explicit ISO-style currency."""

    value: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError("revenue amount must use Decimal")
        if not self.value.is_finite() or self.value < 0:
            raise ValueError("revenue amount must be finite and non-negative")
        if _AMOUNT.fullmatch(canonical_decimal(self.value)) is None:
            raise ValueError("revenue amount exceeds the 26.12 decimal contract")
        if not isinstance(self.currency, str) or _CURRENCY.fullmatch(self.currency) is None:
            raise ValueError("revenue currency must be a three-letter uppercase code")

    @classmethod
    def from_input(cls, value: RevenueInput, currency: str) -> RevenueAmount:
        if isinstance(value, (bool, float)):
            raise TypeError("revenue amount must be a Decimal, integer, or decimal string")
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("revenue amount is not a plain decimal") from exc
        return cls(amount, currency)

    def to_dict(self) -> dict[str, str]:
        return {"value": canonical_decimal(self.value), "currency": self.currency}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RevenueAmount:
        if set(data) != {"value", "currency"}:
            raise ValueError("revenue amount contains unexpected fields")
        value = data["value"]
        currency = data["currency"]
        if not isinstance(value, str) or not isinstance(currency, str):
            raise ValueError("revenue amount must contain string value and currency")
        return cls.from_input(value, currency)


@dataclass(frozen=True)
class RevenueSource:
    """Stable identity of the system that asserted revenue."""

    type: RevenueSourceType = "sdk"
    record_id: str | None = None

    def __post_init__(self) -> None:
        if self.type not in _SOURCE_TYPES:
            raise ValueError(f"unsupported revenue source {self.type!r}")
        if self.record_id is not None and (
            not isinstance(self.record_id, str)
            or self.record_id != self.record_id.strip()
            or not 1 <= len(self.record_id) <= 256
        ):
            raise ValueError("revenue source record_id must contain 1 to 256 characters")

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {"type": self.type}
        if self.record_id is not None:
            result["record_id"] = self.record_id
        return result


@dataclass(frozen=True)
class RevenueRevision:
    """One append-only revision in a revenue lifecycle."""

    task_id: uuid.UUID
    state: RevenueState
    source: RevenueSource = field(default_factory=RevenueSource)
    amount: RevenueAmount | None = None
    outcome_id: uuid.UUID | None = None
    revenue_id: uuid.UUID = field(default_factory=uuid.uuid4)
    revision: int = 1
    effective_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _uuid(self.task_id, "task_id"))
        object.__setattr__(self, "revenue_id", _uuid(self.revenue_id, "revenue_id"))
        if self.outcome_id is not None:
            object.__setattr__(
                self,
                "outcome_id",
                _uuid(self.outcome_id, "outcome_id"),
            )
        object.__setattr__(self, "effective_at", _aware(self.effective_at, "effective_at"))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if self.schema_version != "1":
            raise ValueError("revenue schema_version must be '1'")
        if self.state not in _STATES:
            raise ValueError(f"unsupported revenue state {self.state!r}")
        if isinstance(self.revision, bool) or not 1 <= self.revision <= 2_147_483_647:
            raise ValueError("revenue revision must be between 1 and 2147483647")
        amount_required = self.state in {"provisional", "recognized"}
        if amount_required and self.amount is None:
            raise ValueError(f"{self.state} revenue requires an amount")
        if not amount_required and self.amount is not None:
            raise ValueError(f"{self.state} revenue cannot assert an amount")
        if self.state == "voided" and self.revision == 1:
            raise ValueError("voided revenue must supersede an earlier revision")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "revenue_id": str(self.revenue_id),
            "task_id": str(self.task_id),
            "effective_at": iso_canonical(self.effective_at),
            "observed_at": iso_canonical(self.observed_at),
            "lifecycle": {"state": self.state, "revision": self.revision},
            "source": self.source.to_dict(),
        }
        if self.outcome_id is not None:
            result["outcome_id"] = str(self.outcome_id)
        if self.amount is not None:
            result["amount"] = self.amount.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RevenueRevision:
        lifecycle = data.get("lifecycle")
        source = data.get("source")
        raw_amount = data.get("amount")
        if not isinstance(lifecycle, dict) or not isinstance(source, dict):
            raise ValueError("revenue lifecycle and source must be objects")
        raw_source_type = source.get("type")
        raw_record_id = source.get("record_id")
        if not isinstance(raw_source_type, str) or (
            raw_record_id is not None and not isinstance(raw_record_id, str)
        ):
            raise ValueError("revenue source is invalid")
        amount = None
        if raw_amount is not None:
            if not isinstance(raw_amount, dict):
                raise ValueError("revenue amount must be an object")
            amount = RevenueAmount.from_dict(raw_amount)
        return cls(
            schema_version=str(data.get("schema_version")),
            revenue_id=_uuid(data.get("revenue_id"), "revenue_id"),  # type: ignore[arg-type]
            task_id=_uuid(data.get("task_id"), "task_id"),  # type: ignore[arg-type]
            outcome_id=(
                None
                if data.get("outcome_id") is None
                else _uuid(data["outcome_id"], "outcome_id")  # type: ignore[arg-type]
            ),
            effective_at=parse_canonical(str(data.get("effective_at"))),
            observed_at=parse_canonical(str(data.get("observed_at"))),
            state=str(lifecycle.get("state")),  # type: ignore[arg-type]
            revision=int(lifecycle.get("revision", 0)),
            amount=amount,
            source=RevenueSource(
                type=raw_source_type,  # type: ignore[arg-type]
                record_id=raw_record_id,
            ),
        )


__all__ = [
    "RevenueAmount",
    "RevenueInput",
    "RevenueRevision",
    "RevenueSource",
    "RevenueSourceType",
    "RevenueState",
]
