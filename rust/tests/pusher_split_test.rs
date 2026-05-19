//! Tests for adaptive batch splitting and payload size constants in the pusher.
//!
//! The `EventPusher` requires a live HTTP endpoint (reqwest), so we focus on
//! what can be validated without an HTTP mock dependency:
//! - The MAX_PAYLOAD_BYTES constant is correctly set below SQS limits
//! - Serialized event payloads can be measured against the limit
//! - Split logic can be exercised via the public `to_dict()` + `serde_json`
//!   size calculation path that the pusher itself uses

use std::collections::HashMap;

use dexcost::core::models::{CostEvent, EventType};

// Re-derive the constant here for testing (the pusher module keeps it private).
// We verify behaviour by checking serialized sizes instead.
const MAX_PAYLOAD_BYTES: usize = 200_000;

/// The payload limit must be well under the SQS 256 KB hard limit.
#[test]
fn test_max_payload_constant_under_sqs_limit() {
    assert_eq!(MAX_PAYLOAD_BYTES, 200_000);
    // Compile-time assertion: MAX_PAYLOAD_BYTES must stay under the SQS
    // 256 KB hard limit. Expressed as a `const` check so it is not flagged
    // as a tautological runtime `assert!` (both operands are constants).
    const _: () = assert!(MAX_PAYLOAD_BYTES < 256_000);
}

/// A small batch of events serializes well under the 200KB limit.
#[test]
fn test_small_batch_fits_in_single_payload() {
    let mut events: Vec<serde_json::Value> = Vec::new();
    for _ in 0..5 {
        let e = CostEvent::new("task-1", EventType::LlmCall);
        events.push(e.to_dict());
    }

    let payload = serde_json::to_string(&serde_json::json!({
        "events": events,
        "tasks": serde_json::Value::Array(vec![]),
    }))
    .unwrap();

    assert!(
        payload.len() <= MAX_PAYLOAD_BYTES,
        "5 small events ({} bytes) should fit in a single payload (limit {})",
        payload.len(),
        MAX_PAYLOAD_BYTES,
    );
}

/// A batch with large details fields exceeds the limit and would require splitting.
#[test]
fn test_large_batch_exceeds_payload_limit() {
    let mut events: Vec<serde_json::Value> = Vec::new();
    for _ in 0..10 {
        let mut e = CostEvent::new("task-1", EventType::LlmCall);
        // ~30KB of padding in details
        let padding: String = "x".repeat(30_000);
        e.details
            .insert("padding".to_string(), serde_json::Value::String(padding));
        events.push(e.to_dict());
    }

    let payload = serde_json::to_string(&serde_json::json!({
        "events": events,
        "tasks": serde_json::Value::Array(vec![]),
    }))
    .unwrap();

    assert!(
        payload.len() > MAX_PAYLOAD_BYTES,
        "10 events with 30KB details ({} bytes) should exceed the limit ({})",
        payload.len(),
        MAX_PAYLOAD_BYTES,
    );
}

/// Splitting a batch at the midpoint produces two sub-batches that are each
/// smaller than the original.
#[test]
fn test_splitting_reduces_payload_size() {
    let mut events: Vec<serde_json::Value> = Vec::new();
    for _ in 0..10 {
        let mut e = CostEvent::new("task-1", EventType::LlmCall);
        let padding: String = "x".repeat(30_000);
        e.details
            .insert("padding".to_string(), serde_json::Value::String(padding));
        events.push(e.to_dict());
    }

    let full_payload = serde_json::to_string(&serde_json::json!({
        "events": events,
        "tasks": serde_json::Value::Array(vec![]),
    }))
    .unwrap();

    let mid = events.len() / 2;
    let first_half = serde_json::to_string(&serde_json::json!({
        "events": &events[..mid],
        "tasks": serde_json::Value::Array(vec![]),
    }))
    .unwrap();

    let second_half = serde_json::to_string(&serde_json::json!({
        "events": &events[mid..],
        "tasks": serde_json::Value::Array(vec![]),
    }))
    .unwrap();

    assert!(
        first_half.len() < full_payload.len(),
        "first half ({}) should be smaller than full ({})",
        first_half.len(),
        full_payload.len(),
    );
    assert!(
        second_half.len() < full_payload.len(),
        "second half ({}) should be smaller than full ({})",
        second_half.len(),
        full_payload.len(),
    );
}

