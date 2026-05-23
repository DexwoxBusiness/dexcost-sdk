"""v4→v5 migration adds the network_cost_usd column; Decimal round-trip."""

import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage


def test_fresh_db_has_network_cost_usd_column(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "network_cost_usd" in cols
    assert st.get_schema_version() == 6
    st.close()


def test_network_cost_usd_round_trip_through_storage(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    t.network_cost_usd = Decimal("0.0042")
    st.insert_task(t)
    got = st.get_task(str(t.task_id))
    assert got is not None
    assert got.network_cost_usd == Decimal("0.0042")
    st.close()


def test_v4_db_migrates_to_v5(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, "
        "metadata TEXT, llm_cost_usd TEXT, external_cost_usd TEXT, "
        "compute_cost_usd TEXT, total_cost_usd TEXT, "
        "total_input_tokens INTEGER, total_output_tokens INTEGER, "
        "total_cached_tokens INTEGER, "
        "retry_count INTEGER NOT NULL DEFAULT 0, "
        "retry_cost_usd TEXT NOT NULL DEFAULT '0', "
        "failure_count INTEGER NOT NULL DEFAULT 0, "
        "customer_id TEXT, project_id TEXT, parent_task_id TEXT, "
        "experiment_id TEXT, variant TEXT, "
        "sync_status TEXT NOT NULL DEFAULT 'pending', "
        "network_bytes_in INTEGER NOT NULL DEFAULT 0, "
        "network_bytes_out INTEGER NOT NULL DEFAULT 0, "
        "network_call_count INTEGER NOT NULL DEFAULT 0, "
        "network_by_host TEXT NOT NULL DEFAULT '{\"hosts\": []}')"
    )
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "event_type TEXT NOT NULL, timestamp TEXT NOT NULL, "
        "cost_usd TEXT NOT NULL DEFAULT '0', cost_confidence TEXT, "
        "pricing_source TEXT, pricing_version TEXT, service_name TEXT, "
        "provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
        "cached_tokens INTEGER, latency_ms INTEGER, is_retry INTEGER, "
        "retry_reason TEXT, retry_of TEXT, details TEXT, "
        "sync_status TEXT NOT NULL DEFAULT 'pending')"
    )
    conn.execute(
        "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "version_number INTEGER NOT NULL, applied_at TEXT NOT NULL "
        "DEFAULT (datetime('now')), migration_name TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_version (version_number, migration_name) VALUES (4, 'seed')"
    )
    conn.commit()
    conn.close()

    st = SQLiteStorage(db_path=str(db))
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "network_cost_usd" in cols
    st.close()
    # Re-applying the migration is a no-op (idempotent).
    st2 = SQLiteStorage(db_path=str(db))
    assert st2.get_schema_version() == 6
    st2.close()


def test_v4_task_reads_back_with_zero_network_cost(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, "
        "metadata TEXT, llm_cost_usd TEXT, "
        "external_cost_usd TEXT, "
        "compute_cost_usd TEXT, "
        "total_cost_usd TEXT, "
        "total_input_tokens INTEGER, "
        "total_output_tokens INTEGER, "
        "total_cached_tokens INTEGER, "
        "retry_count INTEGER NOT NULL DEFAULT 0, "
        "retry_cost_usd TEXT NOT NULL DEFAULT '0', "
        "failure_count INTEGER NOT NULL DEFAULT 0, "
        "customer_id TEXT, project_id TEXT, parent_task_id TEXT, "
        "experiment_id TEXT, variant TEXT, "
        "sync_status TEXT NOT NULL DEFAULT 'pending', "
        "network_bytes_in INTEGER NOT NULL DEFAULT 0, "
        "network_bytes_out INTEGER NOT NULL DEFAULT 0, "
        "network_call_count INTEGER NOT NULL DEFAULT 0, "
        "network_by_host TEXT NOT NULL DEFAULT '{\"hosts\": []}')"
    )
    tid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, started_at, "
        "llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd, "
        "total_input_tokens, total_output_tokens, total_cached_tokens) "
        "VALUES (?, ?, ?, ?, '0', '0', '0', '0', 0, 0, 0)",
        (tid, "old", "success", datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "version_number INTEGER NOT NULL, applied_at TEXT NOT NULL "
        "DEFAULT (datetime('now')), migration_name TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_version (version_number, migration_name) VALUES (4, 'seed')"
    )
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "event_type TEXT NOT NULL, timestamp TEXT NOT NULL, "
        "cost_usd TEXT NOT NULL DEFAULT '0', cost_confidence TEXT, "
        "pricing_source TEXT, pricing_version TEXT, service_name TEXT, "
        "provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
        "cached_tokens INTEGER, latency_ms INTEGER, is_retry INTEGER, "
        "retry_reason TEXT, retry_of TEXT, details TEXT, "
        "sync_status TEXT NOT NULL DEFAULT 'pending')"
    )
    conn.commit()
    conn.close()

    st = SQLiteStorage(db_path=str(db))
    got = st.get_task(tid)
    assert got is not None
    assert got.network_cost_usd == Decimal("0")
    assert isinstance(got.network_cost_usd, Decimal)
    st.close()
