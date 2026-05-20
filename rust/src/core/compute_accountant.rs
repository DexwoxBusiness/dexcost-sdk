//! Per-task compute accountant.
//!
//! Holds the cgroup start snapshot + runtime context for one dexcost task. At
//! task finalize, emits exactly one `compute_cost` event with
//! `cost_pending: true` — the pricing engine back-fills `cost_usd` via the
//! deferred-cost pattern inherited from network v2.
//!
//! Capture §5.3 invariant: at most one event per task per runtime. Idempotent —
//! a second call to `snapshot_end_and_build` / `build_serverless_event`
//! returns `None`.
//!
//! Mirrors `python/src/dexcost/compute_accountant.py`.

use std::sync::Mutex;

use serde_json::{json, Value};

use crate::core::cgroup_reader::{
    read_cpu_max, read_cpu_stat, read_memory_current, read_memory_max, read_memory_peak,
};
use crate::core::compute_runtime::RuntimeKind;

fn billing_model_for(runtime: RuntimeKind) -> &'static str {
    match runtime {
        RuntimeKind::Lambda => "lambda",
        RuntimeKind::Fargate => "fargate",
        RuntimeKind::Ec2 => "ec2_share",
        RuntimeKind::Gce => "gce_share",
        RuntimeKind::AzureVm => "azure_vm_share",
        RuntimeKind::CloudRun => "cloud_run_request",
        RuntimeKind::CloudFunctions => "cloud_functions",
        RuntimeKind::AzureFunctions => "azure_functions",
        RuntimeKind::Vercel => "vercel_fluid",
        RuntimeKind::K8sPod => "k8s_pod_share",
        RuntimeKind::Unknown => "unknown",
    }
}

fn detect_arch() -> &'static str {
    let arch = std::env::consts::ARCH;
    if arch == "aarch64" || arch == "arm64" {
        "arm64"
    } else {
        "x86_64"
    }
}

fn host_cpu_count_f64() -> f64 {
    std::thread::available_parallelism()
        .map(|n| n.get() as f64)
        .unwrap_or(1.0)
}

fn vcpu_count_from_cgroup() -> f64 {
    if let Some(m) = read_cpu_max() {
        m.vcpu_count
    } else {
        host_cpu_count_f64()
    }
}

#[derive(Debug)]
struct Inner {
    frozen: bool,
    start_cpu_usec: Option<u64>,
}

#[derive(Debug)]
pub struct ComputeAccountant {
    inner: Mutex<Inner>,
    pub runtime: RuntimeKind,
    pub lambda_memory_mb: Option<u32>,
    pub fargate_vcpu: Option<f64>,
    pub fargate_memory_mib: Option<u64>,
    pub architecture: String,
    pub initialization_type: Option<String>,
    pub region: Option<String>,
}

impl ComputeAccountant {
    /// Construct a fresh accountant. Architecture defaults to the host's.
    pub fn new(runtime: RuntimeKind) -> Self {
        Self {
            inner: Mutex::new(Inner {
                frozen: false,
                start_cpu_usec: None,
            }),
            runtime,
            lambda_memory_mb: None,
            fargate_vcpu: None,
            fargate_memory_mib: None,
            architecture: detect_arch().to_string(),
            initialization_type: None,
            region: None,
        }
    }

    pub fn with_lambda_memory_mb(mut self, mb: u32) -> Self {
        self.lambda_memory_mb = Some(mb);
        self
    }

    pub fn with_fargate_vcpu(mut self, v: f64) -> Self {
        self.fargate_vcpu = Some(v);
        self
    }

    pub fn with_fargate_memory_mib(mut self, m: u64) -> Self {
        self.fargate_memory_mib = Some(m);
        self
    }

    pub fn with_architecture(mut self, a: String) -> Self {
        self.architecture = a;
        self
    }

    pub fn with_initialization_type(mut self, t: String) -> Self {
        self.initialization_type = Some(t);
        self
    }

    pub fn with_region(mut self, r: String) -> Self {
        self.region = Some(r);
        self
    }

    /// Capture the cgroup CPU counter at task start. Idempotent.
    pub fn snapshot_start(&self) {
        {
            let guard = self.inner.lock().expect("accountant mutex poisoned");
            if guard.start_cpu_usec.is_some() {
                return;
            }
        }
        let s = read_cpu_stat();
        let mut guard = self.inner.lock().expect("accountant mutex poisoned");
        guard.start_cpu_usec = Some(s.map(|x| x.usage_usec).unwrap_or(0));
    }

