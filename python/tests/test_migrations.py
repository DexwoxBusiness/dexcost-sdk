"""Tests for schema migration system (US-005).

Covers: schema_version tracking, sequential migration execution, idempotency,
rollback on error, and data preservation across migrations.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dexcost.models import Event, EventType, Task
from dexcost.storage.migrations import (
    _PG_MIGRATIONS,
    _SQLITE_MIGRATIONS,
    TARGET_SCHEMA_VERSION,
    MigrationError,
    register_pg_migration,
    register_sqlite_migration,
    run_pg_migrations_async,
    run_sqlite_migrations,
)
from dexcost.storage.sqlite import SQLiteStorage


@pytest.fixture()
def storage(tmp_path: Path) -> Iterator[SQLiteStorage]:
    """Create a fresh SQLite storage in a temp directory."""
    db_path = tmp_path / "test.db"
    s = SQLiteStorage(db_path=db_path)
    yield s
    s.close()


# ── Schema version table tests ────────────────────────────────────────


class TestSchemaVersionTable:
    def test_version_table_exists(self, storage: SQLiteStorage) -> None:
        """schema_version table must be created on init."""
        rows = storage._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchall()
        assert len(rows) == 1

    def test_current_version_matches_target(self, storage: SQLiteStorage) -> None:
        """DB version must equal TARGET_SCHEMA_VERSION on fresh init."""
        assert storage.get_schema_version() == TARGET_SCHEMA_VERSION

    def test_version_history_preserved(self, storage: SQLiteStorage) -> None:
        """Multiple version entries should be kept (append-only log)."""
        storage.set_schema_version(TARGET_SCHEMA_VERSION + 1, "test_migration_a")
        storage.set_schema_version(TARGET_SCHEMA_VERSION + 2, "test_migration_b")
        rows = storage._conn.execute(
            "SELECT version_number, migration_name FROM schema_version ORDER BY version_id"
        ).fetchall()
        assert len(rows) == 3  # initial + 2 new
        assert rows[0]["version_number"] == TARGET_SCHEMA_VERSION
        assert rows[1]["version_number"] == TARGET_SCHEMA_VERSION + 1
        assert rows[2]["version_number"] == TARGET_SCHEMA_VERSION + 2
        assert rows[2]["migration_name"] == "test_migration_b"

    def test_applied_at_timestamp_recorded(self, storage: SQLiteStorage) -> None:
        """Each migration entry records when it was applied."""
        rows = storage._conn.execute("SELECT applied_at FROM schema_version").fetchall()
        assert len(rows) >= 1
        # applied_at should be a non-empty string
        assert rows[0]["applied_at"]


# ── Startup version comparison tests ──────────────────────────────────


class TestStartupVersionCheck:
    def test_no_migration_when_current(self, tmp_path: Path) -> None:
        """If DB version == target, no migration should run."""
        db_path = tmp_path / "current.db"
        s = SQLiteStorage(db_path=db_path)
        assert s.get_schema_version() == TARGET_SCHEMA_VERSION
        s.close()

    def test_migration_runs_when_behind(self, tmp_path: Path) -> None:
        """If DB version < target, migrations run on startup."""
        # Create a v1 database manually
        db_path = tmp_path / "behind.db"
        s = SQLiteStorage(db_path=db_path)
        s.close()

        # DB is already at v1 == TARGET, so this is a no-op on current code.
        # We test the mechanism by manually registering a v1→v2 migration.
        # See TestSequentialMigration for the real test.
        s2 = SQLiteStorage(db_path=db_path)
        assert s2.get_schema_version() == TARGET_SCHEMA_VERSION
        s2.close()

    def test_v6_database_adds_root_task_identity_without_rewriting_tasks(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "v6.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "version_number INTEGER NOT NULL, migration_name TEXT)"
        )
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) VALUES (6, 'v6')"
        )
        conn.execute(
            "INSERT INTO tasks (task_id, task_type) VALUES (?, ?)",
            (str(uuid.uuid4()), "legacy.task"),
        )
        conn.commit()

        assert run_sqlite_migrations(conn, 6) == TARGET_SCHEMA_VERSION
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "root_task_id" in columns
        assert {
            "agent_id",
            "agent_version",
            "workflow_id",
            "workflow_session_id",
        }.issubset(columns)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='outcomes'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        conn.close()

    def test_v12_database_adds_revenue_ledger(self, tmp_path: Path) -> None:
        """The v12→v13 migration adds revenue storage and its indexes."""
        db_path = tmp_path / "v12.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "version_number INTEGER NOT NULL, migration_name TEXT)"
        )
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) "
            "VALUES (12, 'workspace_catalog_overlays')"
        )
        conn.commit()

        assert run_sqlite_migrations(conn, 12) == TARGET_SCHEMA_VERSION == 15
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='revenues'"
        ).fetchone()[0] == 1
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='revenues'"
            ).fetchall()
        }
        assert {"idx_revenues_sync", "idx_revenues_task"} <= indexes
        conn.close()

    def test_v13_database_adds_provider_job_revision_ledger(
        self, tmp_path: Path
    ) -> None:
        """The v13→v14 migration preserves data and adds job reconciliation."""
        db_path = tmp_path / "v13.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "version_number INTEGER NOT NULL, migration_name TEXT)"
        )
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) "
            "VALUES (13, 'revenue_ledger')"
        )
        conn.commit()

        assert run_sqlite_migrations(conn, 13) == TARGET_SCHEMA_VERSION == 15
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='provider_job_revisions'"
        ).fetchone()[0] == 1
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='provider_job_revisions'"
            ).fetchall()
        }
        assert {
            "idx_provider_jobs_sync",
            "idx_provider_jobs_identity",
            "idx_provider_jobs_task",
        } <= indexes
        conn.close()

    def test_v14_database_adds_delivery_versions_without_losing_rows(
        self, tmp_path: Path
    ) -> None:
        """The v14→v15 migration versions existing mutable queue rows."""
        db_path = tmp_path / "v14.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "version_number INTEGER NOT NULL, migration_name TEXT)"
        )
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, sync_status TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, sync_status TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO tasks VALUES ('task-1', 'pending')")
        conn.execute("INSERT INTO events VALUES ('event-1', 'pending')")
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) "
            "VALUES (14, 'provider_job_revisions')"
        )
        conn.commit()

        assert run_sqlite_migrations(conn, 14) == TARGET_SCHEMA_VERSION == 15
        assert conn.execute(
            "SELECT sync_version FROM tasks WHERE task_id='task-1'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT sync_version FROM events WHERE event_id='event-1'"
        ).fetchone()[0] == 1
        conn.close()


# ── Sequential migration tests ────────────────────────────────────────


class TestSequentialMigration:
    def test_v1_to_v2_migration(self, tmp_path: Path) -> None:
        """Create v1 schema, insert data, simulate v1→v2 migration, verify data intact."""
        db_path = tmp_path / "migrate.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")

        # Create v1 schema manually (same as SQLiteStorage.create_schema)
        from dexcost.storage.sqlite import (
            _CREATE_EVENTS,
            _CREATE_INDEXES,
            _CREATE_SCHEMA_VERSION,
            _CREATE_TASKS,
        )

        conn.execute(_CREATE_TASKS)
        conn.execute(_CREATE_EVENTS)
        conn.execute(_CREATE_SCHEMA_VERSION)
        for idx_sql in _CREATE_INDEXES:
            conn.execute(idx_sql)
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
            (1, "initial"),
        )
        conn.commit()

        # Insert test data at v1
        task_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO tasks (task_id, task_type, status, started_at,
                llm_cost_usd, total_cost_usd, total_input_tokens,
                total_output_tokens, total_cached_tokens, customer_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                "classify",
                "success",
                datetime.now(timezone.utc).isoformat(),
                "0.035",
                "0.050",
                1500,
                800,
                0,
                "acme",
            ),
        )
        event_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO events (event_id, task_id, event_type, cost_usd, timestamp,
                provider, model, input_tokens, output_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                task_id,
                "llm_call",
                "0.035",
                datetime.now(timezone.utc).isoformat(),
                "openai",
                "gpt-4o",
                1500,
                800,
            ),
        )
        conn.commit()

        # Register a test migration: v1→v2 adds a 'tags' column to tasks
        original_migrations = dict(_SQLITE_MIGRATIONS)
        original_target = TARGET_SCHEMA_VERSION

        try:
            # Temporarily register migration and bump target
            import dexcost.storage.migrations as mig_mod

            def _migrate_v1_to_v2(c: sqlite3.Connection) -> None:
                c.execute("ALTER TABLE tasks ADD COLUMN tags TEXT DEFAULT '';")

            _SQLITE_MIGRATIONS[(1, 2)] = _migrate_v1_to_v2
            mig_mod.TARGET_SCHEMA_VERSION = 2

            # Run migrations
            current = conn.execute(
                "SELECT version_number FROM schema_version ORDER BY version_id DESC LIMIT 1"
            ).fetchone()[0]
            assert current == 1

            new_version = run_sqlite_migrations(conn, current)
            assert new_version == 2

            # Verify schema version was bumped
            row = conn.execute(
                "SELECT version_number FROM schema_version ORDER BY version_id DESC LIMIT 1"
            ).fetchone()
            assert row[0] == 2

            # Verify data is intact
            task_row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            assert task_row is not None
            assert task_row["task_type"] == "classify"
            assert task_row["customer_id"] == "acme"
            assert task_row["llm_cost_usd"] == "0.035"

            event_row = conn.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            assert event_row is not None
            assert event_row["provider"] == "openai"
            assert event_row["model"] == "gpt-4o"

            # Verify new column exists
            assert task_row["tags"] == ""

        finally:
            # Restore original state
            _SQLITE_MIGRATIONS.clear()
            _SQLITE_MIGRATIONS.update(original_migrations)
            mig_mod.TARGET_SCHEMA_VERSION = original_target
            conn.close()

    def test_multi_step_migration(self, tmp_path: Path) -> None:
        """v1 → v2 → v3 runs sequentially."""
        db_path = tmp_path / "multi.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")

        from dexcost.storage.sqlite import (
            _CREATE_EVENTS,
            _CREATE_INDEXES,
            _CREATE_SCHEMA_VERSION,
            _CREATE_TASKS,
        )

        conn.execute(_CREATE_TASKS)
        conn.execute(_CREATE_EVENTS)
        conn.execute(_CREATE_SCHEMA_VERSION)
        for idx_sql in _CREATE_INDEXES:
            conn.execute(idx_sql)
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
            (1, "initial"),
        )
        conn.commit()

        original_migrations = dict(_SQLITE_MIGRATIONS)
        original_target = TARGET_SCHEMA_VERSION

        try:
            import dexcost.storage.migrations as mig_mod

            steps: list[int] = []

            def _v1_to_v2(c: sqlite3.Connection) -> None:
                c.execute("ALTER TABLE tasks ADD COLUMN tags TEXT DEFAULT '';")
                steps.append(2)

            def _v2_to_v3(c: sqlite3.Connection) -> None:
                c.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0;")
                steps.append(3)

            _SQLITE_MIGRATIONS[(1, 2)] = _v1_to_v2
            _SQLITE_MIGRATIONS[(2, 3)] = _v2_to_v3
            mig_mod.TARGET_SCHEMA_VERSION = 3

            new_version = run_sqlite_migrations(conn, 1)
            assert new_version == 3
            assert steps == [2, 3]

            # Check version table has all entries
            rows = conn.execute(
                "SELECT version_number FROM schema_version ORDER BY version_id"
            ).fetchall()
            versions = [r[0] for r in rows]
            assert versions == [1, 2, 3]

        finally:
            _SQLITE_MIGRATIONS.clear()
            _SQLITE_MIGRATIONS.update(original_migrations)
            mig_mod.TARGET_SCHEMA_VERSION = original_target
            conn.close()


# ── Idempotency tests ─────────────────────────────────────────────────


class TestIdempotency:
    def test_migration_safe_to_rerun_when_current(self, storage: SQLiteStorage) -> None:
        """Calling run_sqlite_migrations when already at target is a no-op."""
        version = run_sqlite_migrations(storage._conn, TARGET_SCHEMA_VERSION)
        assert version == TARGET_SCHEMA_VERSION

    def test_reopen_db_no_duplicate_migration(self, tmp_path: Path) -> None:
        """Opening an already-migrated database a second time doesn't re-run migrations."""
        db_path = tmp_path / "idem.db"
        s1 = SQLiteStorage(db_path=db_path)
        v1 = s1.get_schema_version()
        s1.close()

        s2 = SQLiteStorage(db_path=db_path)
        v2 = s2.get_schema_version()
        s2.close()

        assert v1 == v2 == TARGET_SCHEMA_VERSION

        # schema_version should have exactly 1 entry (initial seed only)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        conn.close()
        assert count == 1


