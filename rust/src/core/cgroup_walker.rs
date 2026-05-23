//! Cgroup-scope classifier — Phase 2 Decision #1 implementation.
//!
//! Reads `/proc/self/cgroup` and classifies the cgroup scope by prefix
//! into one of:
//!
//! - `"container"` — kubepods.slice / docker / containerd / crio / etc.
//!   The dexcost-task's cgroup IS the right scope to walk; `cgroup.procs`
//!   enumerates exactly the container's PIDs.
//! - `"bare_metal_user_slice"` — `/user.slice/...` (systemd user session).
//!   Walking this would capture every PID in the SSH/login session, not
//!   just dexcost's task. Degrade to self-PID-only at `estimated`
//!   confidence with `pricing_source: ...:no_container_scope`.
//! - `"root_cgroup"` — `/` (privileged single-tenant host). Ambiguous;
//!   degrade to self-PID-only.
//! - `"cgroup_v1"` — multi-line file (multiple controllers).
//! - `"unknown"` — anything else.
//!
//! See the verification matrix at
//! `docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/`
//! for the empirical confirmation of this classification table.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

// ── Decision #1 prefix table (in classification priority order) ────────────

const CONTAINER_PREFIXES: &[&str] = &[
    "/kubepods.slice/",         // modern K8s with systemd cgroup driver
    "/kubepods/",               // legacy K8s with cgroupfs driver
    "/docker/",
    "/system.slice/docker-",
    "/containerd/",
    "/system.slice/containerd-",
    "/crio/",
    "/system.slice/crio-",
];

const BARE_METAL_PREFIXES: &[&str] = &["/user.slice/"];

// ── Test-overrideable paths ────────────────────────────────────────────────

fn proc_self_cgroup_override() -> &'static Mutex<Option<PathBuf>> {
    static SLOT: OnceLock<Mutex<Option<PathBuf>>> = OnceLock::new();
    SLOT.get_or_init(|| Mutex::new(None))
}

fn cgroup_root_override() -> &'static Mutex<Option<PathBuf>> {
    static SLOT: OnceLock<Mutex<Option<PathBuf>>> = OnceLock::new();
    SLOT.get_or_init(|| Mutex::new(None))
}

/// Test-only: override the path read for `/proc/self/cgroup`.
#[doc(hidden)]
pub fn set_proc_self_cgroup_for_tests(p: &Path) {
    let mut g = match proc_self_cgroup_override().lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    *g = Some(p.to_path_buf());
}

/// Test-only: clear the override.
#[doc(hidden)]
pub fn reset_proc_self_cgroup_for_tests() {
    let mut g = match proc_self_cgroup_override().lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    *g = None;
}

/// Test-only: override the cgroup root (default `/sys/fs/cgroup`).
#[doc(hidden)]
pub fn set_cgroup_root_for_tests(p: &Path) {
    let mut g = match cgroup_root_override().lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    *g = Some(p.to_path_buf());
}

/// Test-only: clear the cgroup root override.
#[doc(hidden)]
pub fn reset_cgroup_root_for_tests() {
    let mut g = match cgroup_root_override().lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    *g = None;
}

fn proc_self_cgroup_path() -> PathBuf {
    if let Some(p) = proc_self_cgroup_override().lock().ok().and_then(|g| g.clone()) {
        return p;
    }
    PathBuf::from("/proc/self/cgroup")
}

fn cgroup_root_path() -> PathBuf {
    if let Some(p) = cgroup_root_override().lock().ok().and_then(|g| g.clone()) {
        return p;
    }
    PathBuf::from("/sys/fs/cgroup")
}

// ── Log-once-per-failure-mode state ────────────────────────────────────────

fn warned_modes() -> &'static Mutex<HashSet<String>> {
    static SLOT: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    SLOT.get_or_init(|| Mutex::new(HashSet::new()))
}

fn warn_once(mode: &str, message: &str) {
    let mut set = match warned_modes().lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    if !set.insert(mode.to_string()) {
        return;
    }
    eprintln!("[dexcost][gpu] {}: {}", mode, message);
}

#[doc(hidden)]
pub fn reset_warning_state_for_tests() {
    let mut set = match warned_modes().lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    set.clear();
}

// ── Public types ───────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CgroupKind {
    Container,
    BareMetalUserSlice,
    RootCgroup,
    CgroupV1,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CgroupScope {
    pub kind: CgroupKind,
    /// Set only when `kind == Container` — the cgroup-v2 unified path.
    pub path: Option<String>,
}

// ── Classification ─────────────────────────────────────────────────────────

pub fn classify_scope() -> CgroupScope {
    let raw = match fs::read_to_string(proc_self_cgroup_path()) {
        Ok(s) => s,
        Err(_) => {
            return CgroupScope {
                kind: CgroupKind::Unknown,
                path: None,
            };
        }
    };
    classify_raw(&raw)
}

