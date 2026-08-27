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
from dexcost.capabilities import apply_event_capability
from dexcost.idempotency import (
    apply_event_idempotency,
    equivalent_idempotent_event,
    idempotency_hash,
)
from dexcost.models._serde import canonical_decimal, parse_canonical
from dexcost.models.event import Event
from dexcost.models.outcome import OutcomeRevision, OutcomeValue
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobUsageLine,
)
from dexcost.models.revenue import RevenueAmount, RevenueRevision, RevenueSource
from dexcost.models.task import Task
from dexcost.pricing_explain import apply_event_pricing_provenance
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
    gpu_cost_usd        TEXT NOT NULL DEFAULT '0',
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
    root_task_id        TEXT,
    agent_id            TEXT,
    agent_version       TEXT,
    workflow_id         TEXT,
    workflow_session_id TEXT,
    user_id             TEXT,
    product_id          TEXT,
    experiment_id       TEXT,
    variant             TEXT,
    sync_status         TEXT NOT NULL DEFAULT 'pending',
    sync_version        INTEGER NOT NULL DEFAULT 1,
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
    sync_status     TEXT NOT NULL DEFAULT 'pending',
    sync_version    INTEGER NOT NULL DEFAULT 1
);
"""

_CREATE_OUTCOMES = """
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id      TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    task_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    effective_at    TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    value_type      TEXT,
    value_json      TEXT,
    schema_version  TEXT NOT NULL DEFAULT '1',
    sync_status     TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (outcome_id, revision)
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

_CREATE_REVENUES = """
CREATE TABLE IF NOT EXISTS revenues (
    revenue_id       TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    task_id          TEXT NOT NULL,
    outcome_id       TEXT,
    lifecycle_state  TEXT NOT NULL,
    effective_at     TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    amount           TEXT,
    currency         TEXT,
    source_type      TEXT NOT NULL,
    source_record_id TEXT,
    schema_version   TEXT NOT NULL DEFAULT '1',
    sync_status      TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (revenue_id, revision),
    CHECK (revision > 0),
    CHECK (lifecycle_state IN ('pending', 'provisional', 'recognized', 'voided')),
    CHECK (source_type IN ('sdk', 'workspace_api', 'import', 'manual'))
);
"""

_CREATE_PROVIDER_JOB_REVISIONS = """
CREATE TABLE IF NOT EXISTS provider_job_revisions (
    event_id           TEXT NOT NULL,
    revision           INTEGER NOT NULL,
    task_id            TEXT NOT NULL,
    provider           TEXT NOT NULL,
    service            TEXT NOT NULL,
    provider_record_id TEXT NOT NULL,
    operation          TEXT NOT NULL,
    component          TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    resource_type      TEXT NOT NULL,
    resource_id        TEXT NOT NULL,
    lifecycle_status   TEXT NOT NULL,
    submitted_at       TEXT NOT NULL,
    observed_at        TEXT NOT NULL,
    owns_task          INTEGER NOT NULL DEFAULT 0,
    billing_dimensions_json TEXT NOT NULL DEFAULT '[]',
    usage_json         TEXT NOT NULL DEFAULT '[]',
    cost_amount        TEXT,
    cost_source        TEXT,
    cost_confidence    TEXT,
    pricing_version    TEXT,
    latency_ms         INTEGER,
    error_type         TEXT,
    error_code         TEXT,
    task_input_tokens  INTEGER,
    task_output_tokens INTEGER,
    task_cached_tokens INTEGER,
    capability_json    TEXT,
    schema_version     TEXT NOT NULL DEFAULT '1',
    sync_status        TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (event_id, revision),
    CHECK (revision > 0),
    CHECK (lifecycle_status IN (
        'submitted', 'running', 'succeeded', 'failed', 'cancelled', 'unknown'
    )),
    CHECK (event_type IN ('llm_call', 'external_cost', 'compute_cost')),
    CHECK (owns_task IN (0, 1))
);
"""

_CREATE_CATALOG_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS sdk_catalog_artifacts (
    sha256          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    byte_size       INTEGER NOT NULL,
    payload         BLOB NOT NULL,
    validated_at    TEXT NOT NULL,
    CHECK (length(sha256) = 64),
    CHECK (byte_size >= 2)
);
"""

_CREATE_CATALOG_RELEASES = """
CREATE TABLE IF NOT EXISTS sdk_catalog_releases (
    release_sequence       INTEGER PRIMARY KEY,
    release_id             TEXT NOT NULL UNIQUE,
    channel                TEXT NOT NULL,
    manifest_json          BLOB NOT NULL,
    manifest_sha256        TEXT NOT NULL,
    published_at           TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    safety_policy_version  TEXT NOT NULL,
    stored_at              TEXT NOT NULL,
    CHECK (release_sequence > 0),
    CHECK (channel IN ('stable', 'canary')),
    CHECK (length(manifest_sha256) = 64)
);
"""

_CREATE_CATALOG_RELEASE_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS sdk_catalog_release_artifacts (
    release_sequence  INTEGER NOT NULL,
    kind              TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    PRIMARY KEY (release_sequence, kind),
    FOREIGN KEY (release_sequence)
        REFERENCES sdk_catalog_releases(release_sequence) ON DELETE CASCADE,
    FOREIGN KEY (sha256)
        REFERENCES sdk_catalog_artifacts(sha256) ON DELETE RESTRICT
);
"""

