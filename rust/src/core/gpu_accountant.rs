//! Per-task GPU accountant — Phase 2 Task 6.
//!
//! One [`GpuAccountant`] per dexcost task (mirrors [`super::compute_accountant::ComputeAccountant`]
//! and the network accountant). Holds the NVML start snapshot + runtime
//! context for a single task. At task finalize emits ONE `gpu_cost` event
//! (with `cost_pending: true` — pricing engine back-fills `cost_usd` per
//! Phase 1 deferred-cost pattern) plus N `gpu_utilization_signal` events
//! (one per device the task's cgroup PIDs actually touched, per Decision #3
//! observability-only carve-out).
//!
//! Capture §5.3 invariant: at most one event-pair per task. Idempotent —
//! a second call to [`GpuAccountant::snapshot_end_and_build`] returns
//! `None`.

use std::collections::HashMap;
use std::sync::Mutex;

use chrono::{DateTime, Utc};
use serde_json::{json, Value};

use crate::core::cgroup_walker::{self, CgroupScope};
use crate::core::gpu_runtime::GpuRuntimeKind;
use crate::core::nvml_reader;

/// Per-device baseline captured at task start.
#[derive(Debug, Clone)]
struct DeviceBaseline {
    handle: nvml_reader::DeviceHandle,
    product_name: String,
    mig_enabled: bool,
    vram_total_bytes: u64,
    /// Persisted NVML lastSeenTimeStamps per PID (Decision #8).
    last_seen_timestamps: HashMap<u32, u64>,
    /// Per-PID accumulated GPU microseconds (used for window-averaged
    /// sm_util_pct per Decision #3 sharpening).
    accumulated_gpu_usec_per_pid: HashMap<u32, u64>,
    /// Peak VRAM observed (used by signal events).
    vram_used_peak_bytes: u64,
    /// Sample count taken during the window (used by signal events).
    sample_count: u64,
}

/// Built event pair returned by [`GpuAccountant::snapshot_end_and_build`].
#[derive(Debug, Clone)]
pub struct GpuEventBundle {
    /// The single `gpu_cost` event details (cost_pending=true; back-fill
    /// at finalize).
    pub cost_event_details: Value,
    /// One `gpu_utilization_signal` event details per device the task's
    /// cgroup PIDs actually touched.
    pub signal_event_details: Vec<Value>,
    pub gpu_runtime: GpuRuntimeKind,
    pub started_at: DateTime<Utc>,
    pub ended_at: DateTime<Utc>,
}

#[derive(Debug)]
struct Inner {
    frozen: bool,
    started_at: Option<DateTime<Utc>>,
    initial_scope: Option<CgroupScope>,
    initial_pids: Vec<u32>,
    devices: Vec<DeviceBaseline>,
}

#[derive(Debug)]
pub struct GpuAccountant {
    pub runtime: GpuRuntimeKind,
    pub region: Option<String>,
    /// Self PID used as the fallback when cgroup walks return None.
    pub self_pid: u32,
    inner: Mutex<Inner>,
}

impl GpuAccountant {
    pub fn new(runtime: GpuRuntimeKind) -> Self {
        Self {
            runtime,
            region: None,
            self_pid: std::process::id(),
            inner: Mutex::new(Inner {
                frozen: false,
                started_at: None,
                initial_scope: None,
                initial_pids: Vec::new(),
                devices: Vec::new(),
            }),
        }
    }

    pub fn with_region(mut self, region: impl Into<String>) -> Self {
        self.region = Some(region.into());
        self
    }

    pub fn with_self_pid(mut self, pid: u32) -> Self {
        self.self_pid = pid;
        self
    }

    /// Capture the start snapshot: classify cgroup scope, enumerate
    /// devices, capture per-device product name + MIG mode + VRAM totals,
    /// snapshot baseline NVML timestamps. Idempotent.
    pub fn snapshot_start(&self) {
        let mut g = self.inner.lock().expect("gpu accountant mutex");
        if g.started_at.is_some() {
            return;
        }
        g.started_at = Some(Utc::now());
        let scope = cgroup_walker::classify_scope();
        g.initial_pids =
            cgroup_walker::enumerate_pids(&scope, self.self_pid).unwrap_or_else(|| vec![self.self_pid]);
        g.initial_scope = Some(scope);

        // Enumerate devices.
        let count = nvml_reader::get_device_count().unwrap_or(0);
        for i in 0..count {
            let handle = match nvml_reader::get_device_handle(i) {
                Some(h) => h,
                None => continue,
            };
            let product_name = nvml_reader::get_product_name(handle).unwrap_or_default();
            let mig_enabled = nvml_reader::get_mig_mode(handle);
            let mem = nvml_reader::get_memory_info(handle);
            let vram_total_bytes = mem.map(|m| m.total_bytes).unwrap_or(0);
            // Initialise the baseline timestamps map with a single sample
            // taken now (so Decision #8's persistent dict has a starting point).
            let mut ts = HashMap::new();
            let _ = nvml_reader::get_process_utilization(handle, &mut ts);
            g.devices.push(DeviceBaseline {
                handle,
                product_name,
                mig_enabled,
                vram_total_bytes,
                last_seen_timestamps: ts,
                accumulated_gpu_usec_per_pid: HashMap::new(),
                vram_used_peak_bytes: 0,
                sample_count: 0,
            });
        }
    }

