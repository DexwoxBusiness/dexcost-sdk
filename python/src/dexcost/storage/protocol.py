"""Storage protocol — the contract that all backends must implement."""

from __future__ import annotations

from typing import Any, Protocol

from dexcost.models.event import Event
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

        Supported filters: task_id, event_type, customer_id, after, before.
        """
        ...

    def query_events_for_sync(self, limit: int = 1000) -> list[Event]:
        """Return pending events ready for sync, oldest first."""
        ...

    def mark_synced(self, event_ids: list[str]) -> None:
        """Transition events from pending to synced."""
        ...

    def query_tasks_for_sync(self, task_ids: list[str]) -> list[Task]:
        """Return tasks matching the given IDs (for inclusion in sync payloads)."""
        ...

    def query_pending_tasks_for_sync(self, limit: int = 1000) -> list[Task]:
        """Return tasks not yet synced, oldest first."""
        ...

    def mark_tasks_synced(self, task_ids: list[str]) -> None:
        """Transition tasks from pending to synced."""
        ...

    def purge_synced(self, retention_hours: int = 48) -> int:
        """Delete synced events older than *retention_hours* and reclaim space.

        Returns the number of deleted rows.
        """
        ...

    def purge_old_pending(self, max_age_days: int = 7) -> int:
        """Remove pending events older than *max_age_days*.

        Safety net for events that can never be synced (invalid API key, etc.).
        Returns the number of deleted rows.
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Release any resources held by the backend."""
        ...
