"""Per-task GPU accountant — cgroup walk + NVML snapshot pair + dual-event emission.

Builds 1 gpu_cost event (with cost_pending=true for back-fill) + N
gpu_utilization_signal events (one per device, observability-only).

Window-averaged sm_util_pct (Decision #3 sharpening) — NOT a point sample
at finalize.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from dexcost.cloud_detect import CloudEnv
from dexcost.gpu_runtime import GpuRuntimeKind


@pytest.fixture
def accountant_factory():
    """Construct a GpuAccountant with patched NVML/cgroup primitives."""
    from dexcost.gpu_accountant import GpuAccountant
    return GpuAccountant


# ─── Modal serverless emission (per_gpu_second_active) ──────────────────────

def test_modal_emits_gpu_cost_and_one_signal(accountant_factory, monkeypatch):
    """Decision #3: 1 gpu_cost event + 1 gpu_utilization_signal per GPU."""
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
        lambda h: MemInfo(used_bytes=21474836480, total_bytes=85899345920),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [os.getpid()],
    )

    # Two snapshots: initial (baseline; samples=[]) and end (1234us of GPU time).
    snapshots = [
        {},  # initial: no samples yet
        {os.getpid(): [UtilSample(pid=os.getpid(), sm_util=80, mem_util=30, time_stamp=1_234_000)]},
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )

    acc = accountant_factory(GpuRuntimeKind.MODAL, CloudEnv("modal", None, "env"))
    acc.snapshot_start()
    cost_details, signal_events = acc.snapshot_end_and_build(duration_ms=1234)

    assert cost_details is not None
    assert cost_details["billing_model"] == "per_gpu_second_active"
    assert cost_details["gpu_vendor"] == "nvidia"
    assert cost_details["gpu_sku"] is not None  # alias resolution happened
    assert cost_details["gpu_count"] == 1
    assert cost_details["duration_ms"] == 1234
    assert cost_details["cost_pending"] is True
    assert cost_details["mig_profile"] is None

    assert signal_events is not None
    assert len(signal_events) == 1
    sig = signal_events[0]
    assert sig["gpu_index"] == 0
    assert sig["gpu_sku"] == cost_details["gpu_sku"]
    # sm_util_pct is window-averaged, not the point sample.
    assert sig["sm_util_pct"] is not None
    assert sig["vram_total_bytes"] == 85899345920


# ─── Idempotency: capture spec §5.3 invariant ───────────────────────────────

def test_second_call_per_task_returns_none(accountant_factory, monkeypatch):
    from dexcost.cgroup_walker import CgroupScope
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: True)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_count", lambda: 0)
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )
    acc = accountant_factory(GpuRuntimeKind.MODAL, CloudEnv("modal", None, "env"))
    acc.snapshot_start()
    first = acc.snapshot_end_and_build(duration_ms=1000)
    second = acc.snapshot_end_and_build(duration_ms=2000)
    assert second == (None, None)
    # First call may return (None, None) too since device_count=0, but it
    # MUST have set the frozen flag — second call is a hard no-op.


# ─── No NVML or no devices → no emission ────────────────────────────────────

def test_no_nvml_emits_nothing(accountant_factory, monkeypatch):
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: False)
    acc = accountant_factory(GpuRuntimeKind.MODAL, CloudEnv("modal", None, "env"))
    acc.snapshot_start()
    cost, sigs = acc.snapshot_end_and_build(duration_ms=1000)
    assert cost is None
    assert sigs is None