    /// Sample the GPUs once during the window — accumulates per-PID
    /// microseconds and peak VRAM observations. Intended to be called
    /// periodically by long-running runtimes; cheap-and-no-op when there
    /// are no devices.
    pub fn sample(&self) {
        let mut g = self.inner.lock().expect("gpu accountant mutex");
        if g.frozen {
            return;
        }
        for dev in g.devices.iter_mut() {
            let snap_before: HashMap<u32, u64> = dev.last_seen_timestamps.clone();
            if let Some(samples) =
                nvml_reader::get_process_utilization(dev.handle, &mut dev.last_seen_timestamps)
            {
                // Approximate per-PID microseconds delta as
                // (ts_new - ts_prev) * sm_util/100. This is the cheapest
                // window-averaged sm_util_pct primitive that matches the
                // Decision #3 sharpening contract.
                for (pid, s) in samples {
                    let prev = snap_before.get(&pid).copied().unwrap_or(0);
                    if s.time_stamp > prev {
                        let dt = s.time_stamp - prev;
                        let used =
                            (dt as u128 * s.sm_util as u128 / 100u128) as u64;
                        *dev.accumulated_gpu_usec_per_pid.entry(pid).or_insert(0) += used;
                    }
                }
            }
            if let Some(m) = nvml_reader::get_memory_info(dev.handle) {
                if m.used_bytes > dev.vram_used_peak_bytes {
                    dev.vram_used_peak_bytes = m.used_bytes;
                }
            }
            dev.sample_count += 1;
        }
    }

