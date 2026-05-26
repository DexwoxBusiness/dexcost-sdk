//! Sprint 2 Theme C / §3.1.3 Fix 5 — `total_cost_usd` clobber.
//!
//! Three sites in `core/tracker.rs` recomputed `total_cost_usd` from
//! only 3 subsystems (llm + external + compute), wiping any previously-
//! aggregated network and gpu cost. A subsequent `record_cost` /
//! `record_usage` / LLM call after network or GPU finalize silently
//! dropped those subsystems from the per-task total.
//!
//! Per `dexcost-task.v1.json` the canonical total is the 5-subsystem
//! sum: llm + external + compute + network + gpu.

use std::sync::Arc;

use rust_decimal::Decimal;
use tokio::sync::Mutex;

use dexcost::core::models::Task;
use dexcost::core::tracker::TrackedTask;
use dexcost::pricing::engine::PricingEngine;
use dexcost::transport::buffer::EventBuffer;

fn make_tt() -> TrackedTask {
    let task = Task::new("clobber-test");
    let buffer = Arc::new(Mutex::new(EventBuffer::new().expect("buffer")));
    let pricing = Arc::new(Mutex::new(PricingEngine::new()));
    TrackedTask::new(task, buffer, Some(pricing))
}

#[tokio::test]
async fn record_cost_does_not_clobber_network_cost() {
    let mut tt = make_tt();
    // Simulate a prior network-finalize that set both network_cost_usd
    // and total_cost_usd to include it.
    {
        let t = tt.task_mut_for_tests();
        t.network_cost_usd = Decimal::new(125, 4); // 0.0125
        t.total_cost_usd = t.network_cost_usd;
    }

    // Now record an external cost.
    tt.record_cost("test-service", Decimal::new(5, 3), None, None)
        .await
        .expect("record_cost");

    // total_cost_usd must STILL include network. Pre-fix: 0.005
    // (clobbered to 3-subsystem sum). Post-fix: 0.0125 + 0.005 = 0.0175.
    let total = tt.task().total_cost_usd;
    assert_eq!(
        total,
        Decimal::new(175, 4),
        "expected total=0.0175 (0.0125 network + 0.005 external), got {}",
        total,
    );
}

#[tokio::test]
async fn record_cost_does_not_clobber_gpu_cost() {
    let mut tt = make_tt();
    {
        let t = tt.task_mut_for_tests();
        t.gpu_cost_usd = Decimal::new(50, 4); // 0.005
        t.total_cost_usd = t.gpu_cost_usd;
    }

    tt.record_cost("another-service", Decimal::new(10, 4), None, None)
        .await
        .expect("record_cost");

    let total = tt.task().total_cost_usd;
    assert_eq!(
        total,
        Decimal::new(60, 4),
        "expected total=0.006 (0.005 gpu + 0.001 external), got {}",
        total,
    );
}