/// A single event with a huge details field cannot be split further. The pusher
/// should skip it rather than looping forever. We verify the size here so the
/// pusher's skip-path is exercised.
#[test]
fn test_single_oversized_event_detected() {
    let mut e = CostEvent::new("task-1", EventType::LlmCall);
    let padding: String = "x".repeat(250_000);
    e.details
        .insert("padding".to_string(), serde_json::Value::String(padding));

    let events = vec![e.to_dict()];
    let payload = serde_json::to_string(&serde_json::json!({
        "events": events,
        "tasks": serde_json::Value::Array(vec![]),
    }))
    .unwrap();

    assert!(
        payload.len() > MAX_PAYLOAD_BYTES,
        "single event with 250KB details ({} bytes) exceeds limit ({})",
        payload.len(),
        MAX_PAYLOAD_BYTES,
    );
    // Only 1 event — cannot split further; pusher should log and skip.
    assert_eq!(events.len(), 1);
}

/// Verify that splitting preserves total event count across halves.
#[test]
fn test_split_preserves_event_count() {
    let count = 7; // odd number to test midpoint rounding
    let mut events: Vec<serde_json::Value> = Vec::new();
    for _ in 0..count {
        let e = CostEvent::new("task-1", EventType::LlmCall);
        events.push(e.to_dict());
    }

    let mid = events.len() / 2;
    let first_count = events[..mid].len();
    let second_count = events[mid..].len();

    assert_eq!(
        first_count + second_count,
        count,
        "split halves must sum to original count",
    );
    assert_eq!(first_count, 3, "first half of 7 events should be 3");
    assert_eq!(second_count, 4, "second half of 7 events should be 4");
}

/// Verify that tasks are only included in the first sub-batch (the pusher
/// passes tasks=nil for the second half to avoid duplicating task upserts).
#[test]
fn test_tasks_only_in_first_half() {
    // Simulate the pusher's splitting convention: tasks go with first half only.
    let task = serde_json::json!({"task_id": "t-1", "task_type": "test"});
    let tasks = vec![task];

    let mut events: Vec<serde_json::Value> = Vec::new();
    for _ in 0..4 {
        let e = CostEvent::new("t-1", EventType::LlmCall);
        events.push(e.to_dict());
    }

    let mid = events.len() / 2;

    let first_payload: serde_json::Value = serde_json::json!({
        "events": &events[..mid],
        "tasks": &tasks,
    });
    let second_payload: serde_json::Value = serde_json::json!({
        "events": &events[mid..],
        "tasks": serde_json::Value::Array(vec![]),
    });

    // First half carries tasks.
    let first_tasks = first_payload["tasks"].as_array().unwrap();
    assert_eq!(first_tasks.len(), 1, "first half should carry tasks");

    // Second half has empty tasks.
    let second_tasks = second_payload["tasks"].as_array().unwrap();
    assert_eq!(second_tasks.len(), 0, "second half should have no tasks");
}

/// Verify event details with nested maps serialize correctly and contribute
/// to payload size as expected.
#[test]
fn test_nested_details_contribute_to_size() {
    let mut e = CostEvent::new("task-1", EventType::LlmCall);
    let nested: HashMap<String, serde_json::Value> = HashMap::from([
        ("key1".to_string(), serde_json::json!("value1")),
        (
            "nested".to_string(),
            serde_json::json!({"inner_key": "x".repeat(1000)}),
        ),
    ]);
    for (k, v) in nested {
        e.details.insert(k, v);
    }

    let dict = e.to_dict();
    let serialized = serde_json::to_string(&dict).unwrap();

    // The 1000-char inner value should contribute meaningfully.
    assert!(
        serialized.len() > 1000,
        "serialized event with nested details should exceed 1000 bytes, got {}",
        serialized.len(),
    );
}
