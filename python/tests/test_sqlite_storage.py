"""Tests for SQLite storage backend (US-003).

Covers: schema creation, WAL mode, version tracking, CRUD for tasks/events,
query filters, and round-trip data integrity.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from dexcost.models import Event, EventType, Task, TaskStatus
from dexcost.storage.sqlite import SQLiteStorage


@pytest.fixture()
def storage(tmp_path: Path) -> SQLiteStorage:
    """Create a fresh SQLite storage in a temp directory."""
    db_path = tmp_path / "test.db"
    s = SQLiteStorage(db_path=db_path)
    yield s  # type: ignore[misc]
    s.close()


# ── Schema & config tests ────────────────────────────────────────────


class TestSchemaCreation:
    def test_tables_created(self, storage: SQLiteStorage) -> None:
        """All three tables must exist after init."""
        rows = storage._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
        assert "tasks" in names
        assert "events" in names
        assert "schema_version" in names

    def test_indexes_created(self, storage: SQLiteStorage) -> None:
        rows = storage._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        idx_names = {r[0] for r in rows}
        assert "idx_tasks_customer" in idx_names
        assert "idx_tasks_type" in idx_names
        assert "idx_tasks_period" in idx_names
        assert "idx_events_task" in idx_names
        assert "idx_events_type" in idx_names
        assert "idx_events_sync" in idx_names

    def test_wal_mode_enabled(self, storage: SQLiteStorage) -> None:
        row = storage._conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_schema_version_seeded(self, storage: SQLiteStorage) -> None:
        from dexcost.storage.migrations import TARGET_SCHEMA_VERSION

        assert storage.get_schema_version() == TARGET_SCHEMA_VERSION

    def test_set_schema_version(self, storage: SQLiteStorage) -> None:
        storage.set_schema_version(2, "add_tags_column")
        assert storage.get_schema_version() == 2

    def test_custom_path(self, tmp_path: Path) -> None:
        custom = tmp_path / "sub" / "dir" / "my.db"
        s = SQLiteStorage(db_path=custom)
        assert custom.exists()
        s.close()

    def test_default_path_creates_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Default path should auto-create ~/.dexcost/ directory."""
        import dexcost.storage.sqlite as mod

        fake_dir = tmp_path / "fakehome" / ".dexcost"
        fake_db = fake_dir / "buffer.db"
        monkeypatch.setattr(mod, "_DEFAULT_DB_DIR", fake_dir)
        monkeypatch.setattr(mod, "_DEFAULT_DB_PATH", fake_db)
        s = SQLiteStorage()
        assert fake_db.exists()
        s.close()


# ── Task CRUD ─────────────────────────────────────────────────────────


class TestTaskCrud:
    def test_insert_and_get(self, storage: SQLiteStorage) -> None:
        task = Task(
            task_type="resolve_ticket",
            status=TaskStatus.PENDING.value,
            customer_id="acme",
            project_id="proj_a",
            llm_cost_usd=Decimal("0.035"),
            total_cost_usd=Decimal("0.05"),
            total_input_tokens=1500,
            metadata={"tier": "enterprise"},
        )
        storage.insert_task(task)
        got = storage.get_task(str(task.task_id))
        assert got is not None
        assert got.task_id == task.task_id
        assert got.task_type == "resolve_ticket"
        assert got.customer_id == "acme"
        assert got.llm_cost_usd == Decimal("0.035")
        assert got.total_input_tokens == 1500
        assert got.metadata == {"tier": "enterprise"}

    def test_get_missing_returns_none(self, storage: SQLiteStorage) -> None:
        assert storage.get_task(str(uuid.uuid4())) is None

    def test_update_task(self, storage: SQLiteStorage) -> None:
        task = Task(task_type="generate_report", status=TaskStatus.PENDING.value)
        storage.insert_task(task)

        task.status = TaskStatus.SUCCESS.value
        task.ended_at = datetime.now(timezone.utc)
        task.total_cost_usd = Decimal("1.23")
        task.retry_count = 2
        storage.update_task(task)

        got = storage.get_task(str(task.task_id))
        assert got is not None
        assert got.status == "success"
        assert got.ended_at is not None
        assert got.total_cost_usd == Decimal("1.23")
        assert got.retry_count == 2

    def test_query_by_customer_id(self, storage: SQLiteStorage) -> None:
        for cid in ["acme", "acme", "beta"]:
            storage.insert_task(Task(task_type="t", customer_id=cid))
        results = storage.query_tasks(customer_id="acme")
        assert len(results) == 2
        assert all(t.customer_id == "acme" for t in results)

    def test_query_by_task_type(self, storage: SQLiteStorage) -> None:
        storage.insert_task(Task(task_type="classify"))
        storage.insert_task(Task(task_type="resolve"))
        storage.insert_task(Task(task_type="classify"))
        results = storage.query_tasks(task_type="classify")
        assert len(results) == 2

    def test_query_by_status(self, storage: SQLiteStorage) -> None:
        storage.insert_task(Task(task_type="t", status="success"))
        storage.insert_task(Task(task_type="t", status="failed"))
        storage.insert_task(Task(task_type="t", status="success"))
        assert len(storage.query_tasks(status="success")) == 2
        assert len(storage.query_tasks(status="failed")) == 1

    def test_query_by_date_range(self, storage: SQLiteStorage) -> None:
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=30)
        storage.insert_task(Task(task_type="old", started_at=old))
        storage.insert_task(Task(task_type="new", started_at=now))

        week_ago = now - timedelta(days=7)
        results = storage.query_tasks(started_after=week_ago)
        assert len(results) == 1
        assert results[0].task_type == "new"

    def test_parent_task_id_round_trip(self, storage: SQLiteStorage) -> None:
        parent_id = uuid.uuid4()
        task = Task(task_type="child", parent_task_id=parent_id)
        storage.insert_task(task)
        got = storage.get_task(str(task.task_id))
        assert got is not None
        assert got.parent_task_id == parent_id


