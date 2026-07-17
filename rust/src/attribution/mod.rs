//! Strict attribution-v2 wire contract and durable-record conversion.

mod convert;
mod types;
mod validate;

pub use convert::{to_attribution_event_v2, to_attribution_task_ingest_v1};
pub use types::*;
pub use validate::validate_attribution_event_v2;
