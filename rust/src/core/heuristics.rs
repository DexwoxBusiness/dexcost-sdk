use std::collections::HashMap;

use chrono::{DateTime, Utc};

use super::models::{CostEvent, EventType};

/// Transient error types that are likely to be retried.
pub const TRANSIENT_ERRORS: &[&str] = &[
    "rate_limit",
    "timeout",
    "5xx",
    "server_error",
    "connection_error",
];

/// Returns the base likelihood that a subsequent identical LLM call is a retry,
/// given a particular error type on the preceding call.
pub fn error_likelihood(error_type: &str) -> f64 {
    match error_type {
        "rate_limit" => 1.0,
        "timeout" => 0.9,
        "5xx" => 0.85,
        "server_error" => 0.85,
        "connection_error" => 0.8,
        _ => 0.8,
    }
}

/// Result of a retry heuristic check.
#[derive(Debug, Clone)]
pub struct HeuristicMatch {
    pub is_retry: bool,
    pub confidence: f64,
    pub matched_event_id: Option<String>,
    pub reason: String,
}

impl HeuristicMatch {
    fn no_match() -> Self {
        Self {
            is_retry: false,
            confidence: 0.0,
            matched_event_id: None,
            reason: String::new(),
        }
    }
}

/// Detects likely retries by inspecting recent events for the same task and
/// model within a rolling time window.
///
/// Algorithm (matches Python / TypeScript / Go implementations):
/// - **record**: prune events older than `window_seconds` relative to the new
///   event's `occurred_at`, then append the event.
/// - **check**: walk backwards through task events; on the first same-model
///   LLM call, inspect `details["error_type"]`; if it is a known transient
///   error compute `confidence = base_likelihood * max(0, 1 - gap/window)`;
///   return a match if confidence >= threshold, otherwise return no-match.
pub struct RetryHeuristicEngine {
    window_seconds: f64,
    threshold: f64,
    recent_events: HashMap<String, Vec<CostEvent>>, // task_id -> events
}

impl RetryHeuristicEngine {
    /// Creates a new engine with the given rolling window (seconds) and
    /// confidence threshold. Returns an error if parameters are out of range.
    pub fn new(window_seconds: f64, threshold: f64) -> Result<Self, crate::error::DexcostError> {
        if window_seconds <= 0.0 {
            return Err(crate::error::DexcostError::Config(format!(
                "window_seconds must be positive, got {}",
                window_seconds
            )));
        }
        if threshold <= 0.0 || threshold > 1.0 {
            return Err(crate::error::DexcostError::Config(format!(
                "threshold must be between 0 and 1, got {}",
                threshold
            )));
        }
        Ok(Self {
            window_seconds,
            threshold,
            recent_events: HashMap::new(),
        })
    }

    /// Returns the rolling window size in seconds.
    pub fn window_seconds(&self) -> f64 {
        self.window_seconds
    }

    /// Returns the minimum confidence required to flag a retry.
    pub fn threshold(&self) -> f64 {
        self.threshold
    }

    /// Stores the event for future `check` calls, pruning events that have
    /// fallen outside the rolling window relative to this event's `occurred_at`.
    pub fn record(&mut self, event: CostEvent) {
        let window = self.window_seconds;
        let cutoff: DateTime<Utc> = event.occurred_at;

        let bucket = self.recent_events.entry(event.task_id.clone()).or_default();

        // Prune events outside the window.
        bucket.retain(|e| {
            let gap = (cutoff - e.occurred_at).num_milliseconds() as f64 / 1000.0;
            gap >= 0.0 && gap <= window
        });

        bucket.push(event);

        // Sprint 4 §5.2 (A3) — hard cap at 1000 entries per task to
        // bound memory on long-running tasks. The window-prune above
        // already drops time-stale events, but a task that records
        // many events INSIDE the window (e.g. a 30s task firing
        // 1k LLM calls) would otherwise grow unbounded. FIFO drop the
        // oldest 10% in one batch.
        const PER_TASK_CAP: usize = 1000;
        if bucket.len() > PER_TASK_CAP {
            let drop_n = PER_TASK_CAP / 10;
            bucket.drain(..drop_n);
        }
    }

