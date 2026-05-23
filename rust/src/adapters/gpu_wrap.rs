//! Serverless GPU handler wraps — Phase 2 Task 7.
//!
//! Rust port of `python/src/dexcost/gpu_wrap.py`. Per-runtime async
//! decorators that:
//!   1. Create a [`GpuAccountant`] for the appropriate
//!      [`GpuRuntimeKind`] and attach it to the task.
//!   2. Time the handler with `std::time::Instant::now()`.
//!   3. Persist the dual events (one `gpu_cost` with `cost_pending=true`
//!      plus N `gpu_utilization_signal` events) on exit — even when the
//!      handler returns Err (capture §6 case 7).
//!
//! Like [`crate::adapters::compute_wrap`], the Rust SDK lacks a global
//! tracker singleton so the handler-wrap takes `&mut TrackedTask`
//! explicitly. Mirrors Phase 1 Rust compute foundation idiom.

use std::sync::Arc;
use std::time::Instant;

use rust_decimal::Decimal;
use serde_json::Value;

use crate::core::gpu_accountant::{GpuAccountant, GpuEventBundle};
use crate::core::gpu_runtime::GpuRuntimeKind;
use crate::core::models::{CostConfidence, CostEvent, EventType};
use crate::core::tracker::TrackedTask;

/// Build an accountant for the given GPU runtime. Region is resolved
/// via `cloud_detect` when available.
pub fn build_accountant_for(runtime: GpuRuntimeKind) -> Arc<GpuAccountant> {
    let cloud_env = crate::cloud_detect::get_cloud_env();
    let mut acc = GpuAccountant::new(runtime);
    if let Some(r) = cloud_env.region {
        acc = acc.with_region(r);
    }
    Arc::new(acc)
}

/// Wraps an async Modal-style handler. On success OR error, persists the
/// gpu_cost + N gpu_utilization_signal events.
pub async fn wrap_modal_handler<F, Fut, T, R, E>(
    tracker: &mut TrackedTask,
    arg: T,
    handler: F,
) -> Result<R, E>
where
    F: FnOnce(T) -> Fut,
    Fut: std::future::Future<Output = Result<R, E>>,
{
    wrap_gpu_handler(tracker, GpuRuntimeKind::Modal, arg, handler).await
}

/// Wraps an async RunPod-style handler.
pub async fn wrap_runpod_handler<F, Fut, T, R, E>(
    tracker: &mut TrackedTask,
    arg: T,
    handler: F,
) -> Result<R, E>
where
    F: FnOnce(T) -> Fut,
    Fut: std::future::Future<Output = Result<R, E>>,
{
    wrap_gpu_handler(tracker, GpuRuntimeKind::Runpod, arg, handler).await
}

/// Wraps an async Replicate-style handler.
pub async fn wrap_replicate_handler<F, Fut, T, R, E>(
    tracker: &mut TrackedTask,
    arg: T,
    handler: F,
) -> Result<R, E>
where
    F: FnOnce(T) -> Fut,
    Fut: std::future::Future<Output = Result<R, E>>,
{
    wrap_gpu_handler(tracker, GpuRuntimeKind::Replicate, arg, handler).await
}

/// Generic shared wrap: time + emit dual events.
async fn wrap_gpu_handler<F, Fut, T, R, E>(
    tracker: &mut TrackedTask,
    runtime: GpuRuntimeKind,
    arg: T,
    handler: F,
) -> Result<R, E>
where
    F: FnOnce(T) -> Fut,
    Fut: std::future::Future<Output = Result<R, E>>,
{
    let accountant = build_accountant_for(runtime);
    accountant.snapshot_start();
    tracker.attach_gpu_for_tests(accountant.clone());

    let start = Instant::now();
    let result = handler(arg).await;
    let duration_ms = start.elapsed().as_millis() as i64;

    // Persist the dual events. Persistence itself is fail-silent — if the
    // bundle can't be built (no NVML / no devices touched), persist nothing.
    if let Some(bundle) = accountant.snapshot_end_and_build(duration_ms) {
        persist_bundle(tracker, &bundle).await;
    }

    result
}

