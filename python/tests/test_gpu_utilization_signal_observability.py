"""Decision #3 + convention §1 carve-out — gpu_utilization_signal is observability-only.

LOAD-BEARING TEST. The convention §1 carve-out says: signal events have no
cost_usd / pricing_source / cost_confidence / pricing_version, and the
Control Layer must NEVER aggregate them into any cost field. This test
pins that contract as executable code.

If a future refactor accidentally:
  - aggregates gpu_utilization_signal cost_usd into task.gpu_cost_usd
  - back-fills cost_usd on gpu_utilization_signal events
  - drops the events entirely (no observability surface)
this test fails fast and points to the decision.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from dexcost import cloud_detect
from dexcost.cgroup_walker import CgroupScope
from dexcost.cloud_detect import CloudEnv
from dexcost.gpu_accountant import GpuAccountant
from dexcost.gpu_runtime import GpuRuntimeKind
from dexcost.models.task import Task
from dexcost.nvml_reader import MemInfo, UtilSample
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result",
        CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge"),
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    return CostTracker(storage=storage, auto_instrument=[])


@pytest.fixture(autouse=True)
def _stub_nvml(monkeypatch):
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
        {os.getpid(): [UtilSample(pid=os.getpid(), sm_util=50, mem_util=30,
                                  time_stamp=30_000_000)]},
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )


def _emit_and_finalize_gpu_task(tracker):
    started = datetime.now(timezone.utc) - timedelta(seconds=60)
    t = Task(task_id=uuid.uuid4(), task_type="x", started_at=started)
    t.ended_at = started + timedelta(seconds=60)
    tracker.storage.insert_task(t)
    accountant = GpuAccountant(
        GpuRuntimeKind.AWS_EC2_GPU,
        CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge"),
    )
    accountant.snapshot_start()
    t._gpu = accountant
    tracker._aggregate_costs(t)
    return t


def test_signal_events_have_zero_cost_usd_before_back_fill(tracker):
    t = _emit_and_finalize_gpu_task(tracker)
    sig_events = [e for e in tracker.storage.query_events(task_id=str(t.task_id))
                   if e.event_type == "gpu_utilization_signal"]
    assert len(sig_events) >= 1
    for sig in sig_events:
        assert sig.cost_usd == Decimal("0")


def test_signal_events_have_zero_cost_usd_after_back_fill(tracker):
    """Even after _finalize_gpu runs, signal events stay at cost_usd=0."""
    t = _emit_and_finalize_gpu_task(tracker)
    sig_events = [e for e in tracker.storage.query_events(task_id=str(t.task_id))
                   if e.event_type == "gpu_utilization_signal"]
    for sig in sig_events:
        assert sig.cost_usd == Decimal("0")
        # The back-fill walker filters on cost_pending; signal events
        # don't have cost_pending in details, so they're skipped.
        assert "cost_pending" not in (sig.details or {})


def test_signal_events_never_aggregated_into_gpu_cost_usd(tracker):
    """task.gpu_cost_usd MUST equal sum of gpu_cost events ONLY."""
    t = _emit_and_finalize_gpu_task(tracker)
    events = tracker.storage.query_events(task_id=str(t.task_id))
    gpu_cost_sum = sum(
        (e.cost_usd for e in events if e.event_type == "gpu_cost"),
        Decimal("0"),
    )
    signal_count = sum(
        1 for e in events if e.event_type == "gpu_utilization_signal"
    )
    assert signal_count >= 1, "expected at least one signal event"
    assert t.gpu_cost_usd == gpu_cost_sum, (
        "Decision #3 carve-out violated: gpu_utilization_signal events MUST "
        "NOT contribute to task.gpu_cost_usd. They are observability-only."
    )


def test_signal_events_carry_observability_fields(tracker):
    """Signal events carry the load-bearing observability fields per Decision #3."""
    t = _emit_and_finalize_gpu_task(tracker)
    sig_events = [e for e in tracker.storage.query_events(task_id=str(t.task_id))
                   if e.event_type == "gpu_utilization_signal"]
    assert sig_events
    sig = sig_events[0]
    details = sig.details
    # The fields the Cost Intelligence dashboard depends on:
    for field in (
        "gpu_index", "gpu_sku",
        "sm_util_pct", "mem_util_pct",
        "vram_used_peak_bytes", "vram_total_bytes",
        "process_count", "sample_count", "task_duration_ms",
    ):
        assert field in details, f"signal event missing observability field {field}"


def test_signal_events_have_no_pricing_source_or_version(tracker):
    """The convention §1 carve-out: signal events have NO pricing fields."""
    t = _emit_and_finalize_gpu_task(tracker)
    sig_events = [e for e in tracker.storage.query_events(task_id=str(t.task_id))
                   if e.event_type == "gpu_utilization_signal"]
    for sig in sig_events:
        assert sig.pricing_source is None, (
            "signal events MUST NOT carry a pricing_source per Decision #3"
        )
        assert sig.pricing_version is None