    /// Inspects recent events for the same task and model to determine whether
    /// the supplied event is likely a retry.
    pub fn check(&self, event: &CostEvent) -> HeuristicMatch {
        let events = match self.recent_events.get(&event.task_id) {
            Some(v) if !v.is_empty() => v,
            _ => return HeuristicMatch::no_match(),
        };

        // Walk backwards.
        for candidate in events.iter().rev() {
            // Skip self.
            if candidate.event_id == event.event_id {
                continue;
            }
            // Only consider LLM calls.
            if candidate.event_type != EventType::LlmCall {
                continue;
            }
            // Must be the same model.
            if candidate.model != event.model {
                continue;
            }

            // Found a same-model LLM call — inspect error_type.
            let error_type = match candidate.details.get("error_type") {
                Some(serde_json::Value::String(s)) => s.as_str(),
                _ => return HeuristicMatch::no_match(),
            };

            if !TRANSIENT_ERRORS.contains(&error_type) {
                return HeuristicMatch::no_match();
            }

            // Compute time gap in seconds.
            let gap =
                (event.occurred_at - candidate.occurred_at).num_milliseconds() as f64 / 1000.0;

            if gap < 0.0 || gap > self.window_seconds {
                return HeuristicMatch::no_match();
            }

            // Base likelihood with time decay.
            let base = error_likelihood(error_type);
            let time_decay = (1.0 - gap / self.window_seconds).max(0.0);
            let confidence = base * time_decay;

            if confidence >= self.threshold {
                return HeuristicMatch {
                    is_retry: true,
                    confidence,
                    matched_event_id: Some(candidate.event_id.clone()),
                    reason: error_type.to_string(),
                };
            }

            return HeuristicMatch::no_match();
        }

        HeuristicMatch::no_match()
    }
}

// ---------------------------------------------------------------------------
// Default constants
// ---------------------------------------------------------------------------

/// Default rolling window: 30 seconds.
pub const DEFAULT_WINDOW_SECONDS: f64 = 30.0;

/// Default confidence threshold: 0.8.
pub const DEFAULT_THRESHOLD: f64 = 0.8;

