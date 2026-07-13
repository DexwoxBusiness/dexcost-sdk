"""B10 regression tests — Sprint 1 Theme B / plan §2.2.4.

Two crash/data-corruption sources:

1. ``dexcost.init()`` is not idempotent — calling it twice orphans the
   previous SyncWorker thread (the previous reference is dropped without
   ``.stop()``), so duplicate background workers race on the SQLite file.
2. After ``os.fork()`` the child inherits the parent's SQLite connection
   fd and SyncWorker thread state. Concurrent writes from two processes
   to the same fd corrupt the buffer; the inherited Thread object is not
   actually running in the child but is referenced by module globals.

Fix path: idempotency guard in init(); ``os.register_at_fork`` hook to
close inherited resources and restart a fresh sync worker per child.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import dexcost


def _count_sync_threads() -> int:
    """Count live SyncWorker threads (name == 'dexcost-sync')."""
    return sum(1 for t in threading.enumerate() if t.name == "dexcost-sync")


@pytest.fixture(autouse=True)
def _reset_dexcost():
    """Ensure each test starts with no global tracker / sync worker."""
    dexcost.close()
    yield
    dexcost.close()


def test_double_init_does_not_create_orphan_threads(tmp_path: Path) -> None:
    """B10 / §2.2.4 (a): calling init() twice must not orphan the first
    SyncWorker thread.
    """
    db_path = str(tmp_path / "dexcost.db")

    dexcost.init(api_key="dx_test_abc", buffer_path=db_path)
    # Give the worker a moment to actually start.
    time.sleep(0.05)
    before = _count_sync_threads()
    assert before == 1, f"expected exactly 1 sync worker after first init, got {before}"

    dexcost.init(api_key="dx_test_abc", buffer_path=db_path)
    time.sleep(0.05)
    after = _count_sync_threads()
    assert after == 1, (
        f"expected exactly 1 sync worker after second init, got {after} "
        f"(orphaned worker leak)"
    )


def test_reinit_after_fork_restarts_pricing_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child process must replace the parent's dead pricing thread."""
    inherited_worker = MagicMock()
    child_worker = MagicMock()
    worker_factory = MagicMock(return_value=child_worker)
    inherited_storage = MagicMock()
    child_tracker_storage = MagicMock()
    child_sync_storage = MagicMock()
    sqlite_factory = MagicMock(side_effect=[child_tracker_storage, child_sync_storage])
    pricing = MagicMock()
    tracker = MagicMock()
    tracker._storage = inherited_storage
    tracker.pricing = pricing
    config = MagicMock()
    config.storage_mode = "cloud"
    config.is_dev = False
    config.buffer_path = str(tmp_path / "dexcost.db")
    config.api_key = "dx_test_fork"
    config.endpoint = "https://control.example"

    import dexcost.adapters.browser as browser_adapter
    import dexcost.storage.sqlite as sqlite_module

    monkeypatch.setattr(dexcost, "_sync_worker", inherited_worker)
    monkeypatch.setattr(dexcost, "_pricing_engine", pricing)
    monkeypatch.setattr(dexcost, "_global_tracker", tracker)
    monkeypatch.setattr(dexcost, "_global_config", config)
    monkeypatch.setattr(dexcost, "SyncWorker", worker_factory)
    monkeypatch.setattr(sqlite_module, "SQLiteStorage", sqlite_factory)
    set_browser_storage = MagicMock()
    monkeypatch.setattr(browser_adapter, "set_storage", set_browser_storage)

    dexcost._reinit_after_fork()

    inherited_worker.stop.assert_not_called()
    inherited_storage.close.assert_called_once_with()
    set_browser_storage.assert_called_once_with(child_tracker_storage)
    worker_factory.assert_called_once_with(
        config=config,
        storage=child_sync_storage,
        db_path=config.buffer_path,
    )
    child_worker.start.assert_called_once_with()
    pricing.set_api_key.assert_called_once_with(config.api_key)
    pricing.start_background_refresh.assert_called_once_with(config.endpoint)
    assert dexcost._pricing_engine is pricing


def test_fork_does_not_corrupt_sqlite(tmp_path: Path) -> None:
    """B10 / §2.2.4 (b): the child must not corrupt the parent's SQLite
    file when both processes write through inherited connections.

    We assert SQLite integrity via PRAGMA integrity_check after the
    child exits; pre-fix this can return non-'ok' or trigger malformed-
    database errors on the next parent read.
    """
    if not hasattr(os, "fork"):
        pytest.skip("os.fork unavailable on this platform")

    db_path = str(tmp_path / "dexcost.db")
    dexcost.init(api_key="dx_test_abc", buffer_path=db_path)

    pid = os.fork()
    if pid == 0:
        # Child: record an event using the inherited tracker, then exit
        # immediately. The fork hook must close the inherited connection
        # and restart the worker so this write doesn't trample the
        # parent's fd.
        try:
            with dexcost.task("child-task"):
                dexcost.record_cost(
                    "external_cost",
                    cost_usd="0.01",
                    service="parity-test",
                )
        finally:
            os._exit(0)

    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status), "child did not exit cleanly"
    assert os.WEXITSTATUS(status) == 0, f"child exited with code {os.WEXITSTATUS(status)}"

    # Parent: SQLite file must still be readable + integrity-clean.
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("PRAGMA integrity_check;")
        result = cur.fetchone()[0]
        assert result == "ok", f"SQLite integrity check failed after fork: {result}"
    finally:
        conn.close()