fn classify_raw(raw: &str) -> CgroupScope {
    let lines: Vec<&str> = raw
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect();
    if lines.is_empty() {
        return CgroupScope {
            kind: CgroupKind::Unknown,
            path: None,
        };
    }

    // cgroup v1 → multiple controller lines; v2 → single "0::/path"
    if lines.len() > 1 || !lines[0].starts_with("0::") {
        return CgroupScope {
            kind: CgroupKind::CgroupV1,
            path: None,
        };
    }

    let path = &lines[0][3..]; // strip "0::"
    if path.is_empty() || path == "/" {
        return CgroupScope {
            kind: CgroupKind::RootCgroup,
            path: None,
        };
    }

    for prefix in CONTAINER_PREFIXES {
        if path.starts_with(prefix) {
            return CgroupScope {
                kind: CgroupKind::Container,
                path: Some(path.to_string()),
            };
        }
    }

    for prefix in BARE_METAL_PREFIXES {
        if path.starts_with(prefix) {
            return CgroupScope {
                kind: CgroupKind::BareMetalUserSlice,
                path: None,
            };
        }
    }

    CgroupScope {
        kind: CgroupKind::Unknown,
        path: None,
    }
}

// ── PID enumeration ────────────────────────────────────────────────────────

/// Returns the PID set to attribute GPU usage to.
///
/// For Container scope: walks the resolved cgroup's `cgroup.procs`.
/// Returns `None` (not an empty list) on read failure — signals caller to
/// log-once `gpu_cgroup_walk_forbidden` and fall back.
///
/// For every non-container scope: returns `[self_pid]` (Rust takes the
/// PID as a parameter to keep tests deterministic). The Python sentinel
/// `None` for "no walk attempted" maps to `Some(vec![self_pid])` here —
/// we always return a concrete list when we know we shouldn't walk, and
/// `None` only when the walk was attempted and FAILED.
pub fn enumerate_pids(scope: &CgroupScope, self_pid: u32) -> Option<Vec<u32>> {
    if scope.kind != CgroupKind::Container {
        return Some(vec![self_pid]);
    }
    let path = match &scope.path {
        Some(p) => p,
        None => return Some(vec![self_pid]),
    };

    let mut procs_path = cgroup_root_path();
    // path begins with `/` — push as a relative join.
    procs_path.push(path.trim_start_matches('/'));
    procs_path.push("cgroup.procs");

    let raw = match fs::read_to_string(&procs_path) {
        Ok(s) => s,
        Err(e) => {
            warn_once(
                "gpu_cgroup_walk_forbidden",
                &format!(
                    "Could not read {} ({}); GpuAccountant will degrade to self-PID-only",
                    procs_path.display(),
                    e
                ),
            );
            return None;
        }
    };

    let mut out = Vec::new();
    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(pid) = line.parse::<u32>() {
            out.push(pid);
        }
    }
    Some(out)
}

// ── Decision #1 confidence labelling ───────────────────────────────────────

