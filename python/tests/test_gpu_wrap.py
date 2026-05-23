"""Serverless GPU handler wraps — Modal / RunPod / Replicate."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

import dexcost
from dexcost.context import _current_task, set_current_task
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    t = CostTracker(storage=storage, auto_instrument=[])
    monkeypatch.setattr(dexcost, "_global_tracker", t)
    return t


@pytest.fixture
def stub_nvml_and_cgroup(monkeypatch):
    """Common NVML + cgroup mocks for the handler-wrap path."""
    from dexcost.cgroup_walker import CgroupScope
    from dexcost.nvml_reader import MemInfo, UtilSample

    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: True)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_count", lambda: 1)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_handle", lambda i: f"h{i}")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_product_name",
                        lambda h: "nvidia h100 80gb hbm3")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_mig_mode", lambda h: False)
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_memory_info",
        lambda h: MemInfo(used_bytes=2 * 2**30, total_bytes=80 * 2**30),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [os.getpid()],
    )
    snapshots = [
        {},
        {os.getpid(): UtilSample(pid=os.getpid(), sm_util=50,
                                  mem_util=20, time_stamp=500_000)},
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )


def test_modal_wrap_emits_gpu_cost_and_signal(tracker, stub_nvml_and_cgroup,
                                                monkeypatch):
    monkeypatch.setenv("MODAL_TASK_ID", "task-abc")

    from dexcost.gpu_wrap import wrap_modal_handler

    t = Task(task_id=uuid.uuid4(), task_type="modal",
             started_at=datetime.now(timezone.utc))
    tracker.storage.insert_task(t)
    token = set_current_task(t)

    @wrap_modal_handler
    def handler(payload):
        return {"result": "ok"}

    try:
        result = handler({"input": 42})
    finally:
        _current_task.reset(token)

    assert result == {"result": "ok"}
    events = tracker.storage.query_events(task_id=str(t.task_id))
    cost_events = [e for e in events if e.event_type == "gpu_cost"]
    sig_events = [e for e in events if e.event_type == "gpu_utilization_signal"]
    assert len(cost_events) == 1
    assert len(sig_events) == 1
    assert cost_events[0].details["billing_model"] == "per_gpu_second_active"
    assert cost_events[0].details["cost_pending"] is True
    assert cost_events[0].cost_usd == 0  # back-fills at task finalize
    # signal event has cost_usd=0 (Decision #3 observability carve-out).
    # The Event dataclass has default cost_confidence='exact', but the Control
    # Layer's contract is: never aggregate gpu_utilization_signal cost_usd
    # into any task total — pinned by the invariant test in Task 9.
    sig = sig_events[0]
    assert sig.cost_usd == 0
    assert sig.pricing_source is None
    # The signal event's details carry the load-bearing observability fields.
    assert "sm_util_pct" in sig.details
    assert "vram_used_peak_bytes" in sig.details


def test_runpod_wrap_billing_model(tracker, stub_nvml_and_cgroup, monkeypatch):
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-xyz")
    from dexcost.gpu_wrap import wrap_runpod_handler

    t = Task(task_id=uuid.uuid4(), task_type="runpod",
             started_at=datetime.now(timezone.utc))
    tracker.storage.insert_task(t)
    token = set_current_task(t)

    @wrap_runpod_handler
    def handler(*args, **kwargs):
        return "ok"

    try:
        handler()
    finally:
        _current_task.reset(token)

    events = tracker.storage.query_events(task_id=str(t.task_id))
    cost_events = [e for e in events if e.event_type == "gpu_cost"]
    assert len(cost_events) == 1
    assert cost_events[0].details["billing_model"] == "per_gpu_second_active"


def test_replicate_wrap_billing_model(tracker, stub_nvml_and_cgroup, monkeypatch):
    monkeypatch.setenv("REPLICATE_MODEL", "owner/model")
    from dexcost.gpu_wrap import wrap_replicate_handler

    t = Task(task_id=uuid.uuid4(), task_type="replicate",
             started_at=datetime.now(timezone.utc))
    tracker.storage.insert_task(t)
    token = set_current_task(t)

    @wrap_replicate_handler
    def handler(payload):
        return {}

    try:
        handler({"x": 1})
    finally:
        _current_task.reset(token)

    events = tracker.storage.query_events(task_id=str(t.task_id))
    cost_events = [e for e in events if e.event_type == "gpu_cost"]
    assert len(cost_events) == 1
    assert cost_events[0].details["billing_model"] == "per_gpu_second_active"


def test_no_active_task_pass_through(monkeypatch):
    """No dexcost task in context → wrap is transparent. (capture spec §6 case 2)"""
    monkeypatch.setenv("MODAL_TASK_ID", "task-abc")
    _current_task.set(None)
    from dexcost.gpu_wrap import wrap_modal_handler

    @wrap_modal_handler
    def handler(x):
        return x * 2

    assert handler(21) == 42  # no events emitted, no exception


def test_handler_exception_still_emits_event(tracker, stub_nvml_and_cgroup,
                                              monkeypatch):
    """Handler error → event still emitted, exception re-raised (capture spec §6 case 7)."""
    monkeypatch.setenv("MODAL_TASK_ID", "task-abc")
    from dexcost.gpu_wrap import wrap_modal_handler

    t = Task(task_id=uuid.uuid4(), task_type="modal",
             started_at=datetime.now(timezone.utc))
    tracker.storage.insert_task(t)
    token = set_current_task(t)

    @wrap_modal_handler
    def handler(payload):
        raise ValueError("simulated handler failure")

    try:
        with pytest.raises(ValueError, match="simulated"):
            handler({})
    finally:
        _current_task.reset(token)

    events = tracker.storage.query_events(task_id=str(t.task_id))
    cost_events = [e for e in events if e.event_type == "gpu_cost"]
    assert len(cost_events) == 1, "GPU-seconds consumed; event must emit even on error"


def test_all_three_wraps_exported_from_top_level():
    """The serverless wraps are reachable as dexcost.wrap_*_handler."""
    assert hasattr(dexcost, "wrap_modal_handler")
    assert hasattr(dexcost, "wrap_runpod_handler")
    assert hasattr(dexcost, "wrap_replicate_handler")
