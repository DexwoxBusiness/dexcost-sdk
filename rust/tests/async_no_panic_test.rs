//! B4 regression tests — Sprint 1 Theme B (crash prevention).
//!
//! Assert the sync pricing API does not panic when called from inside a
//! Tokio runtime. Pre-fix, `tokio::sync::RwLock::blocking_read` panics.
//! The 2.2.6 (cloud_detect poison) companion test lives as a `#[cfg(test)]`
//! unit test inside `src/cloud_detect.rs` so it can touch the internal
//! `RESULT` lock directly — see
//! `cloud_detect::tests::get_cloud_env_recovers_from_poisoned_lock`.

use dexcost::pricing::engine::PricingEngine;

/// B4: calling the sync pricing API from an async runtime must not panic.
///
/// Fix path per remediation plan §2.2.1: swap `tokio::sync::RwLock` for a
/// non-runtime-aware lock (parking_lot) so the sync API does not require
/// suspending the current task.
#[tokio::test]
async fn pricing_get_cost_sync_does_not_panic_in_async_context() {
    let engine = PricingEngine::new();
    let _result = engine.get_cost_sync("gpt-4o", 500, 200, 0, 0);
}

/// B4 variant — single-threaded runtime is a separate panic class on
/// `blocking_read` (the runtime has nothing to schedule onto).
#[tokio::test(flavor = "current_thread")]
async fn pricing_get_cost_sync_does_not_panic_on_current_thread_runtime() {
    let engine = PricingEngine::new();
    let _result = engine.get_cost_sync("gpt-4o", 500, 200, 0, 0);
}

/// B4 — write path counterpart. `set_custom_pricing_sync` also uses
/// `blocking_write` and must survive an async caller.
#[tokio::test]
async fn pricing_set_custom_sync_does_not_panic_in_async_context() {
    use rust_decimal::Decimal;

    let engine = PricingEngine::new();
    engine.set_custom_pricing_sync(
        "my-custom-model",
        Decimal::new(1, 3),
        Decimal::new(2, 3),
    );
}

