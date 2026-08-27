"""Storage protocol — the contract that all backends must implement."""

from __future__ import annotations

from typing import Any, Protocol

from dexcost.models.event import Event
from dexcost.models.outcome import OutcomeRevision
from dexcost.models.provider_job import ProviderJobRevision
from dexcost.models.revenue import RevenueRevision
from dexcost.models.task import Task


class StorageBackend(Protocol):
    """Abstract interface for dexcost storage backends.

    Both SQLite (US-003) and PostgreSQL (US-004) implement this protocol.
    """

    # ── Schema management ─────────────────────────────────────────────

    def create_schema(self) -> None:
        """Create all tables, indexes, and initial schema version."""
        ...

    def get_schema_version(self) -> int:
        """Return the current schema version number."""
        ...

    def set_schema_version(self, version: int, migration_name: str = "") -> None:
        """Record a new schema version after a migration."""
        ...

    # ── Task CRUD ─────────────────────────────────────────────────────

    def insert_task(self, task: Task) -> None:
        """Persist a new task."""
        ...

    def update_task(self, task: Task) -> None:
        """Update an existing task (matched by task_id)."""
        ...

    def query_tasks(self, **filters: Any) -> list[Task]:
        """Return tasks matching the given filters.

        Supported filters: customer_id, task_type, project_id, status,
        started_after, started_before.
        """
        ...

    def get_task(self, task_id: str) -> Task | None:
        """Return a single task by ID, or None if not found."""
        ...

    # ── Event CRUD ────────────────────────────────────────────────────

    def insert_event(self, event: Event) -> None:
        """Persist a new event."""
        ...

    def update_event(self, event: Event) -> None:
        """Update an existing event (matched by event_id)."""
        ...

    def query_events(self, **filters: Any) -> list[Event]:
        """Return events matching the given filters.

        Supported filters: event_id, task_id, event_type, customer_id, after, before.
        """
        ...

    def query_events_for_sync(self, limit: int = 1000) -> list[Event]:
        """Return pending events ready for sync, oldest first."""
        ...

    def query_event_deliveries_for_sync(self, limit: int = 1000) -> list[tuple[Event, int]]:
        """Return pending event snapshots with optimistic delivery versions."""
        ...

    def mark_event_deliveries_synced(self, deliveries: list[tuple[str, int]]) -> None:
        """Acknowledge only the exact event revisions accepted by ingestion."""
        ...

    def mark_event_deliveries_quarantined(self, deliveries: list[tuple[str, int]]) -> None:
        """Quarantine only exact event snapshots that failed conversion."""
        ...

    def mark_synced(self, event_ids: list[str]) -> None:
        """Transition events from pending to synced."""
        ...

    def mark_quarantined(self, event_ids: list[str]) -> None:
        """Retain unrepresentable events outside the normal pending queue."""
        ...

    def requeue_quarantined_events(self) -> int:
        """Move retained conversion failures back to pending after an upgrade.

        Returns the number of rows requeued. Implementations must preserve the
        original event identity, occurrence time, and payload.
        """
        ...

    def query_quarantined_events(self, limit: int = 100) -> list[Event]:
        """Return quarantined conversion failures, oldest first."""
        ...

    def query_tasks_for_sync(self, task_ids: list[str]) -> list[Task]:
        """Return tasks matching the given IDs (for inclusion in sync payloads)."""
        ...

    def query_pending_tasks_for_sync(self, limit: int = 1000) -> list[Task]:
        """Return tasks not yet synced, oldest first."""
        ...

    def query_task_deliveries_for_sync(self, limit: int = 1000) -> list[tuple[Task, int]]:
        """Return pending task snapshots with optimistic delivery versions."""
        ...

    def mark_task_deliveries_synced(self, deliveries: list[tuple[str, int]]) -> None:
        """Acknowledge only the exact task revisions accepted by ingestion."""
        ...

    def mark_tasks_synced(self, task_ids: list[str]) -> None:
        """Transition tasks from pending to synced."""
        ...

    # ── Outcome revision CRUD ────────────────────────────────────────

    def insert_outcome(self, outcome: OutcomeRevision) -> None:
        """Append one validated business-outcome revision."""
        ...

    def query_outcomes_for_sync(self, limit: int = 1000) -> list[OutcomeRevision]:
        """Return pending outcome revisions, oldest first."""
        ...

    def query_outcome_history(self, outcome_id: str) -> list[OutcomeRevision]:
        """Return every revision for one outcome, oldest first."""
        ...

    def mark_outcomes_synced(self, revisions: list[tuple[str, int]]) -> None:
        """Transition outcome revisions from pending to synced."""
        ...

    def mark_outcomes_quarantined(self, revisions: list[tuple[str, int]]) -> None:
        """Retain undeliverable outcome revisions outside the pending queue."""
        ...

    # ── Revenue revision CRUD ────────────────────────────────────────

    def insert_revenue(self, revenue: RevenueRevision) -> None:
        """Append one validated revenue revision."""
        ...

    def query_revenues_for_sync(self, limit: int = 1000) -> list[RevenueRevision]:
        """Return pending revenue revisions, oldest first."""
        ...

    def query_revenue_history(self, revenue_id: str) -> list[RevenueRevision]:
        """Return every revision for one revenue record, oldest first."""
        ...

    def mark_revenues_synced(self, revisions: list[tuple[str, int]]) -> None:
        """Transition revenue revisions from pending to synced."""
        ...

    def mark_revenues_quarantined(self, revisions: list[tuple[str, int]]) -> None:
        """Retain undeliverable revenue revisions outside the pending queue."""
        ...

    # ── Asynchronous provider-job revision CRUD ─────────────────────

    def insert_provider_job_revision(self, job: ProviderJobRevision) -> None:
        """Append one immutable provider-job observation revision."""
        ...

    def query_provider_job_history(self, event_id: str) -> list[ProviderJobRevision]:
        """Return every revision for one provider job, oldest first."""
        ...

    def get_provider_job(
        self, provider: str, service: str, provider_record_id: str
    ) -> ProviderJobRevision | None:
        """Return the latest local snapshot for a provider-owned job identity."""
        ...

    def query_provider_jobs_for_sync(self, limit: int = 1000) -> list[ProviderJobRevision]:
        """Return pending provider-job revisions, oldest first."""
        ...

    def query_current_provider_jobs_for_task(self, task_id: str) -> list[ProviderJobRevision]:
        """Return the latest revision of every provider job attached to a task."""
        ...

    def mark_provider_jobs_synced(self, revisions: list[tuple[str, int]]) -> None:
        """Transition provider-job revisions from pending to synced."""
        ...

    def mark_provider_jobs_quarantined(self, revisions: list[tuple[str, int]]) -> None:
        """Retain undeliverable provider-job revisions outside the pending queue."""
        ...

    def delivery_counts(self) -> dict[str, Any]:
        """Return durable pending/quarantine depths for delivery observability."""
        ...

    def purge_synced(self, retention_hours: int = 48) -> int:
        """Delete synced events older than *retention_hours* and reclaim space.

        Returns the number of deleted rows.
        """
        ...

    def purge_old_pending(self, max_age_days: int = 7) -> int:
        """Remove pending or quarantined events older than *max_age_days*.

        This is an explicit destructive maintenance operation. The background
        delivery worker never calls it because unsent attribution records must
        not disappear solely because delivery or conversion was unavailable.
        Returns the number of deleted rows.
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Release any resources held by the backend."""
        ...
