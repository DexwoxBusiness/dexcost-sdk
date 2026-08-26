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
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import dexcost
from dexcost.storage.sqlite import SQLiteStorage

_FORK_SQLITE_HELPER = "--dexcost-fork-sqlite-helper"


def _count_sync_threads() -> int:
    """Count live SyncWorker threads (name == 'dexcost-sync')."""
    return sum(1 for t in threading.enumerate() if t.name == "dexcost-sync")


@pytest.fixture(autouse=True)
def _reset_dexcost():
    """Ensure each test starts with no global tracker / sync worker."""
    dexcost.close()
    yield
    dexcost.close()


def test_close_clears_service_catalog_refresh_credentials() -> None:
    dexcost._service_catalog_refresh_url = "https://old-control.example/catalog"
    dexcost._service_catalog_refresh_api_key = "dx_test_old"

    dexcost.close()

    assert dexcost._service_catalog_refresh_url is None
    assert dexcost._service_catalog_refresh_api_key is None


def test_close_removes_global_provider_instrumentation(tmp_path: Path) -> None:
    pytest.importorskip("openai")
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "global-instrument.db"),
        track_http=False,
        auto_instrument=["openai"],
    )

    dexcost.close()

    storage = SQLiteStorage(tmp_path / "manual-instrument.db")
    tracker = dexcost.CostTracker(storage=storage, auto_instrument=[])
    try:
        # A leaked global patch would raise "already active" here.
        dexcost.instrument_openai(tracker)
    finally:
        dexcost.uninstrument_openai()
        storage.close()


def test_init_without_http_clears_stale_catalog_refresh_state(tmp_path: Path) -> None:
    dexcost._service_catalog_refresh_url = "https://old-control.example/catalog"
    dexcost._service_catalog_refresh_api_key = "dx_test_old"

    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "dexcost.db"),
        track_http=False,
        auto_instrument=[],
    )

    assert dexcost._service_catalog_refresh_url is None
    assert dexcost._service_catalog_refresh_api_key is None


def test_invalid_catalog_trust_fails_before_storage_or_global_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A requested signature policy must never degrade to bundled fallback."""
    import dexcost.storage.sqlite as sqlite_module

    sqlite_factory = MagicMock()
    monkeypatch.setattr(sqlite_module, "SQLiteStorage", sqlite_factory)
    monkeypatch.setenv("DEXCOST_CATALOG_REQUIRE_SIGNATURE", "true")
    previous_config = dexcost._global_config

    with pytest.raises(ValueError, match="requires at least one trusted public key"):
        dexcost.init(
            storage="local",
            buffer_path=str(tmp_path / "must-not-exist.db"),
            auto_instrument=[],
            track_http=False,
            catalog_trusted_keys={},
        )

    sqlite_factory.assert_not_called()
    assert dexcost._global_config is previous_config
    assert dexcost._global_tracker is None


def test_init_uses_configured_buffer_for_capture(tmp_path: Path) -> None:
    """Capture and delivery must share the caller-selected SQLite buffer."""
    db_path = tmp_path / "configured-buffer.db"

    dexcost.init(
        storage="local",
        buffer_path=str(db_path),
        track_http=False,
        auto_instrument=[],
    )
    with dexcost.task("buffer-path-regression") as task:
        task.record_cost(
            service="buffer-path-test",
            cost_usd="0",
            cost_confidence="unknown",
        )

    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_singleton_task_forwards_local_gpu_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The public dexcost.task() wrapper must expose local GPU capture."""
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "gpu-opt-in.db"),
        track_http=False,
        auto_instrument=[],
    )
    assert dexcost._global_tracker is not None
    start_gpu = MagicMock()
    monkeypatch.setattr(
        dexcost._global_tracker,
        "_start_local_gpu_accounting",
        start_gpu,
    )

    with dexcost.task("local-whisper", track_gpu=True):
        pass

    start_gpu.assert_called_once()


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
        f"expected exactly 1 sync worker after second init, got {after} (orphaned worker leak)"
    )


