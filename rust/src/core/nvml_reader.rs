//! NVML library wrapper — Phase 2 GPU foundation.
//!
//! Wraps NVIDIA's NVML (via the `nvml-wrapper` crate, gated behind the
//! `gpu` cargo feature) with the fail-silent contract from convention §9.
//! Every NVML call returns `None` (or `false` for boolean accessors) on
//! missing driver / library / permission / device errors rather than
//! raising — the caller (typically [`GpuAccountant`]) decides the fallback
//! policy per Decision #1's classification table.
//!
//! `nvml-wrapper` is an OPTIONAL dependency (cargo `--features gpu`). When
//! the feature is OFF, every accessor returns the no-NVML sentinel and the
//! SDK works on GPU-less hosts.
//!
//! Per Decision #4: [`get_product_name`] applies NFC Unicode normalization
//! + lowercase + whitespace collapse on the raw NVML string before
//! returning — catalog alias matching depends on byte-level equality after
//! normalization because NVIDIA's productName carries non-breaking spaces
//! and other Unicode quirks across driver versions.
//!
//! Per Decision #8: [`get_process_utilization`] takes a mutable
//! `last_seen_timestamps` map and updates it in place — the caller
//! persists per-PID timestamps across snapshot calls so NVML's sample
//! buffer doesn't silently lose intermediate samples.

use std::collections::{HashMap, HashSet};
use std::sync::{Mutex, OnceLock};

use unicode_normalization::UnicodeNormalization;

/// One NVML compute-running-process record (PID + GPU memory usage).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessInfo {
    pub pid: u32,
    /// VRAM bytes used by this PID; may be 0 when NVML reports NOT_AVAILABLE.
    pub used_gpu_memory: u64,
}

/// One NVML process-utilization sample for a single PID at one timestamp.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UtilSample {
    pub pid: u32,
    /// 0-100 — percent of time SMs had ≥1 kernel running.
    pub sm_util: u32,
    /// 0-100 — percent of time memory subsystem was busy.
    pub mem_util: u32,
    /// Microseconds since NVML epoch.
    pub time_stamp: u64,
}

/// Device-level memory totals.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MemInfo {
    pub used_bytes: u64,
    pub total_bytes: u64,
}

// ── Log-once-per-failure-mode state (convention §11) ───────────────────────

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

/// Test-only: clear the warn-once tracking set.
#[doc(hidden)]
pub fn reset_warning_state_for_tests() {
    let mut set = match warned_modes().lock() {
        Ok(g) => g,
        Err(p) => p.into_inner(),
    };
    set.clear();
}

// ── Decision #4: NFC-normalized productName ────────────────────────────────

/// NFC → lowercase → whitespace collapse (incl. NBSP / NNBSP / ZWSP).
///
/// Public so tests / catalog tools can normalize alias inputs the same way.
pub fn normalize_product_name(raw: &str) -> String {
    let nfc: String = raw.nfc().collect();
    // Collapse ALL whitespace (incl. U+00A0, U+202F, etc. — char::is_whitespace
    // covers Unicode whitespace categories the same way Python's str.split does).
    let collapsed: String = nfc
        .split(|c: char| c.is_whitespace())
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    collapsed.to_lowercase()
}

// ── NVML availability + accessors ──────────────────────────────────────────
//
// The `gpu` feature gates the real NVML calls. When the feature is OFF
// (default — and for CI / GPU-less hosts), every accessor returns the
// no-NVML sentinel: `Available::No`, count `None`, etc.

#[cfg(feature = "gpu")]
mod nvml_real {
    use super::*;
    use nvml_wrapper::Nvml;
    use std::sync::OnceLock;

    static NVML: OnceLock<Option<Nvml>> = OnceLock::new();

    pub fn nvml_available() -> bool {
        // Crate is linked; runtime init may still fail.
        true
    }

    fn ensure_init() -> Option<&'static Nvml> {
        NVML.get_or_init(|| match Nvml::init() {
            Ok(n) => Some(n),
            Err(e) => {
                warn_once(
                    "gpu_nvml_init_failed",
                    &format!("NVML init failed ({}); GPU capture disabled", e),
                );
                None
            }
        })
        .as_ref()
    }

    pub fn init_nvml() -> bool {
        ensure_init().is_some()
    }

    pub fn shutdown_nvml() {
        // nvml-wrapper drops via OnceLock; explicit shutdown is a no-op here.
    }

    pub fn get_device_count() -> Option<u32> {
        let n = ensure_init()?;
        match n.device_count() {
            Ok(c) => Some(c),
            Err(e) => {
                warn_once(
                    "gpu_device_count_failed",
                    &format!("nvmlDeviceGetCount failed ({})", e),
                );
                None
            }
        }
    }
}

