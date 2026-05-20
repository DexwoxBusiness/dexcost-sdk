"""SQLite storage backend — zero-configuration local persistence."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import dexcost.storage.migrations as _migrations
from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.storage.migrations import run_sqlite_migrations

_CURRENT_SCHEMA_VERSION = _migrations.TARGET_SCHEMA_VERSION

_DEFAULT_DB_DIR = Path.home() / ".dexcost"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "buffer.db"

# ── SQL statements ────────────────────────────────────────────────────

_CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    task_type           TEXT NOT NULL,
    status              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    metadata            TEXT,
    llm_cost_usd        TEXT,
    external_cost_usd   TEXT,
    compute_cost_usd    TEXT,
    network_cost_usd    TEXT NOT NULL DEFAULT '0',
    total_cost_usd      TEXT,
    total_input_tokens   INTEGER,
    total_output_tokens  INTEGER,
    total_cached_tokens  INTEGER,
    retry_count         INTEGER DEFAULT 0,
    retry_cost_usd      TEXT DEFAULT '0',
    failure_count       INTEGER DEFAULT 0,
    customer_id         TEXT,
    project_id          TEXT,
    parent_task_id      TEXT,
    experiment_id       TEXT,
    variant             TEXT,
    sync_status         TEXT NOT NULL DEFAULT 'pending',
    network_bytes_in    INTEGER NOT NULL DEFAULT 0,
    network_bytes_out   INTEGER NOT NULL DEFAULT 0,
    network_call_count  INTEGER NOT NULL DEFAULT 0,
    network_by_host     TEXT NOT NULL DEFAULT '{"hosts": []}'
);
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    provider        TEXT,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cached_tokens   INTEGER,
    service_name    TEXT,
    cost_usd        TEXT NOT NULL,
    latency_ms      INTEGER,
    cost_confidence TEXT NOT NULL DEFAULT 'exact',
    pricing_source  TEXT,
    pricing_version TEXT,
    is_retry        INTEGER DEFAULT 0,
    retry_reason    TEXT,
    retry_of        TEXT,
    details         TEXT,
    timestamp       TEXT NOT NULL,
    sync_status     TEXT NOT NULL DEFAULT 'pending'
);
"""

_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    version_number  INTEGER NOT NULL,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    migration_name  TEXT
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tasks_customer ON tasks(customer_id, started_at);",
    "CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type, started_at);",
    "CREATE INDEX IF NOT EXISTS idx_tasks_period ON tasks(started_at);",
    "CREATE INDEX IF NOT EXISTS idx_tasks_sync ON tasks(sync_status, started_at);",
    "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_events_sync ON events(sync_status, timestamp);",
]


# ── Helpers ───────────────────────────────────────────────────────────


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(val: str | None) -> datetime | None:
    if val is None:
        return None
    return datetime.fromisoformat(val)


def _dec(val: str | None) -> Decimal:
    if val is None:
        return Decimal("0")
    return Decimal(val)


def _json_dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj)


def _json_loads(val: str | None) -> dict[str, Any]:
    if val is None:
        return {}
    try:
        return json.loads(val)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        return {}


# ── SQLite backend ────────────────────────────────────────────────────