async fn persist_bundle(tracker: &mut TrackedTask, bundle: &GpuEventBundle) {
    let task_id = tracker.task().task_id.clone();
    let buffer = tracker.buffer_handle_for_tests();
    let mut buf = buffer.lock().await;

    // Build the gpu_cost event — cost_pending=true; finalize back-fills.
    let mut cost_event = CostEvent::new(&task_id, EventType::GpuCost);
    cost_event.cost_usd = Decimal::ZERO;
    cost_event.cost_confidence = CostConfidence::Estimated;
    cost_event.occurred_at = bundle.ended_at;
    if let Value::Object(map) = &bundle.cost_event_details {
        for (k, v) in map {
            cost_event.details.insert(k.clone(), v.clone());
        }
    }
    buf.add_event(cost_event);

    // Build per-device signal events — observability-only per convention §1
    // carve-out (Decision #3). No cost_usd, no pricing_source, no
    // pricing_version. The Control Layer MUST NOT aggregate these.
    for sig in &bundle.signal_event_details {
        let mut sig_event = CostEvent::new(&task_id, EventType::GpuUtilizationSignal);
        sig_event.cost_usd = Decimal::ZERO;
        sig_event.cost_confidence = CostConfidence::Exact;
        sig_event.pricing_source = None;
        sig_event.occurred_at = bundle.ended_at;
        if let Value::Object(map) = sig {
            for (k, v) in map {
                sig_event.details.insert(k.clone(), v.clone());
            }
        }
        buf.add_event(sig_event);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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

    #[tokio::test]
    async fn modal_wrap_no_nvml_emits_no_events() {
        // Default build (no `gpu` feature) — accountant returns None from
        // snapshot_end_and_build → nothing persists. The wrap still
        // returns the handler's value unchanged.
        let _g = lock();
        let task = Task::new("modal-test");
        let buf = Arc::new(TokioMutex::new(EventBuffer::new().unwrap()));
        let mut tt = TrackedTask::new(task, buf.clone(), None);
        let task_id = tt.task().task_id.clone();
        let result: Result<i32, String> =
            wrap_modal_handler(&mut tt, 5_i32, |x| async move { Ok::<i32, String>(x + 1) }).await;
        assert_eq!(result.unwrap(), 6);
        let b = buf.lock().await;
        let evts = b.query_events(&task_id);
        let gpu_events: Vec<_> = evts
            .iter()
            .filter(|e| {
                matches!(
                    e.event_type,
                    EventType::GpuCost | EventType::GpuUtilizationSignal
                )
            })
            .collect();
        // Without the `gpu` feature there is no NVML and no devices touched,
        // so no events are emitted — verifies the no-pollution contract.
        assert_eq!(gpu_events.len(), 0);
    }

    #[tokio::test]
    async fn handler_error_is_propagated_and_events_attempted() {
        let _g = lock();
        let task = Task::new("modal-test");
        let buf = Arc::new(TokioMutex::new(EventBuffer::new().unwrap()));
        let mut tt = TrackedTask::new(task, buf.clone(), None);
        let result: Result<i32, String> =
            wrap_modal_handler(&mut tt, 0_i32, |_| async { Err::<i32, String>("boom".into()) })
                .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn build_accountant_smoke_for_three_runtimes() {
        let _g = lock();
        for rt in [
            GpuRuntimeKind::Modal,
            GpuRuntimeKind::Runpod,
            GpuRuntimeKind::Replicate,
        ] {
            let acc = build_accountant_for(rt);
            assert_eq!(acc.runtime, rt);
        }
    }

    #[tokio::test]
    async fn runpod_and_replicate_wraps_compile_and_run() {
        // Smoke: ensure the two sibling wraps share the same generic shape
        // and return values are propagated.
        let _g = lock();
        let task = Task::new("rp");
        let buf = Arc::new(TokioMutex::new(EventBuffer::new().unwrap()));
        let mut tt = TrackedTask::new(task, buf.clone(), None);
        let r1: Result<i32, String> =
            wrap_runpod_handler(&mut tt, 1, |x| async move { Ok::<i32, String>(x * 2) }).await;
        assert_eq!(r1.unwrap(), 2);
        let r2: Result<i32, String> =
            wrap_replicate_handler(&mut tt, 3, |x| async move { Ok::<i32, String>(x + 7) }).await;
        assert_eq!(r2.unwrap(), 10);
    }
}
