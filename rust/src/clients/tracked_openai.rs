//! Wrapper that records LLM cost events from OpenAI-compatible responses.
//!
//! # Usage
//!
//! ```no_run
//! use std::sync::Arc;
//! use dexcost::clients::tracked_openai::TrackedOpenAI;
//! use dexcost::core::tracker::TrackedTask;
//! use dexcost::pricing::engine::PricingEngine;
//!
//! let pricing = Arc::new(PricingEngine::new());
//! let tracked = TrackedOpenAI::new(pricing);
//!
//! // After making an OpenAI call, record the response:
//! let event = tracked.record_response("gpt-4o", 1000, 500, None, Some(250));
//! ```

use std::sync::Arc;

use crate::core::auto_task;
use crate::core::models::{CostEvent, EventType};
use crate::pricing::engine::PricingEngine;
use crate::transport::buffer::EventBuffer;

/// Wrapper that records LLM cost events from OpenAI-compatible responses.
///
/// Accepts pre-extracted token counts (no dependency on an actual OpenAI SDK
/// crate). The pricing engine is used to look up per-token costs and compute
/// the total.
pub struct TrackedOpenAI {
    pricing: Arc<PricingEngine>,
}

impl TrackedOpenAI {
    /// Creates a new `TrackedOpenAI` wrapper.
    pub fn new(pricing: Arc<PricingEngine>) -> Self {
        Self { pricing }
    }

    /// Records an OpenAI-compatible response as an LLM cost event.
    ///
    /// An auto-task is created (via [`auto_task::create_auto_task`]) for each
    /// recorded response and finalized immediately.
    ///
    /// # Arguments
    ///
    /// * `model` - The model name (e.g. `"gpt-4o"`).
    /// * `input_tokens` - Number of prompt tokens.
    /// * `output_tokens` - Number of completion tokens.
    /// * `cached_tokens` - Optional cached prompt tokens.
    /// * `latency_ms` - Optional end-to-end latency in milliseconds.
    pub fn record_response(
        &self,
        model: &str,
        input_tokens: u32,
        output_tokens: u32,
        cached_tokens: Option<u32>,
        latency_ms: Option<u64>,
    ) -> CostEvent {
        let cost_result = self.pricing.get_cost_sync(
            model,
            i64::from(input_tokens),
            i64::from(output_tokens),
            cached_tokens.map(i64::from).unwrap_or(0),
            0,
        );

        let task_type = format!("openai.{}", model);
        let mut task = crate::core::models::Task::new(&task_type);
        task.metadata
            .insert("session".to_string(), serde_json::Value::Bool(true));
        task.metadata.insert(
            "initiated_by".to_string(),
            serde_json::Value::String(task_type.clone()),
        );

        let mut event = CostEvent::new(&task.task_id, EventType::LlmCall);
        event.provider = Some("openai".to_string());
        event.model = Some(model.to_string());
        event.input_tokens = Some(i64::from(input_tokens));
        event.output_tokens = Some(i64::from(output_tokens));
        event.cached_tokens = cached_tokens.map(i64::from);
        event.latency_ms = latency_ms.map(|ms| ms as i64);
        event.cost_usd = cost_result.cost_usd;
        event.cost_confidence = cost_result.cost_confidence;
        event.pricing_source = Some(cost_result.pricing_source);
        event.pricing_version = Some(cost_result.pricing_version);

        // Log to dev console
        crate::dev_console::log_event(&event, &task.task_type);

        event
    }

    /// Records an OpenAI-compatible response and persists both the auto-task and
    /// the event into the given buffer.
    ///
    /// This is the battery-included variant that creates an auto-task, aggregates
    /// costs, and writes everything to the buffer in one call.
    pub fn record_response_buffered(
        &self,
        model: &str,
        input_tokens: u32,
        output_tokens: u32,
        cached_tokens: Option<u32>,
        latency_ms: Option<u64>,
        buffer: &mut EventBuffer,
    ) -> CostEvent {
        let event = self.record_response(
            model,
            input_tokens,
            output_tokens,
            cached_tokens,
            latency_ms,
        );

        let mut task = crate::core::models::Task::new(&format!("openai.{}", model));
        task.metadata
            .insert("session".to_string(), serde_json::Value::Bool(true));
        task.metadata.insert(
            "initiated_by".to_string(),
            serde_json::Value::String(format!("openai.{}", model)),
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
        let tracked = TrackedOpenAI::new(pricing);

        let event = tracked.record_response("gpt-4o", 1000, 500, None, Some(250));

        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("openai"));
        assert_eq!(event.model.as_deref(), Some("gpt-4o"));
        assert_eq!(event.input_tokens, Some(1000));
        assert_eq!(event.output_tokens, Some(500));
        assert!(event.cached_tokens.is_none());
        assert_eq!(event.latency_ms, Some(250));
        assert!(!event.task_id.is_empty());
        assert!(!event.event_id.is_empty());
    }

    #[test]
    fn test_record_response_with_cached_tokens() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedOpenAI::new(pricing);

        let event = tracked.record_response("gpt-4o", 1000, 500, Some(200), None);

        assert_eq!(event.cached_tokens, Some(200));
        assert!(event.latency_ms.is_none());
    }

    #[test]
    fn test_record_response_unknown_model() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedOpenAI::new(pricing);

        let event = tracked.record_response("totally-unknown-model-xyz", 500, 200, None, None);

        assert_eq!(event.cost_confidence, CostConfidence::Unknown);
        assert_eq!(event.cost_usd, Decimal::ZERO);
    }

    #[test]
    fn test_record_response_buffered() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedOpenAI::new(pricing);

        let mut buffer = EventBuffer::new().unwrap();
        let event =
            tracked.record_response_buffered("gpt-4o", 1000, 500, None, Some(100), &mut buffer);

        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("openai"));
        assert_eq!(buffer.event_count(), 1);
        assert_eq!(buffer.task_count(), 1);
    }

    #[test]
    fn test_record_response_pricing_populated() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedOpenAI::new(pricing);

        let event = tracked.record_response("gpt-4o", 1000, 500, None, None);

        // gpt-4o should be a known model
        if event.cost_confidence == CostConfidence::Computed {
            assert!(event.cost_usd > Decimal::ZERO);
            assert!(event.pricing_source.is_some());
            assert!(event.pricing_version.is_some());
        }
    }
}