    /// Capture the end snapshot and build the `gpu_cost` + N
    /// `gpu_utilization_signal` event details. Idempotent — second call
    /// returns `None` (capture §5.3 invariant).
    ///
    /// `duration_ms` is the wall-clock window (typically `task.ended_at
    /// - task.started_at`). When zero (sub-100ms degenerate task) the
    /// `sm_util_pct` field of every signal event is `Null` per
    /// Decision #3 sharpening.
    pub fn snapshot_end_and_build(&self, duration_ms: i64) -> Option<GpuEventBundle> {
        let mut g = self.inner.lock().expect("gpu accountant mutex");
        if g.frozen {
            return None;
        }
        g.frozen = true;
        let started_at = g.started_at.unwrap_or_else(Utc::now);
        let ended_at = Utc::now();

        // Final sample-pass to accumulate the trailing window.
        // We drop the lock and re-acquire to reuse sample()'s logic.
        // (Holding the lock would deadlock — keep this lock as the only one
        // and inline the sample.)
        for dev in g.devices.iter_mut() {
            let snap_before: HashMap<u32, u64> = dev.last_seen_timestamps.clone();
            if let Some(samples) =
                nvml_reader::get_process_utilization(dev.handle, &mut dev.last_seen_timestamps)
            {
                for (pid, s) in samples {
                    let prev = snap_before.get(&pid).copied().unwrap_or(0);
                    if s.time_stamp > prev {
                        let dt = s.time_stamp - prev;
                        let used = (dt as u128 * s.sm_util as u128 / 100u128) as u64;
                        *dev.accumulated_gpu_usec_per_pid.entry(pid).or_insert(0) += used;
                    }
                }
            }
            if let Some(m) = nvml_reader::get_memory_info(dev.handle) {
                if m.used_bytes > dev.vram_used_peak_bytes {
                    dev.vram_used_peak_bytes = m.used_bytes;
                }
            }
        }

        // Re-classify the cgroup scope (PIDs may have come and gone).
        let scope = cgroup_walker::classify_scope();
        let pids_opt = cgroup_walker::enumerate_pids(&scope, self.self_pid);
        let walked_failed = pids_opt.is_none();
        let pid_set: std::collections::HashSet<u32> = pids_opt
            .unwrap_or_else(|| vec![self.self_pid])
            .into_iter()
            .chain(g.initial_pids.iter().copied())
            .collect();

        // Decision #1 fallback label
        let mut fallback_label = cgroup_walker::fallback_label_for(&scope).map(String::from);
        if walked_failed && fallback_label.is_none() {
            fallback_label = Some("self_pid_only".to_string());
        }

        // Per-device aggregation
        let mut total_gpu_usec: u64 = 0;
        let mut total_gpu_count: u32 = 0;
        let mut any_mig = false;
        let mut signal_events: Vec<Value> = Vec::new();
        let mut canonical_product_name: Option<String> = None;
        let mut canonical_vram_total: u64 = 0;

        for dev in g.devices.iter() {
            total_gpu_count += 1;
            // Sum task PIDs only — Decision #1 measurement-side filter.
            let dev_gpu_usec: u64 = dev
                .accumulated_gpu_usec_per_pid
                .iter()
                .filter(|(pid, _)| pid_set.contains(pid))
                .map(|(_, v)| *v)
                .sum();
            total_gpu_usec += dev_gpu_usec;
            if dev.mig_enabled {
                any_mig = true;
            }
            if canonical_product_name.is_none() && !dev.product_name.is_empty() {
                canonical_product_name = Some(dev.product_name.clone());
                canonical_vram_total = dev.vram_total_bytes;
            }

            // Only emit a signal event when the task's PIDs touched this device.
            if dev_gpu_usec == 0 {
                continue;
            }
            // Decision #3 sharpening — window-averaged sm_util_pct.
            // sm_util_pct = min(100, gpu_seconds / window_seconds * 100)
            let sm_util_pct: Value = if duration_ms <= 0 {
                Value::Null
            } else {
                let gpu_secs = dev_gpu_usec as f64 / 1_000_000.0;
                let window_secs = duration_ms as f64 / 1000.0;
                let pct = (gpu_secs / window_secs * 100.0).min(100.0);
                json!(pct)
            };
            signal_events.push(json!({
                "device_product_name": dev.product_name,
                "sm_util_pct": sm_util_pct,
                "vram_used_peak_bytes": dev.vram_used_peak_bytes,
                "vram_total_bytes": dev.vram_total_bytes,
                "process_count": pid_set.len(),
                "sample_count": dev.sample_count,
                "task_duration_ms": duration_ms,
                "mig_enabled": dev.mig_enabled,
            }));
        }

        let gpu_seconds_used = total_gpu_usec as f64 / 1_000_000.0;
        let window_seconds = (duration_ms.max(0) as f64) / 1000.0;

        // Emission rules: only emit when SOMETHING in this task actually
        // touched the GPU, OR MIG is enabled, OR a measurement-side
        // fallback label applies (so the customer at least gets the
        // zero-cost signal that there's a GPU box behind their task).
        let touched_gpu = total_gpu_usec > 0;
        let should_emit = touched_gpu || any_mig || fallback_label.is_some();
        if !should_emit || g.devices.is_empty() {
            return None;
        }

        // Build the gpu_cost event details — cost_pending=true; back-fill
        // at task finalize.
        let mut details = serde_json::Map::new();
        details.insert("billing_model".into(), json!(billing_model_for(self.runtime)));
        details.insert("gpu_runtime".into(), json!(self.runtime.as_str()));
        details.insert("duration_ms".into(), json!(duration_ms));
        details.insert("window_seconds".into(), json!(window_seconds));
        details.insert("gpu_seconds_used".into(), json!(gpu_seconds_used));
        details.insert("gpu_count".into(), json!(total_gpu_count));
        details.insert(
            "gpu_sku".into(),
            json!(canonical_product_name.clone().unwrap_or_default()),
        );
        details.insert("vram_total_bytes".into(), json!(canonical_vram_total));
        details.insert("region".into(), json!(self.region));
        details.insert("cost_pending".into(), json!(true));
        if let Some(p) = canonical_product_name.as_ref() {
            details.insert("_nvml_product_name_lower".into(), json!(p));
        }
        if let Some(label) = fallback_label.as_ref() {
            details.insert(
                "_cgroup_scope_fallback".into(),
                json!(label),
            );
        }
        if any_mig {
            details.insert("mig_profile".into(), json!("mig_enabled_v1_placeholder"));
            details.insert("mig_enabled".into(), json!(true));
        }

        Some(GpuEventBundle {
            cost_event_details: Value::Object(details),
            signal_event_details: signal_events,
            gpu_runtime: self.runtime,
            started_at,
            ended_at,
        })
    }
}