    /// Capture cgroup CPU/memory at task end and build the event details.
    ///
    /// Returns `None` if already frozen (second call).
    pub fn snapshot_end_and_build(&self, duration_ms: i64) -> Option<Value> {
        let start_cpu = {
            let mut guard = self.inner.lock().expect("accountant mutex poisoned");
            if guard.frozen {
                return None;
            }
            guard.frozen = true;
            guard.start_cpu_usec.unwrap_or(0)
        };

        let end = read_cpu_stat();
        let cpu_max = read_cpu_max();
        // capture §6 case 6 — memory.peak missing → fall back to memory.current.
        let mem_peak = read_memory_peak()
            .or_else(read_memory_current)
            .unwrap_or(0);
        let mem_limit = read_memory_max().unwrap_or(0);

        let vcpu_seconds_used = match &end {
            Some(e) if e.usage_usec >= start_cpu => {
                ((e.usage_usec - start_cpu) as f64) / 1_000_000.0
            }
            _ => 0.0,
        };

        let vcpu_count = cpu_max
            .as_ref()
            .map(|m| m.vcpu_count)
            .unwrap_or_else(host_cpu_count_f64);

        Some(json!({
            "billing_model": billing_model_for(self.runtime),
            "duration_ms": duration_ms,
            "memory_bytes_peak": mem_peak,
            "memory_bytes_limit": mem_limit,
            "vcpu_count": vcpu_count,
            "vcpu_seconds_used": vcpu_seconds_used,
            "invocation_count": 0,
            "region": self.region,
            "architecture": self.architecture,
            "initialization_type": Value::Null,
            "cost_pending": true,
        }))
    }

    /// Build a per-invocation event for serverless runtimes.
    pub fn build_serverless_event(
        &self,
        duration_ms: i64,
        memory_bytes_peak: u64,
    ) -> Option<Value> {
        {
            let mut guard = self.inner.lock().expect("accountant mutex poisoned");
            if guard.frozen {
                return None;
            }
            guard.frozen = true;
        }

        let (mem_limit, vcpu_count) = match self.runtime {
            RuntimeKind::Lambda => {
                // AWS_LAMBDA_FUNCTION_MEMORY_SIZE is DECIMAL MB (10^6 bytes).
                let mb = self.lambda_memory_mb.unwrap_or(128) as u64;
                let mem = mb.saturating_mul(1_000_000);
                (mem, vcpu_count_from_cgroup())
            }
            RuntimeKind::Fargate => {
                let mem = self
                    .fargate_memory_mib
                    .unwrap_or(0)
                    .saturating_mul(1024 * 1024);
                let v = self.fargate_vcpu.unwrap_or_else(vcpu_count_from_cgroup);
                (mem, v)
            }
            _ => {
                let mem = read_memory_max().unwrap_or(memory_bytes_peak);
                (mem, vcpu_count_from_cgroup())
            }
        };

        // Serverless events use the per-invocation billing models:
        // lambda / cloud_run_request / cloud_functions / azure_functions /
        // vercel_fluid. The discriminator already maps correctly via
        // billing_model_for for those runtimes.
        Some(json!({
            "billing_model": billing_model_for(self.runtime),
            "duration_ms": duration_ms,
            "memory_bytes_peak": memory_bytes_peak,
            "memory_bytes": memory_bytes_peak,  // alias for cloud_run / azure_functions / vercel paths
            "memory_bytes_limit": mem_limit,
            "lambda_memory_mb": self.lambda_memory_mb,
            "fargate_vcpu": self.fargate_vcpu,
            "fargate_memory_bytes_limit": self.fargate_memory_mib.map(|m| m * 1024 * 1024),
            "vcpu_count": vcpu_count,
            "vcpu_seconds_used": 0,
            "invocation_count": 1,
            "region": self.region,
            "architecture": self.architecture,
            "initialization_type": self.initialization_type,
            "duration_ms_decimal": duration_ms,
            "cost_pending": true,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::cgroup_reader::{reset_cgroup_root_for_tests, set_cgroup_root_for_tests};
    use std::sync::{LazyLock, Mutex as StdMutex};

    static TEST_LOCK: LazyLock<StdMutex<()>> = LazyLock::new(|| StdMutex::new(()));

    fn lock() -> std::sync::MutexGuard<'static, ()> {
        match TEST_LOCK.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        }
    }

    fn fixture(name: &str, content: &str) -> tempfile::TempDir {
        let t = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join(name), content).unwrap();
        t
    }

    fn write_files(t: &std::path::Path, files: &[(&str, &str)]) {
        for (name, content) in files {
            std::fs::write(t.join(name), content).unwrap();
        }
    }

    #[test]
    fn snapshot_start_is_idempotent() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write_files(t.path(), &[("cpu.stat", "usage_usec 1000\n")]);
        set_cgroup_root_for_tests(t.path());

        let a = ComputeAccountant::new(RuntimeKind::Fargate);
        a.snapshot_start();
        let first = a.inner.lock().unwrap().start_cpu_usec;
        // Overwrite the cgroup file and call again — should NOT change.
        std::fs::write(t.path().join("cpu.stat"), "usage_usec 9999\n").unwrap();
        a.snapshot_start();
        let second = a.inner.lock().unwrap().start_cpu_usec;
        assert_eq!(first, second);

        reset_cgroup_root_for_tests();
    }

    #[test]
    fn snapshot_end_builds_event_with_vcpu_seconds_used() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write_files(
            t.path(),
            &[
                ("cpu.stat", "usage_usec 1000000\n"),  // start
                ("cpu.max", "200000 100000\n"),
                ("memory.peak", "1073741824\n"),
                ("memory.max", "2147483648\n"),
            ],
        );
        set_cgroup_root_for_tests(t.path());