def test_zero_devices_emits_nothing(accountant_factory, monkeypatch):
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: True)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_count", lambda: 0)
    acc = accountant_factory(GpuRuntimeKind.AWS_EC2_GPU,
                              CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge"))
    acc.snapshot_start()
    cost, sigs = acc.snapshot_end_and_build(duration_ms=60_000)
    assert cost is None and sigs is None


# ─── Decision #1 fallback labels ────────────────────────────────────────────

def test_bare_metal_scope_sets_no_container_scope_fallback(accountant_factory, monkeypatch):
    """`/proc/self/cgroup = /user.slice/...` → degrade to self-PID-only + label."""
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
        lambda h: MemInfo(used_bytes=0, total_bytes=85899345920),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="bare_metal_user_slice", path=None),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [os.getpid()],
    )
    snapshots = [
        {},
        {os.getpid(): [UtilSample(pid=os.getpid(), sm_util=50, mem_util=20, time_stamp=500_000)]},
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )
    acc = accountant_factory(GpuRuntimeKind.AWS_EC2_GPU,
                              CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge"))
    acc.snapshot_start()
    cost, _ = acc.snapshot_end_and_build(duration_ms=1000)
    assert cost["_cgroup_scope_fallback"] == "no_container_scope"


def test_cgroup_walk_denied_sets_self_pid_only_fallback(accountant_factory, monkeypatch):
    """cgroup walk returns None → self-PID-only label."""
    from dexcost.cgroup_walker import CgroupScope
    from dexcost.nvml_reader import MemInfo
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: True)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_count", lambda: 1)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_handle", lambda i: f"h{i}")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_product_name",
                        lambda h: "nvidia h100 80gb hbm3")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_mig_mode", lambda h: False)
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_memory_info",
        lambda h: MemInfo(used_bytes=0, total_bytes=80 * 2**30),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: None,  # walk denied
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: {},
    )
    acc = accountant_factory(GpuRuntimeKind.AWS_EC2_GPU,
                              CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge"))
    acc.snapshot_start()
    cost, _ = acc.snapshot_end_and_build(duration_ms=1000)
    assert cost["_cgroup_scope_fallback"] == "self_pid_only"


# ─── Decision #2: MIG transparency ──────────────────────────────────────────

def test_mig_detected_emits_log_and_full_billing(accountant_factory, monkeypatch, caplog):
    import logging
    from dexcost.cgroup_walker import CgroupScope
    from dexcost.nvml_reader import MemInfo
    from dexcost.gpu_accountant import _reset_warning_state

    _reset_warning_state()

    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: True)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_count", lambda: 1)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_handle", lambda i: f"h{i}")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_product_name",
                        lambda h: "nvidia a100 80gb")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_mig_mode", lambda h: True)
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_memory_info",
        lambda h: MemInfo(used_bytes=0, total_bytes=80 * 2**30),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [os.getpid()],
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: {},
    )

    with caplog.at_level(logging.WARNING):
        acc = accountant_factory(GpuRuntimeKind.AWS_EC2_GPU,
                                  CloudEnv("aws", "us-east-1", "imds",
                                            instance_type="p4d.24xlarge"))
        acc.snapshot_start()
        cost, _ = acc.snapshot_end_and_build(duration_ms=1000)

    # MIG was detected → details.mig_profile populated AND log-once fired.
    assert cost["mig_profile"] is not None  # not None (some profile string)
    msgs = [r.getMessage() for r in caplog.records
            if "mig" in r.getMessage().lower()]
    assert any("full_billing_applied" in m or "mig" in m.lower() for m in msgs)


# ─── Decision #3 sharpening: window-averaged sm_util_pct ────────────────────