def test_reinit_after_fork_restarts_atomic_catalog_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child process must replace the parent's dead catalog-release thread."""
    inherited_worker = MagicMock()
    child_worker = MagicMock()
    worker_factory = MagicMock(return_value=child_worker)
    inherited_storage = MagicMock()
    child_tracker_storage = MagicMock()
    child_sync_storage = MagicMock()
    sqlite_factory = MagicMock(side_effect=[child_tracker_storage, child_sync_storage])
    pricing = MagicMock()
    inherited_catalog_runtime = MagicMock()
    inherited_catalog_runtime._track_http = False
    inherited_catalog_runtime._trusted_keys = {
        "dexcost-test-rfc8032-1": "11qYAYdk9JNu81kOIyRUDn69brTa7WHqmX84xB6sSPA"
    }
    inherited_catalog_runtime._require_signature = True
    inherited_catalog_runtime._remote_refresh_enabled = True
    child_catalog_runtime = MagicMock()
    catalog_runtime_factory = MagicMock(return_value=child_catalog_runtime)
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
    monkeypatch.setattr(dexcost, "_catalog_runtime", inherited_catalog_runtime)
    monkeypatch.setattr(dexcost, "_global_tracker", tracker)
    monkeypatch.setattr(dexcost, "_global_config", config)
    monkeypatch.setattr(dexcost, "SyncWorker", worker_factory)
    monkeypatch.setattr(dexcost, "CatalogRuntime", catalog_runtime_factory)
    monkeypatch.setattr(sqlite_module, "SQLiteStorage", sqlite_factory)
    set_browser_storage = MagicMock()
    monkeypatch.setattr(browser_adapter, "set_storage", set_browser_storage)
    start_catalog_refresh = MagicMock()
    monkeypatch.setattr(dexcost, "_start_service_catalog_refresh", start_catalog_refresh)

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
    pricing.start_background_refresh.assert_not_called()
    catalog_runtime_factory.assert_called_once_with(
        endpoint=config.endpoint,
        db_path=config.buffer_path,
        tracker=tracker,
        track_http=False,
        api_key=config.api_key,
        trusted_keys=inherited_catalog_runtime._trusted_keys,
        require_signature=True,
        remote_refresh_enabled=True,
    )
    child_catalog_runtime.load_cached.assert_called_once_with()
    child_catalog_runtime.start.assert_called_once_with()
    start_catalog_refresh.assert_called_once_with()
    assert dexcost._pricing_engine is pricing
    assert dexcost._catalog_runtime is child_catalog_runtime


def _run_fork_sqlite_scenario(db_path: str) -> None:
    """Exercise DexCost's fork hook in a clean process with bounded waits."""
    dexcost.init(
        api_key="dx_test_abc",
        endpoint="http://127.0.0.1:9",
        buffer_path=db_path,
        auto_instrument=[],
        track_http=False,
        track_network=False,
    )
    # The catalog fork path has its own direct regression above. Stop its
    # background refresher so this scenario contains only the SyncWorker and
    # SQLite state it is intended to verify.
    if dexcost._catalog_runtime is not None:
        dexcost._catalog_runtime.close()
        dexcost._catalog_runtime._remote_refresh_enabled = False

    pid = os.fork()
    if pid == 0:
        # Child: record an event using the inherited tracker, then exit
        # immediately. The fork hook must close the inherited connection
        # and restart the worker so this write doesn't trample the
        # parent's fd.
        try:
            with dexcost.task("child-task"):
                dexcost.record_cost(
                    "parity-test",
                    cost_usd="0.01",
                    event_type="external_cost",
                )
        except BaseException:
            traceback.print_exc()
            os._exit(1)
        else:
            os._exit(0)

    try:
        deadline = time.monotonic() + 15.0
        while True:
            child_pid, status = os.waitpid(pid, os.WNOHANG)
            if child_pid == pid:
                break
            if time.monotonic() >= deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                raise AssertionError("fork child did not exit within 15 seconds")
            time.sleep(0.01)

        assert os.WIFEXITED(status), "child did not exit cleanly"
        assert os.WEXITSTATUS(status) == 0, (
            f"child exited with code {os.WEXITSTATUS(status)}"
        )

        # Parent: SQLite file must still be readable + integrity-clean.
        with sqlite3.connect(db_path) as conn:
            result = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            assert result == "ok", f"SQLite integrity check failed after fork: {result}"
    finally:
        dexcost.close()


def test_fork_does_not_corrupt_sqlite(tmp_path: Path) -> None:
    """B10 / §2.2.4 (b): the child must not corrupt the parent's SQLite.

    Python explicitly does not support forking an arbitrary multithreaded
    process. The complete provider suite leaves third-party worker threads
    alive, so execute this regression in a fresh interpreter containing only
    the DexCost threads whose recovery behavior the test owns.
    """
    if not hasattr(os, "fork"):
        pytest.skip("os.fork unavailable on this platform")

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        _FORK_SQLITE_HELPER,
        str(tmp_path / "dexcost.db"),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"isolated fork regression timed out: {exc}")

    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != _FORK_SQLITE_HELPER:
        raise SystemExit(2)
    _run_fork_sqlite_scenario(sys.argv[2])