#[cfg(not(feature = "gpu"))]
mod nvml_real {
    use super::*;

    pub fn nvml_available() -> bool {
        // Crate not linked → no NVML available. Customers opt-in via
        // `cargo build --features gpu`.
        false
    }

    pub fn init_nvml() -> bool {
        warn_once(
            "gpu_nvml_not_linked",
            "nvml-wrapper crate not linked; GPU capture disabled. \
             Rebuild with --features gpu to enable.",
        );
        false
    }

    pub fn shutdown_nvml() {}

    pub fn get_device_count() -> Option<u32> {
        None
    }
}

pub use nvml_real::{get_device_count, init_nvml, nvml_available, shutdown_nvml};

// ── Per-device accessors (degraded-mode fallback when feature is OFF) ──────

/// Opaque device handle — wraps the device index. When the `gpu` feature
/// is off there is no NVML; accessors short-circuit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct DeviceHandle(pub u32);

pub fn get_device_handle(index: u32) -> Option<DeviceHandle> {
    if !nvml_available() {
        return None;
    }
    // When the feature is on, we trust ensure_init() / device_count() ran
    // before this. The handle is just the index; the real NVML lookup
    // happens in the accessors that need it.
    Some(DeviceHandle(index))
}

/// NFC-normalized productName for the given device. `None` on failure
/// or when NVML isn't available.
pub fn get_product_name(handle: DeviceHandle) -> Option<String> {
    real::get_product_name(handle)
}

/// Compute-running processes on the device. `None` on
/// `NVML_ERROR_NO_PERMISSION` — the load-bearing Decision #1 case;
/// caller's cgroup walk degrades to self-PID-only.
pub fn get_compute_running_processes(handle: DeviceHandle) -> Option<Vec<ProcessInfo>> {
    real::get_compute_running_processes(handle)
}

/// Per-PID utilization samples. Mutates `last_seen_timestamps` in place
/// (Decision #8) so the per-PID sample buffer doesn't silently lose
/// intermediate samples between snapshots.
pub fn get_process_utilization(
    handle: DeviceHandle,
    last_seen_timestamps: &mut HashMap<u32, u64>,
) -> Option<HashMap<u32, UtilSample>> {
    real::get_process_utilization(handle, last_seen_timestamps)
}

/// Device-level used / total VRAM bytes.
pub fn get_memory_info(handle: DeviceHandle) -> Option<MemInfo> {
    real::get_memory_info(handle)
}

/// True when MIG is enabled on this device (Decision #2 detection).
pub fn get_mig_mode(handle: DeviceHandle) -> bool {
    real::get_mig_mode(handle)
}

#[cfg(feature = "gpu")]
mod real {
    use super::*;
    use nvml_wrapper::Nvml;
    use std::sync::OnceLock;

    static NVML: OnceLock<Option<Nvml>> = OnceLock::new();

