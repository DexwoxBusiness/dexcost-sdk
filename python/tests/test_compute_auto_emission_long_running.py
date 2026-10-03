"""End-to-end: long-running EC2 task auto-emits a compute_cost event with
cost_pending=true at task finalize, then the pricing engine back-fills it.

Pins the v1+v2 deferred-cost contract for the compute layer (analog of the
network v2 §6.4 pattern)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from dexcost import cloud_detect
from dexcost.cgroup_reader import CpuMax, CpuStat
from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind
from dexcost.context import _current_task
from dexcost.gpu_runtime import GpuRuntimeKind
from dexcost.instruments.runtime import wrap_runtime_handler
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


def test_invoice_ec2_replaces_local_compute_and_gpu_estimates(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_detect, "_result", cloud_detect.CloudEnv("aws", "us-east-1", "imds"))
    storage = SQLiteStorage(db_path=str(tmp_path / "invoice.db"))
    tracker = CostTracker(storage=storage, auto_instrument=[])
    task = Task(task_type="invoice-ec2")
    task.ended_at = task.started_at + timedelta(seconds=1)
    storage.insert_task(task)

    def unexpected_snapshot(**kwargs):
        raise AssertionError("Local instance money must not be added to its invoice allocation")

    task._compute = SimpleNamespace(
        runtime=RuntimeKind.EC2, snapshot_end_and_build=unexpected_snapshot
    )
    task._gpu = SimpleNamespace(
        runtime=GpuRuntimeKind.AWS_EC2_GPU, snapshot_end_and_build=unexpected_snapshot
    )
    ticks = iter([0, 1_000_000_000])
    monkeypatch.setattr("dexcost.instruments.runtime.time.monotonic_ns", lambda: next(ticks))
    wrapped = wrap_runtime_handler(lambda: 42, tracker, service_key="aws_ec2",
                                   billing_account_id="111111111111",
                                   resource_id="222222222222.us-east-1.i-1234567890abcdef0")
    token = _current_task.set(task)
    try:
        assert wrapped() == 42
    finally:
        _current_task.reset(token)
    tracker._finalize_compute(task)
    tracker._finalize_gpu(task)
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    assert events[0].provider == "aws_ec2"
    assert events[0].cost_confidence == "unknown"
    assert task.compute_cost_usd == task.gpu_cost_usd == Decimal("0")
    storage.close()