/// Returns the pricing_source suffix for this scope, or `None` if no
/// fallback label is needed (Container scope → full fidelity).
///
/// - Container → `None`
/// - BareMetalUserSlice / RootCgroup → `Some("no_container_scope")`
/// - CgroupV1 / Unknown → `Some("self_pid_only")`
pub fn fallback_label_for(scope: &CgroupScope) -> Option<&'static str> {
    match scope.kind {
        CgroupKind::Container => None,
        CgroupKind::BareMetalUserSlice | CgroupKind::RootCgroup => Some("no_container_scope"),
        CgroupKind::CgroupV1 | CgroupKind::Unknown => Some("self_pid_only"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{LazyLock, Mutex as StdMutex};

    static TEST_LOCK: LazyLock<StdMutex<()>> = LazyLock::new(|| StdMutex::new(()));

    fn lock() -> std::sync::MutexGuard<'static, ()> {
        match TEST_LOCK.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        }
    }

    #[test]
    fn classify_v2_kubepods_slice_is_container() {
        let s = classify_raw("0::/kubepods.slice/kubepods-burstable.slice/foo");
        assert_eq!(s.kind, CgroupKind::Container);
        assert_eq!(s.path.as_deref(), Some("/kubepods.slice/kubepods-burstable.slice/foo"));
    }

    #[test]
    fn classify_v2_kubepods_legacy_is_container() {
        let s = classify_raw("0::/kubepods/pod123/abc");
        assert_eq!(s.kind, CgroupKind::Container);
    }

    #[test]
    fn classify_v2_docker_is_container() {
        let s = classify_raw("0::/docker/abc123");
        assert_eq!(s.kind, CgroupKind::Container);
    }

    #[test]
    fn classify_v2_system_slice_docker_is_container() {
        let s = classify_raw("0::/system.slice/docker-abc.scope");
        assert_eq!(s.kind, CgroupKind::Container);
    }

    #[test]
    fn classify_v2_containerd_is_container() {
        assert_eq!(classify_raw("0::/containerd/abc").kind, CgroupKind::Container);
        assert_eq!(
            classify_raw("0::/system.slice/containerd-abc.scope").kind,
            CgroupKind::Container
        );
    }

    #[test]
    fn classify_v2_crio_is_container() {
        assert_eq!(classify_raw("0::/crio/abc").kind, CgroupKind::Container);
        assert_eq!(
            classify_raw("0::/system.slice/crio-abc.scope").kind,
            CgroupKind::Container
        );
    }

    #[test]
    fn classify_v2_user_slice_is_bare_metal() {
        let s = classify_raw("0::/user.slice/user-1000.slice/session.scope");
        assert_eq!(s.kind, CgroupKind::BareMetalUserSlice);
        assert!(s.path.is_none());
    }

    #[test]
    fn classify_v2_root_cgroup() {
        let s = classify_raw("0::/");
        assert_eq!(s.kind, CgroupKind::RootCgroup);
    }

    #[test]
    fn classify_v1_multi_line() {
        let raw = "12:devices:/docker/abc\n11:freezer:/docker/abc\n";
        let s = classify_raw(raw);
        assert_eq!(s.kind, CgroupKind::CgroupV1);
    }

    #[test]
    fn classify_unknown_path() {
        let s = classify_raw("0::/init.scope");
        assert_eq!(s.kind, CgroupKind::Unknown);
    }

    #[test]
    fn enumerate_pids_container_walks_cgroup_procs() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        let cgroup_path = "/kubepods.slice/pod-foo";
        let dir = t.path().join("kubepods.slice").join("pod-foo");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("cgroup.procs"), "100\n200\n300\n").unwrap();
        set_cgroup_root_for_tests(t.path());

        let scope = CgroupScope {
            kind: CgroupKind::Container,
            path: Some(cgroup_path.to_string()),
        };
        let pids = enumerate_pids(&scope, 42).expect("walk succeeds");
        assert_eq!(pids, vec![100, 200, 300]);

        reset_cgroup_root_for_tests();
    }

    #[test]
    fn enumerate_pids_container_read_denied_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        // Don't create the cgroup.procs file → read fails.
        set_cgroup_root_for_tests(t.path());

        let scope = CgroupScope {
            kind: CgroupKind::Container,
            path: Some("/kubepods.slice/pod-foo".to_string()),
        };
        let pids = enumerate_pids(&scope, 42);
        assert!(pids.is_none(), "container scope with denied read must return None");

        reset_cgroup_root_for_tests();
    }

    #[test]
    fn enumerate_pids_bare_metal_returns_self_pid_only() {
        let scope = CgroupScope {
            kind: CgroupKind::BareMetalUserSlice,
            path: None,
        };
        assert_eq!(enumerate_pids(&scope, 4242), Some(vec![4242]));
    }

    #[test]
    fn enumerate_pids_root_returns_self_pid_only() {
        let scope = CgroupScope {
            kind: CgroupKind::RootCgroup,
            path: None,
        };
        assert_eq!(enumerate_pids(&scope, 4242), Some(vec![4242]));
    }

    #[test]
    fn enumerate_pids_cgroup_v1_returns_self_pid_only() {
        let scope = CgroupScope {
            kind: CgroupKind::CgroupV1,
            path: None,
        };
        assert_eq!(enumerate_pids(&scope, 4242), Some(vec![4242]));
    }

    #[test]
    fn enumerate_pids_unknown_returns_self_pid_only() {
        let scope = CgroupScope {
            kind: CgroupKind::Unknown,
            path: None,
        };
        assert_eq!(enumerate_pids(&scope, 4242), Some(vec![4242]));
    }

    #[test]
    fn fallback_label_table() {
        assert_eq!(
            fallback_label_for(&CgroupScope {
                kind: CgroupKind::Container,
                path: Some("/x".into())
            }),
            None
        );
        assert_eq!(
            fallback_label_for(&CgroupScope {
                kind: CgroupKind::BareMetalUserSlice,
                path: None
            }),
            Some("no_container_scope")
        );
        assert_eq!(
            fallback_label_for(&CgroupScope {
                kind: CgroupKind::RootCgroup,
                path: None
            }),
            Some("no_container_scope")
        );
        assert_eq!(
            fallback_label_for(&CgroupScope {
                kind: CgroupKind::CgroupV1,
                path: None
            }),
            Some("self_pid_only")
        );
        assert_eq!(
            fallback_label_for(&CgroupScope {
                kind: CgroupKind::Unknown,
                path: None
            }),
            Some("self_pid_only")
        );
    }
}
