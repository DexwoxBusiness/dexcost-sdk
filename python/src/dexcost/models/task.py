"""Task data model — represents a single business task."""

from __future__ import annotations

import decimal
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from dexcost.network_accountant import NetworkAccountant


@dataclass
class Task:
    """A tracked business task (e.g., 'resolve support ticket').

    All downstream events roll up into the aggregated cost and token fields
    on this model.  ``metadata`` is an open dict for caller-defined context
    (customer tier, ticket id, etc.).
    """

    # Identity
    task_id: uuid.UUID = field(default_factory=uuid.uuid4)
    task_type: str = ""
    status: str = "pending"  # pending | success | failed

    # Timing
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None

    # Flexible metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    # Denormalised look-up keys (fast queries without JSONB parsing)
    customer_id: str | None = None
    project_id: str | None = None

    # Hierarchy
    parent_task_id: uuid.UUID | None = None

    # Experiment tracking
    experiment_id: str | None = None
    variant: str | None = None

    # Aggregated costs (rolled up from child events)
    llm_cost_usd: Decimal = Decimal("0")
    external_cost_usd: Decimal = Decimal("0")
    compute_cost_usd: Decimal = Decimal("0")
    network_cost_usd: Decimal = Decimal("0")
    total_cost_usd: Decimal = Decimal("0")

    # Token totals
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0

    # Waste metrics
    retry_count: int = 0
    retry_cost_usd: Decimal = Decimal("0")
    failure_count: int = 0

    # Network capture (rolled up from instrumented HTTP calls)
    network_bytes_in: int = 0
    network_bytes_out: int = 0
    network_call_count: int = 0
    network_by_host: dict[str, Any] = field(default_factory=lambda: {"hosts": []})

    # In-memory only — the per-task byte accumulator. Never serialised:
    # to_dict()/from_dict() do not touch it; a fresh task gets a fresh one.
    _network: NetworkAccountant = field(
        init=False, default_factory=NetworkAccountant, compare=False, repr=False
    )

    # Schema contract
    schema_version: str = "1"

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dictionary."""
        return {
            "task_id": str(self.task_id),
            "task_type": self.task_type,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "metadata": self.metadata,
            "customer_id": self.customer_id,
            "project_id": self.project_id,
            "parent_task_id": str(self.parent_task_id) if self.parent_task_id else None,
            "experiment_id": self.experiment_id,
            "variant": self.variant,
            "llm_cost_usd": str(self.llm_cost_usd),
            "external_cost_usd": str(self.external_cost_usd),
            "compute_cost_usd": str(self.compute_cost_usd),
            "network_cost_usd": str(self.network_cost_usd),
            "total_cost_usd": str(self.total_cost_usd),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "retry_count": self.retry_count,
            "retry_cost_usd": str(self.retry_cost_usd),
            "failure_count": self.failure_count,
            "network_bytes_in": self.network_bytes_in,
            "network_bytes_out": self.network_bytes_out,
            "network_call_count": self.network_call_count,
            "network_by_host": self.network_by_host,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Deserialise from a JSON-safe dictionary.

        Raises:
            ValueError: If required fields are missing or malformed.
        """
        try:
            return cls(
                task_id=uuid.UUID(data["task_id"]),
                task_type=data["task_type"],
                status=data["status"],
                started_at=datetime.fromisoformat(data["started_at"]),
                ended_at=datetime.fromisoformat(data["ended_at"]) if data.get("ended_at") else None,
                metadata=data.get("metadata", {}),
                customer_id=data.get("customer_id"),
                project_id=data.get("project_id"),
                parent_task_id=(
                    uuid.UUID(data["parent_task_id"]) if data.get("parent_task_id") else None
                ),
                experiment_id=data.get("experiment_id"),
                variant=data.get("variant"),
                llm_cost_usd=Decimal(data["llm_cost_usd"]),
                external_cost_usd=Decimal(data["external_cost_usd"]),
                compute_cost_usd=Decimal(data["compute_cost_usd"]),
                network_cost_usd=Decimal(data.get("network_cost_usd", "0")),
                total_cost_usd=Decimal(data["total_cost_usd"]),
                total_input_tokens=data["total_input_tokens"],
                total_output_tokens=data["total_output_tokens"],
                total_cached_tokens=data["total_cached_tokens"],
                retry_count=data["retry_count"],
                retry_cost_usd=Decimal(data["retry_cost_usd"]),
                failure_count=data["failure_count"],
                network_bytes_in=data.get("network_bytes_in", 0),
                network_bytes_out=data.get("network_bytes_out", 0),
                network_call_count=data.get("network_call_count", 0),
                network_by_host=data.get("network_by_host") or {"hosts": []},
                schema_version=data.get("schema_version", "1"),
            )
        except (KeyError, ValueError, TypeError, decimal.InvalidOperation) as exc:
            raise ValueError(f"Invalid task data: {exc}") from exc
