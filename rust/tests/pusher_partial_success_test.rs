//! Sprint 2 Theme D / §3.2.1 (B12) — Rust pusher partial-success accounting.
//!
//! Pre-fix `push_with_split` propagated any sibling-half error up via
//! the `?` operator, and the outer `push_batch` only marked events
//! synced after the recursion completed successfully. A first-half
//! POST that succeeded but a second-half 5xx left the first-half
//! events pending → duplicated on the next tick.
//!
//! Post-fix the leaf POST inside `push_with_split` marks synced
//! immediately, so a sibling failure cannot unwind successful work.

use std::sync::Arc;

use dexcost::config::Config;
use dexcost::core::models::{CostEvent, EventType, Task};
use dexcost::transport::buffer::EventBuffer;
use dexcost::transport::pusher::EventPusher;
use tokio::sync::Mutex as AsyncMutex;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn fast_flush_config(endpoint: &str) -> Config {
    Config {
        api_key: Some("dx_test_abc".into()),
        endpoint: Some(endpoint.to_string()),
        flush_interval_secs: 60,
        batch_size: 1000,
        ..Config::default()
    }
}

#[tokio::test]
async fn first_half_events_marked_synced_when_second_half_fails() {
    // Mock server: first POST 200, every subsequent POST 500. The
    // pusher splits a large batch into halves; first leaf POST
    // succeeds, second fails.
    let server = MockServer::start().await;
    // First call → 200 (insertion order honoured by wiremock).
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string(r#"{"queued":50}"#))
        .up_to_n_times(1)
        .mount(&server)
        .await;
    // Subsequent calls → 500.
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(500).set_body_string("boom"))
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().expect("buffer");
    let task = Task::new("partial-fail");
    // Attribution v2 intentionally drops arbitrary details, so volume—not
    // padding—must force the split. Keep this event-only to exercise sibling
    // event leaves without the task-first dependency request.
    for _ in 0..1000 {
        let ev = CostEvent::new(&task.task_id, EventType::LlmCall);
        buf.add_event(ev);
    }

    let initial_pending = buf.pending_count();
    assert_eq!(initial_pending, 1000);

    let buffer = Arc::new(AsyncMutex::new(buf));
    let pusher = EventPusher::new(buffer.clone(), fast_flush_config(&server.uri()));

    // flush returns Err because the second half failed — that's
    // expected. The KEY invariant: not all events should still be
    // pending.
    let _ = pusher.flush().await;

    let buf = buffer.lock().await;
    let remaining = buf.pending_count();
    assert!(
        remaining < 1000,
        "B12 regression: ALL {} events still pending after partial \
         failure; first-half POST succeeded but was not marked synced",
        remaining,
    );
    assert!(
        remaining > 0,
        "second-half failure was silently swallowed; expected some \
         events still pending"
    );
    eprintln!(
        "partial-failure: {}/1000 events pending after flush",
        remaining
    );
}
