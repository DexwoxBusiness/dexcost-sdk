use dexcost::core::models::*;
use rust_decimal::Decimal;

#[test]
fn test_task_creation() {
    let task = Task::new("resolve_ticket");
    assert_eq!(task.task_type, "resolve_ticket");
    assert_eq!(task.status, TaskStatus::Pending);
    assert_eq!(task.llm_cost_usd, Decimal::ZERO);
    assert_eq!(task.external_cost_usd, Decimal::ZERO);
    assert_eq!(task.compute_cost_usd, Decimal::ZERO);
    assert_eq!(task.total_cost_usd, Decimal::ZERO);
    assert_eq!(task.retry_cost_usd, Decimal::ZERO);
    assert_eq!(task.total_input_tokens, 0);
    assert_eq!(task.total_output_tokens, 0);
    assert_eq!(task.total_cached_tokens, 0);
    assert_eq!(task.retry_count, 0);
    assert_eq!(task.failure_count, 0);
    assert_eq!(task.schema_version, "1");
    assert!(task.ended_at.is_none());
    assert!(task.customer_id.is_none());
    assert!(task.project_id.is_none());
    assert!(task.parent_task_id.is_none());
}

#[test]
fn test_task_unique_ids() {
    let t1 = Task::new("a");
    let t2 = Task::new("b");
    assert_ne!(t1.task_id, t2.task_id);
}

#[test]
fn test_event_creation() {
    let event = CostEvent::new("task-123", EventType::LlmCall);
    assert_eq!(event.task_id, "task-123");
    assert_eq!(event.event_type, EventType::LlmCall);
    assert_eq!(event.cost_usd, Decimal::ZERO);
    assert_eq!(event.cost_confidence, CostConfidence::Exact);
    assert!(!event.is_retry);
    assert!(event.retry_reason.is_none());
    assert!(event.retry_of.is_none());
    assert_eq!(event.schema_version, "1");
}

#[test]
fn test_event_unique_ids() {
    let e1 = CostEvent::new("task-1", EventType::LlmCall);
    let e2 = CostEvent::new("task-1", EventType::LlmCall);
    assert_ne!(e1.event_id, e2.event_id);
}

#[test]
fn test_task_to_dict_schema() {
    let task = Task::new("test");
    let dict = task.to_dict();

    // Verify all required fields exist
    assert!(dict.get("task_id").is_some());
    assert!(dict.get("task_type").is_some());
    assert!(dict.get("status").is_some());
    assert!(dict.get("started_at").is_some());
    assert!(dict.get("schema_version").is_some());

    // Costs must be strings
    assert!(dict["llm_cost_usd"].is_string());
    assert!(dict["external_cost_usd"].is_string());
    assert!(dict["compute_cost_usd"].is_string());
    assert!(dict["total_cost_usd"].is_string());
    assert!(dict["retry_cost_usd"].is_string());

    // Tokens must be numbers
    assert!(dict["total_input_tokens"].is_i64());
    assert!(dict["total_output_tokens"].is_i64());
    assert!(dict["total_cached_tokens"].is_i64());
}

#[test]
fn test_event_to_dict_schema() {
    let event = CostEvent::new("task-123", EventType::ExternalCost);
    let dict = event.to_dict();

    assert!(dict.get("event_id").is_some());
    assert!(dict.get("task_id").is_some());
    assert!(dict.get("event_type").is_some());
    assert!(dict.get("occurred_at").is_some());
    assert!(dict["cost_usd"].is_string());
    assert!(dict.get("is_retry").is_some());
    assert!(dict.get("schema_version").is_some());
}

#[test]
fn test_task_serialization_roundtrip() {
    let mut task = Task::new("test");
    task.customer_id = Some("acme".to_string());
    task.llm_cost_usd = Decimal::new(123, 4); // 0.0123

    let json = serde_json::to_string(&task).unwrap();
    let deserialized: Task = serde_json::from_str(&json).unwrap();

    assert_eq!(deserialized.task_type, "test");
    assert_eq!(deserialized.customer_id, Some("acme".to_string()));
    assert_eq!(deserialized.llm_cost_usd, Decimal::new(123, 4));
}

#[test]
fn test_event_serialization_roundtrip() {
    let mut event = CostEvent::new("task-1", EventType::LlmCall);
    event.provider = Some("openai".to_string());
    event.model = Some("gpt-4o".to_string());
    event.cost_usd = Decimal::new(5, 2); // 0.05

    let json = serde_json::to_string(&event).unwrap();
    let deserialized: CostEvent = serde_json::from_str(&json).unwrap();

    assert_eq!(deserialized.provider, Some("openai".to_string()));
    assert_eq!(deserialized.model, Some("gpt-4o".to_string()));
    assert_eq!(deserialized.cost_usd, Decimal::new(5, 2));
}

#[test]
fn test_status_enum_serialization() {
    assert_eq!(
        serde_json::to_string(&TaskStatus::Pending).unwrap(),
        "\"pending\""
    );
    assert_eq!(
        serde_json::to_string(&TaskStatus::Success).unwrap(),
        "\"success\""
    );
    assert_eq!(
        serde_json::to_string(&TaskStatus::Failed).unwrap(),
        "\"failed\""
    );
}

#[test]
fn test_event_type_serialization() {
    assert_eq!(
        serde_json::to_string(&EventType::LlmCall).unwrap(),
        "\"llm_call\""
    );
    assert_eq!(
        serde_json::to_string(&EventType::ExternalCost).unwrap(),
        "\"external_cost\""
    );
    assert_eq!(
        serde_json::to_string(&EventType::ComputeCost).unwrap(),
        "\"compute_cost\""
    );
    assert_eq!(
        serde_json::to_string(&EventType::RetryMarker).unwrap(),
        "\"retry_marker\""
    );
}

#[test]
fn test_cost_aggregation() {
    let mut task = Task::new("test");
    let costs = vec![
        Decimal::new(10, 2), // 0.10
        Decimal::new(20, 2), // 0.20
        Decimal::new(5, 2),  // 0.05
    ];

    for cost in &costs {
        task.llm_cost_usd += cost;
    }
    task.total_cost_usd = task.llm_cost_usd;

    assert_eq!(task.llm_cost_usd, Decimal::new(35, 2));
    assert_eq!(task.total_cost_usd, Decimal::new(35, 2));
}

#[test]
fn test_task_metadata() {
    let mut task = Task::new("test");
    task.metadata.insert(
        "key".to_string(),
        serde_json::Value::String("value".to_string()),
    );

    let dict = task.to_dict();
    let metadata = dict["metadata"].as_object().unwrap();
    assert_eq!(metadata["key"], "value");
}
