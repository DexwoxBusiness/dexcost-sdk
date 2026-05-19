//! SSE streaming aggregators for OpenAI and Anthropic chat completions.
//!
//! Mirrors the Python `dexcost.clients.tracked_openai_stream` and
//! `tracked_anthropic_stream` helpers. The aggregators consume a stream of
//! Server-Sent Events (`data: ...` lines) and incrementally accumulate token
//! usage. When the stream ends, callers can produce a synthetic response map
//! suitable for [`crate::clients::wrappers::record_openai_response`] /
//! [`crate::clients::wrappers::record_anthropic_response`].
//!
//! # Feature flag
//!
//! This module is gated behind the `streaming` feature.
//!
//! # Typical usage
//!
//! ```ignore
//! use bytes::Bytes;
//! use futures::StreamExt;
//! use dexcost::clients::streaming::{drain_openai_stream};
//! use dexcost::clients::wrappers::record_openai_response;
//!
//! # async fn _example(
//! #     mut buffer: dexcost::transport::buffer::EventBuffer,
//! #     pricing: dexcost::pricing::engine::PricingEngine,
//! #     stream: impl futures::Stream<Item = Result<Bytes, std::io::Error>> + Unpin,
//! # ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
//! let synth = drain_openai_stream(stream).await?;
//! record_openai_response(&mut buffer, &pricing, "task-123", &synth).await?;
//! # Ok(())
//! # }
//! ```

use std::pin::Pin;

use bytes::Bytes;
use futures::Stream;
use futures::StreamExt;
use serde_json::{json, Value};

/// Error returned while aggregating an SSE stream.
#[derive(Debug, thiserror::Error)]
pub enum StreamAggregateError {
    #[error("stream transport error: {0}")]
    Transport(String),
    #[error("invalid UTF-8 in SSE chunk: {0}")]
    Utf8(#[from] std::str::Utf8Error),
}

/// In-memory aggregator for OpenAI chat-completion SSE streams.
///
/// OpenAI emits one `data: {...}` event per chunk. The terminal `data: [DONE]`
/// event is treated as end-of-stream. Token usage is reported on the final
/// chunk when the request was made with `stream_options.include_usage = true`.
#[derive(Default, Debug, Clone)]
pub struct OpenAIStreamAggregator {
    pub model: Option<String>,
    pub prompt_tokens: Option<i64>,
    pub completion_tokens: Option<i64>,
    pub cached_tokens: Option<i64>,
    pub finished: bool,
}

impl OpenAIStreamAggregator {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed one JSON event extracted from a `data: ...` SSE line.
    pub fn feed(&mut self, value: &Value) {
        if self.model.is_none() {
            if let Some(m) = value.get("model").and_then(|v| v.as_str()) {
                self.model = Some(m.to_string());
            }
        }

        if let Some(usage) = value.get("usage") {
            if let Some(n) = usage.get("prompt_tokens").and_then(|v| v.as_i64()) {
                self.prompt_tokens = Some(n);
            }
            if let Some(n) = usage.get("completion_tokens").and_then(|v| v.as_i64()) {
                self.completion_tokens = Some(n);
            }
            if let Some(c) = usage
                .get("prompt_tokens_details")
                .and_then(|d| d.get("cached_tokens"))
                .and_then(|v| v.as_i64())
            {
                self.cached_tokens = Some(c);
            }
        }
    }

