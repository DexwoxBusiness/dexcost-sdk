"""B2 regression — Sprint 2 Theme C / plan §3.1.1.

The Python GPU accountant currently treats NVML's per-PID `timeStamp`
as if it were "accumulated SM-microseconds" and uses `max_ts - base_ts`
as the per-device SM-seconds figure. In reality `timeStamp` is the
wall-clock microseconds since the NVML epoch — so the accountant
reports wall time × N processes as SM time, badly inflating cost on
underutilized GPUs and silently mis-attributing it on shared ones.

The canonical fix per the plan: integrate sampled utilization properly
across the sample sequence NVML returns:

    sm_seconds = Σ over samples (sm_util[i] / 100) × dt[i]

This test pins the corrected semantics with a deterministic mock.
"""

from __future__ import annotations

import os
from unittest.mock import patch  # noqa: F401 — present for parity with existing tests

import pytest

from dexcost.cgroup_walker import CgroupScope
from dexcost.cloud_detect import CloudEnv
from dexcost.gpu_runtime import GpuRuntimeKind
from dexcost.nvml_reader import MemInfo, UtilSample


def _patch_nvml_basics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub everything NVML except get_process_utilization (the test mocks that)."""
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: True)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_count", lambda: 1)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_handle", lambda i: f"h{i}")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_product_name",
                        lambda h: "nvidia h100 80gb hbm3")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_mig_mode", lambda h: False)
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_memory_info",
        lambda h: MemInfo(used_bytes=21474836480, total_bytes=85899345920),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )


def test_gpu_seconds_used_is_integrated_sm_time_not_wall_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2 / §3.1.1 — single-PID, two samples at different utilization.

    PID running 60 seconds wall-clock with NVML samples:
      - at t=20s: sm_util=80%  → covers 0..20s window
      - at t=60s: sm_util=40%  → covers 20..60s window

    Canonical sm_seconds = (80/100)×20 + (40/100)×40 = 16 + 16 = 32.0

    Pre-fix the accountant would compute max_ts - base_ts = 60s and
    report `gpu_seconds_used = 60.0` regardless of sm_util.
    """
    from dexcost.gpu_accountant import GpuAccountant

    _patch_nvml_basics(monkeypatch)
    pid = os.getpid()
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [pid],
    )

    # Baseline call returns empty (no samples yet). End call returns the
    # two-sample sequence for the PID. The new contract: list per PID.
    snapshots = [
        {},
        {
            pid: [
                UtilSample(pid=pid, sm_util=80, mem_util=10, time_stamp=20_000_000),
                UtilSample(pid=pid, sm_util=40, mem_util=10, time_stamp=60_000_000),
            ],
        },
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )

    acc = GpuAccountant(GpuRuntimeKind.MODAL, CloudEnv("modal", None, "env"))
    acc.snapshot_start()
    cost_details, _ = acc.snapshot_end_and_build(duration_ms=60_000)

    assert cost_details is not None
    assert cost_details["gpu_seconds_used"] == pytest.approx(32.0, abs=0.01), (
        f"expected integrated sm_seconds=32.0, got "
        f"{cost_details['gpu_seconds_used']} — accountant likely still using "
        f"wall_dt instead of Σ sm_util×dt"
    )


def test_gpu_seconds_used_two_pids_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2 / §3.1.1 — the plan's canonical example: two PIDs, serial windows.

      - PID A active 0..30s at 75% SM
      - PID B active 30..60s at 50% SM

    Per-PID integration:
      A: (75/100) × 30 = 22.5
      B: (50/100) × 30 = 15.0
    Total = 37.5
    """
    from dexcost.gpu_accountant import GpuAccountant

    _patch_nvml_basics(monkeypatch)
    pid_a, pid_b = 4001, 4002
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [pid_a, pid_b],
    )

    # Baseline: only PID A is running (timestamp=0 means "since epoch").
    # End: NVML returns A's last sample at 30s, B's first observation at
    # 30s + final at 60s. The integrator must use B's first-observed
    # timestamp (30s), NOT 0, as B's start of contribution.
    snapshots = [
        {pid_a: [UtilSample(pid=pid_a, sm_util=75, mem_util=10, time_stamp=0)]},
        {
            pid_a: [UtilSample(pid=pid_a, sm_util=75, mem_util=10, time_stamp=30_000_000)],
            pid_b: [
                UtilSample(pid=pid_b, sm_util=50, mem_util=10, time_stamp=30_000_000),
                UtilSample(pid=pid_b, sm_util=50, mem_util=10, time_stamp=60_000_000),
            ],
        },
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )

    acc = GpuAccountant(GpuRuntimeKind.MODAL, CloudEnv("modal", None, "env"))
    acc.snapshot_start()
    cost_details, _ = acc.snapshot_end_and_build(duration_ms=60_000)

    assert cost_details is not None
    assert cost_details["gpu_seconds_used"] == pytest.approx(37.5, abs=0.1), (
        f"expected serial-PID sm_seconds=37.5, got "
        f"{cost_details['gpu_seconds_used']}"
    )


def test_sm_util_pct_is_window_averaged_from_sm_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2 / §3.1.1 — sm_util_pct signal must come from integrated sm_seconds.

    For the single-PID test above (32 sm-seconds / 60 wall seconds):
      sm_util_pct = 32 / 60 × 100 ≈ 53.33%

    Pre-fix the accountant reports 100% (because wall_dt was being used
    as sm_seconds, so the ratio always equalled 1.0).
    """
    from dexcost.gpu_accountant import GpuAccountant

    _patch_nvml_basics(monkeypatch)
    pid = os.getpid()
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [pid],
    )
    snapshots = [
        {},
        {
            pid: [
                UtilSample(pid=pid, sm_util=80, mem_util=10, time_stamp=20_000_000),
                UtilSample(pid=pid, sm_util=40, mem_util=10, time_stamp=60_000_000),
            ],
        },
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )

    acc = GpuAccountant(GpuRuntimeKind.MODAL, CloudEnv("modal", None, "env"))
    acc.snapshot_start()
    _, signal_events = acc.snapshot_end_and_build(duration_ms=60_000)

    assert signal_events is not None and len(signal_events) == 1
    sig = signal_events[0]
    assert sig["sm_util_pct"] == pytest.approx(53.33, abs=0.5), (
        f"expected window-averaged sm_util_pct≈53.33, got {sig['sm_util_pct']}"
    )
