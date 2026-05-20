"""cgroup v2 file parsing — cpu.stat / cpu.max / memory.peak / memory.max."""

from __future__ import annotations

from pathlib import Path

from dexcost.cgroup_reader import (
    CpuMax, CpuStat,
    read_cpu_max, read_cpu_stat,
    read_memory_current, read_memory_max, read_memory_peak,
)


def _seed_cgroup_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    return tmp_path


def test_read_cpu_stat_parses_usage_usec(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {
        "cpu.stat":
            "usage_usec 12345\n"
            "user_usec 6000\n"
            "system_usec 6345\n"
            "nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n",
    })
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    s = read_cpu_stat()
    assert isinstance(s, CpuStat)
    assert s.usage_usec == 12345


def test_read_cpu_max_with_quota(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {"cpu.max": "100000 100000\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    m = read_cpu_max()
    assert m == CpuMax(quota_us=100000, period_us=100000, vcpu_count=1.0)


def test_read_cpu_max_quota_fraction(tmp_path, monkeypatch):
    # 256 shares / 1024 = 0.25 vCPU (a small Fargate task).
    root = _seed_cgroup_dir(tmp_path, {"cpu.max": "25000 100000\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    m = read_cpu_max()
    assert m is not None
    assert m.vcpu_count == 0.25


def test_read_cpu_max_unlimited(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {"cpu.max": "max 100000\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    m = read_cpu_max()
    assert m is not None
    # Falls back to nproc — assertion is "not None and > 0".
    assert m.quota_us is None
    assert m.vcpu_count > 0


def test_read_memory_peak(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {"memory.peak": "2147483648\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    assert read_memory_peak() == 2147483648


def test_read_memory_max_finite(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {"memory.max": "1073741824\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    assert read_memory_max() == 1073741824


def test_read_memory_max_unlimited(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {"memory.max": "max\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    assert read_memory_max() is None


def test_read_memory_current(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {"memory.current": "1024\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    assert read_memory_current() == 1024


def test_missing_files_return_none(tmp_path, monkeypatch):
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", tmp_path)
    assert read_cpu_stat() is None
    assert read_cpu_max() is None
    assert read_memory_peak() is None
    assert read_memory_max() is None
    assert read_memory_current() is None


def test_malformed_cpu_stat_returns_none(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {"cpu.stat": "garbage\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    assert read_cpu_stat() is None


def test_malformed_cpu_max_returns_none(tmp_path, monkeypatch):
    root = _seed_cgroup_dir(tmp_path, {"cpu.max": "only-one-token\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    assert read_cpu_max() is None


def test_memory_peak_absent_when_kernel_too_old(tmp_path, monkeypatch):
    """Kernel < 5.19 — memory.peak file absent; memory.current present.

    The reader does NOT fabricate a peak from current — the caller decides
    the fallback (capture spec §6 case 6).
    """
    root = _seed_cgroup_dir(tmp_path, {"memory.current": "1024\n"})
    monkeypatch.setattr("dexcost.cgroup_reader._CGROUP_ROOT", root)
    assert read_memory_peak() is None
    assert read_memory_current() == 1024
