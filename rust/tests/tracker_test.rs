use std::sync::Arc;

use dexcost::core::heuristics::RetryHeuristicEngine;
use dexcost::core::models::{CostConfidence, EventType, PricingSource, Task, TaskStatus};
use dexcost::core::tracker::TrackedTask;
use dexcost::pricing::rates::RateRegistry;
use dexcost::transport::buffer::EventBuffer;
use rust_decimal::Decimal;
use tokio::sync::Mutex;

fn make_tracked_task() -> TrackedTask {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let task = Task::new("test_task");
    TrackedTask::new(task, buffer, None)
}

fn make_tracked_task_with_registry(registry: Arc<Mutex<RateRegistry>>) -> TrackedTask {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let task = Task::new("test_task");
    TrackedTask::with_rate_registry(task, buffer, None, Some(registry))
}

#[allow(dead_code)]
fn make_tracked_task_with_heuristics(window: f64, threshold: f64) -> TrackedTask {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let task = Task::new("test_task");
    let engine = Arc::new(std::sync::Mutex::new(
        RetryHeuristicEngine::new(window, threshold).unwrap(),
    ));
    TrackedTask::with_heuristics(task, buffer, None, None, engine)
}

#[tokio::test]
async fn test_record_llm_call() {
    let mut tt = make_tracked_task();
    let cost = Decimal::new(5, 2); // 0.05

    let event = tt
        .record_llm_call("openai", "gpt-4o", 1000, 500, Some(cost), None, None)
        .await
        .unwrap();

    assert_eq!(event.event_type, EventType::LlmCall);
    assert_eq!(event.provider, Some("openai".to_string()));
    assert_eq!(event.model, Some("gpt-4o".to_string()));
    assert_eq!(event.cost_usd, cost);
    assert_eq!(event.input_tokens, Some(1000));
    assert_eq!(event.output_tokens, Some(500));
}

#[tokio::test]
async fn test_record_llm_call_with_cached_tokens() {
    let mut tt = make_tracked_task();
    let cost = Decimal::new(3, 2); // 0.03

    let event = tt
        .record_llm_call("openai", "gpt-4o", 1000, 500, Some(cost), Some(200), None)
        .await
        .unwrap();

    assert_eq!(event.cached_tokens, Some(200));
    assert_eq!(tt.task().total_cached_tokens, 200);
}

#[tokio::test]
async fn test_record_llm_call_with_latency() {
    let mut tt = make_tracked_task();
    let cost = Decimal::new(5, 2);

    let event = tt
        .record_llm_call("openai", "gpt-4o", 1000, 500, Some(cost), None, Some(150))
        .await
        .unwrap();

    assert_eq!(event.latency_ms, Some(150));
}

#[tokio::test]
async fn test_record_multiple_llm_calls_aggregation() {
    let mut tt = make_tracked_task();

    tt.record_llm_call(
        "openai",
        "gpt-4o",
        1000,
        500,
        Some(Decimal::new(5, 2)),
        None,
        None,
    )
    .await
    .unwrap();

    tt.record_llm_call(
        "anthropic",
        "claude-3.5-sonnet",
        2000,
        1000,
        Some(Decimal::new(10, 2)),
        None,
        None,
    )
    .await
    .unwrap();

    assert_eq!(tt.task().llm_cost_usd, Decimal::new(15, 2));
    assert_eq!(tt.task().total_input_tokens, 3000);
    assert_eq!(tt.task().total_output_tokens, 1500);
    assert_eq!(tt.task().total_cost_usd, Decimal::new(15, 2));
}

#[tokio::test]
async fn test_record_cost() {
    let mut tt = make_tracked_task();
    let cost = Decimal::new(1, 2); // 0.01

    let event = tt
        .record_cost("google_maps", cost, None, None)
        .await
        .unwrap();

    assert_eq!(event.event_type, EventType::ExternalCost);
    assert_eq!(event.service_name, Some("google_maps".to_string()));
    assert_eq!(event.cost_usd, cost);
    assert_eq!(tt.task().external_cost_usd, cost);
    assert_eq!(tt.task().total_cost_usd, cost);
}

#[tokio::test]
async fn test_record_cost_with_details() {
    let mut tt = make_tracked_task();
    let mut details = std::collections::HashMap::new();
    details.insert(
        "operation".to_string(),
        serde_json::Value::String("geocode".to_string()),
    );

    let event = tt
        .record_cost("google_maps", Decimal::new(1, 2), Some(details), None)
        .await
        .unwrap();

    assert_eq!(event.details["operation"], "geocode");
}

#[tokio::test]
async fn test_mark_retry() {
    let mut tt = make_tracked_task();
    let cost = Decimal::new(2, 2); // 0.02

    let event = tt.mark_retry("rate_limit", cost).await.unwrap();

    assert_eq!(event.event_type, EventType::RetryMarker);
    assert!(event.is_retry);
    assert_eq!(event.retry_reason, Some("rate_limit".to_string()));
    assert_eq!(event.cost_usd, cost);
    assert_eq!(tt.task().retry_count, 1);
    assert_eq!(tt.task().retry_cost_usd, cost);
}

