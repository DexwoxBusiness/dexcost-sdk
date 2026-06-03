"""Event data model — a single cost-generating event within a task."""

from __future__ import annotations

import decimal
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from dexcost.models._serde import canonical_decimal, iso_canonical, parse_canonical


@dataclass
class Event:
    """An individual cost event (LLM call, external API, compute, retry).

    Matches the Dexcost Standard Event Schema v1.  LLM-specific fields are
    ``None`` for non-LLM event types.
    """

    # Identity
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    task_id: uuid.UUID = field(default_factory=uuid.uuid4)
    event_type: str = "llm_call"  # llm_call | external_cost | compute_cost | retry_marker
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Cost
    cost_usd: Decimal = Decimal("0")
    cost_confidence: str = "exact"  # exact | computed | estimated | unknown
    pricing_source: str | None = None  # litellm | tokencost | provider_response | manual
    pricing_version: str | None = None

    # Service identification
    service_name: str | None = None

    # LLM-specific (nullable for non-LLM events)
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    latency_ms: int | None = None

    # Retry tracking (first-class)
    is_retry: bool = False
    retry_reason: str | None = None  # rate_limit | timeout | parse_error | 5xx
    retry_of: uuid.UUID | None = None  # FK → original event_id

    # Extensible type-specific details
    details: dict[str, Any] = field(default_factory=dict)

    # Schema contract
    schema_version: str = "1"

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dictionary."""
        return {
            "event_id": str(self.event_id),
            "task_id": str(self.task_id),
            "event_type": self.event_type,
            "occurred_at": iso_canonical(self.occurred_at),
            "cost_usd": canonical_decimal(self.cost_usd),
            "cost_confidence": self.cost_confidence,
            "pricing_source": self.pricing_source,
            "pricing_version": self.pricing_version,
            "service_name": self.service_name,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "latency_ms": self.latency_ms,
            "is_retry": self.is_retry,
            "retry_reason": self.retry_reason,
            "retry_of": str(self.retry_of) if self.retry_of else None,
            "details": self.details,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        """Deserialise from a JSON-safe dictionary.

        Raises:
            ValueError: If required fields are missing or malformed.
        """
        try:
            return cls(
                event_id=uuid.UUID(data["event_id"]),
                task_id=uuid.UUID(data["task_id"]),
                event_type=data["event_type"],
                occurred_at=parse_canonical(data["occurred_at"]),
                cost_usd=Decimal(data["cost_usd"]),
                cost_confidence=data["cost_confidence"],
                pricing_source=data.get("pricing_source"),
                pricing_version=data.get("pricing_version"),
                service_name=data.get("service_name"),
                provider=data.get("provider"),
                model=data.get("model"),
                input_tokens=data.get("input_tokens"),
                output_tokens=data.get("output_tokens"),
                cached_tokens=data.get("cached_tokens"),
                latency_ms=data.get("latency_ms"),
                is_retry=data.get("is_retry", False),
                retry_reason=data.get("retry_reason"),
                retry_of=uuid.UUID(data["retry_of"]) if data.get("retry_of") else None,
                details=data.get("details", {}),
                schema_version=data.get("schema_version", "1"),
            )
        except (KeyError, ValueError, TypeError, decimal.InvalidOperation) as exc:
            raise ValueError(f"Invalid event data: {exc}") from exc
