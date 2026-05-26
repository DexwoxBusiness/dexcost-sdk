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
import tempfile
import threading
import time
from pathlib import Path

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