#[tokio::test]
async fn test_mark_multiple_retries() {
    let mut tt = make_tracked_task();

    tt.mark_retry("rate_limit", Decimal::new(1, 2))
        .await
        .unwrap();
    tt.mark_retry("timeout", Decimal::new(2, 2)).await.unwrap();

    assert_eq!(tt.task().retry_count, 2);
    assert_eq!(tt.task().retry_cost_usd, Decimal::new(3, 2));
}

#[tokio::test]
async fn test_end_success() {
    let mut tt = make_tracked_task();
    tt.end(TaskStatus::Success).await.unwrap();

    assert_eq!(tt.task().status, TaskStatus::Success);
    assert!(tt.task().ended_at.is_some());
    assert_eq!(tt.task().failure_count, 0);
}

#[tokio::test]
async fn test_end_failed() {
    let mut tt = make_tracked_task();
    tt.end(TaskStatus::Failed).await.unwrap();

    assert_eq!(tt.task().status, TaskStatus::Failed);
    assert!(tt.task().ended_at.is_some());
    assert_eq!(tt.task().failure_count, 1);
}

#[tokio::test]
async fn test_end_already_ended_error() {
    let mut tt = make_tracked_task();
    tt.end(TaskStatus::Success).await.unwrap();

    let result = tt.end(TaskStatus::Failed).await;
    assert!(result.is_err());
}

#[tokio::test]
async fn test_record_after_end_error() {
    let mut tt = make_tracked_task();
    tt.end(TaskStatus::Success).await.unwrap();

    let result = tt
        .record_llm_call(
            "openai",
            "gpt-4o",
            100,
            50,
            Some(Decimal::new(1, 2)),
            None,
            None,
        )
        .await;
    assert!(result.is_err());

    let result = tt
        .record_cost("service", Decimal::new(1, 2), None, None)
        .await;
    assert!(result.is_err());

    let result = tt.mark_retry("reason", Decimal::ZERO).await;
    assert!(result.is_err());
}

#[tokio::test]
async fn test_link_trace() {
    let mut tt = make_tracked_task();
    tt.link_trace("langfuse", "trace-abc-123");

    let meta = &tt.task().metadata;
    let links = meta.get("_trace_links").unwrap();
    let arr = links.as_array().unwrap();
    assert_eq!(arr.len(), 1);
    assert_eq!(arr[0]["provider"], "langfuse");
    assert_eq!(arr[0]["trace_id"], "trace-abc-123");
}

#[tokio::test]
async fn test_events_list() {
    let mut tt = make_tracked_task();
    assert_eq!(tt.events().len(), 0);

    tt.record_llm_call(
        "openai",
        "gpt-4o",
        100,
        50,
        Some(Decimal::new(1, 2)),
        None,
        None,
    )
    .await
    .unwrap();
    tt.record_cost("svc", Decimal::new(1, 2), None, None)
        .await
        .unwrap();

    assert_eq!(tt.events().len(), 2);
}

#[tokio::test]
async fn test_mixed_cost_aggregation() {
    let mut tt = make_tracked_task();

    // LLM cost
    tt.record_llm_call(
        "openai",
        "gpt-4o",
        1000,
        500,
        Some(Decimal::new(10, 2)),
        None,
        None,
    )
    .await
    .unwrap();

    // External cost
    tt.record_cost("google_maps", Decimal::new(5, 2), None, None)
        .await
        .unwrap();

    // Retry cost
    tt.mark_retry("rate_limit", Decimal::new(2, 2))
        .await
        .unwrap();

    // Total should be llm + external (retry is tracked separately)
    assert_eq!(tt.task().llm_cost_usd, Decimal::new(10, 2));
    assert_eq!(tt.task().external_cost_usd, Decimal::new(5, 2));
    assert_eq!(tt.task().total_cost_usd, Decimal::new(15, 2));
    assert_eq!(tt.task().retry_cost_usd, Decimal::new(2, 2));
    assert_eq!(tt.task().retry_count, 1);
}

// ---------------------------------------------------------------------------
// record_usage tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_record_usage() {
    let mut registry = RateRegistry::new();
    // $0.005 per SMS
    registry.register("twilio_sms", "per_sms", "0.005".parse().unwrap());
    let registry = Arc::new(Mutex::new(registry));

    let mut tt = make_tracked_task_with_registry(registry);

    // 10 units × $0.005 = $0.05
    let event = tt.record_usage("twilio_sms", 10).await.unwrap();

    assert_eq!(event.event_type, EventType::ExternalCost);
    assert_eq!(event.service_name, Some("twilio_sms".to_string()));
    assert_eq!(event.cost_usd, "0.05".parse::<Decimal>().unwrap());
    assert_eq!(event.cost_confidence, CostConfidence::Computed);
    assert_eq!(event.pricing_source, Some(PricingSource::RateRegistry));
    assert!(event.pricing_version.is_some());
    assert_eq!(event.details["units"], serde_json::json!(10));
    assert_eq!(event.details["attribution_usage_quantity"], serde_json::json!(10));
    assert_eq!(event.details["attribution_usage_per"], serde_json::json!("per_sms"));
    assert!(event.details.contains_key("pricing_version"));

    // Should aggregate into external_cost_usd
    assert_eq!(
        tt.task().external_cost_usd,
        "0.05".parse::<Decimal>().unwrap()
    );
    assert_eq!(tt.task().total_cost_usd, "0.05".parse::<Decimal>().unwrap());
}

