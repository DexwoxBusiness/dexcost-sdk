"""B14 regression — Sprint 2 Theme D / plan §3.2.3.

After a 401/403 the SDK's SyncWorker permanently stopped, with no
public API to provide a fresh key and resume. Customers had to
restart their process. This test pins the minimum API contract:

  - `dexcost.set_api_key(new_key)` is a public top-level function.
  - It updates the global config's `api_key`.
  - It clears any prior auth-failed state on the sync worker so the
    worker can resume on the next tick.
  - Calling it before `init()` is a safe no-op with a warning.
"""

from __future__ import annotations

import logging

import pytest

import dexcost


@pytest.fixture(autouse=True)
def _reset():
    dexcost.close()
    yield
    dexcost.close()


def test_set_api_key_is_exported() -> None:
    """The function must be importable from the top-level package."""
    assert hasattr(dexcost, "set_api_key"), "dexcost.set_api_key not exported"
    assert callable(dexcost.set_api_key)


def test_set_api_key_updates_global_config(tmp_path) -> None:
    db = str(tmp_path / "buf.db")
    dexcost.init(api_key="dx_test_old", buffer_path=db)
    assert dexcost._global_config is not None
    assert dexcost._global_config.api_key == "dx_test_old"

    dexcost.set_api_key("dx_live_new")

    assert dexcost._global_config.api_key == "dx_live_new"


def test_set_api_key_before_init_logs_warning_and_no_op(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Calling set_api_key without a prior init() must not crash."""
    with caplog.at_level(logging.WARNING):
        result = dexcost.set_api_key("dx_test_x")
    # Implementation choice: return value may be None or the config; the
    # invariant is "no exception, warning logged".
    assert result is None or result is False
    assert any(
        "init" in r.message.lower() or "set_api_key" in r.message.lower()
        for r in caplog.records
    ), f"expected a log warning, got {[r.message for r in caplog.records]}"


def test_set_api_key_clears_auth_failed_state(tmp_path) -> None:
    """If the sync worker was auth-stopped, set_api_key must clear the
    stop signal so a future push can run again."""
    db = str(tmp_path / "buf.db")
    dexcost.init(api_key="dx_test_old", buffer_path=db)
    worker = dexcost._sync_worker
    if worker is None:
        pytest.skip("no sync worker (dev mode); auth recovery N/A")

    # Simulate auth failure by setting the stop event directly — same
    # path the 401 handler takes at sync.py:368.
    worker._stop_event.set()
    assert worker._stop_event.is_set()

    dexcost.set_api_key("dx_live_new")

    # Either: (a) the existing worker has cleared its stop_event, or
    # (b) a fresh worker has been wired up. Both satisfy the contract.
    new_worker = dexcost._sync_worker
    assert new_worker is not None
    assert not new_worker._stop_event.is_set(), (
        "set_api_key did not clear the auth-failed stop signal"
    )
