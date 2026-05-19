"""Task end finalizes the NetworkAccountant onto the four task fields."""

from __future__ import annotations

import copy

import pytest

from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture()
def storage(tmp_path) -> SQLiteStorage:
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_instrument=[])


def test_recorded_bytes_land_on_task_at_end(
    tracker: CostTracker, storage: SQLiteStorage
) -> None:
    """Bytes recorded into the task's NetworkAccountant are finalized at task end."""
    tracked = tracker.start_task(task_type="scrape")
    # Simulate the adapter recording two HTTP calls.
    tracked.task._network.record("api.a.com", bytes_in=8000, bytes_out=400)
    tracked.task._network.record("api.b.com", bytes_in=200, bytes_out=50)
    tracked.end()

    stored = storage.get_task(str(tracked.task_id))
    assert stored is not None
    assert stored.network_bytes_in == 8200
    assert stored.network_bytes_out == 450
    assert stored.network_call_count == 2
    hosts = {h["host"] for h in stored.network_by_host["hosts"]}
    assert hosts == {"api.a.com", "api.b.com"}


def test_zero_call_task_ships_present_zero_fields(
    tracker: CostTracker, storage: SQLiteStorage
) -> None:
    """A task with no HTTP calls still gets all four network fields set to zero/empty."""
    tracked = tracker.start_task(task_type="noop")
    tracked.end()

    stored = storage.get_task(str(tracked.task_id))
    assert stored is not None
    assert stored.network_bytes_in == 0
    assert stored.network_bytes_out == 0
    assert stored.network_call_count == 0
    assert stored.network_by_host == {"hosts": []}


def test_failed_task_still_carries_finalized_network_fields(
    tracker: CostTracker, storage: SQLiteStorage
) -> None:
    """Network fields are finalized even when a task ends with status='failed'.

    Guards against a regression where network finalize is accidentally gated
    on success status.
    """
    tracked = tracker.start_task(task_type="fetch")
    tracked.task._network.record("api.c.com", bytes_in=1234, bytes_out=567)
    tracked.task._network.record("api.c.com", bytes_in=100, bytes_out=20)
    tracked.end(status="failed")

    stored = storage.get_task(str(tracked.task_id))
    assert stored is not None
    assert stored.status == "failed"
    assert stored.network_call_count == 2
    assert stored.network_bytes_in == 1334
    assert stored.network_bytes_out == 587
    hosts = {h["host"] for h in stored.network_by_host["hosts"]}
    assert "api.c.com" in hosts


def test_task_is_deepcopyable() -> None:
    """copy.deepcopy(Task(...)) must not raise (threading.Lock inside NetworkAccountant).

    The copy gets a fresh, empty NetworkAccountant — not the original's state.
    """
    original = Task(task_type="x")
    original._network.record("host.example.com", bytes_in=500, bytes_out=100)

    copied = copy.deepcopy(original)

    # The copy's accountant is a fresh instance — no recorded hosts.
    assert copied._network.live_host_count() == 0
    # The original is unaffected.
    assert original._network.live_host_count() == 1