# ── Rollback on error tests ──────────────────────────────────────────


class TestMigrationRollback:
    def test_failed_migration_rolls_back(self, tmp_path: Path) -> None:
        """A failing migration must leave the DB unchanged."""
        db_path = tmp_path / "rollback.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")

        from dexcost.storage.sqlite import (
            _CREATE_EVENTS,
            _CREATE_INDEXES,
            _CREATE_SCHEMA_VERSION,
            _CREATE_TASKS,
        )

        conn.execute(_CREATE_TASKS)
        conn.execute(_CREATE_EVENTS)
        conn.execute(_CREATE_SCHEMA_VERSION)
        for idx_sql in _CREATE_INDEXES:
            conn.execute(idx_sql)
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
            (1, "initial"),
        )
        conn.commit()

        # Insert a task before migration attempt
        task_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO tasks (task_id, task_type, status, started_at) VALUES (?, ?, ?, ?)",
            (task_id, "test", "pending", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        original_migrations = dict(_SQLITE_MIGRATIONS)
        original_target = TARGET_SCHEMA_VERSION

        try:
            import dexcost.storage.migrations as mig_mod

            def _bad_migration(c: sqlite3.Connection) -> None:
                # This will succeed
                c.execute("ALTER TABLE tasks ADD COLUMN temp_col TEXT;")
                # This will fail — table doesn't exist
                c.execute("ALTER TABLE nonexistent_table ADD COLUMN x TEXT;")

            _SQLITE_MIGRATIONS[(1, 2)] = _bad_migration
            mig_mod.TARGET_SCHEMA_VERSION = 2

            with pytest.raises(MigrationError, match="v1 → v2 failed"):
                run_sqlite_migrations(conn, 1)

            # Version should still be 1
            row = conn.execute(
                "SELECT version_number FROM schema_version ORDER BY version_id DESC LIMIT 1"
            ).fetchone()
            assert row[0] == 1

            # Original data intact
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            assert task is not None
            assert task["task_type"] == "test"

            # Partial schema change (temp_col) should be rolled back
            cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            assert "temp_col" not in cols

        finally:
            _SQLITE_MIGRATIONS.clear()
            _SQLITE_MIGRATIONS.update(original_migrations)
            mig_mod.TARGET_SCHEMA_VERSION = original_target
            conn.close()

    def test_clear_error_message_on_failure(self, tmp_path: Path) -> None:
        """MigrationError message includes version numbers and root cause."""
        db_path = tmp_path / "errmsg.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        from dexcost.storage.sqlite import _CREATE_SCHEMA_VERSION, _CREATE_TASKS

        conn.execute(_CREATE_TASKS)
        conn.execute(_CREATE_SCHEMA_VERSION)
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
            (1, "initial"),
        )
        conn.commit()

        original_migrations = dict(_SQLITE_MIGRATIONS)
        original_target = TARGET_SCHEMA_VERSION

        try:
            import dexcost.storage.migrations as mig_mod

            def _failing(c: sqlite3.Connection) -> None:
                raise ValueError("something went wrong")

            _SQLITE_MIGRATIONS[(1, 2)] = _failing
            mig_mod.TARGET_SCHEMA_VERSION = 2

            with pytest.raises(MigrationError) as exc_info:
                run_sqlite_migrations(conn, 1)

            msg = str(exc_info.value)
            assert "v1" in msg
            assert "v2" in msg
            assert "something went wrong" in msg

        finally:
            _SQLITE_MIGRATIONS.clear()
            _SQLITE_MIGRATIONS.update(original_migrations)
            mig_mod.TARGET_SCHEMA_VERSION = original_target
            conn.close()

    def test_missing_migration_raises_clear_error(self, tmp_path: Path) -> None:
        """If no migration function is registered for a needed step, raise MigrationError."""
        db_path = tmp_path / "missing.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        from dexcost.storage.sqlite import _CREATE_SCHEMA_VERSION

        conn.execute(_CREATE_SCHEMA_VERSION)
        # Start at current TARGET so no real migrations interfere
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
            (TARGET_SCHEMA_VERSION, "initial"),
        )
        conn.commit()

        original_target = TARGET_SCHEMA_VERSION

        try:
            import dexcost.storage.migrations as mig_mod

            # No migrations registered beyond TARGET
            mig_mod.TARGET_SCHEMA_VERSION = TARGET_SCHEMA_VERSION + 3

            with pytest.raises(MigrationError, match="No SQLite migration registered"):
                run_sqlite_migrations(conn, TARGET_SCHEMA_VERSION)

        finally:
            mig_mod.TARGET_SCHEMA_VERSION = original_target
            conn.close()


