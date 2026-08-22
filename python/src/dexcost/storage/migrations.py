"""Schema migration system — auto-migrate without data loss.

Migration functions are plain Python callables that accept a database connection
(``sqlite3.Connection`` for SQLite, ``asyncpg.Connection`` for PostgreSQL) and
execute raw SQL.  Each migration advances the schema by exactly one version.

On startup the storage backend calls :func:`run_migrations` which compares the
installed target version against the version recorded in the DB and runs any
outstanding migrations sequentially inside a transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

# The target schema version that the *code* expects.  Bump this whenever a new
# migration is added and register the migration below.
TARGET_SCHEMA_VERSION = 14

# ── Migration registry ────────────────────────────────────────────────

# Maps ``(from_version, to_version)`` → migration callable.
# SQLite migrations receive ``sqlite3.Connection``; PostgreSQL migrations
# receive ``asyncpg.Connection``.

_SQLITE_MIGRATIONS: dict[tuple[int, int], Callable[[sqlite3.Connection], None]] = {}

# async callables for PostgreSQL — values are ``async def fn(conn): ...``
_PG_MIGRATIONS: dict[tuple[int, int], Callable[..., Any]] = {}


def register_sqlite_migration(
    from_version: int, to_version: int
) -> Callable[[Callable[[sqlite3.Connection], None]], Callable[[sqlite3.Connection], None]]:
    """Decorator to register a SQLite migration function."""

    def decorator(
        fn: Callable[[sqlite3.Connection], None],
    ) -> Callable[[sqlite3.Connection], None]:
        _SQLITE_MIGRATIONS[(from_version, to_version)] = fn
        return fn

    return decorator


def register_pg_migration(
    from_version: int, to_version: int
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to register a PostgreSQL migration function."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _PG_MIGRATIONS[(from_version, to_version)] = fn
        return fn

    return decorator


# ── SQLite migration runner ───────────────────────────────────────────


class MigrationError(Exception):
    """Raised when a migration fails."""


def run_sqlite_migrations(conn: sqlite3.Connection, current_version: int) -> int:
    """Run outstanding SQLite migrations inside a transaction.

    Parameters
    ----------
    conn:
        An open ``sqlite3.Connection``.
    current_version:
        The schema version currently recorded in the database.

    Returns
    -------
    int
        The schema version after all migrations have been applied.

    Raises
    ------
    MigrationError
        If any migration fails.  The transaction is rolled back so the
        database is left unchanged.
    """
    if current_version >= TARGET_SCHEMA_VERSION:
        return current_version

    version = current_version
    while version < TARGET_SCHEMA_VERSION:
        next_version = version + 1
        key = (version, next_version)
        migration_fn = _SQLITE_MIGRATIONS.get(key)
        if migration_fn is None:
            raise MigrationError(
                f"No SQLite migration registered for v{version} → v{next_version}. "
                f"Target is v{TARGET_SCHEMA_VERSION}."
            )

        try:
            # Run migration + version bump in a single transaction.
            conn.execute("BEGIN")
            migration_fn(conn)
            conn.execute(
                "INSERT INTO schema_version (version_number, migration_name) VALUES (?, ?)",
                (next_version, f"v{version}_to_v{next_version}"),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise MigrationError(
                f"SQLite migration v{version} → v{next_version} failed: {exc}"
            ) from exc

        version = next_version

    return version


# ── PostgreSQL migration runner ───────────────────────────────────────


async def run_pg_migrations_async(conn: Any, current_version: int) -> int:
    """Run outstanding PostgreSQL migrations inside a transaction.

    Parameters
    ----------
    conn:
        An ``asyncpg.Connection``.
    current_version:
        The schema version currently recorded in the database.

    Returns
    -------
    int
        The schema version after all migrations have been applied.

    Raises
    ------
    MigrationError
        If any migration fails.  The transaction is rolled back.
    """
    if current_version >= TARGET_SCHEMA_VERSION:
        return current_version

    version = current_version
    while version < TARGET_SCHEMA_VERSION:
        next_version = version + 1
        key = (version, next_version)
        migration_fn = _PG_MIGRATIONS.get(key)
        if migration_fn is None:
            raise MigrationError(
                f"No PostgreSQL migration registered for v{version} → v{next_version}. "
                f"Target is v{TARGET_SCHEMA_VERSION}."
            )

        try:
            async with conn.transaction():
                await migration_fn(conn)
                await conn.execute(
                    "INSERT INTO schema_version (version_number, migration_name) "
                    "VALUES ($1, $2)",
                    next_version,
                    f"v{version}_to_v{next_version}",
                )
        except Exception as exc:
            raise MigrationError(
                f"PostgreSQL migration v{version} → v{next_version} failed: {exc}"
            ) from exc

        version = next_version

    return version


# ── Migrations ────────────────────────────────────────────────────────

_TASKS_TABLE_INFO = "PRAGMA table_info(tasks)"


@register_sqlite_migration(1, 2)
def _sqlite_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add experiment_id and variant columns to tasks table (idempotent)."""
    existing = {
        row[1]
        for row in conn.execute(_TASKS_TABLE_INFO).fetchall()
    }
    if "experiment_id" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN experiment_id TEXT")
    if "variant" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN variant TEXT")