# ── Event CRUD ────────────────────────────────────────────────────────


class TestEventCrud:
    def test_insert_and_query_by_task(self, storage: SQLiteStorage) -> None:
        task = Task(task_type="t")
        storage.insert_task(task)

        event = Event(
            task_id=task.task_id,
            event_type=EventType.LLM_CALL.value,
            cost_usd=Decimal("0.035"),
            provider="openai",
            model="gpt-4o",
            input_tokens=1500,
            output_tokens=800,
            cached_tokens=500,
            latency_ms=1200,
            service_name="openai",
            cost_confidence="exact",
            pricing_source="provider_response",
        )
        storage.insert_event(event)

        results = storage.query_events(task_id=task.task_id)
        assert len(results) == 1
        got = results[0]
        assert got.event_id == event.event_id
        assert got.provider == "openai"
        assert got.model == "gpt-4o"
        assert got.cost_usd == Decimal("0.035")
        assert got.input_tokens == 1500
        assert got.latency_ms == 1200

    def test_query_by_event_type(self, storage: SQLiteStorage) -> None:
        tid = uuid.uuid4()
        storage.insert_event(Event(task_id=tid, event_type="llm_call"))
        storage.insert_event(Event(task_id=tid, event_type="external_cost"))
        storage.insert_event(Event(task_id=tid, event_type="llm_call"))

        results = storage.query_events(event_type="llm_call")
        assert len(results) == 2

    def test_retry_fields_round_trip(self, storage: SQLiteStorage) -> None:
        original = Event(event_type="llm_call", cost_usd=Decimal("0.03"))
        storage.insert_event(original)

        retry = Event(
            task_id=original.task_id,
            event_type="retry_marker",
            is_retry=True,
            retry_reason="rate_limit",
            retry_of=original.event_id,
            cost_usd=Decimal("0.03"),
        )
        storage.insert_event(retry)

        results = storage.query_events(event_type="retry_marker")
        assert len(results) == 1
        got = results[0]
        assert got.is_retry is True
        assert got.retry_reason == "rate_limit"
        assert got.retry_of == original.event_id

    def test_external_cost_event(self, storage: SQLiteStorage) -> None:
        event = Event(
            event_type="external_cost",
            cost_usd=Decimal("0.50"),
            service_name="google_maps_api",
            details={"endpoint": "/geocode"},
        )
        storage.insert_event(event)
        results = storage.query_events(event_type="external_cost")
        assert len(results) == 1
        assert results[0].service_name == "google_maps_api"
        assert results[0].details == {"endpoint": "/geocode"}
        assert results[0].provider is None  # not an LLM event

    def test_query_by_time_range(self, storage: SQLiteStorage) -> None:
        now = datetime.now(timezone.utc)
        old = now - timedelta(hours=2)
        storage.insert_event(Event(event_type="llm_call", occurred_at=old))
        storage.insert_event(Event(event_type="llm_call", occurred_at=now))

        cutoff = now - timedelta(hours=1)
        results = storage.query_events(after=cutoff)
        assert len(results) == 1