def test_sm_util_pct_is_window_averaged_not_point_sample(accountant_factory, monkeypatch):
    """Task that ran at high util then quieted at end → NOT 0% at finalize."""
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
        lambda h: MemInfo(used_bytes=0, total_bytes=80 * 2**30),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [os.getpid()],
    )

    # Simulate 5 seconds total: 4s @ 80%, 1s @ 0%.
    # B2 (Sprint 2 Theme C / §3.1.1): integration is Σ sm_util × dt, so
    # the mock now exposes two samples covering each utilization window.
    # First sample @ t=4s, sm=80%: covers 0..4s → 0.8 × 4 = 3.2 sm-sec.
    # Second sample @ t=5s, sm=0%:  covers 4..5s → 0   × 1 = 0   sm-sec.
    # Total gpu_seconds_used = 3.2; window-averaged sm_util_pct = 64%.
    snapshots = [
        {},  # baseline (no PIDs at start)
        {os.getpid(): [
            UtilSample(pid=os.getpid(), sm_util=80, mem_util=0, time_stamp=4_000_000),
            UtilSample(pid=os.getpid(), sm_util=0,  mem_util=0, time_stamp=5_000_000),
        ]},
    ]
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: snapshots.pop(0),
    )

    acc = accountant_factory(GpuRuntimeKind.MODAL, CloudEnv("modal", None, "env"))
    acc.snapshot_start()
    cost, sigs = acc.snapshot_end_and_build(duration_ms=5000)

    assert cost is not None
    # gpu_seconds_used should be ~3.2 (NOT 0 — the point sample at finalize
    # would have read sm_util=0 from the last sample).
    assert 3.0 <= cost["gpu_seconds_used"] <= 3.4
    # sm_util_pct window-averaged should be ~64% (NOT 0%).
    assert sigs[0]["sm_util_pct"] is not None
    assert 60.0 <= sigs[0]["sm_util_pct"] <= 70.0


# ─── Sub-100ms degenerate task: sm_util_pct = None ──────────────────────────

def test_sub_100ms_task_emits_null_sm_util(accountant_factory, monkeypatch):
    from dexcost.cgroup_walker import CgroupScope
    from dexcost.nvml_reader import MemInfo

    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: True)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_count", lambda: 1)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_handle", lambda i: f"h{i}")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_product_name",
                        lambda h: "nvidia h100 80gb hbm3")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_mig_mode", lambda h: False)
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_memory_info",
        lambda h: MemInfo(used_bytes=0, total_bytes=80 * 2**30),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [os.getpid()],
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization",
        lambda h, ts: {},
    )

    acc = accountant_factory(GpuRuntimeKind.MODAL, CloudEnv("modal", None, "env"))
    acc.snapshot_start()
    cost, sigs = acc.snapshot_end_and_build(duration_ms=0)
    # Sub-100ms / zero-duration task — sm_util_pct must be None, not div-by-zero.
    assert sigs[0]["sm_util_pct"] is None


# ─── Multi-device: N signal events ──────────────────────────────────────────

def test_multi_device_emits_one_signal_per_device(accountant_factory, monkeypatch):
    from dexcost.cgroup_walker import CgroupScope
    from dexcost.nvml_reader import MemInfo, UtilSample

    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.init_nvml", lambda: True)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_count", lambda: 4)
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_device_handle", lambda i: f"h{i}")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_product_name",
                        lambda h: "nvidia h100 80gb hbm3")
    monkeypatch.setattr("dexcost.gpu_accountant.nvml_reader.get_mig_mode", lambda h: False)
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_memory_info",
        lambda h: MemInfo(used_bytes=2**30, total_bytes=80 * 2**30),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.classify_scope",
        lambda: CgroupScope(kind="container", path="/docker/abc"),
    )
    monkeypatch.setattr(
        "dexcost.gpu_accountant.cgroup_walker.enumerate_pids",
        lambda scope: [os.getpid()],
    )

    # Each device returns its own utilization samples.
    call_count = {"n": 0}
    def fake_util(h, ts):
        call_count["n"] += 1
        if call_count["n"] <= 4:  # 4 baseline (per device)
            return {}
        return {os.getpid(): [UtilSample(pid=os.getpid(), sm_util=50, mem_util=20,
                                         time_stamp=500_000)]}
    monkeypatch.setattr(
        "dexcost.gpu_accountant.nvml_reader.get_process_utilization", fake_util,
    )

    acc = accountant_factory(GpuRuntimeKind.AWS_EC2_GPU,
                              CloudEnv("aws", "us-east-1", "imds",
                                        instance_type="p5.48xlarge"))
    acc.snapshot_start()
    cost, sigs = acc.snapshot_end_and_build(duration_ms=1000)

    assert cost is not None
    assert cost["gpu_count"] == 4
    assert len(sigs) == 4
    for i, sig in enumerate(sigs):
        assert sig["gpu_index"] == i