#[tokio::test]
async fn test_record_usage_unregistered_service() {
    let registry = Arc::new(Mutex::new(RateRegistry::new()));
    let mut tt = make_tracked_task_with_registry(registry);

    let result = tt.record_usage("unknown_service", 5).await;
    assert!(result.is_err());
    let err = result.unwrap_err();
    let msg = err.to_string();
    assert!(msg.contains("No rate registered for service: unknown_service"));
}

// ---------------------------------------------------------------------------
// mark_not_retry tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn test_mark_not_retry_most_recent() {
    let mut tt = make_tracked_task();
    let event = tt
        .mark_retry("rate_limit", Decimal::new(2, 2))
        .await
        .unwrap();

    assert!(event.is_retry);
    assert_eq!(tt.task().retry_count, 1);

    let result = tt.mark_not_retry(None).await.unwrap();
    assert!(result.is_some());
    let cleared = result.unwrap();
    assert!(!cleared.is_retry);
    assert!(cleared.retry_reason.is_none());
    assert!(cleared.retry_of.is_none());

    // In-memory events list should reflect the change
    assert!(!tt.events()[0].is_retry);
}

#[tokio::test]
async fn test_mark_not_retry_by_id() {
    let mut tt = make_tracked_task();

    // Record two retry events
    let first = tt.mark_retry("timeout", Decimal::new(1, 2)).await.unwrap();
    let _second = tt
        .mark_retry("rate_limit", Decimal::new(2, 2))
        .await
        .unwrap();

    // Clear the first one by its event_id
    let result = tt.mark_not_retry(Some(&first.event_id)).await.unwrap();
    assert!(result.is_some());
    let cleared = result.unwrap();
    assert_eq!(cleared.event_id, first.event_id);
    assert!(!cleared.is_retry);

    // Second should still be a retry
    assert!(tt.events()[1].is_retry);
    // First should no longer be a retry
    assert!(!tt.events()[0].is_retry);
}

#[tokio::test]
async fn test_mark_not_retry_no_retries() {
    let mut tt = make_tracked_task();
    // No retry events recorded
    let result = tt.mark_not_retry(None).await.unwrap();
    assert!(result.is_none());
}

// ---------------------------------------------------------------------------
// Heuristic integration tests
// ---------------------------------------------------------------------------

/// When heuristics are enabled, a second LLM call on the same model shortly
/// after a call that ended with a transient error should be flagged as a retry.
#[tokio::test]
async fn test_heuristic_detection_enabled_flags_retry() {
    use dexcost::core::heuristics::RetryHeuristicEngine;
    use dexcost::core::models::{CostEvent, EventType};

    // Build a tracked task with heuristics.
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let task = dexcost::core::models::Task::new("heuristic_test");
    let task_id = task.task_id.clone();
    let engine = Arc::new(std::sync::Mutex::new(
        RetryHeuristicEngine::new(30.0, 0.8).unwrap(),
    ));

    // Pre-seed the engine with a first LLM event that had a rate_limit error.
    {
        let mut eng = engine.lock().unwrap();
        let mut first = CostEvent::new(&task_id, EventType::LlmCall);
        first.model = Some("gpt-4o".to_string());
        first
            .details
            .insert("error_type".to_string(), serde_json::json!("rate_limit"));
        eng.record(first);
    }

    let mut tt2 = TrackedTask::with_heuristics(task, buffer, None, None, engine);

    // Second call — same model, within window. Should be flagged as retry.
    let event = tt2
        .record_llm_call(
            "openai",
            "gpt-4o",
            500,
            200,
            Some(Decimal::new(2, 2)),
            None,
            None,
        )
        .await
        .unwrap();

    assert!(event.is_retry, "second call should be flagged as retry");
    assert_eq!(event.retry_reason, Some("rate_limit".to_string()));
    assert!(event.retry_of.is_some());
    assert_eq!(tt2.task().retry_count, 1);
}

/// When heuristics are NOT enabled, a second LLM call is never flagged as retry
/// regardless of the previous call's details.
#[tokio::test]
async fn test_heuristic_detection_disabled_no_flag() {
    // Use a task WITHOUT heuristics engine.
    let mut tt = make_tracked_task();

    // First call
    tt.record_llm_call(
        "openai",
        "gpt-4o",
        1000,
        500,
        Some(Decimal::new(5, 2)),
        None,
        None,
    )
    .await
    .unwrap();

    // Second call — same model
    let event = tt
        .record_llm_call(
            "openai",
            "gpt-4o",
            500,
            200,
            Some(Decimal::new(2, 2)),
            None,
            None,
        )
        .await
        .unwrap();

    // Without heuristics, is_retry must remain false.
    assert!(
        !event.is_retry,
        "without heuristics, is_retry must be false"
    );
    assert_eq!(tt.task().retry_count, 0);
}