# ── Sync lifecycle tests (US-004) ────────────────────────────────────


class TestSyncLifecycle:
    """sync_status column, query_events_for_sync, and mark_synced."""

    def test_sync_status_column_exists(self, storage: SQLiteStorage) -> None:
        """events table must have a sync_status column."""
        cols = [
            r[1]
            for r in storage._conn.execute("PRAGMA table_info(events)").fetchall()
        ]
        assert "sync_status" in cols

    def test_sync_status_defaults_to_pending(self, storage: SQLiteStorage) -> None:
        """Newly inserted events should have sync_status='pending'."""
        event = Event(event_type="llm_call", cost_usd=Decimal("0.01"))
        storage.insert_event(event)
        row = storage._conn.execute(
            "SELECT sync_status FROM events WHERE event_id=?",
            (str(event.event_id),),
        ).fetchone()
        assert row["sync_status"] == "pending"

    def test_query_events_for_sync_returns_pending(
        self, storage: SQLiteStorage
    ) -> None:
        """query_events_for_sync returns only pending events."""
        e1 = Event(event_type="llm_call", cost_usd=Decimal("0.01"))
        e2 = Event(event_type="llm_call", cost_usd=Decimal("0.02"))
        storage.insert_event(e1)
        storage.insert_event(e2)

        pending = storage.query_events_for_sync()
        assert len(pending) == 2
        event_ids = {e.event_id for e in pending}
        assert e1.event_id in event_ids
        assert e2.event_id in event_ids

    def test_query_events_for_sync_respects_limit(
        self, storage: SQLiteStorage
    ) -> None:
        """query_events_for_sync honours an optional limit parameter."""
        for _ in range(5):
            storage.insert_event(Event(event_type="llm_call", cost_usd=Decimal("0.01")))
        pending = storage.query_events_for_sync(limit=3)
        assert len(pending) == 3

    def test_mark_synced_updates_status(self, storage: SQLiteStorage) -> None:
        """mark_synced transitions events from pending to synced."""
        e1 = Event(event_type="llm_call", cost_usd=Decimal("0.01"))
        e2 = Event(event_type="llm_call", cost_usd=Decimal("0.02"))
        storage.insert_event(e1)
        storage.insert_event(e2)

        storage.mark_synced([str(e1.event_id)])

        # e1 should be synced
        row = storage._conn.execute(
            "SELECT sync_status FROM events WHERE event_id=?",
            (str(e1.event_id),),
        ).fetchone()
        assert row["sync_status"] == "synced"

        # e2 should still be pending
        row2 = storage._conn.execute(
            "SELECT sync_status FROM events WHERE event_id=?",
            (str(e2.event_id),),
        ).fetchone()
        assert row2["sync_status"] == "pending"

    def test_synced_events_excluded_from_query_for_sync(
        self, storage: SQLiteStorage
    ) -> None:
        """After mark_synced, those events should not appear in query_events_for_sync."""
        e1 = Event(event_type="llm_call", cost_usd=Decimal("0.01"))
        e2 = Event(event_type="llm_call", cost_usd=Decimal("0.02"))
        storage.insert_event(e1)
        storage.insert_event(e2)

        storage.mark_synced([str(e1.event_id)])

        pending = storage.query_events_for_sync()
        assert len(pending) == 1
        assert pending[0].event_id == e2.event_id

    def test_mark_synced_empty_list_is_noop(self, storage: SQLiteStorage) -> None:
        """Passing an empty list to mark_synced should not raise."""
        storage.mark_synced([])  # should not raise

    def test_sync_index_exists(self, storage: SQLiteStorage) -> None:
        """An index on (sync_status, timestamp) must exist for sync queries."""
        rows = storage._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_events_sync'"
        ).fetchall()
        assert len(rows) == 1


# ── Default DB path tests (US-004) ───────────────────────────────────


class TestDefaultDbPath:
    def test_default_db_filename_is_buffer(self) -> None:
        """Default DB filename should be 'buffer.db', not 'costs.db'."""
        from dexcost.storage.sqlite import _DEFAULT_DB_PATH

        assert _DEFAULT_DB_PATH.name == "buffer.db"
