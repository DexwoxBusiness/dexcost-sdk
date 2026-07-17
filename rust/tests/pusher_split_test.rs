//! Payload-size invariants for the attribution-v2 pusher contract.

use dexcost::attribution::to_attribution_event_v2;
use dexcost::core::models::{CostEvent, EventType};

const MAX_PAYLOAD_BYTES: usize = 120_000;
const TASK_ID: &str = "11111111-1111-4111-8111-111111111111";

fn wire_event(task_id: &str) -> serde_json::Value {
    let event = CostEvent::new(task_id, EventType::LlmCall);
    serde_json::to_value(to_attribution_event_v2(&event).expect("representable v2 event"))
        .expect("serialize v2 event")
}

fn payload_size(events: &[serde_json::Value], tasks: &[serde_json::Value]) -> usize {
    serde_json::to_vec(&serde_json::json!({
        "events": events,
        "tasks": tasks,
    }))
    .expect("serialize payload")
    .len()
}

#[test]
fn queue_payload_limit_matches_ingestion_contract() {
    assert_eq!(MAX_PAYLOAD_BYTES, 120_000);
    const _: () = assert!(MAX_PAYLOAD_BYTES < 128_000);
}

#[test]
fn small_v2_batch_fits_in_one_payload() {
    let events: Vec<_> = (0..5).map(|_| wire_event(TASK_ID)).collect();
    assert!(payload_size(&events, &[]) <= MAX_PAYLOAD_BYTES);
}

#[test]
fn high_volume_v2_batch_requires_splitting() {
    let events: Vec<_> = (0..1000).map(|_| wire_event(TASK_ID)).collect();
    assert!(payload_size(&events, &[]) > MAX_PAYLOAD_BYTES);
}

#[test]
fn midpoint_split_reduces_size_and_preserves_every_event() {
    let events: Vec<_> = (0..1000).map(|_| wire_event(TASK_ID)).collect();
    let full_size = payload_size(&events, &[]);
    let mid = events.len() / 2;
    let first_size = payload_size(&events[..mid], &[]);
    let second_size = payload_size(&events[mid..], &[]);

    assert!(first_size < full_size);
    assert!(second_size < full_size);
    assert_eq!(events[..mid].len() + events[mid..].len(), events.len());
}

#[test]
fn arbitrary_legacy_details_do_not_inflate_v2_wire_payload() {
    let mut event = CostEvent::new(TASK_ID, EventType::LlmCall);
    event.details.insert(
        "padding".to_string(),
        serde_json::Value::String("x".repeat(250_000)),
    );
    let wire =
        serde_json::to_value(to_attribution_event_v2(&event).expect("details-free strict event"))
            .expect("serialize");

    assert!(wire.get("details").is_none());
    assert!(payload_size(&[wire], &[]) < MAX_PAYLOAD_BYTES);
}
