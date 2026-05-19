//! Wrapper that records LLM cost events from Google Gemini-compatible responses.
//!
//! Gemini uses `prompt_token_count` and `candidates_token_count` instead of the
//! more common `input_tokens` / `output_tokens` naming.
//!
//! # Usage
//!
//! ```no_run
//! use std::sync::Arc;
//! use dexcost::clients::tracked_gemini::TrackedGemini;
//! use dexcost::pricing::engine::PricingEngine;
//!
//! let pricing = Arc::new(PricingEngine::new());
//! let tracked = TrackedGemini::new(pricing);
//!
//! let event = tracked.record_response(
//!     "gemini-1.5-pro",
//!     2000,  // prompt_token_count
//!     800,   // candidates_token_count
//!     Some(300), // cached_content_token_count
//!     Some(950),
//! );
//! ```

use std::sync::Arc;

use crate::core::auto_task;
use crate::core::models::{CostEvent, EventType};
use crate::pricing::engine::PricingEngine;
use crate::transport::buffer::EventBuffer;

/// Wrapper that records LLM cost events from Google Gemini-compatible responses.
///
/// Accepts Gemini-style field names (`prompt_token_count`,
/// `candidates_token_count`, `cached_content_token_count`) and maps them to
/// the standard dexcost event schema.
pub struct TrackedGemini {
    pricing: Arc<PricingEngine>,
}

impl TrackedGemini {
    /// Creates a new `TrackedGemini` wrapper.
    pub fn new(pricing: Arc<PricingEngine>) -> Self {
        Self { pricing }
    }

    /// Records a Gemini-compatible response as an LLM cost event.
    ///
    /// # Arguments
    ///
    /// * `model` - The model name (e.g. `"gemini-1.5-pro"`).
    /// * `prompt_token_count` - Number of prompt tokens.
    /// * `candidates_token_count` - Number of candidate (output) tokens.
    /// * `cached_content_token_count` - Optional cached content tokens.
    /// * `latency_ms` - Optional end-to-end latency in milliseconds.
    pub fn record_response(
        &self,
        model: &str,
        prompt_token_count: u32,
        candidates_token_count: u32,
        cached_content_token_count: Option<u32>,
        latency_ms: Option<u64>,
    ) -> CostEvent {
        let cached_for_pricing = cached_content_token_count.map(i64::from).unwrap_or(0);

        let cost_result = self.pricing.get_cost_sync(
            model,
            i64::from(prompt_token_count),
            i64::from(candidates_token_count),
            cached_for_pricing,
            0,
        );

        let task_type = format!("gemini.{}", model);
        let mut task = crate::core::models::Task::new(&task_type);
        task.metadata
            .insert("session".to_string(), serde_json::Value::Bool(true));
        task.metadata.insert(
            "initiated_by".to_string(),
            serde_json::Value::String(task_type.clone()),
        );

        let mut event = CostEvent::new(&task.task_id, EventType::LlmCall);
        event.provider = Some("google".to_string());
        event.model = Some(model.to_string());
        event.input_tokens = Some(i64::from(prompt_token_count));
        event.output_tokens = Some(i64::from(candidates_token_count));
        event.cached_tokens = cached_content_token_count.map(i64::from);
        event.latency_ms = latency_ms.map(|ms| ms as i64);
        event.cost_usd = cost_result.cost_usd;
        event.cost_confidence = cost_result.cost_confidence;
        event.pricing_source = Some(cost_result.pricing_source);
        event.pricing_version = Some(cost_result.pricing_version);

        // Store Gemini-specific field names in details for provenance.
        event.details.insert(
            "prompt_token_count".to_string(),
            serde_json::Value::Number(serde_json::Number::from(prompt_token_count)),
        );
        event.details.insert(
            "candidates_token_count".to_string(),
            serde_json::Value::Number(serde_json::Number::from(candidates_token_count)),
        );
        if let Some(cached) = cached_content_token_count {
            event.details.insert(
                "cached_content_token_count".to_string(),
                serde_json::Value::Number(serde_json::Number::from(cached)),
            );
        }

        // Log to dev console
        crate::dev_console::log_event(&event, &task.task_type);

        event
    }

    /// Records a Gemini-compatible response and persists both the auto-task and
    /// the event into the given buffer.
    pub fn record_response_buffered(
        &self,
        model: &str,
        prompt_token_count: u32,
        candidates_token_count: u32,
        cached_content_token_count: Option<u32>,
        latency_ms: Option<u64>,
        buffer: &mut EventBuffer,
    ) -> CostEvent {
        let event = self.record_response(
            model,
            prompt_token_count,
            candidates_token_count,
            cached_content_token_count,
            latency_ms,
        );

        let mut task = crate::core::models::Task::new(&format!("gemini.{}", model));
        task.metadata
            .insert("session".to_string(), serde_json::Value::Bool(true));
        task.metadata.insert(
            "initiated_by".to_string(),
            serde_json::Value::String(format!("gemini.{}", model)),
        );

        auto_task::finalize_auto_task(&mut task, Some(&event), "success", buffer);
        buffer.add_event(event.clone());

        event
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::models::{CostConfidence, EventType};
    use rust_decimal::Decimal;

    #[test]
    fn test_record_response_basic() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedGemini::new(pricing);

        let event = tracked.record_response("gemini-1.5-pro", 2000, 800, None, Some(950));

        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("google"));
        assert_eq!(event.model.as_deref(), Some("gemini-1.5-pro"));
        assert_eq!(event.input_tokens, Some(2000));
        assert_eq!(event.output_tokens, Some(800));
        assert!(event.cached_tokens.is_none());
        assert_eq!(event.latency_ms, Some(950));
        assert!(!event.task_id.is_empty());
    }

    #[test]
    fn test_record_response_with_cached_content() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedGemini::new(pricing);

        let event = tracked.record_response("gemini-1.5-pro", 2000, 800, Some(300), None);

        assert_eq!(event.cached_tokens, Some(300));
        assert_eq!(
            event.details.get("prompt_token_count"),
            Some(&serde_json::Value::Number(serde_json::Number::from(
                2000u32
            ))),
        );
        assert_eq!(
            event.details.get("candidates_token_count"),
            Some(&serde_json::Value::Number(serde_json::Number::from(800u32))),
        );
        assert_eq!(
            event.details.get("cached_content_token_count"),
            Some(&serde_json::Value::Number(serde_json::Number::from(300u32))),
        );
    }

    #[test]
    fn test_record_response_unknown_model() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedGemini::new(pricing);

        let event = tracked.record_response("unknown-gemini-model-xyz", 500, 200, None, None);

        assert_eq!(event.cost_confidence, CostConfidence::Unknown);
        assert_eq!(event.cost_usd, Decimal::ZERO);
    }

    #[test]
    fn test_record_response_buffered() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedGemini::new(pricing);

        let mut buffer = EventBuffer::new().unwrap();
        let event = tracked.record_response_buffered(
            "gemini-1.5-pro",
            2000,
            800,
            None,
            Some(600),
            &mut buffer,
        );

        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("google"));
        assert_eq!(buffer.event_count(), 1);
        assert_eq!(buffer.task_count(), 1);
    }

    #[test]
    fn test_gemini_details_fields_present() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedGemini::new(pricing);

        let event = tracked.record_response("gemini-1.5-pro", 1500, 600, None, None);

        // Even without cached content, the base Gemini fields should be in details.
        assert!(event.details.contains_key("prompt_token_count"));
        assert!(event.details.contains_key("candidates_token_count"));
        assert!(!event.details.contains_key("cached_content_token_count"));
    }
}
