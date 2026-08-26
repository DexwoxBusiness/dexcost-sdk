"""Revisioned business outcomes emitted by the Python SDK."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias, TypeGuard

from dexcost.models._serde import canonical_decimal, iso_canonical, parse_canonical

OutcomeState: TypeAlias = Literal["pending", "achieved", "missed", "voided"]
OutcomeValueType: TypeAlias = Literal["string", "boolean", "integer", "decimal"]
OutcomeInput: TypeAlias = str | bool | int | Decimal

_CANONICAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_INTEGER = re.compile(r"^-?(?:0|[1-9]\d{0,25})$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$")
_OUTCOME_STATES = frozenset({"pending", "achieved", "missed", "voided"})


def _is_outcome_value_type(value: object) -> TypeGuard[OutcomeValueType]:
    return isinstance(value, str) and value in {"string", "boolean", "integer", "decimal"}


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
class OutcomeValue:
    """Typed outcome value matching the attribution business-value contract."""

    type: OutcomeValueType
    value: str | bool

    def __post_init__(self) -> None:
        if self.type == "string":
            if not isinstance(self.value, str) or not 1 <= len(self.value) <= 1024:
                raise ValueError("string outcome values must contain 1 to 1024 characters")
            return
        if self.type == "boolean":
            if not isinstance(self.value, bool):
                raise ValueError("boolean outcome values must contain a bool")
            return
        if not isinstance(self.value, str):
            raise ValueError(f"{self.type} outcome values must use a plain decimal string")
        pattern = _INTEGER if self.type == "integer" else _DECIMAL
        if pattern.fullmatch(self.value) is None:
            raise ValueError(f"invalid {self.type} outcome value")

    def to_dict(self) -> dict[str, str | bool]:
        return {"type": self.type, "value": self.value}

    @classmethod
    def from_input(cls, value: OutcomeInput | OutcomeValue) -> OutcomeValue:
        if isinstance(value, OutcomeValue):
            return value
        if isinstance(value, bool):
            return cls(type="boolean", value=value)
        if isinstance(value, int):
            return cls(type="integer", value=str(value))
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("decimal outcome values must be finite")
            return cls(type="decimal", value=canonical_decimal(value))
        if isinstance(value, str):
            return cls(type="string", value=value)
        raise TypeError("outcome value must be a string, bool, int, Decimal, or OutcomeValue")

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> OutcomeValue:
        value_type = data.get("type")
        if not _is_outcome_value_type(value_type):
            raise ValueError("invalid outcome value type")
        value = data.get("value")
        if not isinstance(value, (str, bool)):
            raise ValueError("invalid outcome value")
        return cls(type=value_type, value=value)


@dataclass(frozen=True)
class OutcomeRevision:
    """One immutable revision of a task-linked business outcome."""

    task_id: uuid.UUID
    name: str
    state: OutcomeState = "achieved"
    outcome_id: uuid.UUID = field(default_factory=uuid.uuid4)
    revision: int = 1
    effective_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    value: OutcomeValue | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _uuid(self.task_id, "task_id"))
        object.__setattr__(self, "outcome_id", _uuid(self.outcome_id, "outcome_id"))
        object.__setattr__(self, "effective_at", _aware(self.effective_at, "effective_at"))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if self.schema_version != "1":
            raise ValueError("outcome schema_version must be '1'")
        if _CANONICAL_NAME.fullmatch(self.name) is None:
            raise ValueError("outcome name must be a canonical identifier")
        if self.state not in _OUTCOME_STATES:
            raise ValueError(f"unsupported outcome state {self.state!r}")
        if isinstance(self.revision, bool) or not 1 <= self.revision <= 2_147_483_647:
            raise ValueError("outcome revision must be an integer between 1 and 2147483647")
        if self.state in {"pending", "voided"} and self.value is not None:
            raise ValueError(f"{self.state} outcomes cannot assert a value")
        if self.state == "voided" and self.revision == 1:
            raise ValueError("voided outcomes must supersede an earlier revision")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": self.schema_version,
            "outcome_id": str(self.outcome_id),
            "task_id": str(self.task_id),
            "name": self.name,
            "effective_at": iso_canonical(self.effective_at),
            "observed_at": iso_canonical(self.observed_at),
            "lifecycle": {"state": self.state, "revision": self.revision},
        }
        if self.value is not None:
            data["value"] = self.value.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> OutcomeRevision:
        try:
            lifecycle = data["lifecycle"]
            if not isinstance(lifecycle, dict):
                raise ValueError("outcome lifecycle must be an object")
            raw_value = data.get("value")
            if raw_value is not None and not isinstance(raw_value, dict):
                raise ValueError("outcome value must be an object")
            return cls(
                schema_version=str(data["schema_version"]),
                outcome_id=_uuid(data["outcome_id"], "outcome_id"),  # type: ignore[arg-type]
                task_id=_uuid(data["task_id"], "task_id"),  # type: ignore[arg-type]
                name=str(data["name"]),
                effective_at=parse_canonical(str(data["effective_at"])),
                observed_at=parse_canonical(str(data["observed_at"])),
                state=str(lifecycle["state"]),  # type: ignore[arg-type]
                revision=int(lifecycle["revision"]),
                value=OutcomeValue.from_dict(raw_value) if raw_value is not None else None,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError(f"Invalid outcome data: {exc}") from exc
