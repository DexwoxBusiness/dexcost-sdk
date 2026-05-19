use std::sync::Arc;

use dexcost::core::models::{Task, TaskStatus};
use dexcost::core::tracker::{TaskOptions, TrackedTask};
use dexcost::pricing::engine::PricingEngine;
use dexcost::transport::buffer::EventBuffer;
use rust_decimal::Decimal;
use tokio::sync::Mutex;

/// Full integration flow: create task -> record events -> end -> verify buffer.
#[tokio::test]
async fn test_full_flow() {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let pricing = Arc::new(Mutex::new(PricingEngine::new()));

    // Create a task with options
    let mut task = Task::new("resolve_ticket");
    task.customer_id = Some("acme-corp".to_string());
    task.project_id = Some("support".to_string());

    let mut tt = TrackedTask::new(task, buffer.clone(), Some(pricing));

    // Record an LLM call with explicit cost
    let llm_event = tt
        .record_llm_call(
            "openai",
            "gpt-4o",
            1000,
            500,
            Some(Decimal::new(5, 2)), // 0.05
            None,
            Some(250),
        )
        .await
        .unwrap();

    assert_eq!(llm_event.provider, Some("openai".to_string()));
    assert_eq!(llm_event.model, Some("gpt-4o".to_string()));

    // Record a non-LLM cost
    let cost_event = tt
        .record_cost("google_maps", Decimal::new(1, 2), None, None) // 0.01
        .await
        .unwrap();

    assert_eq!(cost_event.service_name, Some("google_maps".to_string()));

    // Mark a retry
    let retry_event = tt
        .mark_retry("rate_limit", Decimal::new(2, 2)) // 0.02
        .await
        .unwrap();

    assert!(retry_event.is_retry);

    // End the task
    tt.end(TaskStatus::Success).await.unwrap();

    // Verify task state
    let task = tt.task();
    assert_eq!(task.status, TaskStatus::Success);
    assert!(task.ended_at.is_some());
    assert_eq!(task.customer_id, Some("acme-corp".to_string()));
    assert_eq!(task.project_id, Some("support".to_string()));
    assert_eq!(task.llm_cost_usd, Decimal::new(5, 2));
    assert_eq!(task.external_cost_usd, Decimal::new(1, 2));
    assert_eq!(task.total_cost_usd, Decimal::new(6, 2)); // llm + external
    assert_eq!(task.retry_cost_usd, Decimal::new(2, 2));
    assert_eq!(task.retry_count, 1);
    assert_eq!(task.total_input_tokens, 1000);
    assert_eq!(task.total_output_tokens, 500);
    assert_eq!(task.schema_version, "1");

    // Verify buffer state
    let buf = buffer.lock().await;
    assert_eq!(buf.event_count(), 3); // llm + cost + retry
    assert_eq!(buf.task_count(), 1);
    assert!(buf.get_task(&task.task_id).is_some());
}

/// Test auto-pricing with the pricing engine.
#[tokio::test]
async fn test_auto_pricing_flow() {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let pricing = Arc::new(Mutex::new(PricingEngine::new()));

    let task = Task::new("auto_priced_task");
    let mut tt = TrackedTask::new(task, buffer.clone(), Some(pricing));

    // Record without explicit cost -- should auto-price
    let event = tt
        .record_llm_call("openai", "gpt-4o", 1000, 500, None, None, None)
        .await
        .unwrap();

    // If model is known, cost should be computed
    // If unknown, cost will be zero with unknown confidence
    // Either way, the event should be recorded
    assert!(tt.events().len() == 1);
    assert_eq!(event.event_type, dexcost::core::models::EventType::LlmCall);
}

/// Test that events end up in the buffer's pending list.
#[tokio::test]
async fn test_buffer_pending_after_records() {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let task = Task::new("buffer_test");
    let mut tt = TrackedTask::new(task, buffer.clone(), None);

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
    tt.record_cost("svc", Decimal::new(2, 2), None, None)
        .await
        .unwrap();

    let buf = buffer.lock().await;
    let pending = buf.get_pending_events(100);
    assert_eq!(pending.len(), 2);
}

/// Test multiple tasks in the same buffer.
#[tokio::test]
async fn test_multiple_tasks_shared_buffer() {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));

    let task1 = Task::new("task_type_a");
    let task1_id = task1.task_id.clone();
    let mut tt1 = TrackedTask::new(task1, buffer.clone(), None);

    let task2 = Task::new("task_type_b");
    let task2_id = task2.task_id.clone();
    let mut tt2 = TrackedTask::new(task2, buffer.clone(), None);

    tt1.record_llm_call(
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
    tt2.record_cost("svc", Decimal::new(2, 2), None, None)
        .await
        .unwrap();

    tt1.end(TaskStatus::Success).await.unwrap();
    tt2.end(TaskStatus::Failed).await.unwrap();

    let buf = buffer.lock().await;
    assert_eq!(buf.event_count(), 2);
    assert_eq!(buf.task_count(), 2);
    assert!(buf.get_task(&task1_id).is_some());
    assert!(buf.get_task(&task2_id).is_some());
}

/// Test that to_dict produces valid JSON.
#[tokio::test]
async fn test_to_dict_json_output() {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
    let task = Task::new("json_test");
    let mut tt = TrackedTask::new(task, buffer.clone(), None);

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

    tt.end(TaskStatus::Success).await.unwrap();

    // Verify task to_dict produces valid JSON
    let task_dict = tt.task().to_dict();
    let json_str = serde_json::to_string(&task_dict).unwrap();
    assert!(!json_str.is_empty());

    // Verify event to_dict produces valid JSON
    for event in tt.events() {
        let event_dict = event.to_dict();
        let json_str = serde_json::to_string(&event_dict).unwrap();
        assert!(!json_str.is_empty());
    }
}

/// Test task options.
#[tokio::test]
async fn test_task_options() {
    let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));

    let mut task = Task::new("with_options");
    let opts = TaskOptions {
        customer_id: Some("customer-1".to_string()),
        project_id: Some("project-1".to_string()),
        experiment_id: Some("exp-1".to_string()),
        variant: Some("v2".to_string()),
        ..Default::default()
    };
    task.customer_id = opts.customer_id;
    task.project_id = opts.project_id;
    task.experiment_id = opts.experiment_id;
    task.variant = opts.variant;

    let mut tt = TrackedTask::new(task, buffer, None);
    tt.end(TaskStatus::Success).await.unwrap();

    assert_eq!(tt.task().customer_id, Some("customer-1".to_string()));
    assert_eq!(tt.task().project_id, Some("project-1".to_string()));
    assert_eq!(tt.task().experiment_id, Some("exp-1".to_string()));
    assert_eq!(tt.task().variant, Some("v2".to_string()));
}
