//! DEX-297 — EventPusher must include pending tasks in the ingest payload so
//! that task lifecycle changes (start_task, end_task, totals recompute)
//! propagate to the server and populate `/dashboard/tasks`.
//!
//! These tests intercept the real HTTP request via wiremock and assert on
//! the JSON body the SDK actually sends. The target mock server is passed to
//! the SDK via the explicit `Config.endpoint` field (the SDK no longer reads
//! `DEXCOST_ENDPOINT` from the environment).

use std::sync::Arc;

use chrono::{Duration as ChronoDuration, Utc};
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
        batch_size: 100,
        ..Config::default()
    }
}

#[tokio::test]
async fn flush_includes_non_empty_tasks_array_when_task_pending() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().unwrap();
    let task = Task::new("resolve_ticket");
    let event = CostEvent::new(&task.task_id, EventType::LlmCall);
    buf.upsert_task(task);
    buf.add_event(event);

    let buffer = Arc::new(AsyncMutex::new(buf));
    let pusher = EventPusher::new(buffer.clone(), fast_flush_config(&server.uri()));
    pusher.flush().await.expect("flush should succeed");

    let received = server.received_requests().await.expect("requests recorded");
    assert_eq!(received.len(), 1, "expected exactly one ingest request");

    let body: serde_json::Value = serde_json::from_slice(&received[0].body).expect("body is JSON");
    let tasks = body
        .get("tasks")
        .and_then(|v| v.as_array())
        .expect("tasks array");
    assert_eq!(tasks.len(), 1, "tasks array should not be empty");
    let events = body
        .get("events")
        .and_then(|v| v.as_array())
        .expect("events array");
    assert_eq!(events[0]["schema_version"], "2");
    assert!(events[0].get("details").is_none());
    assert!(events[0].get("cost_usd").is_none());
    assert!(tasks[0].get("total_cost_usd").is_none());
    assert!(tasks[0].get("total_input_tokens").is_none());

    // Task is now marked synced — pending_task_count drops to zero.
    let buf = buffer.lock().await;
    assert_eq!(buf.pending_task_count(), 0);
}

#[tokio::test]
async fn flush_includes_pending_tasks_even_with_no_pending_events() {
    // Covers the core DEX-297 failure mode: end_task updates a task
    // (re-upsert -> sync_status='pending') *after* its events have already
    // been flushed. The next pusher cycle has no pending events but must
    // still flush the task so the server sees the final status/totals.
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().unwrap();
    // Only a pending task — no events at all.
    let task = Task::new("generate_report");
    buf.upsert_task(task);

    let buffer = Arc::new(AsyncMutex::new(buf));
    let pusher = EventPusher::new(buffer.clone(), fast_flush_config(&server.uri()));
    pusher.flush().await.expect("flush should succeed");

    let received = server.received_requests().await.expect("requests recorded");
    assert_eq!(
        received.len(),
        1,
        "an empty events / pending tasks flush still hits the server"
    );

    let body: serde_json::Value = serde_json::from_slice(&received[0].body).expect("body is JSON");
    assert!(
        body.get("events")
            .and_then(|v| v.as_array())
            .map(|a| a.is_empty())
            .unwrap_or(false),
        "events array should be empty"
    );
    assert_eq!(
        body.get("tasks")
            .and_then(|v| v.as_array())
            .map(|a| a.len())
            .unwrap_or(0),
        1,
        "tasks array should contain the pending task"
    );

    let buf = buffer.lock().await;
    assert_eq!(buf.pending_task_count(), 0);
}

#[tokio::test]
async fn flush_skips_when_both_events_and_tasks_empty() {
    // Backward compatibility: when there is genuinely nothing to send the
    // pusher must not hit the server.
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&server)
        .await;

    let buf = EventBuffer::new().unwrap();
    let buffer = Arc::new(AsyncMutex::new(buf));
    let pusher = EventPusher::new(buffer, fast_flush_config(&server.uri()));
    pusher
        .flush()
        .await
        .expect("flush of empty buffer succeeds");

    let received = server.received_requests().await.expect("requests recorded");
    assert!(
        received.is_empty(),
        "no request should be sent for an empty buffer"
    );
}

