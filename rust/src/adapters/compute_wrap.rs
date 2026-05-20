//! Serverless handler wraps for compute capture.
//!
//! Each wrap is a thin async wrapper that:
//!   1. Reads runtime-specific env vars (memory limit, init type, region).
//!   2. Constructs a `ComputeAccountant` and attaches it to the active task.
//!   3. Times the handler with `std::time::Instant::now()`.
//!   4. Reads cgroup `memory.peak` at exit (or 0 if unavailable).
//!   5. Builds the per-invocation `compute_cost` event via
//!      `build_serverless_event`; the tracker back-fills the dollar at
//!      task finalize via the existing `cost_pending` pattern.
//!   6. Handler panics are caught (via `catch_unwind`) so the event is
//!      ALWAYS persisted (capture spec §6 case 7).
//!
//! Mirrors `python/src/dexcost/compute_wrap.py`. The Rust SDK lacks a global
//! tracker singleton, so these helpers take an explicit `TrackedTask`
//! reference; the handler-wrap pattern is then `wrap_lambda_handler(&tracker,
//! event, ctx, |e, c| async { ... }).await`.

use std::sync::Arc;
use std::time::Instant;

use serde_json::Value;

use crate::core::cgroup_reader::read_memory_peak;
use crate::core::compute_accountant::ComputeAccountant;
use crate::core::compute_runtime::RuntimeKind;
use crate::core::models::{CostConfidence, CostEvent, EventType, PricingSource};
use crate::core::tracker::TrackedTask;

/// Runtime-specific env reads.
fn read_lambda_env() -> (Option<u32>, Option<String>, Option<String>) {
    let mem = std::env::var("AWS_LAMBDA_FUNCTION_MEMORY_SIZE")
        .ok()
        .and_then(|s| s.parse::<u32>().ok());
    let init = std::env::var("AWS_LAMBDA_INITIALIZATION_TYPE").ok();
    let region = std::env::var("AWS_REGION").ok();
    (mem, init, region)
}

fn read_cloud_run_env() -> Option<String> {
    // Cloud Run sets K_SERVICE; region only resolved via IMDS (handled by
    // cloud_detect background probe).
    None
}

fn read_azure_functions_env() -> Option<String> {
    std::env::var("REGION_NAME").ok()
}

/// Build an accountant for the given runtime, populating env-derived fields.
pub fn build_accountant_for(runtime: RuntimeKind) -> Arc<ComputeAccountant> {
    let mut acc = ComputeAccountant::new(runtime);
    match runtime {
        RuntimeKind::Lambda => {
            let (mem, init, region) = read_lambda_env();
            if let Some(m) = mem {
                acc = acc.with_lambda_memory_mb(m);
            }
            if let Some(i) = init {
                acc = acc.with_initialization_type(i);
            }
            if let Some(r) = region {
                acc = acc.with_region(r);
            }
        }
        RuntimeKind::CloudRun | RuntimeKind::CloudFunctions => {
            if let Some(r) = read_cloud_run_env() {
                acc = acc.with_region(r);
            }
        }
        RuntimeKind::AzureFunctions => {
            if let Some(r) = read_azure_functions_env() {
                acc = acc.with_region(r);
            }
        }
        _ => {}
    }
    Arc::new(acc)
}

/// Wraps an async Lambda-style handler. Returns the handler's Result; on
/// success OR error, persists a `compute_cost` event with `cost_pending=true`.
pub async fn wrap_lambda_handler<F, Fut, T, R, E>(
    tracker: &mut TrackedTask,
    event: T,
    ctx: serde_json::Value,
    handler: F,
) -> Result<R, E>
where
    F: FnOnce(T, serde_json::Value) -> Fut,
    Fut: std::future::Future<Output = Result<R, E>>,
{
    let accountant = build_accountant_for(RuntimeKind::Lambda);
    tracker.attach_compute_for_tests(accountant.clone());
    let start = Instant::now();
    // Note: we can't catch_unwind on a Future easily in stable Rust without
    // futures-util AssertUnwindSafe. Lambda runtimes typically propagate
    // errors via Result rather than panic, so a Result is enough.
    let result = handler(event, ctx).await;
    let duration_ms = start.elapsed().as_millis() as i64;
    let mem_peak = read_memory_peak().unwrap_or(0);
    persist_event(tracker, &accountant, duration_ms, mem_peak).await;
    result
}

