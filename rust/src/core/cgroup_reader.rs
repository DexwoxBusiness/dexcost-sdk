//! Cgroup v2 file readers.
//!
//! Fail-silent contract (convention §9): every read returns `None` on missing
//! or malformed input. Non-Linux hosts, cgroup-v1 kernels, and containers
//! without a cgroup mount all silently return `None` — the caller decides the
//! fallback.
//!
//! Backed file layouts (all under `/sys/fs/cgroup/`):
//!
//! - `cpu.stat`     — multi-line; `usage_usec <N>` is cumulative CPU time
//!                    (microseconds). Read at task start+end for `vcpu_seconds_used`.
//! - `cpu.max`      — `<quota|"max"> <period>` (microseconds). `quota/period`
//!                    is the vCPU count; `"max"` means no limit (fall back to
//!                    `std::thread::available_parallelism()`).
//! - `memory.peak`  — single integer bytes (kernel >= 5.19).
//! - `memory.max`   — single integer bytes or `"max"` (unlimited).
//! - `memory.current` — single integer bytes at the moment of read.
//!
//! Mirrors `python/src/dexcost/cgroup_reader.py`.

use std::path::{Path, PathBuf};
use std::sync::{LazyLock, RwLock};

/// Cumulative CPU usage at the moment of read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CpuStat {
    pub usage_usec: u64,
}

/// CPU quota / period as enforced by the cgroup.
#[derive(Debug, Clone, PartialEq)]
pub struct CpuMax {
    pub quota_us: Option<u64>,
    pub period_us: u64,
    pub vcpu_count: f64,
}

static CGROUP_ROOT: LazyLock<RwLock<PathBuf>> =
    LazyLock::new(|| RwLock::new(PathBuf::from("/sys/fs/cgroup")));

