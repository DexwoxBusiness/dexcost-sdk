"""End-to-end: long-running EC2 GPU task emits dual events + back-fills cost.

Mirrors the Phase 1 compute auto-emission integration test. Pins:
- gpu_cost event with cost_pending=true at emission, back-filled at finalize
- gpu_utilization_signal events (one per device) emit with cost_usd=0 AND
  STAY at cost_usd=0 after back-fill (Decision #3 observability carve-out
  — NEVER aggregated into task.gpu_cost_usd)
- task.gpu_cost_usd == sum(gpu_cost events' cost_usd) — signals excluded
- task.total_cost_usd == llm + external + compute + network + gpu
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


def _stub_nvml_and_cgroup(monkeypatch):
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


def test_ec2_gpu_task_emits_dual_events_and_back_fills_cost(tracker, monkeypatch):
    """Long-running EC2 p5: gpu_cost back-filled; gpu_utilization_signal stays cost_usd=0."""
    _stub_nvml_and_cgroup(monkeypatch)

    # 30 GPU-seconds across 8 GPUs in a 60-second window.
    snapshots = [
        {},
        {os.getpid(): [UtilSample(pid=os.getpid(), sm_util=50, mem_util=30,
                                  time_stamp=30_000_000)]},
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )

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

    events = tracker.storage.query_events(task_id=str(t.task_id))
    cost_events = [e for e in events if e.event_type == "gpu_cost"]
    sig_events = [e for e in events if e.event_type == "gpu_utilization_signal"]

    # Exactly one gpu_cost event, with back-filled cost > 0.
    assert len(cost_events) == 1
    ev = cost_events[0]
    assert ev.cost_usd > Decimal("0")
    assert ev.pricing_source.startswith("gpu_catalog:aws:ec2_gpu:")
    assert ev.cost_confidence == "computed"
    assert ev.pricing_version.startswith("gpu:")
    assert "cost_pending" not in (ev.details or {})

    # gpu_utilization_signal events stay at cost_usd=0 — NEVER back-filled.
    assert len(sig_events) >= 1
    for sig in sig_events:
        assert sig.cost_usd == Decimal("0")

    # task.gpu_cost_usd equals the gpu_cost event sum — signals excluded.
    assert t.gpu_cost_usd == ev.cost_usd

    # total_cost_usd includes gpu portion.
    expected_total = (
        t.llm_cost_usd + t.external_cost_usd + t.compute_cost_usd
        + t.network_cost_usd + t.gpu_cost_usd
    )
    assert t.total_cost_usd == expected_total


def test_unknown_gpu_runtime_emits_no_event(tracker, monkeypatch):
    """Task without _gpu accountant produces zero GPU events."""
    monkeypatch.setattr(
        cloud_detect, "_result", CloudEnv(None, None, "none"),
    )
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    t.ended_at = t.started_at + timedelta(seconds=10)
    tracker.storage.insert_task(t)
    # NO accountant assigned → no event emitted.
    tracker._aggregate_costs(t)

    events = tracker.storage.query_events(task_id=str(t.task_id))
    gpu_events = [e for e in events
                  if e.event_type in ("gpu_cost", "gpu_utilization_signal")]
    assert len(gpu_events) == 0
    assert t.gpu_cost_usd == Decimal("0")


def test_signal_events_never_aggregated_into_gpu_cost_usd(tracker, monkeypatch):
    """Decision #3 contract: gpu_utilization_signal cost_usd never bumps task total.

    Load-bearing test for the convention §1 carve-out. If a future refactor
    accidentally aggregates signal events into the cost rollup, this fails.
    """
    _stub_nvml_and_cgroup(monkeypatch)
    snapshots = [
        {},
        {os.getpid(): [UtilSample(pid=os.getpid(), sm_util=50, mem_util=30,
                                  time_stamp=10_000_000)]},
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )

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

    events = tracker.storage.query_events(task_id=str(t.task_id))
    gpu_cost_sum = sum(
        (e.cost_usd for e in events if e.event_type == "gpu_cost"),
        Decimal("0"),
    )
    signal_count = sum(
        1 for e in events if e.event_type == "gpu_utilization_signal"
    )
    assert signal_count >= 1
    # The convention §1 carve-out: task.gpu_cost_usd is the sum of
    # gpu_cost events ONLY; signals contribute zero.
    assert t.gpu_cost_usd == gpu_cost_sum, (
        "Decision #3 convention carve-out violated: gpu_utilization_signal "
        "events must NOT contribute to task.gpu_cost_usd"
    )