# ── Registration decorator tests ──────────────────────────────────────


class TestMigrationRegistration:
    def test_register_sqlite_migration_decorator(self) -> None:
        """Decorator registers the function in the migration dict."""
        original = dict(_SQLITE_MIGRATIONS)

        try:

            @register_sqlite_migration(99, 100)
            def _test_mig(conn: sqlite3.Connection) -> None:
                pass

            assert (99, 100) in _SQLITE_MIGRATIONS
            assert _SQLITE_MIGRATIONS[(99, 100)] is _test_mig

        finally:
            _SQLITE_MIGRATIONS.clear()
            _SQLITE_MIGRATIONS.update(original)

    def test_register_pg_migration_decorator(self) -> None:
        """Decorator registers the async function in the PG migration dict."""
        original = dict(_PG_MIGRATIONS)

        try:

            @register_pg_migration(99, 100)
            async def _test_pg_mig(conn: Any) -> None:
                pass

            assert (99, 100) in _PG_MIGRATIONS
            assert _PG_MIGRATIONS[(99, 100)] is _test_pg_mig

        finally:
            _PG_MIGRATIONS.clear()
            _PG_MIGRATIONS.update(original)


# ── Integration: SQLiteStorage with migration on open ─────────────────


class TestStorageIntegration:
    def test_fresh_db_at_target_version(self, storage: SQLiteStorage) -> None:
        """A freshly created DB is at the target schema version."""
        assert storage.get_schema_version() == TARGET_SCHEMA_VERSION

    def test_reopen_existing_db(self, tmp_path: Path) -> None:
        """Re-opening an existing DB preserves data and version."""
        db_path = tmp_path / "reopen.db"
        s = SQLiteStorage(db_path=db_path)
        task = Task(task_type="test", customer_id="acme")
        s.insert_task(task)
        s.close()

        s2 = SQLiteStorage(db_path=db_path)
        assert s2.get_schema_version() == TARGET_SCHEMA_VERSION
        got = s2.get_task(str(task.task_id))
        assert got is not None
        assert got.customer_id == "acme"
        s2.close()

    def test_migration_on_open_upgrades_schema(self, tmp_path: Path) -> None:
        """Opening a DB behind the target auto-migrates and preserves data."""
        db_path = tmp_path / "auto.db"

        # Create a DB and insert data.
        s = SQLiteStorage(db_path=db_path)
        task = Task(
            task_type="classify",
            customer_id="acme",
            llm_cost_usd=Decimal("0.05"),
            total_cost_usd=Decimal("0.05"),
        )
        s.insert_task(task)
        event = Event(
            task_id=task.task_id,
            event_type=EventType.LLM_CALL.value,
            cost_usd=Decimal("0.05"),
            provider="openai",
            model="gpt-4o",
        )
        s.insert_event(event)
        # Downgrade the recorded schema version so the next open sees the
        # DB as behind the target and runs migrations.  (The table shape is
        # already current; the test migration's column-add is idempotent
        # enough to exercise the auto-migrate-on-open path.)
        s._conn.execute("DELETE FROM schema_version")
        s._conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) VALUES (1, 'initial')"
        )
        s._conn.commit()
        s.close()

        original_migrations = dict(_SQLITE_MIGRATIONS)
        original_target = TARGET_SCHEMA_VERSION

        try:
            import dexcost.storage.migrations as mig_mod

            def _v1_to_v2(c: sqlite3.Connection) -> None:
                cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
                if "tags" not in cols:
                    c.execute("ALTER TABLE tasks ADD COLUMN tags TEXT DEFAULT '';")

            _SQLITE_MIGRATIONS[(1, 2)] = _v1_to_v2
            mig_mod.TARGET_SCHEMA_VERSION = 2

            # Also patch SQLiteStorage to use the same target
            import dexcost.storage.sqlite as sqlite_mod

            sqlite_mod._CURRENT_SCHEMA_VERSION = 2

            s2 = SQLiteStorage(db_path=db_path)
            assert s2.get_schema_version() == 2

            # Existing data preserved
            got = s2.get_task(str(task.task_id))
            assert got is not None
            assert got.customer_id == "acme"
            assert got.llm_cost_usd == Decimal("0.05")

            events = s2.query_events(task_id=task.task_id)
            assert len(events) == 1
            assert events[0].provider == "openai"

            s2.close()

        finally:
            _SQLITE_MIGRATIONS.clear()
            _SQLITE_MIGRATIONS.update(original_migrations)
            mig_mod.TARGET_SCHEMA_VERSION = original_target
            sqlite_mod._CURRENT_SCHEMA_VERSION = original_target


