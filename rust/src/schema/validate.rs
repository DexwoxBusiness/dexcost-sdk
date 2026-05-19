//! JSON Schema validation for dexcost Standard Event Schema v1.
//!
//! Validates `serde_json::Value` payloads against the embedded JSON schemas.
//! Returns an empty `Vec` on success, or a `Vec` of error strings on failure.

const EVENT_SCHEMA: &str = include_str!("dexcost-event.v1.json");
const TASK_SCHEMA: &str = include_str!("dexcost-task.v1.json");

/// Validates a payload against the dexcost Standard Event Schema v1.
///
/// Dispatch rules:
/// - `schema_version` must be `"1"`.
/// - If `event_id` is present → validated against the event schema.
/// - If `task_id` is present (and no `event_id`) → validated against the task schema.
/// - Otherwise → error.
///
/// Returns an empty `Vec` on success, or a `Vec` of human-readable error strings.
pub fn validate(payload: &serde_json::Value) -> Vec<String> {
    // 1. Check schema_version
    match payload.get("schema_version").and_then(|v| v.as_str()) {
        Some("1") => {}
        Some(other) => {
            return vec![format!(
                "Unsupported schema_version '{}': only '1' is supported",
                other
            )]
        }
        None => return vec!["Missing required field 'schema_version': must be '1'".to_string()],
    }

    // 2. Determine which schema to use
    let schema_str = if payload.get("event_id").is_some() {
        EVENT_SCHEMA
    } else if payload.get("task_id").is_some() {
        TASK_SCHEMA
    } else {
        return vec![
            "Payload must contain 'event_id' (event) or 'task_id' (task) to identify schema"
                .to_string(),
        ];
    };

    // 3. Parse and compile schema
    let schema_value: serde_json::Value = match serde_json::from_str(schema_str) {
        Ok(v) => v,
        Err(e) => return vec![format!("Internal: failed to parse embedded schema: {}", e)],
    };

    let validator = match jsonschema::validator_for(&schema_value) {
        Ok(v) => v,
        Err(e) => return vec![format!("Internal: failed to compile schema: {}", e)],
    };

    // 4. Collect validation errors
    let errors: Vec<String> = validator
        .iter_errors(payload)
        .map(|e| e.to_string())
        .collect();

    errors
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn valid_event() -> serde_json::Value {
        json!({
            "event_id": "550e8400-e29b-41d4-a716-446655440000",
            "task_id": "550e8400-e29b-41d4-a716-446655440001",
            "event_type": "llm_call",
            "occurred_at": "2024-01-01T00:00:00Z",
            "cost_usd": "0.0042",
            "cost_confidence": "exact",
            "schema_version": "1",
            "is_retry": false
        })
    }

    fn valid_task() -> serde_json::Value {
        json!({
            "task_id": "550e8400-e29b-41d4-a716-446655440001",
            "task_type": "resolve_ticket",
            "status": "success",
            "started_at": "2024-01-01T00:00:00Z",
            "schema_version": "1",
            "llm_cost_usd": "0.0042",
            "external_cost_usd": "0.00",
            "compute_cost_usd": "0.00",
            "total_cost_usd": "0.0042",
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_cached_tokens": 0,
            "retry_count": 0,
            "retry_cost_usd": "0.00",
            "failure_count": 0
        })
    }

    /// Test 1: valid event returns empty vec.
    #[test]
    fn test_valid_event_returns_empty() {
        let errors = validate(&valid_event());
        assert!(
            errors.is_empty(),
            "Expected no errors for valid event, got: {:?}",
            errors
        );
    }

    /// Test 2: valid task returns empty vec.
    #[test]
    fn test_valid_task_returns_empty() {
        let errors = validate(&valid_task());
        assert!(
            errors.is_empty(),
            "Expected no errors for valid task, got: {:?}",
            errors
        );
    }

    /// Test 3: invalid event (missing required fields) returns errors.
    #[test]
    fn test_invalid_event_returns_errors() {
        let payload = json!({
            "event_id": "550e8400-e29b-41d4-a716-446655440000",
            "schema_version": "1"
            // missing: task_id, event_type, occurred_at, cost_usd, cost_confidence
        });
        let errors = validate(&payload);
        assert!(
            !errors.is_empty(),
            "Expected validation errors for invalid event"
        );
    }

    /// Test 4: invalid task (missing required fields) returns errors.
    #[test]
    fn test_invalid_task_returns_errors() {
        let payload = json!({
            "task_id": "550e8400-e29b-41d4-a716-446655440001",
            "schema_version": "1"
            // missing: task_type, status, started_at, cost fields, token counts, etc.
        });
        let errors = validate(&payload);
        assert!(
            !errors.is_empty(),
            "Expected validation errors for invalid task"
        );
    }

    /// Test 5: unsupported schema_version returns error.
    #[test]
    fn test_unsupported_schema_version_returns_error() {
        let payload = json!({
            "event_id": "550e8400-e29b-41d4-a716-446655440000",
            "schema_version": "2"
        });
        let errors = validate(&payload);
        assert_eq!(errors.len(), 1);
        assert!(
            errors[0].contains("Unsupported schema_version"),
            "Expected unsupported schema_version error, got: {:?}",
            errors
        );
    }

    /// Test 6: no task_id or event_id returns error.
    #[test]
    fn test_no_id_fields_returns_error() {
        let payload = json!({
            "schema_version": "1",
            "event_type": "llm_call"
        });
        let errors = validate(&payload);
        assert_eq!(errors.len(), 1);
        assert!(
            errors[0].contains("event_id") || errors[0].contains("task_id"),
            "Expected id-missing error, got: {:?}",
            errors
        );
    }
}
