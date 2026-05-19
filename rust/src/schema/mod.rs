//! Schema validation for dexcost Standard Event Schema v1.
//!
//! Provides compile-time embedded JSON schemas and runtime validation for
//! event and task payloads. Use [`validate()`] to check a payload.
//!
//! # Example
//!
//! ```no_run
//! use serde_json::json;
//! use dexcost::schema::validate;
//!
//! let payload = json!({
//!     "event_id": "550e8400-e29b-41d4-a716-446655440000",
//!     "task_id": "550e8400-e29b-41d4-a716-446655440001",
//!     "event_type": "llm_call",
//!     "occurred_at": "2024-01-01T00:00:00Z",
//!     "cost_usd": "0.0042",
//!     "cost_confidence": "exact",
//!     "schema_version": "1",
//!     "is_retry": false
//! });
//!
//! let errors = validate(&payload);
//! assert!(errors.is_empty());
//! ```

pub mod validate;

pub use validate::validate;
