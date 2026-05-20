"""Decisions #9 + #10 — idle compute is invisible to dexcost. THE GAP IS THE DESIGN.

These tests fail fast if a future refactor ever adds synthetic "idle
pseudo-tasks" or otherwise pushes dexcost_compute_total toward the cloud
invoice on long-running runtimes. The under-attribution is the customer-
facing signal for "unaccounted capacity"; surfacing it as a feature
(README, dashboard, marketing) is mandatory per the decisions log.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from dexcost import cloud_detect
from dexcost.cgroup_reader import CpuMax, CpuStat
from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv(
            "aws", "us-east-1", "imds", instance_type="c7g.xlarge",
        ),
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    return CostTracker(storage=storage, auto_instrument=[])


def _run_task(tracker, monkeypatch, *, start_offset_s, duration_s,
              cpu_used_seconds, runtime=RuntimeKind.EC2):
    started = datetime.now(timezone.utc) + timedelta(seconds=start_offset_s)
    t = Task(task_id=uuid.uuid4(), task_type="x", started_at=started)
    t.ended_at = started + timedelta(seconds=duration_s)
    tracker.storage.insert_task(t)
    accountant = ComputeAccountant(
        runtime=runtime, region="us-east-1", architecture="x86_64",
    )
    with patch("dexcost.compute_accountant.read_cpu_stat",
               return_value=CpuStat(usage_usec=0)):
        accountant.snapshot_start()
    t._compute = accountant
    with patch("dexcost.compute_accountant.read_cpu_stat",
               return_value=CpuStat(usage_usec=int(cpu_used_seconds * 1_000_000))), \
         patch("dexcost.compute_accountant.read_cpu_max",
               return_value=CpuMax(quota_us=400000, period_us=100000,
                                   vcpu_count=4.0)), \
         patch("dexcost.compute_accountant.read_memory_peak",
               return_value=512 * 1024 * 1024), \
         patch("dexcost.compute_accountant.read_memory_max",
               return_value=8 * 1024 * 1024 * 1024):
        tracker._aggregate_costs(t)
    return t.compute_cost_usd


def test_ec2_idle_between_tasks_is_invisible_decision_9(tracker, monkeypatch):
    """Two 60s tasks with 600s idle between them on a 4 vCPU @ $0.1450/hr
    c7g.xlarge.

    The cloud bill for the FULL 720s window = 720/3600 * 0.1450 = $0.029.
    dexcost MUST report STRICTLY LESS than that — the 600s idle gap is
    excluded by design (Decision #9). The gap IS the customer's
    "unaccounted capacity" signal."""
    cost_a = _run_task(
        tracker, monkeypatch,
        start_offset_s=0, duration_s=60, cpu_used_seconds=10,
    )
    cost_b = _run_task(
        tracker, monkeypatch,
        start_offset_s=660, duration_s=60, cpu_used_seconds=10,
    )
    total = cost_a + cost_b

    full_window_cloud_share = (
        Decimal("720") / Decimal("3600")
    ) * Decimal("0.1450")
    assert total < full_window_cloud_share, (
        f"dexcost total {total} must be < cloud share "
        f"{full_window_cloud_share} on long-running runtimes — the 600s "
        f"idle gap is by design (Decision #9). If this test starts failing "
        f"because total grew, check whether a refactor added synthetic "
        f"idle pseudo-tasks."
    )
    assert total > Decimal("0"), "we DO bill the 120s of dexcost-covered time"


def test_fargate_container_idle_tail_is_invisible_decision_10(tracker, monkeypatch):
    """3 Fargate tasks back-to-back, then 50 minutes of container idle tail
    before container shutdown. The tail is billable Fargate time NOT
    attributed to any dexcost task — Decision #10 keeps it invisible for
    consistency with Decision #9 (the reconciliation surface explains both)."""
    cost_a = _run_task(
        tracker, monkeypatch,
        start_offset_s=0, duration_s=10, cpu_used_seconds=2,
        runtime=RuntimeKind.FARGATE,
    )
    cost_b = _run_task(
        tracker, monkeypatch,
        start_offset_s=10, duration_s=10, cpu_used_seconds=2,
        runtime=RuntimeKind.FARGATE,
    )
    cost_c = _run_task(
        tracker, monkeypatch,
        start_offset_s=20, duration_s=10, cpu_used_seconds=2,
        runtime=RuntimeKind.FARGATE,
    )
    total = cost_a + cost_b + cost_c

    # Total container lifetime = 30s tasks + 3000s idle tail = 3030s.
    # Fargate rate at 4 vCPU x86_64 us-east-1 = 4 * 0.0000111111 + memory.
    # Just assert dexcost total < total-container-time × rate, which is
    # trivially true because we never billed the 3000s idle tail.
    container_lifetime_s = Decimal("3030")
    full_window_cloud_share = (
        Decimal("4.0") * container_lifetime_s * Decimal("0.0000111111")
    )
    assert total < full_window_cloud_share, (
        f"dexcost total {total} must be < full-container-lifetime cost "
        f"{full_window_cloud_share} (Decision #10). The 50-minute idle "
        f"tail is invisible by design."
    )
    assert total > Decimal("0")
