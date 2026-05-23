"""ComputeAccountant — start/end cgroup snapshots, single event per task,
fail-silent. Capture §5.3: at most one compute_cost event per task per runtime."""

from __future__ import annotations

import pytest

from dexcost.cgroup_reader import CpuMax, CpuStat
from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind


def test_long_running_runtime_emits_one_event_with_diff(monkeypatch):
    """EC2 task: start snapshot at usage_usec=1M, end at 4M → 3 vcpu-seconds."""
    snapshots = iter([
        CpuStat(usage_usec=1_000_000),
        CpuStat(usage_usec=4_000_000),
    ])
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_stat", lambda: next(snapshots),
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_max",
        lambda: CpuMax(quota_us=100000, period_us=100000, vcpu_count=1.0),
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_memory_peak",
        lambda: 512 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_memory_max",
        lambda: 1024 * 1024 * 1024,
    )

    a = ComputeAccountant(runtime=RuntimeKind.EC2)
    a.snapshot_start()
    details = a.snapshot_end_and_build(duration_ms=60_000)

    assert details is not None
    assert details["billing_model"] == "ec2"
    assert details["vcpu_seconds_used"] == pytest.approx(3.0)
    assert details["memory_bytes_peak"] == 512 * 1024 * 1024
    assert details["memory_bytes_limit"] == 1024 * 1024 * 1024
    assert details["vcpu_count"] == 1.0
    assert details["cost_pending"] is True


def test_serverless_lambda_emits_invocation_event():
    a = ComputeAccountant(
        runtime=RuntimeKind.LAMBDA,
        lambda_memory_mb=512,
        architecture="x86_64",
        initialization_type="on-demand",
        region="us-east-1",
    )
    details = a.build_serverless_event(
        duration_ms=200, memory_bytes_peak=400 * 1024 * 1024,
    )
    assert details is not None
    assert details["billing_model"] == "lambda"
    assert details["duration_ms"] == 200
    assert details["invocation_count"] == 1
    # Lambda env var (AWS_LAMBDA_FUNCTION_MEMORY_SIZE) is decimal MB.
    assert details["memory_bytes_limit"] == 512 * 1_000_000
    assert details["architecture"] == "x86_64"
    assert details["initialization_type"] == "on-demand"
    assert details["region"] == "us-east-1"
    assert details["cost_pending"] is True


def test_second_call_per_task_no_ops():
    """Capture §5.3 — at most one event per task per runtime; second call no-ops."""
    a = ComputeAccountant(
        runtime=RuntimeKind.LAMBDA, lambda_memory_mb=128,
        architecture="x86_64",
    )
    first = a.build_serverless_event(duration_ms=10, memory_bytes_peak=0)
    second = a.build_serverless_event(duration_ms=20, memory_bytes_peak=0)
    assert first is not None
    assert second is None


def test_fargate_passes_explicit_vcpu_and_memory():
    """Fargate-specific: vcpu_count + memory_bytes_limit come from the ECS
    task metadata (FargateTaskMetadata), not the cgroup."""
    a = ComputeAccountant(
        runtime=RuntimeKind.FARGATE,
        fargate_vcpu=0.5,
        fargate_memory_mib=1024,
        architecture="arm64",
        region="us-east-1",
    )
    details = a.build_serverless_event(
        duration_ms=60_000, memory_bytes_peak=600 * 1024 * 1024,
    )
    assert details is not None
    assert details["billing_model"] == "fargate"
    assert details["vcpu_count"] == 0.5
    assert details["memory_bytes_limit"] == 1024 * 1024 * 1024
    assert details["architecture"] == "arm64"


def test_non_linux_fallback_emits_with_zero_vcpu_seconds(monkeypatch):
    """When cgroup files don't exist (macOS/Windows/cgroup-v1) the long-running
    snapshot returns vcpu_seconds_used=0 and falls back to os.cpu_count()."""
    monkeypatch.setattr("dexcost.compute_accountant.read_cpu_stat", lambda: None)
    monkeypatch.setattr("dexcost.compute_accountant.read_cpu_max", lambda: None)
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_peak", lambda: None)
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_max", lambda: None)
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_current", lambda: None)

    a = ComputeAccountant(runtime=RuntimeKind.EC2)
    a.snapshot_start()
    details = a.snapshot_end_and_build(duration_ms=60_000)
    assert details is not None
    assert details["vcpu_seconds_used"] == 0
    assert details["vcpu_count"] > 0  # nproc fallback


def test_memory_peak_falls_back_to_current_when_missing(monkeypatch):
    """capture spec §6 case 6 — kernel < 5.19, memory.peak absent, fall back
    to memory.current at task end (accountant decides the fallback, not the reader)."""
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_stat",
        lambda: CpuStat(usage_usec=0),
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_max",
        lambda: CpuMax(quota_us=100000, period_us=100000, vcpu_count=1.0),
    )
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_peak", lambda: None)
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_memory_current",
        lambda: 256 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_memory_max",
        lambda: 1024 * 1024 * 1024,
    )

    a = ComputeAccountant(runtime=RuntimeKind.EC2)
    a.snapshot_start()
    details = a.snapshot_end_and_build(duration_ms=60_000)
    assert details["memory_bytes_peak"] == 256 * 1024 * 1024


def test_architecture_auto_detected_from_os_uname():
    """When architecture isn't passed, it's detected from os.uname().machine."""
    a = ComputeAccountant(runtime=RuntimeKind.LAMBDA, lambda_memory_mb=128)
    assert a.architecture in {"x86_64", "arm64"}


def test_long_running_snapshot_freeze_after_finalize(monkeypatch):
    """Late snapshot_end_and_build calls no-op after first."""
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_stat",
        lambda: CpuStat(usage_usec=100),
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_max",
        lambda: CpuMax(quota_us=100000, period_us=100000, vcpu_count=1.0),
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_memory_peak", lambda: 0,
    )
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_memory_max", lambda: 0,
    )
    a = ComputeAccountant(runtime=RuntimeKind.EC2)
    a.snapshot_start()
    first = a.snapshot_end_and_build(duration_ms=1000)
    second = a.snapshot_end_and_build(duration_ms=2000)
    assert first is not None
    assert second is None