class SQLiteStorage:
    """Local SQLite storage backend.

    Creates ``~/.dexcost/buffer.db`` by default with WAL mode enabled.
    Pass a custom *db_path* to override.

    Thread-safe: all operations are serialised through ``threading.Lock``.
    The connection is created with ``check_same_thread=False`` so the sync
    worker (background thread) and main thread can share one connection
    safely under the lock.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            try:
                _DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot create dexcost storage directory {_DEFAULT_DB_DIR}: {exc}"
                ) from exc
            self._path = _DEFAULT_DB_PATH
        else:
            p = Path(db_path)
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot create dexcost storage directory {p.parent}: {exc}"
                ) from exc
            self._path = p

        self._lock = threading.Lock()
        try:
            # NOTE: check_same_thread=False is set because:
            # 1. The main thread uses this connection for event/task writes
            # 2. The SyncWorker creates its OWN connection via _open_thread_storage()
            # 3. This connection is never actually shared across threads
            # 4. The Lock serializes same-thread access from concurrent coroutines
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"Cannot open dexcost database {self._path}: {exc}"
            ) from exc
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.row_factory = sqlite3.Row
        self.create_schema()
        self._run_migrations()

    # ── Migration support ─────────────────────────────────────────────

    def _run_migrations(self) -> None:
        """Compare DB version against target and apply any pending migrations."""
        db_version = self.get_schema_version()
        if db_version < _migrations.TARGET_SCHEMA_VERSION:
            run_sqlite_migrations(self._conn, db_version)

    # ── Schema management ─────────────────────────────────────────────

    def create_schema(self) -> None:
        """Create all tables, indexes, and seed the schema version."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(_CREATE_TASKS)
            cur.execute(_CREATE_EVENTS)
            cur.execute(_CREATE_SCHEMA_VERSION)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)

            # Seed version if the table is empty
            row = cur.execute("SELECT COUNT(*) FROM schema_version").fetchone()
            if row[0] == 0:
                cur.execute(
                    "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
                    (_CURRENT_SCHEMA_VERSION, "initial"),
                )
            self._conn.commit()

    def get_schema_version(self) -> int:
        """Return the highest recorded schema version."""
        with self._lock:
            row = self._conn.execute(
                "SELECT version_number FROM schema_version ORDER BY version_id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return 0
            return int(row[0])

    def set_schema_version(self, version: int, migration_name: str = "") -> None:
        """Record a new schema version after a migration."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
                (version, migration_name),
            )
            self._conn.commit()

    # ── Task CRUD ─────────────────────────────────────────────────────

    def insert_task(self, task: Task) -> None:
        """Persist a new task."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO tasks (
                    task_id, task_type, status, started_at, ended_at, metadata,
                    llm_cost_usd, external_cost_usd, compute_cost_usd, network_cost_usd, total_cost_usd,
                    total_input_tokens, total_output_tokens, total_cached_tokens,
                    retry_count, retry_cost_usd, failure_count,
                    customer_id, project_id, parent_task_id,
                    experiment_id, variant,
                    network_bytes_in, network_bytes_out, network_call_count, network_by_host
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(task.task_id),
                    task.task_type,
                    task.status,
                    task.started_at.isoformat(),
                    _iso(task.ended_at),
                    _json_dumps(task.metadata),
                    str(task.llm_cost_usd),
                    str(task.external_cost_usd),
                    str(task.compute_cost_usd),
                    str(task.network_cost_usd),
                    str(task.total_cost_usd),
                    task.total_input_tokens,
                    task.total_output_tokens,
                    task.total_cached_tokens,
                    task.retry_count,
                    str(task.retry_cost_usd),
                    task.failure_count,
                    task.customer_id,
                    task.project_id,
                    str(task.parent_task_id) if task.parent_task_id else None,
                    task.experiment_id,
                    task.variant,
                    task.network_bytes_in,
                    task.network_bytes_out,
                    task.network_call_count,
                    _json_dumps(task.network_by_host),
                ),
            )
            self._conn.commit()

    def update_task(self, task: Task) -> None:
        """Update an existing task (matched by task_id).

        The task is re-marked ``sync_status='pending'`` so that mutations
        (e.g. cost aggregation after the task ends) are re-pushed by the
        SyncWorker even if an earlier version was already synced.
        """
        with self._lock:
            self._conn.execute(
                """UPDATE tasks SET
                    task_type=?, status=?, started_at=?, ended_at=?, metadata=?,
                    llm_cost_usd=?, external_cost_usd=?, compute_cost_usd=?, network_cost_usd=?, total_cost_usd=?,
                    total_input_tokens=?, total_output_tokens=?, total_cached_tokens=?,
                    retry_count=?, retry_cost_usd=?, failure_count=?,
                    customer_id=?, project_id=?, parent_task_id=?,
                    experiment_id=?, variant=?,
                    network_bytes_in=?, network_bytes_out=?, network_call_count=?, network_by_host=?,
                    sync_status='pending'
                WHERE task_id=?""",
                (
                    task.task_type,
                    task.status,
                    task.started_at.isoformat(),
                    _iso(task.ended_at),
                    _json_dumps(task.metadata),
                    str(task.llm_cost_usd),
                    str(task.external_cost_usd),
                    str(task.compute_cost_usd),
                    str(task.network_cost_usd),
                    str(task.total_cost_usd),
                    task.total_input_tokens,
                    task.total_output_tokens,
                    task.total_cached_tokens,
                    task.retry_count,
                    str(task.retry_cost_usd),
                    task.failure_count,
                    task.customer_id,
                    task.project_id,
                    str(task.parent_task_id) if task.parent_task_id else None,
                    task.experiment_id,
                    task.variant,
                    task.network_bytes_in,
                    task.network_bytes_out,
                    task.network_call_count,
                    _json_dumps(task.network_by_host),
                    str(task.task_id),
                ),
            )
            self._conn.commit()

    def get_task(self, task_id: str) -> Task | None:
        """Return a single task by ID, or None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def query_tasks(self, **filters: Any) -> list[Task]:
        """Return tasks matching filters.

        Supported: customer_id, task_type, project_id, status,
        started_after (datetime), started_before (datetime).
        """
        clauses: list[str] = []
        params: list[Any] = []

        if "customer_id" in filters:
            clauses.append("customer_id = ?")
            params.append(filters["customer_id"])
        if "task_type" in filters:
            clauses.append("task_type = ?")
            params.append(filters["task_type"])
        if "project_id" in filters:
            clauses.append("project_id = ?")
            params.append(filters["project_id"])
        if "status" in filters:
            clauses.append("status = ?")
            params.append(filters["status"])
        if "started_after" in filters:
            clauses.append("started_at >= ?")
            params.append(filters["started_after"].isoformat())
        if "started_before" in filters:
            clauses.append("started_at <= ?")
            params.append(filters["started_before"].isoformat())

        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    # ── Event CRUD ────────────────────────────────────────────────────

    def insert_event(self, event: Event) -> None:
        """Persist a new event."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO events (
                    event_id, task_id, event_type, provider, model,
                    input_tokens, output_tokens, cached_tokens,
                    service_name, cost_usd, latency_ms,
                    cost_confidence, pricing_source, pricing_version,
                    is_retry, retry_reason, retry_of,
                    details, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(event.event_id),
                    str(event.task_id),
                    event.event_type,
                    event.provider,
                    event.model,
                    event.input_tokens,
                    event.output_tokens,
                    event.cached_tokens,
                    event.service_name,
                    str(event.cost_usd),
                    event.latency_ms,
                    event.cost_confidence,
                    event.pricing_source,
                    event.pricing_version,
                    1 if event.is_retry else 0,
                    event.retry_reason,
                    str(event.retry_of) if event.retry_of else None,
                    _json_dumps(event.details),
                    event.occurred_at.isoformat(),
                ),
            )
            self._conn.commit()

    def update_event(self, event: Event) -> None:
        """Update an existing event (matched by event_id).

        Re-marks ``sync_status='pending'`` so any mutation after the row
        was previously synced is re-pushed by the SyncWorker. Mirrors the
        behaviour of :meth:`update_task`.
        """
        with self._lock:
            self._conn.execute(
                """UPDATE events SET
                    event_type=?, provider=?, model=?,
                    input_tokens=?, output_tokens=?, cached_tokens=?,
                    service_name=?, cost_usd=?, latency_ms=?,
                    cost_confidence=?, pricing_source=?, pricing_version=?,
                    is_retry=?, retry_reason=?, retry_of=?,
                    details=?, timestamp=?,
                    sync_status='pending'
                WHERE event_id=?""",
                (
                    event.event_type,
                    event.provider,
                    event.model,
                    event.input_tokens,
                    event.output_tokens,
                    event.cached_tokens,
                    event.service_name,
                    str(event.cost_usd),
                    event.latency_ms,
                    event.cost_confidence,
                    event.pricing_source,
                    event.pricing_version,
                    1 if event.is_retry else 0,
                    event.retry_reason,
                    str(event.retry_of) if event.retry_of else None,
                    _json_dumps(event.details),
                    event.occurred_at.isoformat(),
                    str(event.event_id),
                ),
            )
            self._conn.commit()

    def query_events(self, **filters: Any) -> list[Event]:
        """Return events matching filters.

        Supported: task_id, event_type, customer_id, after (datetime),
        before (datetime).
        """
        clauses: list[str] = []
        params: list[Any] = []
        need_join = False

        if "task_id" in filters:
            clauses.append("e.task_id = ?")
            params.append(str(filters["task_id"]))
        if "event_type" in filters:
            clauses.append("e.event_type = ?")
            params.append(filters["event_type"])
        if "customer_id" in filters:
            clauses.append("t.customer_id = ?")
            params.append(filters["customer_id"])
            need_join = True
        if "after" in filters:
            clauses.append("e.timestamp >= ?")
            params.append(filters["after"].isoformat())
        if "before" in filters:
            clauses.append("e.timestamp <= ?")
            params.append(filters["before"].isoformat())

        if need_join:
            sql = "SELECT e.* FROM events e JOIN tasks t ON e.task_id = t.task_id"
        else:
            sql = "SELECT e.* FROM events e"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY e.timestamp DESC"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def query_events_for_sync(self, limit: int = 1000) -> list[Event]:
        """Return pending events ready for sync, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE sync_status = 'pending' "
                "ORDER BY timestamp ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def mark_synced(self, event_ids: list[str]) -> None:
        """Transition events from pending to synced."""
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        sql = (
            "UPDATE events SET sync_status = 'synced' "
            "WHERE event_id IN (" + placeholders + ")"
        )
        with self._lock:
            self._conn.execute(sql, event_ids)
            self._conn.commit()

    def query_tasks_for_sync(self, task_ids: list[str]) -> list[Task]:
        """Return tasks matching the given IDs (for inclusion in sync payloads)."""
        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        sql = "SELECT * FROM tasks WHERE task_id IN (" + placeholders + ")"
        with self._lock:
            rows = self._conn.execute(sql, task_ids).fetchall()
        return [self._row_to_task(r) for r in rows]

    def query_all_tasks(self) -> list[Task]:
        """Return all tasks regardless of sync status."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tasks").fetchall()
        return [self._row_to_task(r) for r in rows]

    def query_pending_tasks_for_sync(self, limit: int = 1000) -> list[Task]:
        """Return tasks not yet synced, oldest first.

        Used by the :class:`~dexcost.sync.SyncWorker` so already-synced tasks
        are not re-POSTed on every cycle.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE sync_status = 'pending' "
                "ORDER BY started_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def mark_tasks_synced(self, task_ids: list[str]) -> None:
        """Transition tasks from pending to synced."""
        if not task_ids:
            return
        placeholders = ",".join("?" for _ in task_ids)
        sql = (
            "UPDATE tasks SET sync_status = 'synced' "
            "WHERE task_id IN (" + placeholders + ")"
        )
        with self._lock:
            self._conn.execute(sql, task_ids)
            self._conn.commit()

    def purge_synced(self, retention_hours: int = 48) -> int:
        """Delete synced events older than *retention_hours* and VACUUM.

        Returns the number of deleted rows.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM events WHERE sync_status = 'synced' "
                "AND timestamp < datetime('now', ? || ' hours')",
                (str(-retention_hours),),
            )
            deleted = cur.rowcount
            self._conn.commit()
            self._conn.execute("VACUUM")
        return deleted

    def purge_old_pending(self, max_age_days: int = 7) -> int:
        """Remove pending events older than *max_age_days*.

        Safety net for events that can never be synced (invalid API key, etc.).
        Returns the number of deleted rows.
        """
        with self._lock:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=max_age_days)
            ).isoformat()
            cursor = self._conn.execute(
                "DELETE FROM events WHERE sync_status = 'pending' AND timestamp < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount
            self._conn.commit()
            if deleted > 0:
                self._conn.execute("VACUUM")
        return deleted

    # ── Lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()

    # ── Private row converters ────────────────────────────────────────

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        try:
            task_id = uuid.UUID(row["task_id"])
        except ValueError:
            task_id = uuid.uuid4()
        try:
            parent_task_id = uuid.UUID(row["parent_task_id"]) if row["parent_task_id"] else None
        except ValueError:
            parent_task_id = None
        return Task(
            task_id=task_id,
            task_type=row["task_type"],
            status=row["status"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
            metadata=_json_loads(row["metadata"]),
            llm_cost_usd=_dec(row["llm_cost_usd"]),
            external_cost_usd=_dec(row["external_cost_usd"]),
            compute_cost_usd=_dec(row["compute_cost_usd"]),
            network_cost_usd=(
                _dec(row["network_cost_usd"])
                if "network_cost_usd" in row.keys()
                else Decimal("0")
            ),
            total_cost_usd=_dec(row["total_cost_usd"]),
            total_input_tokens=row["total_input_tokens"] or 0,
            total_output_tokens=row["total_output_tokens"] or 0,
            total_cached_tokens=row["total_cached_tokens"] or 0,
            retry_count=row["retry_count"] or 0,
            retry_cost_usd=_dec(row["retry_cost_usd"]),
            failure_count=row["failure_count"] or 0,
            customer_id=row["customer_id"],
            project_id=row["project_id"],
            parent_task_id=parent_task_id,
            experiment_id=row["experiment_id"],
            variant=row["variant"],
            network_bytes_in=row["network_bytes_in"] or 0,
            network_bytes_out=row["network_bytes_out"] or 0,
            network_call_count=row["network_call_count"] or 0,
            network_by_host=_json_loads(row["network_by_host"]) or {"hosts": []},
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        try:
            event_id = uuid.UUID(row["event_id"])
        except ValueError:
            event_id = uuid.uuid4()
        try:
            task_id = uuid.UUID(row["task_id"])
        except ValueError:
            task_id = uuid.uuid4()
        try:
            retry_of = uuid.UUID(row["retry_of"]) if row["retry_of"] else None
        except ValueError:
            retry_of = None
        return Event(
            event_id=event_id,
            task_id=task_id,
            event_type=row["event_type"],
            occurred_at=datetime.fromisoformat(row["timestamp"]),
            cost_usd=_dec(row["cost_usd"]),
            cost_confidence=row["cost_confidence"],
            pricing_source=row["pricing_source"],
            pricing_version=row["pricing_version"],
            service_name=row["service_name"],
            provider=row["provider"],
            model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cached_tokens=row["cached_tokens"],
            latency_ms=row["latency_ms"],
            is_retry=bool(row["is_retry"]),
            retry_reason=row["retry_reason"],
            retry_of=retry_of,
            details=_json_loads(row["details"]),
        )