#[tokio::test]
async fn second_flush_after_task_update_resends_task() {
    // After end_task re-upserts the task, the next flush must include it.
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().unwrap();
    let mut task = Task::new("workflow");
    let task_id = task.task_id.clone();
    buf.upsert_task(task.clone());

    let buffer = Arc::new(AsyncMutex::new(buf));
    let pusher = EventPusher::new(buffer.clone(), fast_flush_config(&server.uri()));

    // First flush — task gets sent and marked synced.
    pusher.flush().await.expect("first flush");
    {
        let buf = buffer.lock().await;
        assert_eq!(buf.pending_task_count(), 0);
    }

    // Simulate end_task: status/totals updated, re-upsert.
    task.status = dexcost::core::models::TaskStatus::Success;
    task.total_cost_usd = rust_decimal::Decimal::new(150, 2); // 1.50
    {
        let mut buf = buffer.lock().await;
        buf.upsert_task(task);
        assert_eq!(
            buf.pending_task_count(),
            1,
            "re-upsert must mark the task pending again"
        );
    }

    // Second flush — task is re-sent.
    pusher.flush().await.expect("second flush");
    // A successful sync must stay synced until another task mutation.
    pusher.flush().await.expect("third no-op flush");

    let received = server.received_requests().await.expect("requests recorded");
    assert_eq!(received.len(), 2, "task lifecycle update triggered a flush");

    let body2: serde_json::Value = serde_json::from_slice(&received[1].body).expect("body is JSON");
    let tasks = body2
        .get("tasks")
        .and_then(|v| v.as_array())
        .expect("tasks array");
    assert_eq!(tasks.len(), 1);
    assert_eq!(
        tasks[0].get("task_id").and_then(|v| v.as_str()),
        Some(task_id.as_str())
    );
}

#[tokio::test]
async fn event_redaction_happens_before_v2_field_promotion() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&server)
        .await;

    let mut event = CostEvent::new(&uuid::Uuid::new_v4().to_string(), EventType::GpuCost);
    event
        .details
        .insert("gpu_seconds_used".into(), serde_json::json!(1));
    event.details.insert(
        "request_id".into(),
        serde_json::json!("provider-secret-record"),
    );
    event
        .details
        .insert("gpu_sku".into(), serde_json::json!("secret-gpu-sku"));

    let mut buf = EventBuffer::new().unwrap();
    buf.add_event(event);
    let buffer = Arc::new(AsyncMutex::new(buf));
    let config = Config {
        api_key: Some("dx_test_abc".into()),
        endpoint: Some(server.uri()),
        redact_fields: vec!["request_id".into(), "gpu_sku".into()],
        ..Config::default()
    };
    EventPusher::new(buffer, config)
        .flush()
        .await
        .expect("flush");

    let requests = server.received_requests().await.expect("requests");
    let body_text = String::from_utf8_lossy(&requests[0].body);
    assert!(!body_text.contains("provider-secret-record"));
    assert!(!body_text.contains("secret-gpu-sku"));
    let body: serde_json::Value = serde_json::from_slice(&requests[0].body).expect("json");
    let sent = &body["events"][0];
    assert!(sent["provider"].get("record_id").is_none());
    assert!(sent.get("resource").is_none());
}

#[tokio::test]
async fn partial_ingestion_rejection_keeps_records_pending() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(
            ResponseTemplate::new(202)
                .set_body_string(r#"{"accepted":1,"rejected":1,"errors":[]}"#),
        )
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().unwrap();
    let task = Task::new("partial-rejection");
    buf.add_event(CostEvent::new(&task.task_id, EventType::LlmCall));
    buf.upsert_task(task);
    let buffer = Arc::new(AsyncMutex::new(buf));
    let pusher = EventPusher::new(buffer.clone(), fast_flush_config(&server.uri()));

    assert!(pusher.flush().await.is_err());
    let buf = buffer.lock().await;
    assert_eq!(buf.pending_count(), 1);
    assert_eq!(buf.pending_task_count(), 1);
}

