"""v3->v4 migration adds the four network columns; round-trip works."""

import sqlite3
import uuid
from datetime import datetime, timezone

from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage


def test_fresh_db_has_network_columns(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert {"network_bytes_in", "network_bytes_out",
            "network_call_count", "network_by_host"} <= cols
    st.close()


def test_task_network_fields_round_trip_through_storage(tmp_path):
    st = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
    t = Task(task_id=uuid.uuid4(), task_type="scrape",
             started_at=datetime.now(timezone.utc))
    t.network_bytes_in = 9000
    t.network_bytes_out = 1200
    t.network_call_count = 5
    t.network_by_host = {"hosts": [{"host": "x.com", "calls": 5,
                                    "bytes_in": 9000, "bytes_out": 1200}]}
    st.insert_task(t)
    got = st.get_task(str(t.task_id))
    assert got.network_bytes_in == 9000
    assert got.network_bytes_out == 1200
    assert got.network_call_count == 5
    assert got.network_by_host == {"hosts": [{"host": "x.com", "calls": 5,
                                              "bytes_in": 9000, "bytes_out": 1200}]}

    # Also verify the update_task path writes and reads back new values.
    got.network_bytes_in = 42000
    got.network_bytes_out = 3300
    got.network_call_count = 17
    got.network_by_host = {"hosts": [{"host": "y.com", "calls": 17,
                                      "bytes_in": 42000, "bytes_out": 3300}]}
    st.update_task(got)
    updated = st.get_task(str(t.task_id))
    assert updated.network_bytes_in == 42000
    assert updated.network_bytes_out == 3300
    assert updated.network_call_count == 17
    assert updated.network_by_host == {"hosts": [{"host": "y.com", "calls": 17,
                                                  "bytes_in": 42000, "bytes_out": 3300}]}
    st.close()


def test_v3_db_migrates_to_v4(tmp_path):
    # Build a v3-shaped tasks table (no network columns), record version 3.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, "
        "status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, "
        "metadata TEXT, llm_cost_usd TEXT, external_cost_usd TEXT, "
        "compute_cost_usd TEXT, total_cost_usd TEXT, "
        "total_input_tokens INTEGER, total_output_tokens INTEGER, "
        "total_cached_tokens INTEGER, retry_count INTEGER DEFAULT 0, "
        "retry_cost_usd TEXT DEFAULT '0', failure_count INTEGER DEFAULT 0, "
        "customer_id TEXT, project_id TEXT, parent_task_id TEXT, "
        "experiment_id TEXT, variant TEXT, "
        "sync_status TEXT NOT NULL DEFAULT 'pending')"
    )
    conn.execute(
        "CREATE TABLE schema_version (version_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "version_number INTEGER NOT NULL, applied_at TEXT NOT NULL "
        "DEFAULT (datetime('now')), migration_name TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_version (version_number, migration_name) VALUES (3, 'seed')"
    )
    # Insert a pre-existing row using only the v3 columns; after migration the
    # four new network columns must carry their DEFAULT values.
    pre_existing_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, started_at, sync_status) "
        "VALUES (?, 'smoke', 'running', '2024-01-01T00:00:00+00:00', 'pending')",
        (pre_existing_id,),
    )
    conn.commit()
    conn.close()

    st = SQLiteStorage(db_path=str(db))  # opening runs migrations
    cols = {r[1] for r in st._conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "network_by_host" in cols
    # v3→v4 (network capture) AND v4→v5 (network_cost_usd) both run.
    assert st.get_schema_version() == 6

    # Verify that the pre-existing row received the column DEFAULTs.
    row = st.get_task(pre_existing_id)
    assert row is not None
    assert row.network_bytes_in == 0
    assert row.network_bytes_out == 0
    assert row.network_call_count == 0
    assert row.network_by_host == {"hosts": []}

    st.close()