fn root() -> PathBuf {
    // Lock-poison safe: a panic in a writer no longer crashes future readers
    // (Sprint 1 Theme B / §2.2.6). The path is plain data with no invariants
    // a mid-write panic could have broken.
    match CGROUP_ROOT.read() {
        Ok(g) => g.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

/// Test-only: override the cgroup root path. Use a `tempfile::TempDir`
/// per test to isolate file fixtures.
#[doc(hidden)]
pub fn set_cgroup_root_for_tests(path: &Path) {
    *CGROUP_ROOT.write().expect("CGROUP_ROOT rwlock poisoned") = path.to_path_buf();
}

/// Test-only: restore the production default.
#[doc(hidden)]
pub fn reset_cgroup_root_for_tests() {
    *CGROUP_ROOT.write().expect("CGROUP_ROOT rwlock poisoned") =
        PathBuf::from("/sys/fs/cgroup");
}

fn read_int(name: &str) -> Option<u64> {
    let p = root().join(name);
    let raw = std::fs::read_to_string(&p).ok()?;
    let trimmed = raw.trim();
    if trimmed == "max" {
        return None;
    }
    trimmed.parse::<u64>().ok()
}

/// `cpu.stat` — `usage_usec <N>` (microseconds of CPU time consumed).
pub fn read_cpu_stat() -> Option<CpuStat> {
    let p = root().join("cpu.stat");
    let raw = std::fs::read_to_string(&p).ok()?;
    for line in raw.lines() {
        if let Some(rest) = line.strip_prefix("usage_usec ") {
            let usage = rest.split_whitespace().next()?;
            return usage.parse::<u64>().ok().map(|usage_usec| CpuStat { usage_usec });
        }
    }
    None
}

/// `cpu.max` — `<quota|"max"> <period>` (microseconds).
pub fn read_cpu_max() -> Option<CpuMax> {
    let p = root().join("cpu.max");
    let raw = std::fs::read_to_string(&p).ok()?;
    let trimmed = raw.trim();
    let parts: Vec<&str> = trimmed.split_whitespace().collect();
    if parts.len() != 2 {
        return None;
    }
    let period_us = parts[1].parse::<u64>().ok()?;
    if period_us == 0 {
        return None;
    }
    if parts[0] == "max" {
        let host = std::thread::available_parallelism()
            .map(|n| n.get() as f64)
            .unwrap_or(1.0);
        return Some(CpuMax {
            quota_us: None,
            period_us,
            vcpu_count: host,
        });
    }
    let quota_us = parts[0].parse::<u64>().ok()?;
    let vcpu_count = (quota_us as f64) / (period_us as f64);
    Some(CpuMax {
        quota_us: Some(quota_us),
        period_us,
        vcpu_count,
    })
}

/// `memory.peak` — bytes (kernel >= 5.19). `None` if file absent.
pub fn read_memory_peak() -> Option<u64> {
    read_int("memory.peak")
}

/// `memory.max` — bytes. `None` if "max" (unlimited) or absent.
pub fn read_memory_max() -> Option<u64> {
    read_int("memory.max")
}

/// `memory.current` — bytes at the moment of read.
pub fn read_memory_current() -> Option<u64> {
    read_int("memory.current")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    static TEST_LOCK: LazyLock<StdMutex<()>> = LazyLock::new(|| StdMutex::new(()));

    fn lock() -> std::sync::MutexGuard<'static, ()> {
        match TEST_LOCK.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        }
    }

    fn write(tmp: &Path, name: &str, content: &str) {
        std::fs::write(tmp.join(name), content).expect("write fixture");
    }

    #[test]
    fn cpu_stat_returns_usage_usec() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "cpu.stat", "usage_usec 123456789\nuser_usec 100\nsystem_usec 23\n");
        set_cgroup_root_for_tests(t.path());
        let stat = read_cpu_stat().expect("cpu.stat parses");
        assert_eq!(stat.usage_usec, 123_456_789);
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn cpu_stat_missing_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        set_cgroup_root_for_tests(t.path());
        assert!(read_cpu_stat().is_none());
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn cpu_stat_malformed_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "cpu.stat", "user_usec 100\nsystem_usec 23\n");
        set_cgroup_root_for_tests(t.path());
        assert!(read_cpu_stat().is_none());
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn cpu_max_with_explicit_quota_yields_vcpu_count() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        // quota=200000 period=100000 → 2.0 vCPU
        write(t.path(), "cpu.max", "200000 100000\n");
        set_cgroup_root_for_tests(t.path());
        let m = read_cpu_max().expect("cpu.max parses");
        assert_eq!(m.quota_us, Some(200_000));
        assert_eq!(m.period_us, 100_000);
        assert!((m.vcpu_count - 2.0).abs() < 1e-9);
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn cpu_max_with_max_quota_falls_back_to_host_cpus() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "cpu.max", "max 100000\n");
        set_cgroup_root_for_tests(t.path());
        let m = read_cpu_max().expect("cpu.max parses");
        assert!(m.quota_us.is_none());
        assert_eq!(m.period_us, 100_000);
        assert!(m.vcpu_count >= 1.0, "host cpu count is at least 1");
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn cpu_max_zero_period_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "cpu.max", "100000 0\n");
        set_cgroup_root_for_tests(t.path());
        assert!(read_cpu_max().is_none());
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn cpu_max_malformed_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "cpu.max", "just one token\n");
        set_cgroup_root_for_tests(t.path());
        assert!(read_cpu_max().is_none());
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn memory_peak_returns_bytes() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "memory.peak", "1073741824\n");
        set_cgroup_root_for_tests(t.path());
        assert_eq!(read_memory_peak(), Some(1_073_741_824));
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn memory_peak_missing_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        set_cgroup_root_for_tests(t.path());
        assert!(read_memory_peak().is_none());
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn memory_max_with_max_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "memory.max", "max\n");
        set_cgroup_root_for_tests(t.path());
        assert!(read_memory_max().is_none());
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn memory_max_with_integer_returns_bytes() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "memory.max", "2147483648\n");
        set_cgroup_root_for_tests(t.path());
        assert_eq!(read_memory_max(), Some(2_147_483_648));
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn memory_current_returns_bytes() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "memory.current", "536870912\n");
        set_cgroup_root_for_tests(t.path());
        assert_eq!(read_memory_current(), Some(536_870_912));
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn malformed_integer_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "memory.current", "not-a-number\n");
        set_cgroup_root_for_tests(t.path());
        assert!(read_memory_current().is_none());
        reset_cgroup_root_for_tests();
    }
}