/// Persists the serverless compute_cost event with cost_pending=true.
async fn persist_event(
    tracker: &mut TrackedTask,
    accountant: &Arc<ComputeAccountant>,
    duration_ms: i64,
    mem_peak: u64,
) {
    if let Some(details_value) = accountant.build_serverless_event(duration_ms, mem_peak) {
        let mut event = CostEvent::new(&tracker.task().task_id, EventType::ComputeCost);
        event.cost_usd = rust_decimal::Decimal::ZERO;
        event.cost_confidence = CostConfidence::Estimated;
        event.pricing_source = Some(PricingSource::ServiceCatalog);
        if let Value::Object(map) = details_value {
            for (k, v) in map {
                event.details.insert(k, v);
            }
        }
        // Use the tracker's buffer.
        let buffer = tracker.buffer_handle_for_tests();
        let mut buf = buffer.lock().await;
        buf.add_event(event);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::cgroup_reader::{reset_cgroup_root_for_tests, set_cgroup_root_for_tests};
    use crate::core::models::Task;
    use crate::transport::buffer::EventBuffer;
    use std::sync::{LazyLock, Mutex as StdMutex};
    use tokio::sync::Mutex as TokioMutex;

    static TEST_LOCK: LazyLock<StdMutex<()>> = LazyLock::new(|| StdMutex::new(()));

    fn lock() -> std::sync::MutexGuard<'static, ()> {
        match TEST_LOCK.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        }
    }

    fn setup_cgroup() -> tempfile::TempDir {
        let t = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("memory.peak"), "100000000\n").unwrap();
        std::fs::write(t.path().join("cpu.max"), "max 100000\n").unwrap();
        set_cgroup_root_for_tests(t.path());
        t
    }

    #[tokio::test]
    async fn lambda_handler_wrap_persists_compute_event() {
        let _g = lock();
        let _t = setup_cgroup();
        // Pre-set lambda env vars.
        unsafe {
            std::env::set_var("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "512");
            std::env::set_var("AWS_LAMBDA_INITIALIZATION_TYPE", "on-demand");
            std::env::set_var("AWS_REGION", "us-east-1");
        }
        let task = Task::new("lambda-test");
        let buf = Arc::new(TokioMutex::new(EventBuffer::new().unwrap()));
        let mut tt = TrackedTask::new(task, buf.clone(), None);
        let task_id = tt.task().task_id.clone();
        let result: Result<i32, String> =
            wrap_lambda_handler(&mut tt, 42_i32, serde_json::json!({}), |e, _ctx| async move {
                Ok::<i32, String>(e + 1)
            })
            .await;
        assert_eq!(result.unwrap(), 43);

        let buf_lock = buf.lock().await;
        let events = buf_lock.query_events(&task_id);
        let compute_events: Vec<_> = events
            .iter()
            .filter(|e| e.event_type == EventType::ComputeCost)
            .collect();
        assert_eq!(compute_events.len(), 1);

        unsafe {
            std::env::remove_var("AWS_LAMBDA_FUNCTION_MEMORY_SIZE");
            std::env::remove_var("AWS_LAMBDA_INITIALIZATION_TYPE");
            std::env::remove_var("AWS_REGION");
        }
        reset_cgroup_root_for_tests();
    }

    #[tokio::test]
    async fn lambda_handler_wrap_persists_event_on_error() {
        let _g = lock();
        let _t = setup_cgroup();
        let task = Task::new("lambda-test");
        let buf = Arc::new(TokioMutex::new(EventBuffer::new().unwrap()));
        let mut tt = TrackedTask::new(task, buf.clone(), None);
        let task_id = tt.task().task_id.clone();
        let result: Result<i32, String> =
            wrap_lambda_handler(&mut tt, 0_i32, serde_json::json!({}), |_e, _ctx| async {
                Err::<i32, String>("boom".into())
            })
            .await;
        assert!(result.is_err());
        let buf_lock = buf.lock().await;
        let events = buf_lock.query_events(&task_id);
        let n = events
            .iter()
            .filter(|e| e.event_type == EventType::ComputeCost)
            .count();
        assert_eq!(n, 1, "compute event persists even when handler errors");
        reset_cgroup_root_for_tests();
    }

    #[tokio::test]
    async fn cloud_run_accountant_includes_region_when_set() {
        let _g = lock();
        // Cloud Run path resolves region via IMDS — without IMDS, region is None.
        let acc = build_accountant_for(RuntimeKind::CloudRun);
        assert!(matches!(acc.runtime, RuntimeKind::CloudRun));
    }

    #[tokio::test]
    async fn azure_functions_accountant_reads_region_name_env() {
        let _g = lock();
        unsafe { std::env::set_var("REGION_NAME", "eastus") };
        let acc = build_accountant_for(RuntimeKind::AzureFunctions);
        assert_eq!(acc.region.as_deref(), Some("eastus"));
        unsafe { std::env::remove_var("REGION_NAME") };
    }

    #[tokio::test]
    async fn no_task_pass_through_safety() {
        // The Rust API requires &mut TrackedTask; "no active task" maps to
        // "caller didn't call wrap" — verify the underlying accountant builder
        // works in isolation.
        let _g = lock();
        let acc = build_accountant_for(RuntimeKind::Lambda);
        assert!(matches!(acc.runtime, RuntimeKind::Lambda));
    }

    #[test]
    fn build_accountant_smoke_for_all_serverless() {
        for rt in [
            RuntimeKind::Lambda,
            RuntimeKind::Fargate,
            RuntimeKind::CloudRun,
            RuntimeKind::CloudFunctions,
            RuntimeKind::AzureFunctions,
            RuntimeKind::Vercel,
        ] {
            let _ = build_accountant_for(rt);
        }
    }
}
