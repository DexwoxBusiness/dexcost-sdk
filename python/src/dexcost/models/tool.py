"""Exact public inputs for general tool/function metering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TypeAlias

from dexcost.models._serde import canonical_decimal

ToolQuantityInput: TypeAlias = Decimal | str | int

_CANONICAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._{}/*^+\-]{0,63}$")
_POSITIVE_DECIMAL = re.compile(r"^(?=.*[1-9])(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$")


@dataclass(frozen=True)
class ToolUsage:
    """One exact usage meter asserted for a tool invocation."""

    metric: str = "call_count"
    quantity: Decimal = Decimal("1")
    unit: str = "Calls"

    def __post_init__(self) -> None:
        if _CANONICAL_NAME.fullmatch(self.metric) is None:
            raise ValueError("tool usage metric must be a canonical lowercase identifier")
        if not isinstance(self.quantity, Decimal):
            raise TypeError("tool usage quantity must use Decimal")
        if (
            not self.quantity.is_finite()
            or self.quantity <= 0
            or _POSITIVE_DECIMAL.fullmatch(canonical_decimal(self.quantity)) is None
        ):
            raise ValueError("tool usage quantity must be a positive 26.12 decimal")
        if _UNIT.fullmatch(self.unit) is None:
            raise ValueError("tool usage unit must be a canonical unit")

    @classmethod
    def from_input(
        cls,
        quantity: ToolQuantityInput = 1,
        *,
        metric: str = "call_count",
        unit: str = "Calls",
    ) -> ToolUsage:
        """Create exact usage without accepting lossy binary floats."""
        if isinstance(quantity, (bool, float)):
            raise TypeError("tool usage quantity must be a Decimal, integer, or decimal string")
        try:
            exact = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("tool usage quantity is not a plain decimal") from exc
        return cls(metric=metric, quantity=exact, unit=unit)


__all__ = ["ToolQuantityInput", "ToolUsage"]