@register_sqlite_migration(2, 3)
def _sqlite_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Add sync_status column to tasks table (idempotent).

    Without this column the SyncWorker had no way to tell which tasks had
    already been pushed, so it re-POSTed every task on every sync cycle.
    Existing rows default to ``'pending'`` so they are pushed exactly once.
    """
    existing = {
        row[1]
        for row in conn.execute(_TASKS_TABLE_INFO).fetchall()
    }
    if "sync_status" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'pending'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_sync ON tasks(sync_status, started_at)"
    )


@register_sqlite_migration(3, 4)
def _sqlite_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Add network capture columns to tasks table (idempotent).

    Four new columns track egress/ingress byte counts, HTTP call counts,
    and a per-host JSON breakdown.  Existing rows default to zero / empty
    so they are valid without a data backfill.
    """
    existing = {
        row[1]
        for row in conn.execute(_TASKS_TABLE_INFO).fetchall()
    }
    if "network_bytes_in" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN network_bytes_in INTEGER NOT NULL DEFAULT 0"
        )
    if "network_bytes_out" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN network_bytes_out INTEGER NOT NULL DEFAULT 0"
        )
    if "network_call_count" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN network_call_count INTEGER NOT NULL DEFAULT 0"
        )
    if "network_by_host" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN network_by_host TEXT NOT NULL "
            "DEFAULT '{\"hosts\": []}'"
        )


@register_sqlite_migration(4, 5)
def _sqlite_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Add network_cost_usd column to tasks table (idempotent).

    Stores the per-task cloud-egress cost as Decimal-in-TEXT, consistent
    with the other *_cost_usd columns. Existing rows default to '0' so
    no data backfill is required.
    """
    existing = {
        row[1]
        for row in conn.execute(_TASKS_TABLE_INFO).fetchall()
    }
    if "network_cost_usd" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN network_cost_usd TEXT NOT NULL DEFAULT '0'"
        )


@register_sqlite_migration(5, 6)
def _sqlite_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Add gpu_cost_usd column to tasks table (idempotent).

    Stores the per-task GPU cost as Decimal-in-TEXT, consistent with the
    other *_cost_usd columns. Existing rows default to '0' so no backfill
    is required. Total cost becomes llm + external + compute + network + gpu.
    """
    existing = {
        row[1]
        for row in conn.execute(_TASKS_TABLE_INFO).fetchall()
    }
    if "gpu_cost_usd" not in existing:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN gpu_cost_usd TEXT NOT NULL DEFAULT '0'"
        )


@register_sqlite_migration(6, 7)
def _sqlite_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Add the opt-in canonical root task identity."""
    existing = {row[1] for row in conn.execute(_TASKS_TABLE_INFO).fetchall()}
    if "root_task_id" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN root_task_id TEXT")


@register_sqlite_migration(7, 8)
def _sqlite_v7_to_v8(conn: sqlite3.Connection) -> None:
    """Add the durable revisioned business-outcome queue."""
    from dexcost.storage.sqlite import _CREATE_OUTCOME_INDEXES, _CREATE_OUTCOMES

    conn.execute(_CREATE_OUTCOMES)
    for index_sql in _CREATE_OUTCOME_INDEXES:
        conn.execute(index_sql)


@register_sqlite_migration(8, 9)
def _sqlite_v8_to_v9(conn: sqlite3.Connection) -> None:
    """Persist canonical agent and workflow identity on buffered tasks."""
    existing = {row[1] for row in conn.execute(_TASKS_TABLE_INFO).fetchall()}
    for column in (
        "agent_id",
        "agent_version",
        "workflow_id",
        "workflow_session_id",
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")


@register_sqlite_migration(9, 10)
def _sqlite_v9_to_v10(conn: sqlite3.Connection) -> None:
    """Persist canonical end-user and product assignment on buffered tasks."""
    existing = {row[1] for row in conn.execute(_TASKS_TABLE_INFO).fetchall()}
    for column in ("user_id", "product_id"):
        if column not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")


@register_sqlite_migration(10, 11)
def _sqlite_v10_to_v11(conn: sqlite3.Connection) -> None:
    """Add the durable, atomic SDK catalog-release cache."""
    from dexcost.storage.sqlite import (
        _CREATE_CATALOG_ARTIFACTS,
        _CREATE_CATALOG_INDEXES,
        _CREATE_CATALOG_RELEASE_ARTIFACTS,
        _CREATE_CATALOG_RELEASES,
        _CREATE_CATALOG_STATE,
    )

    conn.execute(_CREATE_CATALOG_ARTIFACTS)
    conn.execute(_CREATE_CATALOG_RELEASES)
    conn.execute(_CREATE_CATALOG_RELEASE_ARTIFACTS)
    conn.execute(_CREATE_CATALOG_STATE)
    for index_sql in _CREATE_CATALOG_INDEXES:
        conn.execute(index_sql)


@register_sqlite_migration(11, 12)
def _sqlite_v11_to_v12(conn: sqlite3.Connection) -> None:
    """Add authenticated, API-key-isolated workspace catalog overlays."""
    from dexcost.storage.sqlite import _CREATE_CATALOG_OVERLAYS

    conn.execute(_CREATE_CATALOG_OVERLAYS)


@register_sqlite_migration(12, 13)
def _sqlite_v12_to_v13(conn: sqlite3.Connection) -> None:
    """Add the durable immutable revenue-revision ledger."""
    from dexcost.storage.sqlite import _CREATE_REVENUE_INDEXES, _CREATE_REVENUES

    conn.execute(_CREATE_REVENUES)
    for index_sql in _CREATE_REVENUE_INDEXES:
        conn.execute(index_sql)


@register_sqlite_migration(13, 14)
def _sqlite_v13_to_v14(conn: sqlite3.Connection) -> None:
    """Add immutable asynchronous-provider-job revision storage."""
    from dexcost.storage.sqlite import (
        _CREATE_PROVIDER_JOB_INDEXES,
        _CREATE_PROVIDER_JOB_REVISIONS,
    )

    conn.execute(_CREATE_PROVIDER_JOB_REVISIONS)
    for index_sql in _CREATE_PROVIDER_JOB_INDEXES:
        conn.execute(index_sql)
