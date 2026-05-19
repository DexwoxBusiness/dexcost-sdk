//! Wrapper that records LLM cost events from Anthropic-compatible responses.
//!
//! Anthropic responses include `cache_creation_input_tokens` and
//! `cache_read_input_tokens` in addition to standard `input_tokens` and
//! `output_tokens`.
//!
//! # Usage
//!
//! ```no_run
//! use std::sync::Arc;
//! use dexcost::clients::tracked_anthropic::TrackedAnthropic;
//! use dexcost::pricing::engine::PricingEngine;
//!
//! let pricing = Arc::new(PricingEngine::new());
//! let tracked = TrackedAnthropic::new(pricing);
//!
//! let event = tracked.record_response(
//!     "claude-3-5-sonnet-20241022",
//!     800, 300,
//!     Some(100), // cache_creation_tokens
//!     Some(50),  // cache_read_tokens
//!     Some(1200),
//! );
//! ```

use std::sync::Arc;

use crate::core::auto_task;
use crate::core::models::{CostEvent, EventType};
use crate::pricing::engine::PricingEngine;
use crate::transport::buffer::EventBuffer;

/// Wrapper that records LLM cost events from Anthropic-compatible responses.
///
/// Supports Anthropic-specific cache token fields (`cache_creation_input_tokens`,
/// `cache_read_input_tokens`) in addition to the standard `input_tokens` and
/// `output_tokens`.
pub struct TrackedAnthropic {
    pricing: Arc<PricingEngine>,
}

impl TrackedAnthropic {
    /// Creates a new `TrackedAnthropic` wrapper.
    pub fn new(pricing: Arc<PricingEngine>) -> Self {
        Self { pricing }
    }

    /// Records an Anthropic-compatible response as an LLM cost event.
    ///
    /// # Arguments
    ///
    /// * `model` - The model name (e.g. `"claude-3-5-sonnet-20241022"`).
    /// * `input_tokens` - Number of input tokens.
    /// * `output_tokens` - Number of output tokens.
    /// * `cache_creation_tokens` - Optional cache creation input tokens.
    /// * `cache_read_tokens` - Optional cache read input tokens (used for
    ///   pricing discount).
    /// * `latency_ms` - Optional end-to-end latency in milliseconds.
    pub fn record_response(
        &self,
        model: &str,
        input_tokens: u32,
        output_tokens: u32,
        cache_creation_tokens: Option<u32>,
        cache_read_tokens: Option<u32>,
        latency_ms: Option<u64>,
    ) -> CostEvent {
        // Anthropic cache_read_tokens map to the pricing engine's cached_tokens
        // parameter for discount calculation; cache_creation_tokens map to the
        // dedicated cache-creation rate.
        let cached_for_pricing = cache_read_tokens.map(i64::from).unwrap_or(0);
        let cache_creation_for_pricing = cache_creation_tokens.map(i64::from).unwrap_or(0);

        let cost_result = self.pricing.get_cost_sync(
            model,
            i64::from(input_tokens),
            i64::from(output_tokens),
            cached_for_pricing,
            cache_creation_for_pricing,
        );

        let task_type = format!("anthropic.{}", model);
        let mut task = crate::core::models::Task::new(&task_type);
        task.metadata
            .insert("session".to_string(), serde_json::Value::Bool(true));
        task.metadata.insert(
            "initiated_by".to_string(),
            serde_json::Value::String(task_type.clone()),
        );

        let mut event = CostEvent::new(&task.task_id, EventType::LlmCall);
        event.provider = Some("anthropic".to_string());
        event.model = Some(model.to_string());
        event.input_tokens = Some(i64::from(input_tokens));
        event.output_tokens = Some(i64::from(output_tokens));
        event.cached_tokens = cache_read_tokens.map(i64::from);
        event.latency_ms = latency_ms.map(|ms| ms as i64);
        event.cost_usd = cost_result.cost_usd;
        event.cost_confidence = cost_result.cost_confidence;
        event.pricing_source = Some(cost_result.pricing_source);
        event.pricing_version = Some(cost_result.pricing_version);

        // Store Anthropic-specific cache fields in details.
        if let Some(creation) = cache_creation_tokens {
            event.details.insert(
                "cache_creation_input_tokens".to_string(),
                serde_json::Value::Number(serde_json::Number::from(creation)),
            );
        }
        if let Some(read) = cache_read_tokens {
            event.details.insert(
                "cache_read_input_tokens".to_string(),
                serde_json::Value::Number(serde_json::Number::from(read)),
            );
        }

        // Log to dev console
        crate::dev_console::log_event(&event, &task.task_type);

        event
    }