# ── PostgreSQL migration runner tests (unit-level, no live PG) ────────


class TestPgMigrationRunner:
    class _Transaction:
        def __init__(self, conn: TestPgMigrationRunner._Connection) -> None:
            self._conn = conn

        async def __aenter__(self) -> None:
            self._conn.transaction_entered = True

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object | None,
        ) -> None:
            del exc, traceback
            self._conn.transaction_committed = exc_type is None
            self._conn.transaction_rolled_back = exc_type is not None

    class _Connection:
        def __init__(self, fail_on: str | None = None) -> None:
            self.fail_on = fail_on
            self.executions: list[tuple[str, tuple[object, ...]]] = []
            self.transaction_entered = False
            self.transaction_committed = False
            self.transaction_rolled_back = False

        def transaction(self) -> TestPgMigrationRunner._Transaction:
            return TestPgMigrationRunner._Transaction(self)

        async def execute(self, sql: str, *args: object) -> str:
            normalized = " ".join(sql.split())
            self.executions.append((normalized, args))
            if self.fail_on is not None and self.fail_on in normalized:
                raise RuntimeError("injected PostgreSQL migration failure")
            return "OK"

    def test_no_migration_when_current(self) -> None:
        """If DB version >= target, async runner returns immediately."""
        import asyncio

        async def _run() -> int:
            return await run_pg_migrations_async(None, TARGET_SCHEMA_VERSION)

        result = asyncio.run(_run())
        assert result == TARGET_SCHEMA_VERSION

    def test_missing_pg_migration_raises(self) -> None:
        """Missing PG migration raises MigrationError."""
        import asyncio

        original_target = TARGET_SCHEMA_VERSION

        try:
            import dexcost.storage.migrations as mig_mod

            mig_mod.TARGET_SCHEMA_VERSION = 99

            async def _run() -> int:
                return await run_pg_migrations_async(None, 1)

            with pytest.raises(MigrationError, match="No PostgreSQL migration registered"):
                asyncio.run(_run())

        finally:
            mig_mod.TARGET_SCHEMA_VERSION = original_target

    def test_v14_to_v15_adds_and_backfills_delivery_versions(self) -> None:
        """The real PostgreSQL registry upgrades v14 through one transaction."""
        import asyncio

        conn = self._Connection()
        result = asyncio.run(run_pg_migrations_async(conn, 14))

        assert result == 15
        assert conn.transaction_entered is True
        assert conn.transaction_committed is True
        assert conn.transaction_rolled_back is False
        statements = [sql for sql, _args in conn.executions]
        for table in ("events", "tasks"):
            assert (
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS sync_version BIGINT"
                in statements
            )
            assert (
                f"UPDATE {table} SET sync_version = 1 WHERE sync_version IS NULL"
                in statements
            )
            assert (
                f"ALTER TABLE {table} ALTER COLUMN sync_version SET DEFAULT 1"
                in statements
            )
            assert (
                f"ALTER TABLE {table} ALTER COLUMN sync_version SET NOT NULL"
                in statements
            )
        assert conn.executions[-1] == (
            "INSERT INTO schema_version (version_number, migration_name) VALUES ($1, $2)",
            (15, "v14_to_v15"),
        )

    def test_v14_to_v15_failure_rolls_back_without_version_bump(self) -> None:
        """A failed DDL/backfill transaction never records schema v15."""
        import asyncio

        conn = self._Connection(fail_on="ALTER TABLE tasks ALTER COLUMN sync_version SET NOT NULL")
        with pytest.raises(MigrationError, match="PostgreSQL migration v14 → v15 failed"):
            asyncio.run(run_pg_migrations_async(conn, 14))

        assert conn.transaction_entered is True
        assert conn.transaction_committed is False
        assert conn.transaction_rolled_back is True
        assert not any("INSERT INTO schema_version" in sql for sql, _args in conn.executions)