    /// Return a synthetic non-streaming response shape suitable for
    /// [`crate::clients::wrappers::record_openai_response`].
    pub fn into_response_map(self) -> Value {
        let model = self.model.unwrap_or_else(|| "unknown".to_string());
        let mut usage = json!({
            "prompt_tokens": self.prompt_tokens.unwrap_or(0),
            "completion_tokens": self.completion_tokens.unwrap_or(0),
        });
        if let Some(cached) = self.cached_tokens {
            usage["prompt_tokens_details"] = json!({ "cached_tokens": cached });
        }
        json!({ "model": model, "usage": usage })
    }
}

/// In-memory aggregator for Anthropic message SSE streams.
///
/// The relevant events are:
/// - `message_start` — carries the initial `usage.input_tokens` and `model`.
/// - `message_delta` — the *final* `usage.output_tokens` (cumulative).
/// - `message_stop` — terminator.
#[derive(Default, Debug, Clone)]
pub struct AnthropicStreamAggregator {
    pub model: Option<String>,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub cache_read_input_tokens: Option<i64>,
    pub finished: bool,
}

impl AnthropicStreamAggregator {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed one JSON event extracted from a `data: ...` SSE line.
    pub fn feed(&mut self, value: &Value) {
        let event_type = value.get("type").and_then(|v| v.as_str()).unwrap_or("");
        match event_type {
            "message_start" => {
                if let Some(message) = value.get("message") {
                    if self.model.is_none() {
                        if let Some(m) = message.get("model").and_then(|v| v.as_str()) {
                            self.model = Some(m.to_string());
                        }
                    }
                    if let Some(usage) = message.get("usage") {
                        if let Some(n) = usage.get("input_tokens").and_then(|v| v.as_i64()) {
                            self.input_tokens = Some(n);
                        }
                        if let Some(n) = usage.get("output_tokens").and_then(|v| v.as_i64()) {
                            self.output_tokens = Some(n);
                        }
                        if let Some(n) = usage
                            .get("cache_read_input_tokens")
                            .and_then(|v| v.as_i64())
                        {
                            self.cache_read_input_tokens = Some(n);
                        }
                    }
                }
            }
            "message_delta" => {
                if let Some(usage) = value.get("usage") {
                    if let Some(n) = usage.get("output_tokens").and_then(|v| v.as_i64()) {
                        // The delta carries the *final* cumulative output count.
                        self.output_tokens = Some(n);
                    }
                }
            }
            "message_stop" => {
                self.finished = true;
            }
            _ => {}
        }
    }

    /// Return a synthetic non-streaming response shape suitable for
    /// [`crate::clients::wrappers::record_anthropic_response`].
    pub fn into_response_map(self) -> Value {
        let model = self.model.unwrap_or_else(|| "unknown".to_string());
        let mut usage = json!({
            "input_tokens": self.input_tokens.unwrap_or(0),
            "output_tokens": self.output_tokens.unwrap_or(0),
        });
        if let Some(cached) = self.cache_read_input_tokens {
            usage["cache_read_input_tokens"] = json!(cached);
        }
        json!({ "model": model, "usage": usage })
    }
}

// ---------------------------------------------------------------------------
// SSE line parsing helpers
// ---------------------------------------------------------------------------

/// Iterate over the `data:` payloads in a buffered SSE chunk and pass each as
/// a JSON value to `feed`. Skips the SSE terminator `[DONE]`.
fn feed_sse_chunk<F>(buffer: &str, mut feed: F)
where
    F: FnMut(&Value),
{
    for line in buffer.lines() {
        // RFC 8895: lines starting with ":" are comments; ignore.
        if line.starts_with(':') {
            continue;
        }
        let payload = match line.strip_prefix("data:") {
            Some(p) => p.trim_start(),
            None => continue,
        };
        if payload.is_empty() || payload == "[DONE]" {
            continue;
        }
        if let Ok(value) = serde_json::from_str::<Value>(payload) {
            feed(&value);
        }
    }
}

/// Drain an OpenAI SSE byte stream, returning a synthetic response map ready
/// to be passed to [`crate::clients::wrappers::record_openai_response`].
///
/// Any transport error short-circuits the stream and is propagated.
pub async fn drain_openai_stream<S, E>(
    mut stream: Pin<Box<S>>,
) -> Result<Value, StreamAggregateError>
where
    S: Stream<Item = Result<Bytes, E>> + ?Sized,
    E: std::fmt::Display,
{
    let mut agg = OpenAIStreamAggregator::new();
    let mut text_buf = String::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| StreamAggregateError::Transport(e.to_string()))?;
        let s = std::str::from_utf8(&chunk)?;
        text_buf.push_str(s);

