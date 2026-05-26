"""Compute math fixes — Sprint 2 Theme C / plan §3.1.3.

Two Python-side bugs:

- **Fix 1**: ``memory.peak`` (cgroup v2) is a monotonically-increasing
  high-water mark since the cgroup was created — NOT the per-task
  peak. Pre-fix the accountant emitted this raw value as
  ``memory_bytes_peak``, so back-to-back tasks in the same container
  reported the SAME peak (the second task's peak == the first task's).

- **Fix 2**: when the cgroup is remounted mid-task (CI/CD pipelines,
  container restarts), ``cpu.stat::usage_usec`` resets to 0 and the
  end-minus-start delta goes negative. The pre-fix code silently
  clamped to 0 and emitted ``vcpu_seconds_used=0`` with no signal to
  downstream pricing — turning a real workload into a $0 charge with
  ``cost_confidence`` still reading "computed".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind


def _build_stat(usage_usec: int):
    """Mimic the shape of CpuStat / MemoryPeak returns."""
    from dexcost.cgroup_reader import CpuStat
    return CpuStat(usage_usec=usage_usec)


def test_memory_peak_is_task_local_not_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix 1: memory_bytes_peak must subtract the cgroup peak observed
    at task start, so a long-lived container's prior workload doesn't
    inflate the current task's reported peak."""
    # Task 1 starts at lifetime-peak 200 MB, ends at 800 MB → reports 600 MB.
    # Task 2 starts at lifetime-peak 800 MB, ends at 800 MB → reports 0 MB
    #   (task 2 didn't push the peak higher).
    monkeypatch.setattr("dexcost.compute_accountant.read_cpu_stat",
                        lambda: _build_stat(0))
    monkeypatch.setattr("dexcost.compute_accountant.read_cpu_max", lambda: None)
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_max", lambda: 0)

    # First task: peak rises from 200 MB to 800 MB.
    sequence = iter([200_000_000, 800_000_000])
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_peak",
                        lambda: next(sequence))

    acc = ComputeAccountant(RuntimeKind.K8S_POD)
    acc.snapshot_start()  # reads 200_000_000 — store as task-start baseline
    event = acc.snapshot_end_and_build(duration_ms=60_000)

    assert event is not None
    delta = event["memory_bytes_peak"]
    assert delta == 600_000_000, (
        f"expected per-task memory_bytes_peak=600 MB (end 800 MB − start "
        f"200 MB), got {delta!r} — likely still emitting the raw "
        f"lifetime peak"
    )


def test_memory_peak_zero_when_no_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix 1 corollary: if the second task in a container doesn't push
    the peak higher, memory_bytes_peak must be 0 — not the inherited
    lifetime peak."""
    monkeypatch.setattr("dexcost.compute_accountant.read_cpu_stat",
                        lambda: _build_stat(0))
    monkeypatch.setattr("dexcost.compute_accountant.read_cpu_max", lambda: None)
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_max", lambda: 0)
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_peak",
                        lambda: 800_000_000)  # constant — no growth

    acc = ComputeAccountant(RuntimeKind.K8S_POD)
    acc.snapshot_start()
    event = acc.snapshot_end_and_build(duration_ms=60_000)
    assert event is not None
    assert event["memory_bytes_peak"] == 0


def test_vcpu_negative_delta_emits_estimated_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: when the cpu.stat counter resets between snapshot_start
    and the end read (cgroup remount / container restart mid-task),
    the negative delta must NOT silently zero. The accountant should:

      - Set vcpu_seconds_used = 0 (we genuinely don't know)
      - Set cost_confidence='estimated' in the details to signal the
        downstream pricer the value is untrustworthy.
    """
    # Start counter: 1_000_000_000 usec. End counter: 5_000_000 usec
    # (cgroup remount — counter reset). Delta is negative.
    start_then_end = iter([1_000_000_000, 5_000_000])
    monkeypatch.setattr(
        "dexcost.compute_accountant.read_cpu_stat",
        lambda: _build_stat(next(start_then_end)),
    )
    monkeypatch.setattr("dexcost.compute_accountant.read_cpu_max", lambda: None)
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_max", lambda: 0)
    monkeypatch.setattr("dexcost.compute_accountant.read_memory_peak", lambda: 0)

    acc = ComputeAccountant(RuntimeKind.K8S_POD)
    acc.snapshot_start()  # captures usage_usec = 1_000_000_000
    event = acc.snapshot_end_and_build(duration_ms=60_000)

    assert event is not None
    assert event["vcpu_seconds_used"] == 0.0
    assert event.get("cost_confidence") == "estimated", (
        f"expected cost_confidence='estimated' to signal counter reset, "
        f"got {event.get('cost_confidence')!r}"
    )
