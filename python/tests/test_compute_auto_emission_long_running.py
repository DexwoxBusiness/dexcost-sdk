"""End-to-end: long-running EC2 task auto-emits a compute_cost event with
cost_pending=true at task finalize, then the pricing engine back-fills it.

Pins the v1+v2 deferred-cost contract for the compute layer (analog of the
network v2 §6.4 pattern)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dexcost import cloud_detect
from dexcost.cgroup_reader import CpuMax, CpuStat
from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def test_ec2_task_emits_and_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv(
            "aws", "us-east-1", "imds", instance_type="c7g.xlarge",
        ),
    )
    # Strip env vars that would cause CostTracker auto-instrument to fail.
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    tracker = CostTracker(storage=storage, auto_instrument=[])

    started = datetime.now(timezone.utc) - timedelta(seconds=60)
    t = Task(task_id=uuid.uuid4(), task_type="x", started_at=started)
    t.ended_at = started + timedelta(seconds=60)
    storage.insert_task(t)

    accountant = ComputeAccountant(
        runtime=RuntimeKind.EC2, region="us-east-1", architecture="x86_64",
    )
    # Mock cgroup reads for snapshot_start (called immediately).
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_stat",
        lambda: CpuStat(usage_usec=0),
    )
    accountant.snapshot_start()
    t._compute = accountant

    # Now mock the end-snapshot reads for snapshot_end_and_build inside
    # _aggregate_costs: 1_000_000 usec = 1 vCPU-second used.
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_stat",
        lambda: CpuStat(usage_usec=1_000_000),
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_max",
        lambda: CpuMax(quota_us=400000, period_us=100000, vcpu_count=4.0),
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_memory_peak",
        lambda: 512 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_memory_max",
        lambda: 8 * 1024 * 1024 * 1024,
    )

    tracker._aggregate_costs(t)

    events = storage.query_events(task_id=str(t.task_id))
    compute_events = [e for e in events if e.event_type == "compute_cost"]
    assert len(compute_events) == 1
    ev = compute_events[0]
    assert ev.cost_usd > Decimal("0")
    assert ev.pricing_source.startswith("compute_catalog:aws:ec2:")
    assert ev.cost_confidence == "computed"
    assert "cost_pending" not in (ev.details or {})
    assert t.compute_cost_usd == ev.cost_usd


def test_unknown_runtime_emits_no_event(tmp_path, monkeypatch):
    """A task without a _compute accountant produces zero compute events."""
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv(None, None, "none"),
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    tracker = CostTracker(storage=storage, auto_instrument=[])

    t = Task(
        task_id=uuid.uuid4(), task_type="x",
        started_at=datetime.now(timezone.utc),
    )
    t.ended_at = t.started_at + timedelta(seconds=10)
    storage.insert_task(t)
    # NO accountant assigned → no event emitted.
    tracker._aggregate_costs(t)

    events = storage.query_events(task_id=str(t.task_id))
    compute = [e for e in events if e.event_type == "compute_cost"]
    assert len(compute) == 0
    assert t.compute_cost_usd == Decimal("0")
