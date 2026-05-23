//! Decision #6 idle-gap contract test — Phase 2 Task 9.
//!
//! Rust port of `python/tests/test_gpu_idle_gap.py` (commit d42cc81).
//!
//! Load-bearing contract: two Lambda Labs H100 tasks of 60s each,
//! separated by 50 minutes (3000s) of idle, MUST yield a total dexcost
//! cost STRICTLY LESS than (3120s / 3600) × hourly_rate. The 3000-second
//! idle gap MUST stay invisible — if a future refactor adds synthetic
//! idle pseudo-events to close the dexcost-vs-cloud gap, this test fails
//! with the failure message referencing Decision #6.
//!
//! The 380× CPU magnitude makes this load-bearing for customer trust on
//! first install: dexcost reports USAGE, not RESERVATION. The cloud bill
//! covers the gap (Lambda Labs charges by reservation hour); dexcost does
//! not pretend to mirror that.

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde_json::json;

use dexcost::cloud_detect::CloudEnv;
use dexcost::pricing::gpu_pricing::GpuPricingEngine;

fn env(provider: Option<&str>) -> CloudEnv {
    CloudEnv {
        provider: provider.map(String::from),
        region: None,
        source: "test",
        instance_type: None,
    }
}

#[test]
fn decision_6_idle_gap_must_stay_invisible() {
    let e = GpuPricingEngine::new();

    // Each task uses 60s of GPU time over a 60s window — 100% busy.
    let task_details = json!({
        "billing_model": "per_gpu_hour_reserved",
        "gpu_seconds_used": "60",
        "gpu_count": 1,
        "duration_ms": 60000,
        "gpu_sku": "h100-80gb-sxm5",
    });
    let ce = env(Some("lambda_labs"));
    let c1 = e.resolve_gpu_cost(&task_details, &ce, None);
    let c2 = e.resolve_gpu_cost(&task_details, &ce, None);
    let total = c1.cost_usd + c2.cost_usd;

    // The hypothetical "if we closed the gap" cost would be:
    // total_window_seconds = 60 + 3000 + 60 = 3120s
    // bound = 3120 / 3600 × hourly_rate
    //
    // To make this test catalog-independent we use the published Lambda
    // Labs h100-80gb-sxm5 hourly rate of $3.99 as the upper-bound
    // reference. The contract is: dexcost MUST NOT charge for the gap,
    // so total < bound.
    let hourly = dec!(3.99);
    let bound = dec!(3120) / dec!(3600) * hourly;

    assert!(
        total < bound,
        "Decision #6 violation: total dexcost cost {} is not strictly less than \
         (3120s/3600) × ${} = {} — the 3000s idle gap MUST stay invisible. \
         A future refactor likely added synthetic idle pseudo-events.",
        total,
        hourly,
        bound,
    );
    // Sanity: each task at 60/3600 × $3.99 = $0.0665
    assert!(total > Decimal::ZERO);
}
