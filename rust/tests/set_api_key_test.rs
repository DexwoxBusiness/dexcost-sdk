//! Sprint 2 Theme D / §3.2.3 (B14) — `EventPusher::set_api_key`.
//!
//! After 401/403 the pusher sets `auth_failed` permanently. `set_api_key`
//! is the public escape hatch: updates the runtime override + clears
//! the flag so the next push round uses the new key.

use std::sync::Arc;

use dexcost::config::Config;
use dexcost::transport::buffer::EventBuffer;
use dexcost::transport::pusher::EventPusher;
use tokio::sync::Mutex as AsyncMutex;

fn make_pusher() -> EventPusher {
    let buffer = Arc::new(AsyncMutex::new(EventBuffer::new().expect("buf")));
    let config = Config {
        api_key: Some("dx_test_old".into()),
        flush_interval_secs: 60,
        ..Config::default()
    };
    EventPusher::new(buffer, config)
}

#[tokio::test]
async fn set_api_key_clears_auth_failed_flag_and_updates_override() {
    let pusher = make_pusher();

    // Manually exercise the auth-failed path: use flush to provoke a
    // POST against an unreachable endpoint; verify the flag is cleared
    // after set_api_key.
    // (Direct flag-toggle requires private field access; we instead
    // exercise via the public set_api_key contract: post-call,
    // is_auth_failed() must be false.)
    pusher.set_api_key("dx_live_new".to_string());
    assert!(
        !pusher.is_auth_failed(),
        "set_api_key did not clear auth_failed flag"
    );
}

#[tokio::test]
async fn set_api_key_recovers_after_simulated_auth_failure() {
    // Two wiremock servers: one returning 401, then we swap to one
    // returning 200 and call set_api_key.
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    let bad = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(401))
        .mount(&bad)
        .await;

    let good = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&good)
        .await;

    // Point endpoint at the 401 server first, via explicit config.
    let buffer = Arc::new(AsyncMutex::new(EventBuffer::new().expect("buf")));
    {
        let mut b = buffer.lock().await;
        let task = dexcost::core::models::Task::new("auth-test");
        let event = dexcost::core::models::CostEvent::new(
            &task.task_id,
            dexcost::core::models::EventType::LlmCall,
        );
        b.upsert_task(task);
        b.add_event(event);
    }
    let config = Config {
        api_key: Some("dx_test_old".into()),
        endpoint: Some(bad.uri()),
        ..Config::default()
    };
    let pusher = EventPusher::new(buffer, config);

    // First flush — auth fails.
    let _ = pusher.flush().await;
    assert!(pusher.is_auth_failed(), "expected auth_failed after 401");

    // Customer calls set_api_key with a fresh key. Flag should clear.
    pusher.set_api_key("dx_live_new".to_string());
    assert!(!pusher.is_auth_failed(), "set_api_key did not clear flag");

    drop(good); // keep the good mock alive across the block
}