        // Process every complete event (terminated by a blank line).
        while let Some(idx) = text_buf.find("\n\n") {
            let event = text_buf[..idx].to_string();
            text_buf.drain(..idx + 2);
            feed_sse_chunk(&event, |v| agg.feed(v));
        }
    }
    if !text_buf.is_empty() {
        feed_sse_chunk(&text_buf, |v| agg.feed(v));
    }
    Ok(agg.into_response_map())
}

/// Drain an Anthropic SSE byte stream, returning a synthetic response map
/// ready to be passed to
/// [`crate::clients::wrappers::record_anthropic_response`].
pub async fn drain_anthropic_stream<S, E>(
    mut stream: Pin<Box<S>>,
) -> Result<Value, StreamAggregateError>
where
    S: Stream<Item = Result<Bytes, E>> + ?Sized,
    E: std::fmt::Display,
{
    let mut agg = AnthropicStreamAggregator::new();
    let mut text_buf = String::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| StreamAggregateError::Transport(e.to_string()))?;
        let s = std::str::from_utf8(&chunk)?;
        text_buf.push_str(s);
        while let Some(idx) = text_buf.find("\n\n") {
            let event = text_buf[..idx].to_string();
            text_buf.drain(..idx + 2);
            feed_sse_chunk(&event, |v| agg.feed(v));
        }
    }
    if !text_buf.is_empty() {
        feed_sse_chunk(&text_buf, |v| agg.feed(v));
    }
    Ok(agg.into_response_map())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use futures::stream;
    use serde_json::json;

    #[test]
    fn openai_aggregator_collects_model_and_usage() {
        let mut agg = OpenAIStreamAggregator::new();
        agg.feed(&json!({
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [{ "delta": { "content": "Hello" } }],
        }));
        agg.feed(&json!({
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "choices": [{ "delta": { "content": " world" } }],
        }));
        agg.feed(&json!({
            "id": "chatcmpl-1",
            "model": "gpt-4o",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": { "cached_tokens": 2 }
            }
        }));

        let synth = agg.into_response_map();
        assert_eq!(synth["model"], "gpt-4o");
        assert_eq!(synth["usage"]["prompt_tokens"], 10);
        assert_eq!(synth["usage"]["completion_tokens"], 5);
        assert_eq!(synth["usage"]["prompt_tokens_details"]["cached_tokens"], 2);
    }

    #[test]
    fn openai_aggregator_handles_no_usage() {
        let mut agg = OpenAIStreamAggregator::new();
        agg.feed(&json!({
            "model": "gpt-4o-mini",
            "choices": [{ "delta": { "content": "hi" } }]
        }));
        let synth = agg.into_response_map();
        assert_eq!(synth["model"], "gpt-4o-mini");
        assert_eq!(synth["usage"]["prompt_tokens"], 0);
        assert_eq!(synth["usage"]["completion_tokens"], 0);
        assert!(synth["usage"].get("prompt_tokens_details").is_none());
    }

    #[test]
    fn anthropic_aggregator_collects_input_and_output_from_events() {
        let mut agg = AnthropicStreamAggregator::new();
        agg.feed(&json!({
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-3-5-sonnet-20241022",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 4
                }
            }
        }));
        agg.feed(&json!({
            "type": "content_block_delta",
            "index": 0,
            "delta": { "type": "text_delta", "text": "Hello" }
        }));
        agg.feed(&json!({
            "type": "message_delta",
            "delta": { "stop_reason": "end_turn" },
            "usage": { "output_tokens": 27 }
        }));
        agg.feed(&json!({ "type": "message_stop" }));

        assert!(agg.finished);
        let synth = agg.into_response_map();
        assert_eq!(synth["model"], "claude-3-5-sonnet-20241022");
        assert_eq!(synth["usage"]["input_tokens"], 12);
        // Final cumulative count, not the placeholder from message_start.
        assert_eq!(synth["usage"]["output_tokens"], 27);
        assert_eq!(synth["usage"]["cache_read_input_tokens"], 4);
    }

    #[test]
    fn anthropic_aggregator_handles_missing_message_start() {
        // Some integrators only forward delta events; aggregator must not panic.
        let mut agg = AnthropicStreamAggregator::new();
        agg.feed(&json!({
            "type": "message_delta",
            "delta": { "stop_reason": "end_turn" },
            "usage": { "output_tokens": 4 }
        }));
        let synth = agg.into_response_map();
        assert_eq!(synth["model"], "unknown");
        assert_eq!(synth["usage"]["input_tokens"], 0);
        assert_eq!(synth["usage"]["output_tokens"], 4);
    }

    #[test]
    fn feed_sse_chunk_skips_done_and_comments() {
        let mut events: Vec<Value> = Vec::new();
        let chunk = ": keep-alive\n\
             data: {\"x\":1}\n\
             \n\
             data: {\"x\":2}\n\
             data: [DONE]\n";
        feed_sse_chunk(chunk, |v| events.push(v.clone()));
        assert_eq!(events.len(), 2);
        assert_eq!(events[0], json!({ "x": 1 }));
        assert_eq!(events[1], json!({ "x": 2 }));
    }

    #[tokio::test]
    async fn drain_openai_stream_aggregates_usage() {
        // Three SSE chunks: two delta events and one usage event, each followed
        // by the SSE event delimiter (\n\n).
        let chunks: Vec<Result<Bytes, std::io::Error>> = vec![
            Ok(Bytes::from(
                "data: {\"model\":\"gpt-4o\",\"choices\":[{\"delta\":{\"content\":\"Hi\"}}]}\n\n",
            )),
            Ok(Bytes::from(
                "data: {\"model\":\"gpt-4o\",\"choices\":[{\"delta\":{\"content\":\" \"}}]}\n\n",
            )),
            Ok(Bytes::from(
                "data: {\"model\":\"gpt-4o\",\"usage\":{\"prompt_tokens\":7,\"completion_tokens\":3}}\n\n",
            )),
            Ok(Bytes::from("data: [DONE]\n\n")),
        ];
        let s = stream::iter(chunks);
        let synth = drain_openai_stream(Box::pin(s)).await.expect("drain ok");
        assert_eq!(synth["model"], "gpt-4o");
        assert_eq!(synth["usage"]["prompt_tokens"], 7);
        assert_eq!(synth["usage"]["completion_tokens"], 3);
    }

    #[tokio::test]
    async fn drain_openai_stream_propagates_transport_errors() {
        let chunks: Vec<Result<Bytes, std::io::Error>> = vec![
            Ok(Bytes::from("data: {\"model\":\"gpt-4o\"}\n\n")),
            Err(std::io::Error::new(
                std::io::ErrorKind::ConnectionReset,
                "reset",
            )),
        ];
        let s = stream::iter(chunks);
        let res = drain_openai_stream(Box::pin(s)).await;
        match res {
            Err(StreamAggregateError::Transport(msg)) => {
                assert!(msg.contains("reset"), "unexpected error: {msg}");
            }
            other => panic!("expected transport error, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn drain_anthropic_stream_aggregates_message_events() {
        let chunks: Vec<Result<Bytes, std::io::Error>> = vec![
            Ok(Bytes::from(
                "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"model\":\"claude-3\",\"usage\":{\"input_tokens\":10,\"output_tokens\":1}}}\n\n",
            )),
            Ok(Bytes::from(
                "event: message_delta\ndata: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":42}}\n\n",
            )),
            Ok(Bytes::from(
                "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
            )),
        ];
        let s = stream::iter(chunks);
        let synth = drain_anthropic_stream(Box::pin(s)).await.expect("drain ok");
        assert_eq!(synth["model"], "claude-3");
        assert_eq!(synth["usage"]["input_tokens"], 10);
        assert_eq!(synth["usage"]["output_tokens"], 42);
    }
}