    /// Records an Anthropic-compatible response and persists both the auto-task
    /// and the event into the given buffer.
    #[allow(clippy::too_many_arguments)]
    pub fn record_response_buffered(
        &self,
        model: &str,
        input_tokens: u32,
        output_tokens: u32,
        cache_creation_tokens: Option<u32>,
        cache_read_tokens: Option<u32>,
        latency_ms: Option<u64>,
        buffer: &mut EventBuffer,
    ) -> CostEvent {
        let event = self.record_response(
            model,
            input_tokens,
            output_tokens,
            cache_creation_tokens,
            cache_read_tokens,
            latency_ms,
        );

        let mut task = crate::core::models::Task::new(&format!("anthropic.{}", model));
        task.metadata
            .insert("session".to_string(), serde_json::Value::Bool(true));
        task.metadata.insert(
            "initiated_by".to_string(),
            serde_json::Value::String(format!("anthropic.{}", model)),
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
        let tracked = TrackedAnthropic::new(pricing);

        let event = tracked.record_response(
            "claude-3-5-sonnet-20241022",
            800,
            300,
            None,
            None,
            Some(500),
        );

        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("anthropic"));
        assert_eq!(event.model.as_deref(), Some("claude-3-5-sonnet-20241022"));
        assert_eq!(event.input_tokens, Some(800));
        assert_eq!(event.output_tokens, Some(300));
        assert!(event.cached_tokens.is_none());
        assert_eq!(event.latency_ms, Some(500));
        assert!(!event.task_id.is_empty());
    }

    #[test]
    fn test_record_response_with_cache_tokens() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedAnthropic::new(pricing);

        let event = tracked.record_response(
            "claude-3-5-sonnet-20241022",
            1000,
            400,
            Some(200),
            Some(150),
            None,
        );

        assert_eq!(event.cached_tokens, Some(150));
        assert_eq!(
            event.details.get("cache_creation_input_tokens"),
            Some(&serde_json::Value::Number(serde_json::Number::from(200u32))),
        );
        assert_eq!(
            event.details.get("cache_read_input_tokens"),
            Some(&serde_json::Value::Number(serde_json::Number::from(150u32))),
        );
    }

    #[test]
    fn test_record_response_unknown_model() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedAnthropic::new(pricing);

        let event =
            tracked.record_response("unknown-anthropic-model-xyz", 500, 200, None, None, None);

        assert_eq!(event.cost_confidence, CostConfidence::Unknown);
        assert_eq!(event.cost_usd, Decimal::ZERO);
    }

    #[test]
    fn test_record_response_buffered() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedAnthropic::new(pricing);

        let mut buffer = EventBuffer::new().unwrap();
        let event = tracked.record_response_buffered(
            "claude-3-5-sonnet-20241022",
            800,
            300,
            None,
            None,
            Some(400),
            &mut buffer,
        );

        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("anthropic"));
        assert_eq!(buffer.event_count(), 1);
        assert_eq!(buffer.task_count(), 1);
    }

    #[test]
    fn test_record_response_pricing_populated() {
        let pricing = Arc::new(PricingEngine::new());
        let tracked = TrackedAnthropic::new(pricing);

        let event =
            tracked.record_response("claude-3-5-sonnet-20241022", 800, 300, None, None, None);

        // claude-3-5-sonnet-20241022 should be a known model
        if event.cost_confidence == CostConfidence::Computed {
            assert!(event.cost_usd > Decimal::ZERO);
            assert!(event.pricing_source.is_some());
            assert!(event.pricing_version.is_some());
        }
    }
}
