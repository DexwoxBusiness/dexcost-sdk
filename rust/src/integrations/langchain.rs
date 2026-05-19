//! LangChain-compatible callback handler for dexcost cost tracking.
//!
//! Rust has no canonical LangChain library, so this module provides a
//! framework-agnostic equivalent of the Python SDK's `DexcostCallbackHandler`
//! (`integrations/langchain.py`). A caller wiring dexcost into a LangChain-like
//! pipeline drives the handler through the same lifecycle:
//!
//! 1. [`DexcostCallbackHandler::on_llm_start`] — records the start time and the
//!    model name for a run.
//! 2. [`DexcostCallbackHandler::on_llm_end`] — extracts token usage, computes
//!    cost via the [`PricingEngine`], and records an `llm_call` event.
//! 3. [`DexcostCallbackHandler::on_llm_error`] — records a failure `llm_call`
//!    event carrying `error_type` in its details.
//!
//! Events are attributed to the supplied `task_id` and appended to the shared
//! [`EventBuffer`].

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use tokio::sync::Mutex;

use crate::core::models::{CostConfidence, CostEvent, EventType, PricingSource};
use crate::pricing::engine::PricingEngine;
use crate::transport::buffer::EventBuffer;

/// Per-run state captured between `on_llm_start` and `on_llm_end`/`on_llm_error`.
#[derive(Debug, Clone)]
struct PendingRun {
    start_time: Instant,
    model: String,
}

/// LangChain-compatible callback handler that records LLM calls as dexcost
/// cost events.
///
/// Mirrors Python's `DexcostCallbackHandler`: it does not depend on any
/// LangChain crate and is driven entirely through its public methods.
pub struct DexcostCallbackHandler {
    buffer: Arc<Mutex<EventBuffer>>,
    pricing: Arc<Mutex<PricingEngine>>,
    /// Task that recorded events are attributed to.
    task_id: String,
    /// In-flight runs keyed by `run_id`.
    pending: Mutex<HashMap<String, PendingRun>>,
}

impl DexcostCallbackHandler {
    /// Creates a new handler. All events are attributed to `task_id`.
    pub fn new(
        buffer: Arc<Mutex<EventBuffer>>,
        pricing: Arc<Mutex<PricingEngine>>,
        task_id: impl Into<String>,
    ) -> Self {
        Self {
            buffer,
            pricing,
            task_id: task_id.into(),
            pending: Mutex::new(HashMap::new()),
        }
    }

    /// Called when an LLM starts generating. Records the start time and model
    /// name so they are available when the run completes.
    pub async fn on_llm_start(&self, run_id: &str, model: &str) {
        let mut pending = self.pending.lock().await;
        pending.insert(
            run_id.to_string(),
            PendingRun {
                start_time: Instant::now(),
                model: model.to_string(),
            },
        );
    }

    /// Called when an LLM finishes generating. Computes cost from the token
    /// usage and records an `llm_call` event. Returns the recorded event.
    pub async fn on_llm_end(
        &self,
        run_id: &str,
        input_tokens: i64,
        output_tokens: i64,
    ) -> CostEvent {
        let pending = self.pending.lock().await.remove(run_id);
        let (model, latency_ms) = match pending {
            Some(p) => (
                p.model,
                Some(p.start_time.elapsed().as_millis() as i64),
            ),
            None => ("unknown".to_string(), None),
        };

        let has_usage = input_tokens > 0 || output_tokens > 0;

        let mut event = CostEvent::new(&self.task_id, EventType::LlmCall);
        event.provider = Some("langchain".to_string());
        event.model = Some(model.clone());
        event.latency_ms = latency_ms;

        if has_usage {
            event.input_tokens = Some(input_tokens);
            event.output_tokens = Some(output_tokens);

            let result = {
                let engine = self.pricing.lock().await;
                engine
                    .get_cost(&model, input_tokens, output_tokens, 0, 0)
                    .await
            };
            event.cost_usd = result.cost_usd;
            event.cost_confidence = result.cost_confidence;
            event.pricing_source = Some(result.pricing_source);
            event.pricing_version = Some(result.pricing_version);
        } else {
            // No usage data — record an unknown-cost event.
            event.cost_confidence = CostConfidence::Unknown;
            event.pricing_source = Some(PricingSource::Unknown);
        }

        self.buffer.lock().await.add_event(event.clone());
        event
    }

