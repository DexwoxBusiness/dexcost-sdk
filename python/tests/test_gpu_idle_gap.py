"""Decision #6 (Phase 2) — idle GPU is invisible to dexcost. THE GAP IS THE DESIGN.

The 380× CPU magnitude makes this test load-bearing. A future refactor that
adds synthetic idle pseudo-events (to make dexcost totals match the cloud
invoice) would fail this test fast.
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
        CloudEnv("lambda_labs", None, "dmi"),
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    return CostTracker(storage=storage, auto_instrument=[])


def _stub_nvml_and_cgroup(monkeypatch, gpu_seconds: float):
    """Set up NVML mocks for a task that used `gpu_seconds` seconds of GPU."""
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
        {os.getpid(): UtilSample(pid=os.getpid(), sm_util=50, mem_util=30,
                                  time_stamp=int(gpu_seconds * 1_000_000))},
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )


def _run_task(tracker, monkeypatch, start_offset_s, duration_s, gpu_seconds):
    """Run one synthetic Lambda Labs H100 task. Returns the task with cost
    populated."""
    _stub_nvml_and_cgroup(monkeypatch, gpu_seconds=gpu_seconds)
    started = datetime.now(timezone.utc) + timedelta(seconds=start_offset_s)
    t = Task(task_id=uuid.uuid4(), task_type="x", started_at=started)
    t.ended_at = started + timedelta(seconds=duration_s)
    tracker.storage.insert_task(t)
    accountant = GpuAccountant(
        GpuRuntimeKind.LAMBDA_LABS,
        CloudEnv("lambda_labs", None, "dmi"),
    )
    accountant.snapshot_start()
    t._gpu = accountant
    tracker._aggregate_costs(t)
    return t


def test_lambda_h100_idle_between_tasks_is_invisible(tracker, monkeypatch):
    """Two short Lambda Labs H100 tasks separated by 50 minutes of idle.

    Decision #6 — dexcost total MUST be strictly less than the full
    container-lifetime cloud share. The 50-minute idle gap is by design;
    if a future refactor adds synthetic idle pseudo-events to close the
    gap, this assertion fails immediately and points to the decision.
    """
    # 1 GPU-second across 60s window — task A
    cost_a = _run_task(tracker, monkeypatch,
                       start_offset_s=0, duration_s=60, gpu_seconds=1.0)
    # 1 GPU-second across 60s window — task B (50 minutes later)
    cost_b = _run_task(tracker, monkeypatch,
                       start_offset_s=3060, duration_s=60, gpu_seconds=1.0)

    total = cost_a.gpu_cost_usd + cost_b.gpu_cost_usd
    # Full container lifetime: 60s task A + 3000s idle + 60s task B = 3120s.
    # At Lambda Labs H100 SXM 8x ($3.99/GPU-hour, 1 GPU touched) the upper
    # bound on cloud spend across the window is:
    full_window_cloud_share = (
        Decimal("3120") / Decimal("3600") * Decimal("3.99")
    )
    assert total < full_window_cloud_share, (
        f"Decision #6 VIOLATED: dexcost gpu total {total} must be < cloud "
        f"share {full_window_cloud_share} on long-running GPU runtimes. "
        f"The 3000-second (50-minute) idle gap is by design — if this "
        f"test starts failing because total grew, check whether a refactor "
        f"added synthetic idle pseudo-events. The 380× CPU magnitude makes "
        f"this signal load-bearing for customer trust."
    )
    # And both tasks DID consume actual GPU time → totals should still be > 0.
    assert total > Decimal("0")
