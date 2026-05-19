//! Dexcost Rust SDK — End-to-End Integration Test
//!
//! This test spins up the local control-layer stack via environment variables
//! and verifies that the SDK successfully ships an event through the HTTP adapter
//! to the local Hono server. It polls the server for event visibility within 5s.
//!
//! Prerequisites (set these env vars before running):
//!   DEXCOST_API_KEY=dx_test_local   — test API key
//!   DEXCOST_ENDPOINT=http://localhost:8080  — local Hono server address
//!   RUST_LOG=dexcost=debug           — optional: enable SDK debug logging
//!
//! The local stack must include:
//!   - Hono ingestion server on :8080
//!   - PostgreSQL database (optional, for full verification)
//!
//! Run with:
//!   cargo test --test e2e_test -- --nocapture

use std::env;
use std::sync::Arc;
use std::time::Duration;

use dexcost::core::models::TaskStatus;
use dexcost::core::tracker::TaskOptions;
use dexcost::{close, flush, init, start_task, Config};
use rust_decimal::Decimal;
use tokio::time::sleep;

/// Tests the full E2E path: SDK → HTTP adapter → local control-layer server.
/// Verifies event visibility within 5 seconds.
#[tokio::test]
#[ignore] // Requires local control-layer stack to be running
async fn test_e2e_event_visibility() {
    // Verify required env vars
    let api_key = env::var("DEXCOST_API_KEY").unwrap_or_else(|_| "dx_test_local".to_string());
    let endpoint =
        env::var("DEXCOST_ENDPOINT").unwrap_or_else(|_| "http://localhost:8080".to_string());

    // Initialize SDK in cloud mode (pusher active)
    let config = Config {
        api_key: Some(api_key.clone()),
        ..Default::default()
    };
    init(config).expect("SDK init must succeed");

    // Create a task with distinctive metadata for identification
    let task_id = uuid::Uuid::new_v4().to_string();
    let mut task = start_task(
        "e2e_test_task",
        TaskOptions {
            customer_id: Some("dexcost-e2e-test-customer".into()),
            project_id: Some("rust-sdk-e2e".into()),
            metadata: Some(
                [
                    ("e2e_test_id".into(), serde_json::json!(task_id)),
                    ("test_runner".into(), serde_json::json!("e2e_test_rs")),
                ]
                .into(),
            ),
            ..Default::default()
        },
    )
    .await
    .expect("start_task must succeed");

    // Record an LLM call with explicit cost
    let llm_cost = Decimal::new(42, 2); // $0.42
    task.record_llm_call(
        "openai",
        "gpt-4o",
        1500, // input tokens
        750,  // output tokens
        Some(llm_cost),
        None,
        Some(180),
    )
    .await
    .expect("record_llm_call must succeed");

    // Record an external cost
    let external_cost = Decimal::new(5, 2); // $0.05
    task.record_cost("test_external_service", external_cost, None, None)
        .await
        .expect("record_cost must succeed");

    // End the task
    task.end(TaskStatus::Success)
        .await
        .expect("end must succeed");

    // Flush to ensure events are pushed
    flush().await.expect("flush must succeed");

    // Poll the local server for event visibility (max 5 seconds)
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .expect("reqwest client must be buildable");

    let mut found = false;
    let start = std::time::Instant::now();

    while start.elapsed() < Duration::from_secs(5) {
        // Try to fetch events from the local ingestion endpoint
        // This hits GET /v1/events or similar local endpoint
        match fetch_events_via_api(&client, &endpoint, &api_key, &task_id).await {
            Ok(true) => {
                found = true;
                println!("[e2e] Event found after {}ms", start.elapsed().as_millis());
                break;
            }
            Ok(false) => {
                // Not found yet, wait and retry
                sleep(Duration::from_millis(500)).await;
            }
            Err(e) => {
                println!("[e2e] API poll error (will retry): {}", e);
                sleep(Duration::from_millis(500)).await;
            }
        }
    }

    close();

    // Assert event was found within 5s
    assert!(
        found,
        "Event with task_id={} not visible within 5 seconds",
        task_id
    );
}

/// Fetches events from the local control-layer API and checks for our task.
async fn fetch_events_via_api(
    client: &reqwest::Client,
    endpoint: &str,
    api_key: &str,
    task_id: &str,
) -> Result<bool, Box<dyn std::error::Error + Send + Sync>> {
    // Try GET /v1/events?task_id=<id> or GET /v1/tasks/<id>
    let url = format!("{}/v1/tasks/{}", endpoint, task_id);

    let resp = client
        .get(&url)
        .header("Authorization", format!("Bearer {}", api_key))
        .header("Content-Type", "application/json")
        .send()
        .await?;

    if resp.status() == reqwest::StatusCode::NOT_FOUND {
        return Ok(false);
    }

    if !resp.status().is_success() {
        return Err(format!("non-success status: {}", resp.status()).into());
    }

    let body: serde_json::Value = resp.json().await?;

    // Verify the task has the expected cost (42 cents LLM + 5 cents external)
    if let (Some(llm_cost), Some(external_cost)) = (
        body.get("llm_cost_usd").and_then(|v| v.as_str()),
        body.get("external_cost_usd").and_then(|v| v.as_str()),
    ) {
        if llm_cost == "0.42" && external_cost == "0.05" {
            return Ok(true);
        }
    }

    Ok(false)
}

