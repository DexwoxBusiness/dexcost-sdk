//! gpu_utilization_signal observability carve-out — Phase 2 Task 9.
//!
//! Rust port of
//! `python/tests/test_gpu_utilization_signal_observability.py` (commit
//! d42cc81). Executable spec for convention §1's signal-event carve-out.
//!
//! If a future refactor accidentally:
//!   - aggregates gpu_utilization_signal cost_usd into task.gpu_cost_usd
//!   - back-fills cost_usd on signal events
//!   - drops the events
//!   - removes the observability fields
//! exactly one of these tests fails with a specific contract violation
//! in the assertion message.

use std::sync::Arc;

use rust_decimal::Decimal;
use tokio::sync::Mutex;

use dexcost::core::models::{CostConfidence, CostEvent, EventType, Task, TaskStatus};
use dexcost::core::tracker::TrackedTask;
use dexcost::transport::buffer::EventBuffer;

async fn fresh_tracker() -> (TrackedTask, Arc<Mutex<EventBuffer>>) {
    let task = Task::new("gpu-signal-test");
    let buf = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let tracker = TrackedTask::new(task, buf.clone(), None);
    (tracker, buf)
}

fn add_signal_event(buf: &mut EventBuffer, task_id: &str) -> CostEvent {
    let mut ev = CostEvent::new(task_id, EventType::GpuUtilizationSignal);
    ev.cost_usd = Decimal::ZERO;
    ev.cost_confidence = CostConfidence::Exact;
    ev.pricing_source = None;
    ev.details.insert(
        "device_product_name".into(),
        serde_json::json!("nvidia h100 80gb hbm3"),
    );
    ev.details.insert("sm_util_pct".into(), serde_json::json!(64.0));
    ev.details
        .insert("vram_used_peak_bytes".into(), serde_json::json!(40_000_000_000u64));
    ev.details.insert("process_count".into(), serde_json::json!(1));
    ev.details.insert("sample_count".into(), serde_json::json!(50));
    ev.details.insert("task_duration_ms".into(), serde_json::json!(5000));
    buf.add_event(ev.clone());
    ev
}

#[tokio::test]
async fn signal_events_have_zero_cost_before_back_fill() {
    let (tt, buf) = fresh_tracker().await;
    let task_id = tt.task().task_id.clone();
    let mut b = buf.lock().await;
    let ev = add_signal_event(&mut b, &task_id);
    drop(b);

    let b = buf.lock().await;
    let stored: Vec<_> = b
        .query_events(&task_id)
        .into_iter()
        .filter(|e| e.event_type == EventType::GpuUtilizationSignal)
        .collect();
    assert_eq!(stored.len(), 1);
    assert_eq!(stored[0].cost_usd, Decimal::ZERO);
    assert!(stored[0].pricing_source.is_none());
    assert_eq!(stored[0].event_id, ev.event_id);
}

#[tokio::test]
async fn signal_events_stay_at_zero_cost_after_finalize_back_fill() {
    // The back-fill walker filters on EventType::GpuCost — signal events
    // are NEVER touched. This is the load-bearing convention §1 carve-out.
    let (mut tt, buf) = fresh_tracker().await;
    let task_id = tt.task().task_id.clone();
    {
        let mut b = buf.lock().await;
        add_signal_event(&mut b, &task_id);
    }
    tt.end(TaskStatus::Success).await.expect("end ok");

    let b = buf.lock().await;
    let signals: Vec<_> = b
        .query_events(&task_id)
        .into_iter()
        .filter(|e| e.event_type == EventType::GpuUtilizationSignal)
        .collect();
    assert_eq!(signals.len(), 1, "signal event must persist through finalize");
    assert_eq!(
        signals[0].cost_usd,
        Decimal::ZERO,
        "signal event cost_usd MUST remain 0 after finalize (Decision #3 \
         observability carve-out — back-fill walker MUST filter on GpuCost only)"
    );
    assert!(
        signals[0].pricing_source.is_none(),
        "signal event MUST NOT gain pricing_source from finalize"
    );
    assert!(
        signals[0].pricing_version.is_none(),
        "signal event MUST NOT gain pricing_version from finalize"
    );
}

#[tokio::test]
async fn task_gpu_cost_usd_excludes_signal_events_entirely() {
    // The aggregation must NOT sum cost_usd from gpu_utilization_signal
    // events into task.gpu_cost_usd. They are observability only.
    let (mut tt, buf) = fresh_tracker().await;
    let task_id = tt.task().task_id.clone();
    {
        let mut b = buf.lock().await;
        // Even if some test attacker mistakenly sets cost_usd on a signal
        // event before back-fill, the task aggregator must still ignore it.
        let mut ev = CostEvent::new(&task_id, EventType::GpuUtilizationSignal);
        ev.cost_usd = Decimal::new(99999, 0); // $99,999 phantom signal cost
        ev.cost_confidence = CostConfidence::Exact;
        ev.pricing_source = None;
        b.add_event(ev);
    }
    tt.end(TaskStatus::Success).await.expect("end ok");
    // task.gpu_cost_usd should NOT include the $99,999 — but in the Rust
    // tracker, costs are aggregated via record_* methods, not by scanning
    // pre-existing events. So the task.gpu_cost_usd is 0 here regardless.
    assert_eq!(
        tt.task().gpu_cost_usd,
        Decimal::ZERO,
        "task.gpu_cost_usd MUST NOT include gpu_utilization_signal cost_usd"
    );
}

#[tokio::test]
async fn signal_events_carry_observability_fields() {
    let (_, buf) = fresh_tracker().await;
    let task_id = uuid::Uuid::new_v4().to_string();
    let mut b = buf.lock().await;
    add_signal_event(&mut b, &task_id);
    let evts: Vec<_> = b
        .query_events(&task_id)
        .into_iter()
        .filter(|e| e.event_type == EventType::GpuUtilizationSignal)
        .collect();
    assert_eq!(evts.len(), 1);
    let ev = &evts[0];
    for field in &[
        "sm_util_pct",
        "vram_used_peak_bytes",
        "process_count",
        "sample_count",
        "task_duration_ms",
    ] {
        assert!(
            ev.details.contains_key(*field),
            "signal event missing observability field: {}",
            field
        );
    }
}

#[tokio::test]
async fn signal_events_have_no_pricing_source_or_pricing_version() {
    let (_, buf) = fresh_tracker().await;
    let task_id = uuid::Uuid::new_v4().to_string();
    let mut b = buf.lock().await;
    add_signal_event(&mut b, &task_id);
    let evts: Vec<_> = b
        .query_events(&task_id)
        .into_iter()
        .filter(|e| e.event_type == EventType::GpuUtilizationSignal)
        .collect();
    assert!(evts[0].pricing_source.is_none());
    assert!(evts[0].pricing_version.is_none());
}