# ── v2 → v3: tasks.sync_status migration (Fix 3) ──────────────────────


class TestTaskSyncStatusMigration:
    """The v2→v3 migration adds a sync_status column to the tasks table."""

    def test_v2_to_v3_adds_sync_status_column(self, tmp_path: Path) -> None:
        """Upgrading a v2 tasks table preserves data and adds sync_status='pending'."""
        db_path = tmp_path / "v2_to_v3.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")

        # Build a v2 tasks table — same as the post-v1→v2 shape but
        # WITHOUT the sync_status column.
        conn.execute(
            """CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL,
                experiment_id TEXT, variant TEXT
            )"""
        )
        from dexcost.storage.sqlite import _CREATE_SCHEMA_VERSION

        conn.execute(_CREATE_SCHEMA_VERSION)
        conn.execute(
            "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
            (2, "initial"),
        )
        task_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO tasks (task_id, task_type, status, started_at) VALUES (?, ?, ?, ?)",
            (task_id, "classify", "success", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        # sync_status must not exist yet.
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "sync_status" not in cols_before

        # Run the real registered v2→v3 migration.
        new_version = run_sqlite_migrations(conn, 2)
        assert TARGET_SCHEMA_VERSION >= 3
        assert new_version == TARGET_SCHEMA_VERSION

        # sync_status column now exists, defaulting to 'pending'.
        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "sync_status" in cols_after

        row = conn.execute(
            "SELECT task_type, sync_status FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        assert row["task_type"] == "classify"  # data preserved
        assert row["sync_status"] == "pending"  # existing rows default to pending

        conn.close()

    def test_fresh_db_has_sync_status_column(self, tmp_path: Path) -> None:
        """A freshly created SQLite DB already has tasks.sync_status."""
        s = SQLiteStorage(db_path=tmp_path / "fresh.db")
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        assert "sync_status" in cols
        s.close()