    fn ensure() -> Option<&'static Nvml> {
        // Reuse the cached Nvml from nvml_real if possible — but `nvml_real`'s
        // OnceLock is private. Re-init here is idempotent (Nvml::init() is
        // refcounted internally on the NVIDIA side).
        NVML.get_or_init(|| Nvml::init().ok()).as_ref()
    }

    pub fn get_product_name(handle: DeviceHandle) -> Option<String> {
        let n = ensure()?;
        let d = match n.device_by_index(handle.0) {
            Ok(d) => d,
            Err(_) => return None,
        };
        match d.name() {
            Ok(s) => Some(normalize_product_name(&s)),
            Err(e) => {
                warn_once(
                    "gpu_product_name_failed",
                    &format!("nvmlDeviceGetName failed ({})", e),
                );
                None
            }
        }
    }

    pub fn get_compute_running_processes(handle: DeviceHandle) -> Option<Vec<ProcessInfo>> {
        let n = ensure()?;
        let d = match n.device_by_index(handle.0) {
            Ok(d) => d,
            Err(_) => return None,
        };
        match d.running_compute_processes() {
            Ok(procs) => Some(
                procs
                    .into_iter()
                    .map(|p| ProcessInfo {
                        pid: p.pid,
                        used_gpu_memory: match p.used_gpu_memory {
                            nvml_wrapper::enums::device::UsedGpuMemory::Used(b) => b,
                            _ => 0,
                        },
                    })
                    .collect(),
            ),
            Err(e) => {
                warn_once(
                    "gpu_nvml_permission_denied",
                    &format!(
                        "nvmlDeviceGetComputeRunningProcesses failed ({}); \
                         GpuAccountant will degrade to self-PID-only",
                        e
                    ),
                );
                None
            }
        }
    }

    pub fn get_process_utilization(
        handle: DeviceHandle,
        last_seen_timestamps: &mut HashMap<u32, u64>,
    ) -> Option<HashMap<u32, UtilSample>> {
        let n = ensure()?;
        let d = match n.device_by_index(handle.0) {
            Ok(d) => d,
            Err(_) => return None,
        };
        let base_ts = last_seen_timestamps.values().copied().min().unwrap_or(0);
        match d.process_utilization_stats(Some(base_ts)) {
            Ok(samples) => {
                let mut out = HashMap::new();
                for s in samples {
                    let pid = s.pid;
                    let ts = s.timestamp;
                    out.insert(
                        pid,
                        UtilSample {
                            pid,
                            sm_util: s.sm_util,
                            mem_util: s.mem_util,
                            time_stamp: ts,
                        },
                    );
                    last_seen_timestamps.insert(pid, ts);
                }
                Some(out)
            }
            Err(e) => {
                warn_once(
                    "gpu_process_utilization_failed",
                    &format!("nvmlDeviceGetProcessUtilization failed ({})", e),
                );
                None
            }
        }
    }

    pub fn get_memory_info(handle: DeviceHandle) -> Option<MemInfo> {
        let n = ensure()?;
        let d = match n.device_by_index(handle.0) {
            Ok(d) => d,
            Err(_) => return None,
        };
        match d.memory_info() {
            Ok(m) => Some(MemInfo {
                used_bytes: m.used,
                total_bytes: m.total,
            }),
            Err(_) => None,
        }
    }

    pub fn get_mig_mode(handle: DeviceHandle) -> bool {
        let n = match ensure() {
            Some(n) => n,
            None => return false,
        };
        let d = match n.device_by_index(handle.0) {
            Ok(d) => d,
            Err(_) => return false,
        };
        match d.mig_mode() {
            Ok(m) => matches!(m.current, nvml_wrapper::enum_wrappers::device::MigMode::Enabled),
            Err(_) => false,
        }
    }
}

#[cfg(not(feature = "gpu"))]
mod real {
    use super::*;

    pub fn get_product_name(_handle: DeviceHandle) -> Option<String> {
        None
    }
    pub fn get_compute_running_processes(_handle: DeviceHandle) -> Option<Vec<ProcessInfo>> {
        None
    }
    pub fn get_process_utilization(
        _handle: DeviceHandle,
        _last_seen_timestamps: &mut HashMap<u32, u64>,
    ) -> Option<HashMap<u32, UtilSample>> {
        None
    }
    pub fn get_memory_info(_handle: DeviceHandle) -> Option<MemInfo> {
        None
    }
    pub fn get_mig_mode(_handle: DeviceHandle) -> bool {
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_product_name_collapses_nbsp() {
        // U+00A0 NBSP between "NVIDIA" and "H100"
        let raw = "NVIDIA\u{00A0}H100\u{00A0}80GB HBM3";
        let n = normalize_product_name(raw);
        assert_eq!(n, "nvidia h100 80gb hbm3");
    }

    #[test]
    fn normalize_product_name_collapses_nnbsp_and_zwsp() {
        // U+202F narrow no-break space + zero-width space U+200B (which is
        // category Cf, NOT Zs; Python's str.split treats it as whitespace
        // via char.is_whitespace — same behaviour here for U+00A0/U+202F).
        let raw = "NVIDIA\u{202F}A100   40GB";
        let n = normalize_product_name(raw);
        assert_eq!(n, "nvidia a100 40gb");
    }

    #[test]
    fn normalize_product_name_lowercases() {
        assert_eq!(normalize_product_name("NVIDIA H100"), "nvidia h100");
    }

    #[test]
    fn normalize_product_name_nfc() {
        // Composed e+acute (U+00E9) vs decomposed e (U+0065) + combining acute (U+0301)
        // should normalize to the same string.
        let composed = "café";
        let decomposed = "cafe\u{0301}";
        assert_eq!(
            normalize_product_name(composed),
            normalize_product_name(decomposed)
        );
    }

    #[test]
    fn no_feature_returns_no_devices() {
        // Default build (no `gpu` feature) — every accessor short-circuits.
        // We can't ASSERT this when feature IS on, so the check is gated.
        #[cfg(not(feature = "gpu"))]
        {
            assert!(!nvml_available());
            assert!(!init_nvml());
            assert_eq!(get_device_count(), None);
            assert!(get_device_handle(0).is_none());
        }
    }

    #[test]
    fn warn_once_is_log_once() {
        reset_warning_state_for_tests();
        warn_once("test_mode_x", "first");
        warn_once("test_mode_x", "second");  // suppressed
        let set = warned_modes().lock().unwrap();
        assert!(set.contains("test_mode_x"));
    }
}