    /// Called when an LLM call errors. Records a failure `llm_call` event with
    /// the error string and `error_type` in its details. Never panics.
    pub async fn on_llm_error(&self, run_id: &str, error_type: &str, error: &str) -> CostEvent {
        let pending = self.pending.lock().await.remove(run_id);
        let (model, latency_ms) = match pending {
            Some(p) => (
                p.model,
                Some(p.start_time.elapsed().as_millis() as i64),
            ),
            None => ("unknown".to_string(), None),
        };

        let mut event = CostEvent::new(&self.task_id, EventType::LlmCall);
        event.provider = Some("langchain".to_string());
        event.model = Some(model);
        event.latency_ms = latency_ms;
        event.cost_confidence = CostConfidence::Unknown;
        event.pricing_source = Some(PricingSource::Unknown);
        event.details.insert(
            "error".to_string(),
            serde_json::Value::String(error.to_string()),
        );
        event.details.insert(
            "error_type".to_string(),
            serde_json::Value::String(error_type.to_string()),
        );

        self.buffer.lock().await.add_event(event.clone());
        event
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixtures() -> (Arc<Mutex<EventBuffer>>, Arc<Mutex<PricingEngine>>) {
        (
            Arc::new(Mutex::new(EventBuffer::new().expect("buffer"))),
            Arc::new(Mutex::new(PricingEngine::new())),
        )
    }

    #[tokio::test]
    async fn test_on_llm_end_records_event() {
        let (buffer, pricing) = fixtures();
        let handler = DexcostCallbackHandler::new(buffer.clone(), pricing, "task-lc");

        handler.on_llm_start("run-1", "gpt-4o").await;
        let event = handler.on_llm_end("run-1", 1000, 500).await;

        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.provider.as_deref(), Some("langchain"));
        assert_eq!(event.model.as_deref(), Some("gpt-4o"));
        assert_eq!(event.input_tokens, Some(1000));
        assert_eq!(event.output_tokens, Some(500));

        let buf = buffer.lock().await;
        assert_eq!(buf.event_count(), 1);
    }

    #[tokio::test]
    async fn test_on_llm_error_records_failure_with_error_type() {
        let (buffer, pricing) = fixtures();
        let handler = DexcostCallbackHandler::new(buffer.clone(), pricing, "task-lc");

        handler.on_llm_start("run-err", "gpt-4o").await;
        let event = handler
            .on_llm_error("run-err", "rate_limit", "429 Too Many Requests")
            .await;

        assert_eq!(event.event_type, EventType::LlmCall);
        assert_eq!(event.cost_confidence, CostConfidence::Unknown);
        assert_eq!(
            event.details.get("error_type"),
            Some(&serde_json::Value::String("rate_limit".to_string()))
        );
        assert_eq!(
            event.details.get("error"),
            Some(&serde_json::Value::String(
                "429 Too Many Requests".to_string()
            ))
        );

        let buf = buffer.lock().await;
        assert_eq!(buf.event_count(), 1);
    }

    #[tokio::test]
    async fn test_on_llm_end_without_start_uses_unknown_model() {
        let (buffer, pricing) = fixtures();
        let handler = DexcostCallbackHandler::new(buffer, pricing, "task-lc");

        // No matching on_llm_start — model falls back to "unknown".
        let event = handler.on_llm_end("missing-run", 0, 0).await;
        assert_eq!(event.model.as_deref(), Some("unknown"));
        assert_eq!(event.cost_confidence, CostConfidence::Unknown);
    }
}
