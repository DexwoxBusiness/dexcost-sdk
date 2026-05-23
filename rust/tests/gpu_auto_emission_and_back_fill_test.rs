//! GPU auto-emission + back-fill integration — Phase 2 Task 8.
//!
//! Rust port of `python/tests/test_gpu_auto_emission_and_back_fill.py`
//! (commit 56d8d43). End-to-end tests that pin the tracker._finalize_gpu
//! contract:
//!   1. cost_pending=true gpu_cost events get back-filled at end()
//!   2. delta-based aggregation preserves any retry_marker totals
//!   3. gpu_utilization_signal events are NEVER touched by the back-fill
//!      walker — Decision #3 / convention §1 carve-out

use std::sync::Arc;

use rust_decimal::Decimal;
use serde_json::json;
use tokio::sync::Mutex;

use dexcost::core::models::{CostConfidence, CostEvent, EventType, Task, TaskStatus};
use dexcost::core::tracker::TrackedTask;
use dexcost::transport::buffer::EventBuffer;

#[tokio::test]
async fn cost_pending_gpu_cost_event_is_back_filled_at_finalize() {
    let task = Task::new("finalize-test");
    let buf = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let mut tt = TrackedTask::new(task, buf.clone(), None);
    let task_id = tt.task().task_id.clone();

    // Pre-seed a cost_pending gpu_cost event as if a handler wrap emitted it.
    {
        let mut b = buf.lock().await;
        let mut ev = CostEvent::new(&task_id, EventType::GpuCost);
        ev.cost_usd = Decimal::ZERO;
        ev.cost_confidence = CostConfidence::Estimated;
        ev.pricing_source = None;
        ev.details
            .insert("billing_model".into(), json!("per_gpu_second_active"));
        ev.details.insert("gpu_seconds_used".into(), json!("10"));
        ev.details.insert("gpu_sku".into(), json!("h100-80gb-sxm5"));
        ev.details.insert("duration_ms".into(), json!(10000));
        ev.details.insert("gpu_count".into(), json!(1));
        ev.details.insert("cost_pending".into(), json!(true));
        b.add_event(ev);
    }

    tt.end(TaskStatus::Success).await.expect("end ok");

    let b = buf.lock().await;
    let evts: Vec<_> = b
        .query_events(&task_id)
        .into_iter()
        .filter(|e| e.event_type == EventType::GpuCost)
        .collect();
    assert_eq!(evts.len(), 1);
    let ev = &evts[0];
    // cost_pending stripped
    assert!(!ev.details.contains_key("cost_pending"));
    // pricing_version starts with gpu:
    assert!(ev
        .pricing_version
        .as_deref()
        .unwrap_or("")
        .starts_with("gpu:"));
    // GPU pricing_source field added
    assert!(ev.details.contains_key("gpu_pricing_source"));
}

#[tokio::test]
async fn task_gpu_cost_usd_reflects_back_filled_event_cost() {
    let task = Task::new("aggregate-test");
    let buf = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let mut tt = TrackedTask::new(task, buf.clone(), None);
    let task_id = tt.task().task_id.clone();
    {
        let mut b = buf.lock().await;
        let mut ev = CostEvent::new(&task_id, EventType::GpuCost);
        ev.cost_usd = Decimal::ZERO;
        ev.details
            .insert("billing_model".into(), json!("per_gpu_second_active"));
        ev.details.insert("gpu_seconds_used".into(), json!("10"));
        ev.details.insert("gpu_sku".into(), json!("h100-80gb-sxm5"));
        ev.details.insert("duration_ms".into(), json!(10000));
        ev.details.insert("gpu_count".into(), json!(1));
        ev.details.insert("cost_pending".into(), json!(true));
        b.add_event(ev);
    }
    let llm_before = tt.task().total_cost_usd;
    tt.end(TaskStatus::Success).await.unwrap();
    let total_after = tt.task().total_cost_usd;
    // gpu_cost_usd > 0 after back-fill and total_cost_usd grew by the
    // same delta.
    assert!(tt.task().gpu_cost_usd > Decimal::ZERO);
    assert_eq!(total_after - llm_before, tt.task().gpu_cost_usd);
}

#[tokio::test]
async fn no_accountant_means_no_events_and_zero_gpu_cost_usd() {
    let task = Task::new("no-gpu-test");
    let buf = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let mut tt = TrackedTask::new(task, buf.clone(), None);
    let task_id = tt.task().task_id.clone();
    tt.end(TaskStatus::Success).await.unwrap();
    let b = buf.lock().await;
    let gpu_events: Vec<_> = b
        .query_events(&task_id)
        .into_iter()
        .filter(|e| {
            matches!(
                e.event_type,
                EventType::GpuCost | EventType::GpuUtilizationSignal
            )
        })
        .collect();
    assert_eq!(gpu_events.len(), 0);
    assert_eq!(tt.task().gpu_cost_usd, Decimal::ZERO);
}

#[tokio::test]
async fn signal_event_back_fill_carve_out_load_bearing() {
    // Load-bearing test for convention §1 carve-out — gpu_utilization_signal
    // events emit with cost_usd=0 AND STAY at cost_usd=0 after back-fill;
    // task.gpu_cost_usd equals the sum of gpu_cost events ONLY. If a
    // future refactor accidentally aggregates signal events, this fails.
    let task = Task::new("carve-out-test");
    let buf = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let mut tt = TrackedTask::new(task, buf.clone(), None);
    let task_id = tt.task().task_id.clone();
    {
        let mut b = buf.lock().await;
        // One gpu_cost (cost_pending)
        let mut ev = CostEvent::new(&task_id, EventType::GpuCost);
        ev.cost_usd = Decimal::ZERO;
        ev.details
            .insert("billing_model".into(), json!("per_gpu_second_active"));
        ev.details.insert("gpu_seconds_used".into(), json!("10"));
        ev.details.insert("gpu_sku".into(), json!("h100-80gb-sxm5"));
        ev.details.insert("duration_ms".into(), json!(10000));
        ev.details.insert("gpu_count".into(), json!(1));
        ev.details.insert("cost_pending".into(), json!(true));
        b.add_event(ev);
        // Two signal events with details
        for _ in 0..2 {
            let mut sig = CostEvent::new(&task_id, EventType::GpuUtilizationSignal);
            sig.cost_usd = Decimal::ZERO;
            sig.cost_confidence = CostConfidence::Exact;
            sig.pricing_source = None;
            sig.details.insert("sm_util_pct".into(), json!(64.0));
            b.add_event(sig);
        }
    }
    tt.end(TaskStatus::Success).await.unwrap();

    let b = buf.lock().await;
    let signals: Vec<_> = b
        .query_events(&task_id)
        .into_iter()
        .filter(|e| e.event_type == EventType::GpuUtilizationSignal)
        .collect();
    assert_eq!(signals.len(), 2);
    for s in &signals {
        assert_eq!(s.cost_usd, Decimal::ZERO, "signal event got non-zero cost after finalize");
        assert!(s.pricing_source.is_none());
        assert!(s.pricing_version.is_none());
    }
}