_CREATE_CATALOG_STATE = """
CREATE TABLE IF NOT EXISTS sdk_catalog_state (
    channel                    TEXT PRIMARY KEY,
    active_release_sequence    INTEGER,
    previous_release_sequence  INTEGER,
    manifest_etag              TEXT,
    last_checked_at            TEXT,
    last_error                 TEXT,
    CHECK (channel IN ('stable', 'canary')),
    CHECK (
        active_release_sequence IS NULL
        OR previous_release_sequence IS NULL
        OR active_release_sequence != previous_release_sequence
    ),
    FOREIGN KEY (active_release_sequence)
        REFERENCES sdk_catalog_releases(release_sequence) ON DELETE RESTRICT,
    FOREIGN KEY (previous_release_sequence)
        REFERENCES sdk_catalog_releases(release_sequence) ON DELETE RESTRICT
);
"""

_CREATE_CATALOG_OVERLAYS = """
CREATE TABLE IF NOT EXISTS sdk_catalog_overlays (
    principal_sha256       TEXT NOT NULL,
    base_release_id       TEXT NOT NULL,
    base_release_sequence INTEGER NOT NULL,
    payload               BLOB NOT NULL,
    payload_sha256        TEXT NOT NULL,
    etag                  TEXT,
    generated_at          TEXT NOT NULL,
    stored_at             TEXT NOT NULL,
    PRIMARY KEY (principal_sha256, base_release_id),
    CHECK (length(principal_sha256) = 64),
    CHECK (length(payload_sha256) = 64),
    CHECK (base_release_sequence > 0)
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

_CREATE_OUTCOME_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_outcomes_sync ON outcomes(sync_status, observed_at);",
]

_CREATE_REVENUE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_revenues_sync " "ON revenues(sync_status, observed_at);",
    "CREATE INDEX IF NOT EXISTS idx_revenues_task " "ON revenues(task_id, observed_at);",
]

_CREATE_PROVIDER_JOB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_provider_jobs_sync "
    "ON provider_job_revisions(sync_status, observed_at, event_id, revision);",
    "CREATE INDEX IF NOT EXISTS idx_provider_jobs_identity "
    "ON provider_job_revisions(provider, service, provider_record_id, revision DESC);",
    "CREATE INDEX IF NOT EXISTS idx_provider_jobs_task "
    "ON provider_job_revisions(task_id, event_id, revision DESC);",
]

_CREATE_CATALOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sdk_catalog_releases_channel "
    "ON sdk_catalog_releases(channel, release_sequence DESC);",
    "CREATE INDEX IF NOT EXISTS idx_sdk_catalog_members_sha "
    "ON sdk_catalog_release_artifacts(sha256);",
]


# ── Helpers ───────────────────────────────────────────────────────────


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(val: str | None) -> datetime | None:
    if val is None:
        return None
    return parse_canonical(val)


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
            raise RuntimeError(f"Cannot open dexcost database {self._path}: {exc}") from exc
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
            cur.execute(_CREATE_OUTCOMES)
            cur.execute(_CREATE_REVENUES)
            cur.execute(_CREATE_PROVIDER_JOB_REVISIONS)
            cur.execute(_CREATE_SCHEMA_VERSION)
            cur.execute(_CREATE_CATALOG_ARTIFACTS)
            cur.execute(_CREATE_CATALOG_RELEASES)
            cur.execute(_CREATE_CATALOG_RELEASE_ARTIFACTS)
            cur.execute(_CREATE_CATALOG_STATE)
            cur.execute(_CREATE_CATALOG_OVERLAYS)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
            for idx_sql in _CREATE_OUTCOME_INDEXES:
                cur.execute(idx_sql)
            for idx_sql in _CREATE_REVENUE_INDEXES:
                cur.execute(idx_sql)
            for idx_sql in _CREATE_PROVIDER_JOB_INDEXES:
                cur.execute(idx_sql)
            for idx_sql in _CREATE_CATALOG_INDEXES:
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
                    llm_cost_usd, external_cost_usd, compute_cost_usd,
                    network_cost_usd, gpu_cost_usd, total_cost_usd,
                    total_input_tokens, total_output_tokens, total_cached_tokens,
                    retry_count, retry_cost_usd, failure_count,
                    customer_id, project_id, parent_task_id, root_task_id,
                    agent_id, agent_version, workflow_id, workflow_session_id,
                    user_id, product_id,
                    experiment_id, variant,
                    network_bytes_in, network_bytes_out, network_call_count, network_by_host
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
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
                    str(task.gpu_cost_usd),
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
                    str(task.root_task_id) if task.root_task_id else None,
                    task.agent_id,
                    task.agent_version,
                    task.workflow_id,
                    task.workflow_session_id,
                    task.user_id,
                    task.product_id,
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
                    llm_cost_usd=?, external_cost_usd=?, compute_cost_usd=?,
                    network_cost_usd=?, gpu_cost_usd=?, total_cost_usd=?,
                    total_input_tokens=?, total_output_tokens=?, total_cached_tokens=?,
                    retry_count=?, retry_cost_usd=?, failure_count=?,
                    customer_id=?, project_id=?, parent_task_id=?, root_task_id=?,
                    agent_id=?, agent_version=?, workflow_id=?, workflow_session_id=?,
                    user_id=?, product_id=?,
                    experiment_id=?, variant=?,
                    network_bytes_in=?, network_bytes_out=?, network_call_count=?,
                    network_by_host=?,
                    sync_status='pending', sync_version=sync_version + 1
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
                    str(task.gpu_cost_usd),
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
                    str(task.root_task_id) if task.root_task_id else None,
                    task.agent_id,
                    task.agent_version,
                    task.workflow_id,
                    task.workflow_session_id,
                    task.user_id,
                    task.product_id,
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
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
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
        """Persist an event, enforcing caller-idempotency conflicts locally."""
        apply_event_capability(event)
        apply_event_pricing_provenance(event)
        apply_event_idempotency(event)
        with self._lock:
            same = self._conn.execute(
                "SELECT * FROM events WHERE event_id=?", (str(event.event_id),)
            ).fetchone()
            if same is not None:
                stored = self._row_to_event(same)
                if stored.to_dict() == event.to_dict():
                    return
                if equivalent_idempotent_event(stored, event):
                    # Return the durable first-write identity to callers that
                    # re-executed the same logical operation later.
                    event.occurred_at = stored.occurred_at
                    return
                if idempotency_hash(stored) is not None:
                    raise ValueError("idempotency key was reused for different economic facts")
                raise ValueError(f"event {event.event_id} already exists with different contents")
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
                    sync_status='pending', sync_version=sync_version + 1
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
        if "event_id" in filters:
            clauses.append("e.event_id = ?")
            params.append(str(filters["event_id"]))
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
        # Multiple operations can legitimately share the same wall-clock
        # timestamp on coarse clocks. SQLite otherwise leaves tied rows in an
        # unspecified order, so use insertion order as the stable tiebreaker.
        sql += " ORDER BY e.timestamp DESC, e.rowid DESC"

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def query_events_for_sync(self, limit: int = 1000) -> list[Event]:
        """Return pending events ready for sync, oldest first."""
        return [event for event, _version in self.query_event_deliveries_for_sync(limit)]

    def query_event_deliveries_for_sync(self, limit: int = 1000) -> list[tuple[Event, int]]:
        """Return pending event snapshots with optimistic delivery versions."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE sync_status = 'pending' "
                "ORDER BY timestamp ASC, rowid ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(self._row_to_event(row), int(row["sync_version"])) for row in rows]

    def mark_event_deliveries_synced(self, deliveries: list[tuple[str, int]]) -> None:
        """Acknowledge only the exact event revisions accepted by ingestion."""
        if not deliveries:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE events SET sync_status='synced' "
                "WHERE event_id=? AND sync_version=? AND sync_status='pending'",
                deliveries,
            )
            self._conn.commit()

    def mark_synced(self, event_ids: list[str]) -> None:
        """Transition events from pending to synced."""
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        sql = (
            "UPDATE events SET sync_status = 'synced' " "WHERE event_id IN (" + placeholders + ")"
        )
        with self._lock:
            self._conn.execute(sql, event_ids)
            self._conn.commit()

    def mark_quarantined(self, event_ids: list[str]) -> None:
        """Retain unrepresentable events outside the pending sync scan.

        Quarantine is deliberately distinct from ``synced``: the control
        plane did not receive these records. A later :meth:`update_event`
        moves a corrected record back to ``pending``.
        """
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        sql = (
            "UPDATE events SET sync_status = 'quarantined' "
            "WHERE sync_status = 'pending' AND event_id IN (" + placeholders + ")"
        )
        with self._lock:
            self._conn.execute(sql, event_ids)
            self._conn.commit()

    def mark_event_deliveries_quarantined(self, deliveries: list[tuple[str, int]]) -> None:
        """Quarantine only the exact event snapshots that failed conversion."""
        if not deliveries:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE events SET sync_status='quarantined' "
                "WHERE event_id=? AND sync_version=? AND sync_status='pending'",
                deliveries,
            )
            self._conn.commit()

    def requeue_quarantined_events(self) -> int:
        """Requeue retained conversion failures without mutating event data."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE events SET sync_status = 'pending' " "WHERE sync_status = 'quarantined'"
            )
            restored = cursor.rowcount
            self._conn.commit()
        return restored

    def query_quarantined_events(self, limit: int = 100) -> list[Event]:
        """Return quarantined conversion failures, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE sync_status = 'quarantined' "
                "ORDER BY timestamp ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

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
        return [task for task, _version in self.query_task_deliveries_for_sync(limit)]

    def query_task_deliveries_for_sync(self, limit: int = 1000) -> list[tuple[Task, int]]:
        """Return pending task snapshots with optimistic delivery versions."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE sync_status = 'pending' "
                "ORDER BY started_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(self._row_to_task(row), int(row["sync_version"])) for row in rows]

    def mark_task_deliveries_synced(self, deliveries: list[tuple[str, int]]) -> None:
        """Acknowledge only the exact task revisions accepted by ingestion."""
        if not deliveries:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE tasks SET sync_status='synced' "
                "WHERE task_id=? AND sync_version=? AND sync_status='pending'",
                deliveries,
            )
            self._conn.commit()

    def mark_tasks_synced(self, task_ids: list[str]) -> None:
        """Transition tasks from pending to synced."""
        if not task_ids:
            return
        placeholders = ",".join("?" for _ in task_ids)
        sql = "UPDATE tasks SET sync_status = 'synced' " "WHERE task_id IN (" + placeholders + ")"
        with self._lock:
            self._conn.execute(sql, task_ids)
            self._conn.commit()

    # ── Outcome revision CRUD ────────────────────────────────────────

    def insert_outcome(self, outcome: OutcomeRevision) -> None:
        """Append one outcome revision with local ledger invariants."""
        payload = outcome.to_dict()
        value = payload.get("value")
        value_type = outcome.value.type if outcome.value is not None else None
        value_json = json.dumps(value, sort_keys=True) if value is not None else None

        with self._lock:
            same = self._conn.execute(
                "SELECT * FROM outcomes WHERE outcome_id=? AND revision=?",
                (str(outcome.outcome_id), outcome.revision),
            ).fetchone()
            if same is not None:
                if self._row_to_outcome(same).to_dict() != payload:
                    raise ValueError(
                        f"outcome {outcome.outcome_id} revision {outcome.revision} "
                        "already exists with different contents"
                    )
                return

            previous = self._conn.execute(
                "SELECT * FROM outcomes WHERE outcome_id=? ORDER BY revision DESC LIMIT 1",
                (str(outcome.outcome_id),),
            ).fetchone()
            expected_revision = (int(previous["revision"]) if previous is not None else 0) + 1
            if outcome.revision != expected_revision:
                raise ValueError(
                    f"outcome {outcome.outcome_id} expected revision "
                    f"{expected_revision}, received {outcome.revision}"
                )
            if previous is not None:
                if previous["task_id"] != str(outcome.task_id) or previous["name"] != outcome.name:
                    raise ValueError("an outcome cannot change task_id or name across revisions")
                allowed = {
                    "pending": {"pending", "achieved", "missed", "voided"},
                    "achieved": {"achieved", "missed", "voided"},
                    "missed": {"missed", "achieved", "voided"},
                    "voided": set(),
                }
                if outcome.state not in allowed[str(previous["lifecycle_state"])]:
                    raise ValueError(
                        "invalid outcome lifecycle transition "
                        f"{previous['lifecycle_state']} -> {outcome.state}"
                    )

            self._conn.execute(
                """INSERT INTO outcomes (
                    outcome_id, revision, task_id, name, lifecycle_state,
                    effective_at, observed_at, value_type, value_json,
                    schema_version, sync_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    str(outcome.outcome_id),
                    outcome.revision,
                    str(outcome.task_id),
                    outcome.name,
                    outcome.state,
                    outcome.effective_at.isoformat(),
                    outcome.observed_at.isoformat(),
                    value_type,
                    value_json,
                    outcome.schema_version,
                ),
            )
            self._conn.commit()

    def query_outcomes_for_sync(self, limit: int = 1000) -> list[OutcomeRevision]:
        """Return pending outcome revisions, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outcomes WHERE sync_status='pending' "
                "ORDER BY observed_at ASC, outcome_id ASC, revision ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_outcome(row) for row in rows]

    def query_outcome_history(self, outcome_id: str) -> list[OutcomeRevision]:
        """Return the immutable revision history for one outcome."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outcomes WHERE outcome_id=? ORDER BY revision ASC",
                (outcome_id,),
            ).fetchall()
        return [self._row_to_outcome(row) for row in rows]

    def mark_outcomes_synced(self, revisions: list[tuple[str, int]]) -> None:
        """Transition the identified outcome revisions to synced."""
        if not revisions:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE outcomes SET sync_status='synced' " "WHERE outcome_id=? AND revision=?",
                revisions,
            )
            self._conn.commit()

    def mark_outcomes_quarantined(self, revisions: list[tuple[str, int]]) -> None:
        """Retain undeliverable outcome revisions without blocking later rows."""
        if not revisions:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE outcomes SET sync_status='quarantined' "
                "WHERE sync_status='pending' AND outcome_id=? AND revision=?",
                revisions,
            )
            self._conn.commit()

    # ── Revenue revision CRUD ────────────────────────────────────────

    def insert_revenue(self, revenue: RevenueRevision) -> None:
        """Append one revenue revision with the server's ledger invariants."""
        payload = revenue.to_dict()

        with self._lock:
            same = self._conn.execute(
                "SELECT * FROM revenues WHERE revenue_id=? AND revision=?",
                (str(revenue.revenue_id), revenue.revision),
            ).fetchone()
            if same is not None:
                if self._row_to_revenue(same).to_dict() != payload:
                    raise ValueError(
                        f"revenue {revenue.revenue_id} revision {revenue.revision} "
                        "already exists with different contents"
                    )
                return

            previous = self._conn.execute(
                "SELECT * FROM revenues WHERE revenue_id=? " "ORDER BY revision DESC LIMIT 1",
                (str(revenue.revenue_id),),
            ).fetchone()
            expected_revision = (int(previous["revision"]) if previous is not None else 0) + 1
            if revenue.revision != expected_revision:
                raise ValueError(
                    f"revenue {revenue.revenue_id} expected revision "
                    f"{expected_revision}, received {revenue.revision}"
                )

            if previous is not None:
                immutable_changed = (
                    previous["task_id"] != str(revenue.task_id)
                    or previous["outcome_id"]
                    != (str(revenue.outcome_id) if revenue.outcome_id is not None else None)
                    or previous["source_type"] != revenue.source.type
                    or previous["source_record_id"] != revenue.source.record_id
                )
                if immutable_changed:
                    raise ValueError(
                        "revenue cannot change task_id, outcome_id, or source " "across revisions"
                    )

                allowed = {
                    "pending": {"pending", "provisional", "recognized", "voided"},
                    "provisional": {"provisional", "recognized", "voided"},
                    "recognized": {"recognized", "voided"},
                    "voided": set(),
                }
                previous_state = str(previous["lifecycle_state"])
                if revenue.state not in allowed[previous_state]:
                    raise ValueError(
                        "invalid revenue lifecycle transition "
                        f"{previous_state} -> {revenue.state}"
                    )
                previous_currency = previous["currency"]
                current_currency = revenue.amount.currency if revenue.amount is not None else None
                if (
                    previous_currency is not None
                    and current_currency is not None
                    and previous_currency != current_currency
                ):
                    raise ValueError(
                        "revenue currency cannot change across revisions; "
                        "void it and create a new revenue_id"
                    )

            self._conn.execute(
                """INSERT INTO revenues (
                    revenue_id, revision, task_id, outcome_id, lifecycle_state,
                    effective_at, observed_at, amount, currency, source_type,
                    source_record_id, schema_version, sync_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    str(revenue.revenue_id),
                    revenue.revision,
                    str(revenue.task_id),
                    str(revenue.outcome_id) if revenue.outcome_id is not None else None,
                    revenue.state,
                    revenue.effective_at.isoformat(),
                    revenue.observed_at.isoformat(),
                    (
                        canonical_decimal(revenue.amount.value)
                        if revenue.amount is not None
                        else None
                    ),
                    revenue.amount.currency if revenue.amount is not None else None,
                    revenue.source.type,
                    revenue.source.record_id,
                    revenue.schema_version,
                ),
            )
            self._conn.commit()

    def query_revenues_for_sync(self, limit: int = 1000) -> list[RevenueRevision]:
        """Return pending revenue revisions, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM revenues WHERE sync_status='pending' "
                "ORDER BY observed_at ASC, revenue_id ASC, revision ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_revenue(row) for row in rows]

    def query_revenue_history(self, revenue_id: str) -> list[RevenueRevision]:
        """Return the immutable revision history for one revenue record."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM revenues WHERE revenue_id=? ORDER BY revision ASC",
                (revenue_id,),
            ).fetchall()
        return [self._row_to_revenue(row) for row in rows]

    def mark_revenues_synced(self, revisions: list[tuple[str, int]]) -> None:
        """Transition the identified revenue revisions to synced."""
        if not revisions:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE revenues SET sync_status='synced' " "WHERE revenue_id=? AND revision=?",
                revisions,
            )
            self._conn.commit()

    def mark_revenues_quarantined(self, revisions: list[tuple[str, int]]) -> None:
        """Retain undeliverable revenue revisions without blocking the queue."""
        if not revisions:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE revenues SET sync_status='quarantined' "
                "WHERE sync_status='pending' AND revenue_id=? AND revision=?",
                revisions,
            )
            self._conn.commit()

    # ── Asynchronous provider-job revision CRUD ─────────────────────

    def insert_provider_job_revision(self, job: ProviderJobRevision) -> None:
        """Append a provider-job snapshot with local ledger invariants."""
        with self._lock:
            same = self._conn.execute(
                "SELECT * FROM provider_job_revisions WHERE event_id=? AND revision=?",
                (str(job.event_id), job.revision),
            ).fetchone()
            if same is not None:
                stored = self._row_to_provider_job(same)
                if stored.to_dict() == job.to_dict():
                    return
                raise ValueError(
                    f"provider job {job.event_id} revision {job.revision} "
                    "already exists with different contents"
                )

            previous_row = self._conn.execute(
                "SELECT * FROM provider_job_revisions WHERE event_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (str(job.event_id),),
            ).fetchone()
            previous = (
                self._row_to_provider_job(previous_row) if previous_row is not None else None
            )
            expected_revision = (previous.revision if previous is not None else 0) + 1
            if job.revision != expected_revision:
                raise ValueError(
                    f"provider job {job.event_id} expected revision "
                    f"{expected_revision}, received {job.revision}"
                )
            if previous is not None:
                immutable = (
                    "task_id",
                    "provider",
                    "service",
                    "provider_record_id",
                    "operation",
                    "component",
                    "event_type",
                    "resource_type",
                    "resource_id",
                    "submitted_at",
                    "owns_task",
                    "billing_dimensions",
                    "capability",
                )
                if any(getattr(previous, key) != getattr(job, key) for key in immutable):
                    raise ValueError(
                        "a provider job cannot change task, provider, resource, "
                        "operation, or capability identity across revisions"
                    )
                if job.observed_at < previous.observed_at:
                    raise ValueError(
                        "provider job observed_at cannot move backwards across revisions"
                    )
                previous_pending = previous.status in {"submitted", "running"}
                next_pending = job.status in {"submitted", "running"}
                if not previous_pending and next_pending:
                    raise ValueError("a terminal provider job cannot return to pending")
                if previous.status == "running" and job.status == "submitted":
                    raise ValueError("a running provider job cannot return to submitted")

            try:
                self._conn.execute(
                    """INSERT INTO provider_job_revisions (
                        event_id, revision, task_id, provider, service,
                        provider_record_id, operation, component, event_type,
                        resource_type, resource_id, lifecycle_status,
                        submitted_at, observed_at, owns_task,
                        billing_dimensions_json, usage_json,
                        cost_amount, cost_source, cost_confidence, pricing_version,
                        latency_ms, error_type, error_code, task_input_tokens,
                        task_output_tokens, task_cached_tokens, capability_json,
                        schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(job.event_id),
                        job.revision,
                        str(job.task_id),
                        job.provider,
                        job.service,
                        job.provider_record_id,
                        job.operation,
                        job.component,
                        job.event_type,
                        job.resource_type,
                        job.resource_id,
                        job.status,
                        job.submitted_at.isoformat(),
                        job.observed_at.isoformat(),
                        1 if job.owns_task else 0,
                        json.dumps(
                            [{"key": key, "value": value} for key, value in job.billing_dimensions]
                        ),
                        json.dumps([line.to_dict() for line in job.usage]),
                        str(job.cost_amount) if job.cost_amount is not None else None,
                        job.cost_source,
                        job.cost_confidence,
                        job.pricing_version,
                        job.latency_ms,
                        job.error_type,
                        job.error_code,
                        job.task_input_tokens,
                        job.task_output_tokens,
                        job.task_cached_tokens,
                        (
                            json.dumps(job.capability.to_dict())
                            if job.capability is not None
                            else None
                        ),
                        job.schema_version,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                # A second process may have appended the same next revision
                # between our read and write. Surface the conflict in the
                # ledger's stable ValueError vocabulary so reconciliation can
                # reload and retry without provider replay double-counting.
                raise ValueError(
                    f"provider job {job.event_id} revision {job.revision} "
                    "already exists with different contents"
                ) from exc
            self._conn.commit()

    def query_provider_job_history(self, event_id: str) -> list[ProviderJobRevision]:
        """Return every immutable revision for one provider job."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM provider_job_revisions WHERE event_id=? " "ORDER BY revision ASC",
                (event_id,),
            ).fetchall()
        return [self._row_to_provider_job(row) for row in rows]

    def get_provider_job(
        self, provider: str, service: str, provider_record_id: str
    ) -> ProviderJobRevision | None:
        """Return the latest revision for a provider-owned job identity."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM provider_job_revisions "
                "WHERE provider=? AND service=? AND provider_record_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (provider, service, provider_record_id),
            ).fetchone()
        return self._row_to_provider_job(row) if row is not None else None

    def query_provider_jobs_for_sync(self, limit: int = 1000) -> list[ProviderJobRevision]:
        """Return pending provider-job revisions in causal order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM provider_job_revisions WHERE sync_status='pending' "
                "ORDER BY observed_at ASC, event_id ASC, revision ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_provider_job(row) for row in rows]

    def query_current_provider_jobs_for_task(self, task_id: str) -> list[ProviderJobRevision]:
        """Return exactly one latest snapshot for every job on a task."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT job.*
                   FROM provider_job_revisions AS job
                   JOIN (
                     SELECT event_id, MAX(revision) AS revision
                     FROM provider_job_revisions
                     WHERE task_id=?
                     GROUP BY event_id
                   ) AS latest
                     ON latest.event_id=job.event_id AND latest.revision=job.revision
                   ORDER BY job.event_id ASC""",
                (task_id,),
            ).fetchall()
        return [self._row_to_provider_job(row) for row in rows]

    def mark_provider_jobs_synced(self, revisions: list[tuple[str, int]]) -> None:
        """Acknowledge identified provider-job revisions."""
        if not revisions:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE provider_job_revisions SET sync_status='synced' "
                "WHERE event_id=? AND revision=?",
                revisions,
            )
            self._conn.commit()

    def mark_provider_jobs_quarantined(self, revisions: list[tuple[str, int]]) -> None:
        """Retain an undeliverable provider-job revision without queue poison."""
        if not revisions:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE provider_job_revisions SET sync_status='quarantined' "
                "WHERE sync_status='pending' AND event_id=? AND revision=?",
                revisions,
            )
            self._conn.commit()

    def delivery_counts(self) -> dict[str, Any]:
        """Return one consistent snapshot of every durable delivery queue."""
        with self._lock:
            row = self._conn.execute("""SELECT
                     (SELECT COUNT(*) FROM events
                       WHERE sync_status='pending') AS pending_events,
                     (SELECT COUNT(*) FROM events
                       WHERE sync_status='quarantined') AS quarantined_events,
                     (SELECT COUNT(*) FROM tasks
                       WHERE sync_status='pending') AS pending_tasks,
                     (SELECT COUNT(*) FROM outcomes
                       WHERE sync_status='pending') AS pending_outcomes,
                     (SELECT COUNT(*) FROM outcomes
                       WHERE sync_status='quarantined') AS quarantined_outcomes,
                     (SELECT COUNT(*) FROM revenues
                       WHERE sync_status='pending') AS pending_revenues,
                     (SELECT COUNT(*) FROM revenues
                       WHERE sync_status='quarantined') AS quarantined_revenues,
                     (SELECT COUNT(*) FROM provider_job_revisions
                       WHERE sync_status='pending') AS pending_provider_jobs,
                     (SELECT COUNT(*) FROM provider_job_revisions
                       WHERE sync_status='quarantined') AS quarantined_provider_jobs,
                     (SELECT MIN(pending_at) FROM (
                       SELECT MIN(timestamp) AS pending_at FROM events
                         WHERE sync_status='pending'
                       UNION ALL
                       SELECT MIN(started_at) AS pending_at FROM tasks
                         WHERE sync_status='pending'
                       UNION ALL
                       SELECT MIN(observed_at) AS pending_at FROM outcomes
                         WHERE sync_status='pending'
                       UNION ALL
                       SELECT MIN(observed_at) AS pending_at FROM revenues
                         WHERE sync_status='pending'
                       UNION ALL
                       SELECT MIN(observed_at) AS pending_at FROM provider_job_revisions
                         WHERE sync_status='pending'
                     )) AS oldest_pending_at""").fetchone()
        if row is None:  # pragma: no cover - aggregate query always returns one row
            return {}
        return {
            "pending_events": int(row["pending_events"]),
            "quarantined_events": int(row["quarantined_events"]),
            "pending_tasks": int(row["pending_tasks"]),
            "pending_outcomes": int(row["pending_outcomes"]),
            "quarantined_outcomes": int(row["quarantined_outcomes"]),
            "pending_revenues": int(row["pending_revenues"]),
            "quarantined_revenues": int(row["quarantined_revenues"]),
            "pending_provider_jobs": int(row["pending_provider_jobs"]),
            "quarantined_provider_jobs": int(row["quarantined_provider_jobs"]),
            "oldest_pending_at": row["oldest_pending_at"],
        }

    def purge_synced(self, retention_hours: int = 48) -> int:
        """Delete synced telemetry events older than *retention_hours* and VACUUM.

        Outcome revisions are intentionally retained. They form a local
        append-only ledger whose latest revision is required to validate a
        correction recorded days or months later.

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
        """Remove pending or quarantined telemetry older than *max_age_days*.

        This is an explicit destructive maintenance operation. The background
        worker intentionally never calls it: financial attribution waiting on
        authentication, transport, or a converter upgrade must remain durable.
        Outcome revisions are retained because removing one revision would
        corrupt the local sequence required by a later correction.
        Returns the number of deleted rows.
        """
        with self._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            cursor = self._conn.execute(
                "DELETE FROM events "
                "WHERE sync_status IN ('pending', 'quarantined') AND timestamp < ?",
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
    def _row_to_outcome(row: sqlite3.Row) -> OutcomeRevision:
        raw_value = json.loads(row["value_json"]) if row["value_json"] is not None else None
        value = OutcomeValue.from_dict(raw_value) if raw_value is not None else None
        return OutcomeRevision(
            outcome_id=uuid.UUID(row["outcome_id"]),
            revision=int(row["revision"]),
            task_id=uuid.UUID(row["task_id"]),
            name=row["name"],
            state=row["lifecycle_state"],
            effective_at=parse_canonical(row["effective_at"]),
            observed_at=parse_canonical(row["observed_at"]),
            value=value,
            schema_version=row["schema_version"],
        )

    @staticmethod
    def _row_to_revenue(row: sqlite3.Row) -> RevenueRevision:
        amount = (
            RevenueAmount.from_input(row["amount"], row["currency"])
            if row["amount"] is not None
            else None
        )
        return RevenueRevision(
            revenue_id=uuid.UUID(row["revenue_id"]),
            revision=int(row["revision"]),
            task_id=uuid.UUID(row["task_id"]),
            outcome_id=(uuid.UUID(row["outcome_id"]) if row["outcome_id"] else None),
            state=row["lifecycle_state"],
            effective_at=parse_canonical(row["effective_at"]),
            observed_at=parse_canonical(row["observed_at"]),
            amount=amount,
            source=RevenueSource(
                type=row["source_type"],
                record_id=row["source_record_id"],
            ),
            schema_version=row["schema_version"],
        )

    @staticmethod
    def _row_to_provider_job(row: sqlite3.Row) -> ProviderJobRevision:
        raw_usage = json.loads(row["usage_json"])
        raw_capability = (
            json.loads(row["capability_json"]) if row["capability_json"] is not None else None
        )
        return ProviderJobRevision.from_dict(
            {
                "schema_version": row["schema_version"],
                "event_id": row["event_id"],
                "revision": row["revision"],
                "task_id": row["task_id"],
                "provider": row["provider"],
                "service": row["service"],
                "provider_record_id": row["provider_record_id"],
                "operation": row["operation"],
                "component": row["component"],
                "event_type": row["event_type"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "status": row["lifecycle_status"],
                "submitted_at": row["submitted_at"],
                "observed_at": row["observed_at"],
                "owns_task": bool(row["owns_task"]),
                "billing_dimensions": json.loads(row["billing_dimensions_json"]),
                "usage": [ProviderJobUsageLine.from_dict(line).to_dict() for line in raw_usage],
                "cost_amount": row["cost_amount"],
                "cost_source": row["cost_source"],
                "cost_confidence": row["cost_confidence"],
                "pricing_version": row["pricing_version"],
                "latency_ms": row["latency_ms"],
                "error_type": row["error_type"],
                "error_code": row["error_code"],
                "task_input_tokens": row["task_input_tokens"],
                "task_output_tokens": row["task_output_tokens"],
                "task_cached_tokens": row["task_cached_tokens"],
                "capability": raw_capability,
            }
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        row_keys = frozenset(row.keys())
        try:
            task_id = uuid.UUID(row["task_id"])
        except ValueError:
            task_id = uuid.uuid4()
        try:
            parent_task_id = uuid.UUID(row["parent_task_id"]) if row["parent_task_id"] else None
        except ValueError:
            parent_task_id = None
        try:
            root_task_id = (
                uuid.UUID(row["root_task_id"])
                if "root_task_id" in row_keys and row["root_task_id"]
                else None
            )
        except ValueError:
            root_task_id = None
        return Task(
            task_id=task_id,
            task_type=row["task_type"],
            status=row["status"],
            started_at=parse_canonical(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
            metadata=_json_loads(row["metadata"]),
            llm_cost_usd=_dec(row["llm_cost_usd"]),
            external_cost_usd=_dec(row["external_cost_usd"]),
            compute_cost_usd=_dec(row["compute_cost_usd"]),
            network_cost_usd=(
                _dec(row["network_cost_usd"]) if "network_cost_usd" in row_keys else Decimal("0")
            ),
            gpu_cost_usd=(
                _dec(row["gpu_cost_usd"]) if "gpu_cost_usd" in row_keys else Decimal("0")
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
            root_task_id=root_task_id,
            agent_id=row["agent_id"] if "agent_id" in row_keys else None,
            agent_version=(row["agent_version"] if "agent_version" in row_keys else None),
            workflow_id=row["workflow_id"] if "workflow_id" in row_keys else None,
            workflow_session_id=(
                row["workflow_session_id"] if "workflow_session_id" in row_keys else None
            ),
            user_id=row["user_id"] if "user_id" in row_keys else None,
            product_id=row["product_id"] if "product_id" in row_keys else None,
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
            occurred_at=parse_canonical(row["timestamp"]),
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