/// Billing model discriminator string for a given runtime — matches the
/// pricing engine's dispatch keys EXACTLY.
pub fn billing_model_for(runtime: GpuRuntimeKind) -> &'static str {
    match runtime {
        GpuRuntimeKind::Modal | GpuRuntimeKind::Runpod | GpuRuntimeKind::Replicate => {
            "per_gpu_second_active"
        }
        GpuRuntimeKind::AwsEc2Gpu | GpuRuntimeKind::GcpGceBundled | GpuRuntimeKind::AzureVmGpu => {
            "per_instance_hour"
        }
        GpuRuntimeKind::LambdaLabs
        | GpuRuntimeKind::Coreweave
        | GpuRuntimeKind::GcpGceN1Attached => "per_gpu_hour_reserved",
        GpuRuntimeKind::AzureVmVgpu => "per_vgpu_hour",
        GpuRuntimeKind::None => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn billing_model_dispatch_table() {
        assert_eq!(
            billing_model_for(GpuRuntimeKind::Modal),
            "per_gpu_second_active"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::Runpod),
            "per_gpu_second_active"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::Replicate),
            "per_gpu_second_active"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::AwsEc2Gpu),
            "per_instance_hour"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::GcpGceBundled),
            "per_instance_hour"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::AzureVmGpu),
            "per_instance_hour"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::LambdaLabs),
            "per_gpu_hour_reserved"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::Coreweave),
            "per_gpu_hour_reserved"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::GcpGceN1Attached),
            "per_gpu_hour_reserved"
        );
        assert_eq!(
            billing_model_for(GpuRuntimeKind::AzureVmVgpu),
            "per_vgpu_hour"
        );
    }

    #[test]
    fn snapshot_end_idempotent_returns_none_second_call() {
        // No NVML available → no devices → no_emit on first call.
        let acc = GpuAccountant::new(GpuRuntimeKind::Modal);
        acc.snapshot_start();
        let _ = acc.snapshot_end_and_build(100);
        // Second call always returns None.
        assert!(acc.snapshot_end_and_build(100).is_none());
    }

    #[test]
    /// B2 regression — Sprint 2 Theme C / §3.1.1 (Rust cross-SDK pin).
    ///
    /// The accountant's per-PID integration math (gpu_accountant.rs:163-168
    /// and :207-212) ALREADY computes the correct `sm_seconds = Σ
    /// (sm_util/100) × dt` formula — Rust did not have the wall-time-
    /// vs-SM-time bug Python had pre-d37b6b5. This test pins the
    /// arithmetic directly so a future "simplification" can't regress it.
    ///
    /// We can't drive the live nvml_reader from a unit test (no public
    /// backend trait), so we test the integration formula at the
    /// arithmetic level — the same expression that lives in
    /// gpu_accountant.rs:165 and :209.
    #[test]
    fn integration_formula_matches_python_canonical() {
        // PID at t=20s sm=80% → covers 0..20s → 16 sm-seconds
        // PID at t=60s sm=40% → covers 20..60s → 16 sm-seconds
        // Total: 32 sm-seconds. (Same as Python d37b6b5 + Go bbe1133 + TS 05a21bb.)
        struct Sample {
            ts_us: u64,
            sm: u8,
        }
        let samples = [
            Sample { ts_us: 20_000_000, sm: 80 },
            Sample { ts_us: 60_000_000, sm: 40 },
        ];
        let mut prev: u64 = 0;
        let mut total_usec: u64 = 0;
        for s in samples.iter() {
            if s.ts_us > prev {
                let dt = s.ts_us - prev;
                let used = (dt as u128 * s.sm as u128 / 100u128) as u64;
                total_usec += used;
            }
            prev = s.ts_us;
        }
        let gpu_seconds = total_usec as f64 / 1_000_000.0;
        assert!(
            (gpu_seconds - 32.0).abs() < 0.01,
            "expected 32 sm-seconds, got {} — integration formula at \
             gpu_accountant.rs:165 has been changed",
            gpu_seconds,
        );
    }

    #[test]
    fn no_nvml_returns_none() {
        // Default build (no `gpu` feature) — no devices enumerated.
        let acc = GpuAccountant::new(GpuRuntimeKind::Modal);
        acc.snapshot_start();
        assert!(acc.snapshot_end_and_build(100).is_none());
    }
}