#[tokio::test]
async fn invalid_v2_prefix_is_quarantined_while_valid_sibling_is_delivered() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(202).set_body_string(r#"{"accepted":1,"rejected":0}"#))
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().unwrap();
    let mut invalid = CostEvent::new("task-123", EventType::LlmCall);
    let mut second_invalid = CostEvent::new("task-456", EventType::LlmCall);
    let mut valid = CostEvent::new("11111111-1111-4111-8111-111111111111", EventType::LlmCall);
    invalid.occurred_at = Utc::now() - ChronoDuration::minutes(1);
    second_invalid.occurred_at = invalid.occurred_at + ChronoDuration::seconds(1);
    valid.occurred_at = second_invalid.occurred_at + ChronoDuration::seconds(1);
    let invalid_id = invalid.event_id.clone();
    let second_invalid_id = second_invalid.event_id.clone();
    buf.add_event(invalid);
    buf.add_event(second_invalid);
    buf.add_event(valid);

    let buffer = Arc::new(AsyncMutex::new(buf));
    let mut config = fast_flush_config(&server.uri());
    config.batch_size = 2;
    let pusher = EventPusher::new(buffer.clone(), config);
    let error = pusher
        .flush()
        .await
        .expect_err("invalid event must surface");
    assert!(error.to_string().contains("were quarantined"));

    let buf = buffer.lock().await;
    assert_eq!(buf.pending_count(), 0);
    let quarantined = buf.get_quarantined_events(10);
    assert_eq!(quarantined.len(), 2);
    assert_eq!(quarantined[0].event_id, invalid_id);
    assert_eq!(quarantined[1].event_id, second_invalid_id);
    drop(buf);

    let requests = server.received_requests().await.expect("requests");
    assert_eq!(requests.len(), 1);
    let body: serde_json::Value = serde_json::from_slice(&requests[0].body).expect("json");
    assert_eq!(body["events"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn observability_only_gpu_signal_is_acknowledged_without_upload() {
    let server = MockServer::start().await;
    let mut buf = EventBuffer::new().unwrap();
    buf.add_event(CostEvent::new(
        "11111111-1111-4111-8111-111111111111",
        EventType::GpuUtilizationSignal,
    ));
    let buffer = Arc::new(AsyncMutex::new(buf));
    EventPusher::new(buffer.clone(), fast_flush_config(&server.uri()))
        .flush()
        .await
        .expect("signal-only flush");

    assert_eq!(buffer.lock().await.pending_count(), 0);
    assert!(server
        .received_requests()
        .await
        .expect("requests")
        .is_empty());
}

// Fix 4 — task metadata must be redacted, and customer_id / project_id must
// be hashed, before the task is POSTed. Previously `Task` objects were
// serialized raw, leaking PII even when redaction / hashing were configured.
#[tokio::test]
async fn flush_redacts_and_hashes_task_metadata_before_post() {
    use dexcost::hash_value;

    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().unwrap();

    // A pending task carrying PII in attribution fields and metadata.
    let mut task = Task::new("resolve_ticket");
    task.customer_id = Some("acme-corp".to_string());
    task.project_id = Some("support-eu".to_string());
    task.metadata.insert(
        "email".to_string(),
        serde_json::Value::String("alice@example.com".to_string()),
    );
    task.metadata.insert(
        "region".to_string(),
        serde_json::Value::String("eu-west-1".to_string()),
    );
    // Nested object — redaction must reach matching keys at any depth.
    task.metadata.insert(
        "context".to_string(),
        serde_json::json!({ "email": "bob@example.com", "tier": "gold" }),
    );
    buf.upsert_task(task);

    let buffer = Arc::new(AsyncMutex::new(buf));

    let config = Config {
        api_key: Some("dx_test_abc".into()),
        endpoint: Some(server.uri()),
        flush_interval_secs: 60,
        batch_size: 100,
        redact_fields: vec!["email".to_string()],
        hash_customer_id: true,
        ..Config::default()
    };
    let pusher = EventPusher::new(buffer.clone(), config);
    pusher.flush().await.expect("flush should succeed");

    let received = server.received_requests().await.expect("requests recorded");
    assert_eq!(received.len(), 1, "expected exactly one ingest request");

    let body: serde_json::Value = serde_json::from_slice(&received[0].body).expect("body is JSON");
    let tasks = body
        .get("tasks")
        .and_then(|v| v.as_array())
        .expect("tasks array");
    assert_eq!(tasks.len(), 1, "the pending task must be sent");
    let sent = &tasks[0];

    // customer_id / project_id must be SHA-256 hashed, not raw.
    assert_eq!(
        sent.get("customer_id").and_then(|v| v.as_str()),
        Some(hash_value("acme-corp").as_str()),
        "customer_id must be hashed in the POST body"
    );
    assert_eq!(
        sent.get("project_id").and_then(|v| v.as_str()),
        Some(hash_value("support-eu").as_str()),
        "project_id must be hashed in the POST body"
    );
    // Raw values must not leak.
    let body_text = String::from_utf8_lossy(&received[0].body);
    assert!(
        !body_text.contains("acme-corp"),
        "raw customer_id leaked: {body_text}"
    );
    assert!(
        !body_text.contains("support-eu"),
        "raw project_id leaked: {body_text}"
    );

    // Redacted field must be dropped from metadata, top-level and nested.
    let meta = sent
        .get("metadata")
        .and_then(|v| v.as_object())
        .expect("metadata");
    assert!(
        !meta.contains_key("email"),
        "top-level `email` must be redacted"
    );
    assert_eq!(
        meta.get("region").and_then(|v| v.as_str()),
        Some("eu-west-1"),
        "non-sensitive fields must be preserved"
    );
    let ctx = meta
        .get("context")
        .and_then(|v| v.as_object())
        .expect("nested context object");
    assert!(
        !ctx.contains_key("email"),
        "nested `email` must be redacted too"
    );
    assert_eq!(ctx.get("tier").and_then(|v| v.as_str()), Some("gold"));
    assert!(
        !body_text.contains("alice@example.com") && !body_text.contains("bob@example.com"),
        "redacted email values leaked: {body_text}"
    );
}

// Fix 4 — when redaction / hashing are NOT configured, task data passes
// through unchanged (no accidental mutation).
#[tokio::test]
async fn flush_leaves_task_metadata_intact_when_unconfigured() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().unwrap();
    let mut task = Task::new("resolve_ticket");
    task.customer_id = Some("acme-corp".to_string());
    task.metadata.insert(
        "email".to_string(),
        serde_json::Value::String("alice@example.com".to_string()),
    );
    buf.upsert_task(task);

    let buffer = Arc::new(AsyncMutex::new(buf));
    // fast_flush_config has no redact_fields and hash_customer_id = false.
    let pusher = EventPusher::new(buffer.clone(), fast_flush_config(&server.uri()));
    pusher.flush().await.expect("flush should succeed");

    let received = server.received_requests().await.expect("requests recorded");
    let body: serde_json::Value = serde_json::from_slice(&received[0].body).expect("body is JSON");
    let sent = &body.get("tasks").and_then(|v| v.as_array()).expect("tasks")[0];

    assert_eq!(
        sent.get("customer_id").and_then(|v| v.as_str()),
        Some("acme-corp"),
        "customer_id must be untouched when hashing is disabled"
    );
    let meta = sent
        .get("metadata")
        .and_then(|v| v.as_object())
        .expect("metadata");
    assert_eq!(
        meta.get("email").and_then(|v| v.as_str()),
        Some("alice@example.com"),
        "metadata must be untouched when no redact_fields are configured"
    );
}

// Gap 5 — a rejected API key (HTTP 401/403) permanently stops the pusher.
#[tokio::test]
async fn flush_stops_pusher_permanently_on_401() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/ingest"))
        .respond_with(ResponseTemplate::new(401).set_body_string("unauthorized"))
        .mount(&server)
        .await;

    let mut buf = EventBuffer::new().unwrap();
    let task = Task::new("resolve_ticket");
    let event = CostEvent::new(&task.task_id, EventType::LlmCall);
    buf.upsert_task(task);
    buf.add_event(event);

    let buffer = Arc::new(AsyncMutex::new(buf));
    let pusher = EventPusher::new(buffer.clone(), fast_flush_config(&server.uri()));

    // First flush hits the server and is rejected with 401.
    let first = pusher.flush().await;
    assert!(first.is_err(), "401 must surface as an error");
    assert!(pusher.is_auth_failed(), "auth_failed flag must be set");

    // A second flush is a no-op — it must NOT hit the server again.
    let _ = pusher.flush().await;
    let received = server.received_requests().await.expect("requests recorded");
    assert_eq!(
        received.len(),
        1,
        "pusher must not retry the rejected key after 401"
    );
}