// ---------------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use chrono::Duration;
    use rust_decimal::Decimal;

    use super::*;
    use crate::core::models::{CostConfidence, CostEvent, EventType};

    fn make_llm_event(task_id: &str, model: &str) -> CostEvent {
        let mut e = CostEvent::new(task_id, EventType::LlmCall);
        e.model = Some(model.to_string());
        e.cost_usd = Decimal::ZERO;
        e.cost_confidence = CostConfidence::Unknown;
        e
    }

    fn with_error(mut event: CostEvent, error_type: &str) -> CostEvent {
        event
            .details
            .insert("error_type".to_string(), serde_json::json!(error_type));
        event
    }

    // 1. Detects retry after transient error on same model
    #[test]
    fn test_detects_retry_after_transient_error() {
        let mut engine = RetryHeuristicEngine::new(30.0, 0.8).unwrap();

        let base_time = Utc::now();

        let mut first = make_llm_event("task-1", "gpt-4o");
        first.occurred_at = base_time;
        let first = with_error(first, "rate_limit"); // confidence base = 1.0
        engine.record(first.clone());

        let mut second = make_llm_event("task-1", "gpt-4o");
        second.occurred_at = base_time + Duration::seconds(1); // gap = 1s → decay = 1-1/30 ≈ 0.967
        engine.record(second.clone());

        let result = engine.check(&second);
        assert!(result.is_retry);
        assert!(result.confidence >= 0.8);
        assert_eq!(result.matched_event_id, Some(first.event_id.clone()));
        assert_eq!(result.reason, "rate_limit");
    }

    // 2. Does not flag for different model
    #[test]
    fn test_no_flag_different_model() {
        let mut engine = RetryHeuristicEngine::new(30.0, 0.8).unwrap();

        let base_time = Utc::now();

        let mut first = make_llm_event("task-1", "gpt-4o");
        first.occurred_at = base_time;
        let first = with_error(first, "rate_limit");
        engine.record(first);

        let mut second = make_llm_event("task-1", "claude-3.5-sonnet");
        second.occurred_at = base_time + Duration::seconds(1);
        engine.record(second.clone());

        let result = engine.check(&second);
        assert!(!result.is_retry);
    }

    // 3. Does not flag for different task
    #[test]
    fn test_no_flag_different_task() {
        let mut engine = RetryHeuristicEngine::new(30.0, 0.8).unwrap();

        let base_time = Utc::now();

        let mut first = make_llm_event("task-A", "gpt-4o");
        first.occurred_at = base_time;
        let first = with_error(first, "rate_limit");
        engine.record(first);

        let mut second = make_llm_event("task-B", "gpt-4o");
        second.occurred_at = base_time + Duration::seconds(1);
        engine.record(second.clone());

        let result = engine.check(&second);
        assert!(!result.is_retry);
    }

    // 4. Does not flag when previous succeeded (no error)
    #[test]
    fn test_no_flag_when_previous_succeeded() {
        let mut engine = RetryHeuristicEngine::new(30.0, 0.8).unwrap();

        let base_time = Utc::now();

        // No error_type in details
        let mut first = make_llm_event("task-1", "gpt-4o");
        first.occurred_at = base_time;
        engine.record(first);

        let mut second = make_llm_event("task-1", "gpt-4o");
        second.occurred_at = base_time + Duration::seconds(1);
        engine.record(second.clone());

        let result = engine.check(&second);
        assert!(!result.is_retry);
    }

    // 5. Does not flag outside window
    #[test]
    fn test_no_flag_outside_window() {
        let mut engine = RetryHeuristicEngine::new(30.0, 0.8).unwrap();

        let base_time = Utc::now();

        let mut first = make_llm_event("task-1", "gpt-4o");
        first.occurred_at = base_time;
        let first = with_error(first, "rate_limit");
        engine.record(first);

        // 31 seconds later — just outside the 30s window
        let mut second = make_llm_event("task-1", "gpt-4o");
        second.occurred_at = base_time + Duration::seconds(31);
        // record() will prune the first event before pushing second
        engine.record(second.clone());

        let result = engine.check(&second);
        assert!(!result.is_retry);
    }

    // 6. Confidence decays with time gap
    #[test]
    fn test_confidence_decays_with_gap() {
        let mut engine = RetryHeuristicEngine::new(30.0, 0.001).unwrap(); // near-zero to observe all values

        let base_time = Utc::now();

        let mut first = make_llm_event("task-1", "gpt-4o");
        first.occurred_at = base_time;
        let first = with_error(first, "rate_limit"); // base = 1.0
        engine.record(first);

        // gap = 15s → decay = 1 - 15/30 = 0.5 → confidence = 1.0 * 0.5 = 0.5
        let mut second = make_llm_event("task-1", "gpt-4o");
        second.occurred_at = base_time + Duration::seconds(15);
        engine.record(second.clone());

        let result = engine.check(&second);
        assert!(result.is_retry); // threshold=0 so any positive confidence matches
        let expected = 1.0 * (1.0 - 15.0 / 30.0);
        assert!((result.confidence - expected).abs() < 0.001);
    }

    // 7. Prunes old events on record
    #[test]
    fn test_prunes_old_events() {
        let mut engine = RetryHeuristicEngine::new(30.0, 0.8).unwrap();

        let base_time = Utc::now();

        // Record an event at t=0
        let mut first = make_llm_event("task-1", "gpt-4o");
        first.occurred_at = base_time;
        let first = with_error(first, "rate_limit");
        engine.record(first);

        // Record a second event at t=31 — this should prune the first
        let mut second = make_llm_event("task-1", "gpt-4o");
        second.occurred_at = base_time + Duration::seconds(31);
        engine.record(second.clone());

        // Only the second event should remain in the bucket
        let bucket = engine.recent_events.get("task-1").unwrap();
        assert_eq!(bucket.len(), 1);
        assert_eq!(bucket[0].event_id, second.event_id);
    }

    // 8. Default window=30, threshold=0.8 constants
    #[test]
    fn test_defaults() {
        assert_eq!(DEFAULT_WINDOW_SECONDS, 30.0);
        assert_eq!(DEFAULT_THRESHOLD, 0.8);

        let engine = RetryHeuristicEngine::new(DEFAULT_WINDOW_SECONDS, DEFAULT_THRESHOLD).unwrap();
        assert_eq!(engine.window_seconds(), 30.0);
        assert_eq!(engine.threshold(), 0.8);
    }
}