        let a = ComputeAccountant::new(RuntimeKind::Ec2);
        a.snapshot_start();
        // Advance cpu.stat for end-of-task snapshot.
        std::fs::write(t.path().join("cpu.stat"), "usage_usec 3000000\n").unwrap();
        let event = a.snapshot_end_and_build(2000).expect("event built");
        // (3000000 - 1000000) / 1_000_000 = 2.0 vcpu-seconds
        assert!((event["vcpu_seconds_used"].as_f64().unwrap() - 2.0).abs() < 1e-9);
        assert_eq!(event["billing_model"], "ec2_share");
        assert_eq!(event["memory_bytes_peak"], 1_073_741_824u64);
        assert_eq!(event["cost_pending"], true);
        assert_eq!(event["duration_ms"], 2000);

        reset_cgroup_root_for_tests();
    }

    #[test]
    fn snapshot_end_is_idempotent_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        write_files(
            t.path(),
            &[
                ("cpu.stat", "usage_usec 100\n"),
                ("cpu.max", "100000 100000\n"),
                ("memory.max", "1000\n"),
            ],
        );
        set_cgroup_root_for_tests(t.path());
        let a = ComputeAccountant::new(RuntimeKind::Ec2);
        a.snapshot_start();
        assert!(a.snapshot_end_and_build(100).is_some());
        assert!(a.snapshot_end_and_build(100).is_none());
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn memory_peak_falls_back_to_memory_current() {
        let _g = lock();
        // memory.peak missing — fall through to memory.current per capture §6 case 6.
        let t = fixture("cpu.stat", "usage_usec 0\n");
        write_files(
            t.path(),
            &[
                ("cpu.max", "100000 100000\n"),
                ("memory.current", "5000000\n"),
                ("memory.max", "100000000\n"),
            ],
        );
        set_cgroup_root_for_tests(t.path());
        let a = ComputeAccountant::new(RuntimeKind::Ec2);
        a.snapshot_start();
        let event = a.snapshot_end_and_build(100).unwrap();
        assert_eq!(event["memory_bytes_peak"], 5_000_000u64);
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn serverless_event_for_lambda_uses_decimal_mb() {
        let _g = lock();
        // Override cgroup to avoid real reads.
        let t = tempfile::tempdir().unwrap();
        set_cgroup_root_for_tests(t.path());
        let a = ComputeAccountant::new(RuntimeKind::Lambda)
            .with_lambda_memory_mb(512)
            .with_region("us-east-1".into())
            .with_initialization_type("on-demand".into());
        let ev = a
            .build_serverless_event(250, 200_000_000)
            .expect("serverless event");
        assert_eq!(ev["billing_model"], "lambda");
        // 512 * 10^6 = 512_000_000
        assert_eq!(ev["memory_bytes_limit"], 512_000_000u64);
        assert_eq!(ev["invocation_count"], 1);
        assert_eq!(ev["region"], "us-east-1");
        assert_eq!(ev["initialization_type"], "on-demand");
        assert_eq!(ev["cost_pending"], true);
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn serverless_event_idempotent_returns_none() {
        let _g = lock();
        let t = tempfile::tempdir().unwrap();
        set_cgroup_root_for_tests(t.path());
        let a = ComputeAccountant::new(RuntimeKind::Lambda).with_lambda_memory_mb(128);
        assert!(a.build_serverless_event(100, 100_000_000).is_some());
        assert!(a.build_serverless_event(100, 100_000_000).is_none());
        reset_cgroup_root_for_tests();
    }

    #[test]
    fn detect_arch_uses_consts_arch() {
        // Smoke test — detect_arch must return one of the two values.
        let a = detect_arch();
        assert!(a == "arm64" || a == "x86_64");
    }

    #[test]
    fn billing_model_for_runtime_matches_dispatch_keys() {
        // Cross-SDK invariant: discriminator strings must match the
        // ComputePricingEngine dispatch in pricing/compute_pricing.rs.
        assert_eq!(billing_model_for(RuntimeKind::Lambda), "lambda");
        assert_eq!(billing_model_for(RuntimeKind::Fargate), "fargate");
        assert_eq!(billing_model_for(RuntimeKind::Ec2), "ec2_share");
        assert_eq!(billing_model_for(RuntimeKind::CloudRun), "cloud_run_request");
        assert_eq!(billing_model_for(RuntimeKind::CloudFunctions), "cloud_functions");
        assert_eq!(billing_model_for(RuntimeKind::AzureFunctions), "azure_functions");
        assert_eq!(billing_model_for(RuntimeKind::AzureVm), "azure_vm_share");
        assert_eq!(billing_model_for(RuntimeKind::Gce), "gce_share");
        assert_eq!(billing_model_for(RuntimeKind::Vercel), "vercel_fluid");
        assert_eq!(billing_model_for(RuntimeKind::K8sPod), "k8s_pod_share");
    }
}
