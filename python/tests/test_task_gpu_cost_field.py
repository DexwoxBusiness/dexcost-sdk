"""Task model carries gpu_cost_usd; EventType enum has GPU values; v5→v6 migration."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dexcost.models.enums import EventType
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage


# ─── Enum ──────────────────────────────────────────────────────────────────

def test_event_type_enum_includes_gpu_values():
    assert EventType.GPU_COST.value == "gpu_cost"
    assert EventType.GPU_UTILIZATION_SIGNAL.value == "gpu_utilization_signal"


# ─── Task field ────────────────────────────────────────────────────────────

def test_gpu_cost_usd_defaults_to_zero():
    t = Task(task_type="x")
    assert t.gpu_cost_usd == Decimal("0")
    assert isinstance(t.gpu_cost_usd, Decimal)


def test_gpu_cost_usd_round_trip_through_dict():
    t = Task(task_type="x")
    t.gpu_cost_usd = Decimal("3.99")
    d = t.to_dict()
    assert d["gpu_cost_usd"] == "3.99"
    t2 = Task.from_dict(d)
    assert t2.gpu_cost_usd == Decimal("3.99")


def test_from_dict_defaults_gpu_cost_usd_for_old_payloads():
    d = Task(task_type="x").to_dict()
    d.pop("gpu_cost_usd")
    t = Task.from_dict(d)
    assert t.gpu_cost_usd == Decimal("0")


# ─── SQLite migration v5 → v6 ──────────────────────────────────────────────

def test_fresh_db_has_gpu_cost_usd_column(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "gpu_cost_usd" in cols
    assert st.get_schema_version() == 6
    st.close()


def test_gpu_cost_usd_round_trip_through_storage(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    t.gpu_cost_usd = Decimal("3.99")
    st.insert_task(t)
    got = st.get_task(str(t.task_id))
    assert got.gpu_cost_usd == Decimal("3.99")
    st.close()


def test_v5_db_migrates_to_v6(tmp_path):
    """A v5-shaped tasks table (no gpu_cost_usd) gets the new column added."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    # Full v5 schema (all columns that exist before the v5→v6 migration adds gpu_cost_usd).
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, "
        "metadata TEXT, customer_id TEXT, project_id TEXT, parent_task_id TEXT, "
        "experiment_id TEXT, variant TEXT, "
        "sync_status TEXT NOT NULL DEFAULT 'pending', "
        "llm_cost_usd TEXT NOT NULL DEFAULT '0', "
        "external_cost_usd TEXT NOT NULL DEFAULT '0', "
        "compute_cost_usd TEXT NOT NULL DEFAULT '0', "
        "network_cost_usd TEXT NOT NULL DEFAULT '0', "
        "total_cost_usd TEXT NOT NULL DEFAULT '0', "
        "total_input_tokens INTEGER NOT NULL DEFAULT 0, "
        "total_output_tokens INTEGER NOT NULL DEFAULT 0, "
        "total_cached_tokens INTEGER NOT NULL DEFAULT 0, "
        "retry_count INTEGER NOT NULL DEFAULT 0, "
        "retry_cost_usd TEXT NOT NULL DEFAULT '0', "
        "failure_count INTEGER NOT NULL DEFAULT 0, "
        "network_bytes_in INTEGER NOT NULL DEFAULT 0, "
        "network_bytes_out INTEGER NOT NULL DEFAULT 0, "
        "network_call_count INTEGER NOT NULL DEFAULT 0, "
        "network_by_host TEXT NOT NULL DEFAULT '{\"hosts\": []}')"
    )
    # The events table SQLiteStorage opens cleanly when present.
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "event_type TEXT NOT NULL, timestamp TEXT NOT NULL, "
        "cost_usd TEXT NOT NULL DEFAULT '0', sync_status TEXT NOT NULL DEFAULT 'pending')"
    )
    conn.execute(
        "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "version_number INTEGER NOT NULL, applied_at TEXT NOT NULL "
        "DEFAULT (datetime('now')), migration_name TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_version (version_number, migration_name) VALUES (5, 'seed')"
    )
    conn.commit()
    conn.close()

    st = SQLiteStorage(db_path=str(db))
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "gpu_cost_usd" in cols
    assert st.get_schema_version() == 6
    # idempotent re-apply
    st.close()
    st2 = SQLiteStorage(db_path=str(db))
    assert st2.get_schema_version() == 6
    st2.close()
