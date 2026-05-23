"""Cgroup v2 file readers.

Fail-silent contract (convention §9): every read returns ``None`` on missing
or malformed input. Non-Linux hosts, cgroup-v1 kernels, and containers without
a cgroup mount all silently return ``None`` — the caller decides the fallback.

Backed file layouts (all under ``/sys/fs/cgroup/``):

- ``cpu.stat``    — multi-line; ``usage_usec <N>`` is the cumulative CPU
                    time consumed (microseconds). Read at task start + end
                    to compute ``vcpu_seconds_used`` for long-running runtimes.
- ``cpu.max``     — single line ``<quota|"max"> <period>`` (both in
                    microseconds). ``quota/period`` is the vCPU count
                    enforced on this cgroup; ``"max"`` means no limit
                    (fall back to ``os.cpu_count()``).
- ``memory.peak`` — single integer (bytes); the high-water mark since cgroup
                    creation. Available on kernels >= 5.19; absent otherwise.
- ``memory.max``  — single integer (bytes) or ``"max"`` (unlimited).
- ``memory.current`` — single integer (bytes); the current RSS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class CpuStat:
    """Cumulative CPU usage at the moment of read."""

    usage_usec: int


@dataclass(frozen=True)
class CpuMax:
    """CPU quota / period as enforced by the cgroup."""

    quota_us: int | None
    period_us: int
    vcpu_count: float


def _read_int(name: str) -> int | None:
    """Read a single-integer cgroup file; return ``None`` if absent / "max" /
    malformed."""
    try:
        raw = (_CGROUP_ROOT / name).read_text().strip()
    except OSError:
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def read_cpu_stat() -> CpuStat | None:
    """``cpu.stat`` — ``usage_usec <N>`` (microseconds of CPU time consumed)."""
    try:
        raw = (_CGROUP_ROOT / "cpu.stat").read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        if line.startswith("usage_usec "):
            try:
                return CpuStat(usage_usec=int(line.split()[1]))
            except (ValueError, IndexError):
                return None
    return None


def read_cpu_max() -> CpuMax | None:
    """``cpu.max`` — ``<quota|"max"> <period>`` (microseconds)."""
    try:
        raw = (_CGROUP_ROOT / "cpu.max").read_text().strip()
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 2:
        return None
    try:
        period_us = int(parts[1])
    except ValueError:
        return None
    if period_us <= 0:
        return None
    if parts[0] == "max":
        return CpuMax(
            quota_us=None,
            period_us=period_us,
            vcpu_count=float(os.cpu_count() or 1),
        )
    try:
        quota_us = int(parts[0])
    except ValueError:
        return None
    return CpuMax(
        quota_us=quota_us,
        period_us=period_us,
        vcpu_count=quota_us / period_us,
    )


def read_memory_peak() -> int | None:
    """``memory.peak`` — bytes (kernel >= 5.19). ``None`` if file absent."""
    return _read_int("memory.peak")


def read_memory_max() -> int | None:
    """``memory.max`` — bytes. ``None`` if "max" (unlimited) or absent."""
    return _read_int("memory.max")


def read_memory_current() -> int | None:
    """``memory.current`` — bytes at the moment of read."""
    return _read_int("memory.current")