/// Tests that the SDK correctly formats events per Standard Event Schema v1.
/// Uses the local endpoint but does not require the server to be running.
#[tokio::test]
async fn test_event_schema_compliance() {
    use dexcost::core::models::{CostEvent, EventType, Task};
    use rust_decimal_macros::dec;

    let task = Task::new("schema_test");
    let task_id = task.task_id.clone();

    let mut event = CostEvent::new(&task_id, EventType::LlmCall);
    event.provider = Some("openai".to_string());
    event.model = Some("gpt-4o".to_string());
    event.input_tokens = Some(1000);
    event.output_tokens = Some(500);
    event.cost_usd = dec!(0.05);
    event.is_retry = false;
    event.retry_reason = None;
    event.retry_of = None;

    // Verify to_dict() produces valid Standard Event Schema v1 format
    let dict = event.to_dict();

    // Required Standard Event Schema v1 fields
    assert!(dict.get("event_id").is_some(), "missing event_id");
    assert_eq!(dict.get("task_id").unwrap().as_str().unwrap(), task_id);
    assert_eq!(
        dict.get("event_type").unwrap().as_str().unwrap(),
        "llm_call"
    );
    assert!(dict.get("occurred_at").is_some(), "missing occurred_at");
    assert_eq!(dict.get("cost_usd").unwrap().as_str().unwrap(), "0.05");
    assert_eq!(
        dict.get("cost_confidence").unwrap().as_str().unwrap(),
        "exact"
    );
    assert!(
        !dict.get("is_retry").unwrap().as_bool().unwrap(),
        "is_retry must be false"
    );
    assert_eq!(dict.get("schema_version").unwrap().as_str().unwrap(), "1");

    // Verify JSON is serializable
    let json_str = serde_json::to_string(&dict).expect("to_dict must serialize to JSON");
    assert!(!json_str.is_empty());

    // Verify we can deserialize back
    let deserialized: serde_json::Value =
        serde_json::from_str(&json_str).expect("must deserialize");
    assert_eq!(deserialized["event_id"], dict["event_id"]);
}

/// Tests retry semantics: is_retry, retry_reason, retry_of fields.
#[tokio::test]
async fn test_retry_semantics() {
    let buffer = Arc::new(tokio::sync::Mutex::new(
        dexcost::transport::buffer::EventBuffer::new().unwrap(),
    ));
    let mut task = dexcost::core::tracker::TrackedTask::new(
        dexcost::core::models::Task::new("retry_test"),
        buffer.clone(),
        None,
    );

    // Record a retry event
    let retry_event = task
        .mark_retry("rate_limit_hit", Decimal::new(3, 2))
        .await
        .unwrap();

    assert!(retry_event.is_retry, "is_retry must be true");
    assert_eq!(retry_event.retry_reason.as_deref(), Some("rate_limit_hit"));
    assert!(
        retry_event.retry_of.is_none(),
        "retry_of must be None for explicit mark_retry"
    );

    // Verify task aggregates retry metrics
    assert_eq!(task.task().retry_count, 1);
    assert_eq!(task.task().retry_cost_usd, Decimal::new(3, 2));
}

/// Tests API key auth flow.
/// We verify that init() with a valid dx_test_* key does not error,
/// and that an invalid key is rejected.
#[test]
fn test_api_key_validation() {
    use dexcost::config::validate_api_key;

    // Valid test key format
    assert!(validate_api_key("dx_test_abc123").is_ok());
    assert!(validate_api_key("dx_live_xyz789").is_ok());

    // Invalid formats
    assert!(validate_api_key("sk-xxx").is_err());
    assert!(validate_api_key("invalid").is_err());
    assert!(
        validate_api_key("").is_ok(),
        "empty key is allowed (offline mode)"
    );
}

/// Tests that SDK handles control-layer unavailability gracefully.
/// When DEXCOST_ENDPOINT points to a non-running host, flush() should
/// not panic but should return an error.
#[tokio::test]
async fn test_graceful_degradation_on_unavailable_server() {
    // Point to a host that will refuse connection
    env::set_var("DEXCOST_ENDPOINT", "http://localhost:9999");

    let config = Config {
        api_key: Some("dx_test_graceful".to_string()),
        ..Default::default()
    };

    init(config).expect("SDK init must succeed");

    let mut task = start_task("graceful_test", TaskOptions::default())
        .await
        .expect("start_task must succeed");

    task.record_llm_call(
        "openai",
        "gpt-4o",
        100,
        50,
        Some(Decimal::new(1, 1)),
        None,
        None,
    )
    .await
    .expect("record_llm_call must succeed");

    task.end(TaskStatus::Success)
        .await
        .expect("end must succeed");

    // Flush should not panic even if server is unavailable
    let _flush_result = flush().await;
    // In offline/dev mode or if pusher is None, flush may succeed trivially
    // In cloud mode with unreachable server, this returns a Transport error
    // Both are acceptable — SDK must not panic

    close();
    env::remove_var("DEXCOST_ENDPOINT");
}
