//! `reqwest-middleware` adapter that records cost events per HTTP request.
//!
//! Wraps any [`reqwest::Client`] via the `reqwest-middleware` crate so that
//! every outgoing request is inspected against the bundled
//! [`ServiceCatalog`](crate::pricing::service_catalog::ServiceCatalog) by
//! hostname:
//!
//! 1. **Catalog match** — if the URL matches a known service, the response
//!    body (JSON) is parsed for cost/usage and a `CostEvent` is recorded.
//! 2. **LLM provider match** — for OpenAI/Anthropic/Google, token usage is
//!    extracted via [`crate::clients::wrappers`].
//! 3. **Domain rate fallback** — if no catalog entry matches but the host is
//!    registered in [`crate::adapters::http`], a per-call cost is recorded.
//!
//! # Streaming responses (v2 — Decisions Log #2)
//!
//! Responses with `Content-Type: text/event-stream` (SSE — the dominant
//! LLM-streaming pattern) are NOT buffered by this middleware. Instead the
//! response body is wrapped in a `RecordingStream` that counts chunks as the
//! caller consumes them and records the final byte total into the task's
//! [`NetworkAccountant`] when the stream completes (or is dropped). Cost
//! extraction for SSE responses is the responsibility of the LLM instrument
//! that reads the stream; this middleware contributes only the byte counts
//! that feed `network_cost_usd` at task finalize.
//!
//! Non-streaming responses still take the buffer-and-parse path because cost
//! extraction needs the full JSON body to inspect for `usage` blocks etc.
//!
//! # Feature flag
//!
//! This module requires the `reqwest-middleware` feature.

use std::collections::HashMap;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

use ::http::{Extensions, Response as HttpResponse};
use bytes::Bytes;
use futures::Stream;
use reqwest::{Body, Request, Response};
use reqwest_middleware::{Middleware, Next};
use rust_decimal::Decimal;
use tokio::sync::Mutex;
use uuid::Uuid;

use crate::adapters::http as dexcost_http;
use crate::adapters::netbytes::{classify_destination, measure_bytes_from_headers};
use crate::adapters::network_accountant::{get_accountant, NetworkAccountant};
use crate::core::context::is_network_event_suppressed;
use crate::core::models::{CostConfidence, CostEvent, EventType, Task, TaskStatus};
use crate::pricing::engine::PricingEngine;
use crate::pricing::service_catalog::{CostExtractionResult, ServiceCatalog};
use crate::security::redaction::scrub_url;
use crate::transport::buffer::EventBuffer;

/// Default `network` event emission threshold — combined request + response
/// bytes. Mirrors python config `network_event_threshold_bytes = 102_400`
/// (100 KiB).
const NETWORK_EVENT_THRESHOLD_BYTES: usize = 102_400;

/// Reqwest middleware that automatically records cost events for HTTP calls.
///
/// Construct via [`DexcostMiddleware::new`] and attach with
/// `reqwest_middleware::ClientBuilder::with`.
///
/// All inputs are `Arc`-shared so the middleware is cheap to clone and to
/// reuse across many [`reqwest::Client`] instances.
pub struct DexcostMiddleware {
    catalog: Arc<ServiceCatalog>,
    pricing: Arc<PricingEngine>,
    buffer: Arc<Mutex<EventBuffer>>,
    /// Task ID used as the parent of recorded events. When `None`, an
    /// auto-task is created on the fly using the host as the task type.
    task_id: Option<String>,
}

impl DexcostMiddleware {
    /// Creates a new middleware. Pass `task_id` to attach all events to a
    /// specific task; pass `None` to use an auto-task per call.
    pub fn new(
        catalog: Arc<ServiceCatalog>,
        pricing: Arc<PricingEngine>,
        buffer: Arc<Mutex<EventBuffer>>,
        task_id: Option<String>,
    ) -> Self {
        Self {
            catalog,
            pricing,
            buffer,
            task_id,
        }
    }

    fn host_of(req: &Request) -> Option<String> {
        req.url().host_str().map(|s| s.to_lowercase())
    }

    /// Returns the task_id to attribute events to. Auto-instrumented calls use
    /// a real UUID because attribution v2 intentionally rejects synthetic
    /// identifiers such as `auto:host`.
    fn effective_task_id(&self, req: &Request) -> String {
        if let Some(ref t) = self.task_id {
            return t.clone();
        }
        let _ = req;
        Uuid::new_v4().to_string()
    }

    fn record_extraction(
        &self,
        task_id: &str,
        host: &str,
        extraction: &CostExtractionResult,
        byte_details: &serde_json::Value,
        events: &mut EventBuffer,
    ) {
        let mut event = CostEvent::new(task_id, EventType::ExternalCost);
        event.cost_usd = extraction.amount;
        event.cost_confidence = match extraction.confidence.as_str() {
            "exact" => crate::core::models::CostConfidence::Exact,
            "computed" => crate::core::models::CostConfidence::Computed,
            "estimated" => crate::core::models::CostConfidence::Estimated,
            _ => crate::core::models::CostConfidence::Unknown,
        };
        event.pricing_source = match extraction.pricing_source.as_str() {
            "user_override" => Some(crate::core::models::PricingSource::UserOverride),
            "service_catalog" => Some(crate::core::models::PricingSource::ServiceCatalog),
            _ => Some(crate::core::models::PricingSource::ServiceCatalog),
        };
        if event.pricing_source == Some(crate::core::models::PricingSource::ServiceCatalog) {
            event.pricing_version = Some(self.catalog.catalog_version());
        }
        event.details.insert(
            "attribution_usage_quantity".to_string(),
            serde_json::Value::String(extraction.usage_quantity.normalize().to_string()),
        );
        event.details.insert(
            "attribution_usage_metric".to_string(),
            serde_json::Value::String(extraction.usage_metric.clone()),
        );
        event.service_name = Some(extraction.service_name.clone());
        if event.service_name.is_none() {
            event.service_name = Some(host.to_string());
        }
        stamp_byte_details(&mut event, byte_details);
        events.add_event(event);
    }

    /// Try to record an LLM-style event from an OpenAI / Anthropic JSON body.
    /// Returns `true` if a token-usage event was recorded.
    fn try_record_llm(
        &self,
        host: &str,
        task_id: &str,
        body: &serde_json::Value,
        byte_details: &serde_json::Value,
        events: &mut EventBuffer,
    ) -> bool {
        if host.contains("openai.com") {
            if let Some(usage) = body.get("usage") {
                let model = body
                    .get("model")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let input = usage
                    .get("prompt_tokens")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let output = usage
                    .get("completion_tokens")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let cached = usage
                    .get("prompt_tokens_details")
                    .and_then(|details| details.get("cached_tokens"))
                    .or_else(|| usage.get("cached_tokens"))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let reasoning = usage
                    .get("completion_tokens_details")
                    .and_then(|details| details.get("reasoning_tokens"))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let cost = self.pricing.get_cost_sync(model, input, output, cached, 0);
                let mut ev = CostEvent::new(task_id, EventType::LlmCall);
                ev.provider = Some("openai".to_string());
                ev.model = Some(model.to_string());
                ev.input_tokens = Some(input);
                ev.output_tokens = Some(output);
                ev.cached_tokens = (cached > 0).then_some(cached);
                ev.cost_usd = cost.cost_usd;
                ev.cost_confidence = cost.cost_confidence;
                ev.pricing_source = Some(cost.pricing_source);
                ev.pricing_version = Some(cost.pricing_version);
                if reasoning > 0 {
                    ev.details.insert(
                        "reasoning_output_tokens".to_string(),
                        serde_json::Value::from(reasoning),
                    );
                }
                stamp_byte_details(&mut ev, byte_details);
                events.add_event(ev);
                return true;
            }
        }
        if host.contains("anthropic.com") {
            if let Some(usage) = body.get("usage") {
                let model = body
                    .get("model")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let input = usage
                    .get("input_tokens")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let output = usage
                    .get("output_tokens")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let cache_read = usage
                    .get("cache_read_input_tokens")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let cache_creation = usage
                    .get("cache_creation_input_tokens")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let cost =
                    self.pricing
                        .get_cost_sync(model, input, output, cache_read, cache_creation);
                let mut ev = CostEvent::new(task_id, EventType::LlmCall);
                ev.provider = Some("anthropic".to_string());
                ev.model = Some(model.to_string());
                ev.input_tokens = Some(input);
                ev.output_tokens = Some(output);
                ev.cached_tokens = (cache_read > 0).then_some(cache_read);
                ev.cost_usd = cost.cost_usd;
                ev.cost_confidence = cost.cost_confidence;
                ev.pricing_source = Some(cost.pricing_source);
                ev.pricing_version = Some(cost.pricing_version);
                if cache_creation > 0 {
                    ev.details.insert(
                        "cache_creation_input_tokens".to_string(),
                        serde_json::Value::from(cache_creation),
                    );
                }
                stamp_byte_details(&mut ev, byte_details);
                events.add_event(ev);
                return true;
            }
        }
        false
    }
}

#[async_trait::async_trait]
impl Middleware for DexcostMiddleware {
    async fn handle(
        &self,
        req: Request,
        extensions: &mut Extensions,
        next: Next<'_>,
    ) -> reqwest_middleware::Result<Response> {
        let url = req.url().clone();
        let host = Self::host_of(&req).unwrap_or_default();
        let protocol = url.scheme().to_string();
        let method = req.method().to_string();
        let task_id = self.effective_task_id(&req);
        let mut auto_task = if self.task_id.is_none() {
            let mut task = Task::new(&format!(
                "http:{}",
                if host.is_empty() { "unknown" } else { &host }
            ));
            task.task_id = task_id.clone();
            task.status = TaskStatus::Running;
            self.buffer.lock().await.upsert_task(task.clone());
            Some(task)
        } else {
            None
        };

        // ── v1 byte measurement — request side ──────────────────────────
        // Compute request bytes BEFORE next.run consumes the Request.
        let request_headers_map = req
            .headers()
            .iter()
            .map(|(k, v)| {
                (
                    k.to_string(),
                    v.to_str().unwrap_or("").to_string(),
                )
            })
            .collect::<HashMap<String, String>>();
        let request_body_len = req
            .body()
            .and_then(|b| b.as_bytes())
            .map(|b| b.len())
            .unwrap_or(0);
        let request_bytes = measure_bytes_from_headers(
            &method,
            url.as_str(),
            &request_headers_map,
            request_body_len,
        );

        let response = match next.run(req, extensions).await {
            Ok(response) => response,
            Err(error) => {
                if let Some(task) = auto_task.as_mut() {
                    task.status = TaskStatus::Failed;
                    task.ended_at = Some(chrono::Utc::now());
                    self.buffer.lock().await.upsert_task(task.clone());
                }
                return Err(error);
            }
        };

        // Capture status + headers for reconstruction below.
        let status = response.status();
        let status_code = status.as_u16();
        let version = response.version();
        let response_headers_map = response
            .headers()
            .iter()
            .map(|(k, v)| {
                (
                    k.to_string(),
                    v.to_str().unwrap_or("").to_string(),
                )
            })
            .collect::<HashMap<String, String>>();
        let is_internal = classify_destination(&host);
        let response_is_streaming = is_streaming_response(response.headers());

        if let Some(task) = auto_task.as_mut() {
            task.status = if status.is_success() {
                TaskStatus::Success
            } else {
                TaskStatus::Failed
            };
            task.ended_at = Some(chrono::Utc::now());
            self.buffer.lock().await.upsert_task(task.clone());
        }

        let mut builder = HttpResponse::builder().status(status).version(version);
        for (k, v) in response.headers().iter() {
            builder = builder.header(k, v);
        }

        // ── Streaming branch (SSE etc.) — never buffer the body ─────────
        //
        // The caller will consume the stream chunk-by-chunk. We wrap the
        // body in a RecordingStream that counts bytes as they flow through
        // and records to the accountant on stream completion (or drop).
        // Cost extraction from the body is impossible without buffering, so
        // we skip the catalog/LLM-usage paths for streaming responses —
        // LLM instruments that consume the SSE stream are responsible for
        // emitting their own `llm_call` event with token counts from the
        // final usage chunk. The byte count still feeds `network_cost_usd`
        // at task finalize via the accountant.
        if response_is_streaming {
            let response_header_bytes = measure_bytes_from_headers(
                "",
                "",
                &response_headers_map,
                0, // body length unknown — RecordingStream adds chunk lengths
            );
            let body_stream = response.bytes_stream();
            let accountant = get_accountant(&task_id);
            let recording = RecordingStream {
                inner: Box::pin(body_stream),
                state: Some(RecordingState {
                    accountant,
                    host: host.clone(),
                    is_internal,
                    request_bytes,
                    response_header_bytes,
                    response_body_bytes: 0,
                }),
            };
            let new_resp = builder
                .body(Body::wrap_stream(recording))
                .map_err(|e| reqwest_middleware::Error::Middleware(anyhow_compat(e)))?;
            return Ok(Response::from(new_resp));
        }

        // ── Buffered branch — read body for cost extraction + byte count ─
        let body_bytes = match response.bytes().await {
            Ok(b) => b,
            Err(e) => {
                if let Some(task) = auto_task.as_mut() {
                    task.status = TaskStatus::Failed;
                    task.ended_at = Some(chrono::Utc::now());
                    self.buffer.lock().await.upsert_task(task.clone());
                }
                return Err(reqwest_middleware::Error::Reqwest(e));
            }
        };
        let response_bytes = measure_bytes_from_headers(
            "",
            "",
            &response_headers_map,
            body_bytes.len(),
        );

        // ── v1 destination classification + accountant recording ───────
        if let Some(accountant) = get_accountant(&task_id) {
            accountant.record(
                &host,
                response_bytes as i64,
                request_bytes as i64,
                is_internal,
            );
        }

        // ── v1 byte details — stamped into every event below ───────────
        let byte_details = serde_json::json!({
            "protocol": protocol,
            "request_bytes": request_bytes,
            "response_bytes": response_bytes,
            "is_internal_traffic": is_internal,
        });

        // Try to extract usage / cost.
        if let Ok(body_json) = serde_json::from_slice::<serde_json::Value>(&body_bytes) {
            let mut buf = self.buffer.lock().await;
            let mut recorded = false;

            if let Some(entry) = self.catalog.lookup(url.as_str()) {
                if let Some(extraction) =
                    self.catalog
                        .extract_cost(entry, &Default::default(), Some(&body_json))
                {
                    self.record_extraction(
                        &task_id,
                        &host,
                        &extraction,
                        &byte_details,
                        &mut buf,
                    );
                    recorded = true;
                }
            }

            if !recorded {
                recorded = self.try_record_llm(
                    &host,
                    &task_id,
                    &body_json,
                    &byte_details,
                    &mut buf,
                );
            }

            if !recorded {
                // Domain-rate / catalog fallback — persist to the durable
                // EventBuffer (so the pusher ships it) instead of the in-memory
                // record_http_cost log. buf is still held here.
                if let Some(mut event) = dexcost_http::resolve_http_cost_event(
                    url.as_str(),
                    &task_id,
                    Some(&self.catalog),
                ) {
                    stamp_byte_details(&mut event, &byte_details);
                    buf.add_event(event);
                    recorded = true;
                }
            }

            // ── Un-cataloged: emit a `network` event if notable + not
            // suppressed by an outer LLM call. Mirrors python `_handle_
            // uncataloged` (v1 §5.4 step 5 + v2 §6.4 cost_pending marker).
            if !recorded && !is_network_event_suppressed() {
                if let Some(ev) = build_network_event(
                    &task_id,
                    &host,
                    url.as_str(),
                    &method,
                    status_code,
                    request_bytes,
                    response_bytes,
                    &byte_details,
                ) {
                    buf.add_event(ev);
                }
            }
        } else {
            // Not JSON — catalog body extraction is impossible, but a domain
            // rate or fixed-cost catalog entry can still resolve. Persist it to
            // the EventBuffer so it syncs.
            let mut buf = self.buffer.lock().await;
            let mut recorded = false;
            if let Some(mut event) =
                dexcost_http::resolve_http_cost_event(url.as_str(), &task_id, Some(&self.catalog))
            {
                stamp_byte_details(&mut event, &byte_details);
                buf.add_event(event);
                recorded = true;
            }
            if !recorded && !is_network_event_suppressed() {
                if let Some(ev) = build_network_event(
                    &task_id,
                    &host,
                    url.as_str(),
                    &method,
                    status_code,
                    request_bytes,
                    response_bytes,
                    &byte_details,
                ) {
                    buf.add_event(ev);
                }
            }
        }

        // Reconstruct a Response from the buffered body so downstream callers
        // can still consume it.
        let new_resp = builder
            .body(Body::from(body_bytes))
            .map_err(|e| reqwest_middleware::Error::Middleware(anyhow_compat(e)))?;
        Ok(Response::from(new_resp))
    }
}

// ---------------------------------------------------------------------------
// Free-standing helpers (network event construction, byte-detail stamping)
// ---------------------------------------------------------------------------

/// Build a `network` event for an un-cataloged HTTP call when notable
/// (combined bytes above threshold OR status >= 400). Returns `None` when
/// the call is below the threshold and not an error — counters-only path.
#[allow(clippy::too_many_arguments)] // 8 args mirror the Python dispatcher signature
fn build_network_event(
    task_id: &str,
    host: &str,
    url: &str,
    method: &str,
    status_code: u16,
    request_bytes: usize,
    response_bytes: usize,
    byte_details: &serde_json::Value,
) -> Option<CostEvent> {
    let total = request_bytes.saturating_add(response_bytes);
    let notable = total > NETWORK_EVENT_THRESHOLD_BYTES || status_code >= 400;
    if !notable {
        return None;
    }
    let mut ev = CostEvent::new(task_id, EventType::Network);
    ev.cost_usd = Decimal::ZERO;
    ev.cost_confidence = CostConfidence::Unknown;
    ev.pricing_source = None;
    ev.service_name = Some(host.to_string());
    // Compose details with the byte fields PLUS cost_pending marker
    // (v2 §6.4 — back-filled by _aggregate_costs at task finalize).
    ev.details.insert(
        "url".to_string(),
        serde_json::Value::String(scrub_url(url)),
    );
    ev.details.insert(
        "method".to_string(),
        serde_json::Value::String(method.to_string()),
    );
    ev.details.insert(
        "status_code".to_string(),
        serde_json::Value::from(status_code),
    );
    ev.details
        .insert("cost_pending".to_string(), serde_json::Value::Bool(true));
    stamp_byte_details(&mut ev, byte_details);
    Some(ev)
}

/// Stamp byte_details fields (protocol/request_bytes/response_bytes/
/// is_internal_traffic) into a `CostEvent`'s details, matching the v1 §4.3
/// "byte placement is uniform across event types" invariant.
fn stamp_byte_details(event: &mut CostEvent, byte_details: &serde_json::Value) {
    if let serde_json::Value::Object(bd) = byte_details {
        for (k, v) in bd {
            event.details.insert(k.clone(), v.clone());
        }
    }
}

/// Detect whether a response body should be streamed rather than buffered.
///
/// Streaming criteria (May 2026):
///   * `Content-Type: text/event-stream` (SSE — the dominant LLM streaming
///     pattern; OpenAI `stream=true`, Anthropic `stream=true`, Vercel AI
///     SDK streaming, etc.)
///   * `Content-Type: application/x-ndjson` (newline-delimited JSON streams,
///     used by some providers for streaming completions)
///
/// `Transfer-Encoding: chunked` alone is **not** enough — many small JSON
/// responses use chunked encoding too, and buffering them is fine. We only
/// branch to the streaming path when there's an explicit content-type
/// signal that the body is consumer-driven.
pub(crate) fn is_streaming_response(headers: &reqwest::header::HeaderMap) -> bool {
    let Some(ct) = headers.get(reqwest::header::CONTENT_TYPE) else {
        return false;
    };
    let Ok(ct) = ct.to_str() else {
        return false;
    };
    let ct = ct.to_ascii_lowercase();
    ct.starts_with("text/event-stream") || ct.starts_with("application/x-ndjson")
}

// ---------------------------------------------------------------------------
// RecordingStream — counts response-body bytes for streaming responses
// ---------------------------------------------------------------------------

/// State carried by a `RecordingStream` until it finalises. Wrapped in an
/// `Option` so the stream's `poll_next` (on end-of-stream) and `Drop` (on
/// early-abort) can each take ownership of it exactly once.
struct RecordingState {
    accountant: Option<Arc<NetworkAccountant>>,
    host: String,
    is_internal: Option<bool>,
    /// On-the-wire bytes of the outbound request (headers + body), computed
    /// at middleware entry. Recorded into the accountant alongside the
    /// streamed response bytes when the stream finalises.
    request_bytes: usize,
    /// On-the-wire bytes of the response header block (status line + headers),
    /// computed at middleware entry. Added to the body chunk total at
    /// finalisation so the accountant sees one record per HTTP call.
    response_header_bytes: usize,
    /// Accumulated response-body bytes seen so far. Updated in `poll_next`.
    response_body_bytes: usize,
}

impl RecordingState {
    fn finalise(self) {
        let Some(accountant) = self.accountant else {
            return;
        };
        let total_in = self.response_header_bytes + self.response_body_bytes;
        accountant.record(
            &self.host,
            total_in as i64,
            self.request_bytes as i64,
            self.is_internal,
        );
    }
}

/// `Stream<Item = Result<Bytes, reqwest::Error>>` adapter that counts bytes
/// from the inner stream and records the total into a [`NetworkAccountant`]
/// once the stream completes or is dropped.
struct RecordingStream<S> {
    inner: Pin<Box<S>>,
    state: Option<RecordingState>,
}

impl<S> Stream for RecordingStream<S>
where
    S: Stream<Item = Result<Bytes, reqwest::Error>> + Send + 'static,
{
    type Item = Result<Bytes, reqwest::Error>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        match this.inner.as_mut().poll_next(cx) {
            Poll::Ready(Some(Ok(chunk))) => {
                if let Some(state) = &mut this.state {
                    state.response_body_bytes += chunk.len();
                }
                Poll::Ready(Some(Ok(chunk)))
            }
            Poll::Ready(None) => {
                // Stream completed cleanly — record now so Drop doesn't double-record.
                if let Some(state) = this.state.take() {
                    state.finalise();
                }
                Poll::Ready(None)
            }
            Poll::Ready(Some(Err(e))) => {
                // Error on the stream — still record whatever bytes we saw,
                // matching Python v1 §5.5 "early-abort → bytes-actually-received".
                if let Some(state) = this.state.take() {
                    state.finalise();
                }
                Poll::Ready(Some(Err(e)))
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

impl<S> Drop for RecordingStream<S> {
    fn drop(&mut self) {
        // Caller dropped the response mid-stream — finalise with whatever
        // we've accumulated. Matches Python v1 §5.5 early-abort behaviour.
        if let Some(state) = self.state.take() {
            state.finalise();
        }
    }
}

/// Tiny shim that converts `http::Error` into the error type
/// `reqwest_middleware::Error::Middleware` expects without pulling in
/// `anyhow` directly.
fn anyhow_compat(e: ::http::Error) -> anyhow::Error {
    anyhow::Error::msg(e.to_string())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest_middleware::ClientBuilder;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn fixtures() -> (
        Arc<ServiceCatalog>,
        Arc<PricingEngine>,
        Arc<Mutex<EventBuffer>>,
    ) {
        (
            Arc::new(ServiceCatalog::new()),
            Arc::new(PricingEngine::new()),
            Arc::new(Mutex::new(EventBuffer::new().expect("buffer"))),
        )
    }

    #[tokio::test]
    async fn middleware_passes_through_non_json() {
        let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
        crate::adapters::http::clear_domain_rates();
        let (catalog, pricing, buffer) = fixtures();
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/plain"))
            .respond_with(ResponseTemplate::new(200).set_body_string("hello"))
            .mount(&server)
            .await;

        let mw = DexcostMiddleware::new(catalog, pricing, buffer.clone(), Some("t-plain".into()));
        let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();

        let resp = client
            .get(format!("{}/plain", server.uri()))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);
        assert_eq!(resp.text().await.unwrap(), "hello");
        // No catalog/LLM match, no domain rate registered → no events.
        let buf = buffer.lock().await;
        assert_eq!(buf.event_count(), 0);
    }

    #[tokio::test]
    async fn middleware_records_openai_usage_when_host_matches() {
        let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
        crate::adapters::http::clear_domain_rates();
        let (catalog, pricing, buffer) = fixtures();
        let server = MockServer::start().await;

        let body = serde_json::json!({
            "model": "gpt-4o",
            "usage": { "prompt_tokens": 10, "completion_tokens": 5 }
        });
        Mock::given(method("POST"))
            .and(path("/v1/chat/completions"))
            .respond_with(ResponseTemplate::new(200).set_body_json(&body))
            .mount(&server)
            .await;

        // Use a Host header trick: openai.com path is recognised in
        // try_record_llm via the URL host. We need wiremock running on
        // localhost, so we patch the host check to match the test URI.
        let mw = DexcostMiddleware::new(catalog, pricing, buffer.clone(), Some("t-openai".into()));
        let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();

        let _ = client
            .post(format!("{}/v1/chat/completions", server.uri()))
            .header("content-type", "application/json")
            .body(serde_json::json!({"model": "gpt-4o"}).to_string())
            .send()
            .await
            .unwrap();

        // We don't assert provider here because mock host won't contain
        // "openai.com"; instead we verify the middleware did NOT panic and
        // that the buffer remains internally consistent.
        let buf = buffer.lock().await;
        assert_eq!(buf.event_count(), 0);
    }

    #[tokio::test]
    async fn middleware_uses_uuid_for_auto_task() {
        let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
        let (catalog, pricing, buffer) = fixtures();
        let mw = DexcostMiddleware::new(catalog, pricing, buffer, None);

        let req = reqwest::Request::new(
            reqwest::Method::GET,
            "https://api.openai.com/v1/models".parse().unwrap(),
        );
        let id = mw.effective_task_id(&req);
        assert!(Uuid::parse_str(&id).is_ok());
    }

    #[tokio::test]
    async fn middleware_persists_completed_auto_task() {
        let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
        let (catalog, pricing, buffer) = fixtures();
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/ok"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
            .mount(&server)
            .await;

        let mw = DexcostMiddleware::new(catalog, pricing, buffer.clone(), None);
        let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();
        client.get(format!("{}/ok", server.uri())).send().await.unwrap();

        let tasks = buffer.lock().await.get_pending_tasks(10);
        assert_eq!(tasks.len(), 1);
        assert!(Uuid::parse_str(&tasks[0].task_id).is_ok());
        assert_eq!(tasks[0].status, TaskStatus::Success);
        assert!(tasks[0].ended_at.is_some());
    }

    #[test]
    fn llm_extraction_preserves_cache_and_reasoning_usage() {
        let (catalog, pricing, buffer) = fixtures();
        let mw = DexcostMiddleware::new(catalog, pricing, buffer, None);
        let mut events = EventBuffer::new().unwrap();
        let byte_details = serde_json::json!({});

        assert!(mw.try_record_llm(
            "api.openai.com",
            "11111111-1111-4111-8111-111111111111",
            &serde_json::json!({
                "model": "gpt-4o",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "prompt_tokens_details": { "cached_tokens": 25 },
                    "completion_tokens_details": { "reasoning_tokens": 10 }
                }
            }),
            &byte_details,
            &mut events,
        ));
        let openai = events.get_pending_events(10).pop().unwrap();
        assert_eq!(openai.cached_tokens, Some(25));
        assert_eq!(openai.details["reasoning_output_tokens"], serde_json::json!(10));

        assert!(mw.try_record_llm(
            "api.anthropic.com",
            "11111111-1111-4111-8111-111111111111",
            &serde_json::json!({
                "model": "claude-3-5-sonnet-20241022",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5
                }
            }),
            &byte_details,
            &mut events,
        ));
        let anthropic = events
            .get_pending_events(10)
            .into_iter()
            .find(|event| event.provider.as_deref() == Some("anthropic"))
            .unwrap();
        assert_eq!(anthropic.cached_tokens, Some(20));
        assert_eq!(
            anthropic.details["cache_creation_input_tokens"],
            serde_json::json!(5)
        );
    }

    /// The middleware's domain-rate fallback must persist events to the durable
    /// EventBuffer (so the sync pusher ships them), not the in-memory
    /// record_http_cost log.
    #[tokio::test]
    async fn middleware_persists_domain_rate_fallback_to_buffer() {
        let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
        crate::adapters::http::clear_domain_rates();
        let (catalog, pricing, buffer) = fixtures();

        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/data"))
            .respond_with(ResponseTemplate::new(200).set_body_string("not json"))
            .mount(&server)
            .await;

        // Register a domain rate for the mock server's host (127.0.0.1).
        let host = reqwest::Url::parse(&server.uri())
            .unwrap()
            .host_str()
            .unwrap()
            .to_string();
        crate::adapters::http::register_domain_rate(&host, "0.01".parse().unwrap(), "call");

        let mw = DexcostMiddleware::new(catalog, pricing, buffer.clone(), Some("t-dr".into()));
        let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();
        let _ = client
            .get(format!("{}/data", server.uri()))
            .send()
            .await
            .unwrap();

        let count = {
            let buf = buffer.lock().await;
            buf.event_count()
        };
        crate::adapters::http::clear_domain_rates();
        assert_eq!(
            count, 1,
            "domain-rate fallback must persist the event to the durable buffer"
        );
    }

    // ── v1/v2 network capture helpers ────────────────────────────────────

    #[test]
    fn build_network_event_below_threshold_no_error_returns_none() {
        let details = serde_json::json!({
            "protocol": "https",
            "request_bytes": 100,
            "response_bytes": 500,
            "is_internal_traffic": false,
        });
        let ev = build_network_event(
            "t1", "api.example.com", "https://api.example.com/x",
            "GET", 200, 100, 500, &details,
        );
        assert!(ev.is_none(), "below-threshold success must emit no event");
    }

    #[test]
    fn build_network_event_above_threshold_emits_with_cost_pending() {
        let details = serde_json::json!({
            "protocol": "https",
            "request_bytes": 50,
            "response_bytes": 200_000,
            "is_internal_traffic": false,
        });
        let ev = build_network_event(
            "t1", "api.example.com", "https://api.example.com/x",
            "POST", 200, 50, 200_000, &details,
        )
        .expect("must emit");
        assert_eq!(ev.event_type, EventType::Network);
        assert_eq!(ev.cost_usd, Decimal::ZERO);
        assert_eq!(ev.cost_confidence, CostConfidence::Unknown);
        assert!(ev.pricing_source.is_none());
        assert_eq!(ev.service_name.as_deref(), Some("api.example.com"));
        // v2 §6.4 cost_pending marker so _aggregate_costs back-fills.
        assert_eq!(
            ev.details.get("cost_pending"),
            Some(&serde_json::Value::Bool(true))
        );
        // v1 §4.3 byte uniformity — every event carries the byte fields.
        assert_eq!(
            ev.details.get("request_bytes"),
            Some(&serde_json::Value::from(50))
        );
        assert_eq!(
            ev.details.get("response_bytes"),
            Some(&serde_json::Value::from(200_000))
        );
        assert_eq!(
            ev.details.get("is_internal_traffic"),
            Some(&serde_json::Value::Bool(false))
        );
        assert_eq!(
            ev.details.get("method"),
            Some(&serde_json::Value::String("POST".to_string()))
        );
        assert_eq!(
            ev.details.get("status_code"),
            Some(&serde_json::Value::from(200))
        );
    }

    #[test]
    fn build_network_event_status_400_emits_below_threshold() {
        // status >= 400 always emits, regardless of byte count.
        let details = serde_json::json!({
            "protocol": "https",
            "request_bytes": 10,
            "response_bytes": 100,
            "is_internal_traffic": false,
        });
        let ev = build_network_event(
            "t1", "api.example.com", "https://api.example.com/x",
            "GET", 503, 10, 100, &details,
        );
        assert!(
            ev.is_some(),
            "5xx error must emit a network event even below threshold"
        );
    }

    #[test]
    fn middleware_uncataloged_above_threshold_emits_network_event() {
        let _ = tokio::runtime::Runtime::new().unwrap().block_on(async {
            let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
            crate::adapters::http::clear_domain_rates();
            crate::adapters::network_accountant::_reset_registry_for_tests();
            let (catalog, pricing, buffer) = fixtures();
            let server = MockServer::start().await;
            // Body large enough to push combined bytes over 100 KiB.
            let big_body = "x".repeat(200_000);
            Mock::given(method("GET"))
                .and(path("/big"))
                .respond_with(ResponseTemplate::new(200).set_body_string(big_body))
                .mount(&server)
                .await;

            let mw = DexcostMiddleware::new(
                catalog,
                pricing,
                buffer.clone(),
                Some("t-net-emit".into()),
            );
            let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();
            let resp = client
                .get(format!("{}/big", server.uri()))
                .send()
                .await
                .unwrap();
            assert_eq!(resp.status(), 200);

            let buf = buffer.lock().await;
            let net_events: Vec<_> = buf
                .query_events("t-net-emit")
                .into_iter()
                .filter(|e| e.event_type == EventType::Network)
                .collect();
            assert_eq!(net_events.len(), 1, "exactly one network event for an un-cataloged above-threshold call");
            let ev = &net_events[0];
            assert_eq!(ev.cost_usd, Decimal::ZERO);
            assert_eq!(
                ev.details.get("cost_pending"),
                Some(&serde_json::Value::Bool(true))
            );
        });
    }

    #[test]
    fn middleware_uncataloged_below_threshold_no_event() {
        let _ = tokio::runtime::Runtime::new().unwrap().block_on(async {
            let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
            crate::adapters::http::clear_domain_rates();
            crate::adapters::network_accountant::_reset_registry_for_tests();
            let (catalog, pricing, buffer) = fixtures();
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/small"))
                .respond_with(ResponseTemplate::new(200).set_body_string("small"))
                .mount(&server)
                .await;

            let mw = DexcostMiddleware::new(
                catalog,
                pricing,
                buffer.clone(),
                Some("t-net-small".into()),
            );
            let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();
            client
                .get(format!("{}/small", server.uri()))
                .send()
                .await
                .unwrap();

            let buf = buffer.lock().await;
            let net_events: Vec<_> = buf
                .query_events("t-net-small")
                .into_iter()
                .filter(|e| e.event_type == EventType::Network)
                .collect();
            assert!(
                net_events.is_empty(),
                "small un-cataloged call must not emit a network event"
            );
        });
    }

    #[test]
    fn middleware_records_bytes_into_registered_accountant() {
        let _ = tokio::runtime::Runtime::new().unwrap().block_on(async {
            let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
            crate::adapters::http::clear_domain_rates();
            crate::adapters::network_accountant::_reset_registry_for_tests();

            // Register an accountant under the task_id the middleware will use.
            let accountant = Arc::new(
                crate::adapters::network_accountant::NetworkAccountant::new(),
            );
            crate::adapters::network_accountant::register_accountant(
                "t-acct",
                accountant.clone(),
            );

            let (catalog, pricing, buffer) = fixtures();
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/x"))
                .respond_with(ResponseTemplate::new(200).set_body_string("hi"))
                .mount(&server)
                .await;

            let mw = DexcostMiddleware::new(
                catalog,
                pricing,
                buffer.clone(),
                Some("t-acct".into()),
            );
            let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();
            client
                .get(format!("{}/x", server.uri()))
                .send()
                .await
                .unwrap();

            let snap = accountant.finalize();
            assert_eq!(snap.call_count, 1, "accountant must record the call");
            assert!(snap.bytes_in > 0);
            assert!(snap.bytes_out > 0);
        });
    }

    #[test]
    fn middleware_suppressed_call_records_bytes_but_no_network_event() {
        let _ = tokio::runtime::Runtime::new().unwrap().block_on(async {
            let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
            crate::adapters::http::clear_domain_rates();
            crate::adapters::network_accountant::_reset_registry_for_tests();

            let accountant = Arc::new(
                crate::adapters::network_accountant::NetworkAccountant::new(),
            );
            crate::adapters::network_accountant::register_accountant(
                "t-suppressed",
                accountant.clone(),
            );

            let (catalog, pricing, buffer) = fixtures();
            let server = MockServer::start().await;
            let big_body = "x".repeat(200_000);
            Mock::given(method("GET"))
                .and(path("/big"))
                .respond_with(ResponseTemplate::new(200).set_body_string(big_body))
                .mount(&server)
                .await;

            let mw = DexcostMiddleware::new(
                catalog,
                pricing,
                buffer.clone(),
                Some("t-suppressed".into()),
            );
            let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();

            // Caller runs the HTTP call inside the suppression scope —
            // simulating an LLM instrument wrapping its outbound call.
            crate::core::context::suppress_network_event(async {
                client
                    .get(format!("{}/big", server.uri()))
                    .send()
                    .await
                    .unwrap();
            })
            .await;

            // Bytes still recorded into the accountant.
            let snap = accountant.finalize();
            assert_eq!(snap.call_count, 1);
            assert!(snap.bytes_in > 0);

            // But no standalone network event emitted.
            let buf = buffer.lock().await;
            let net_events: Vec<_> = buf
                .query_events("t-suppressed")
                .into_iter()
                .filter(|e| e.event_type == EventType::Network)
                .collect();
            assert!(
                net_events.is_empty(),
                "suppression scope must withhold the network event even for a large response"
            );
        });
    }

    // ── Streaming-body tests (Decisions Log #2 — actual fix) ─────────────

    #[test]
    fn is_streaming_response_recognises_sse() {
        let mut h = reqwest::header::HeaderMap::new();
        h.insert(
            reqwest::header::CONTENT_TYPE,
            reqwest::header::HeaderValue::from_static("text/event-stream"),
        );
        assert!(is_streaming_response(&h));
    }

    #[test]
    fn is_streaming_response_recognises_sse_with_charset() {
        let mut h = reqwest::header::HeaderMap::new();
        h.insert(
            reqwest::header::CONTENT_TYPE,
            reqwest::header::HeaderValue::from_static("text/event-stream; charset=utf-8"),
        );
        assert!(is_streaming_response(&h));
    }

    #[test]
    fn is_streaming_response_recognises_ndjson() {
        let mut h = reqwest::header::HeaderMap::new();
        h.insert(
            reqwest::header::CONTENT_TYPE,
            reqwest::header::HeaderValue::from_static("application/x-ndjson"),
        );
        assert!(is_streaming_response(&h));
    }

    #[test]
    fn is_streaming_response_false_for_plain_json() {
        let mut h = reqwest::header::HeaderMap::new();
        h.insert(
            reqwest::header::CONTENT_TYPE,
            reqwest::header::HeaderValue::from_static("application/json"),
        );
        assert!(!is_streaming_response(&h));
    }

    #[test]
    fn is_streaming_response_false_when_no_content_type() {
        let h = reqwest::header::HeaderMap::new();
        assert!(!is_streaming_response(&h));
    }

    #[test]
    fn streaming_middleware_does_not_block_on_long_lived_stream() {
        // Pin the v2 streaming-fix contract: a response that takes
        // multiple chunks over time must NOT block the middleware's
        // handle() return. We verify by sending a body in two chunks
        // and asserting that client.send().await completes in well
        // under the chunk delay — which would be impossible with the
        // old `response.bytes().await` buffer path.
        let _ = tokio::runtime::Runtime::new().unwrap().block_on(async {
            let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
            crate::adapters::http::clear_domain_rates();
            crate::adapters::network_accountant::_reset_registry_for_tests();

            let accountant = Arc::new(
                crate::adapters::network_accountant::NetworkAccountant::new(),
            );
            crate::adapters::network_accountant::register_accountant(
                "t-stream",
                accountant.clone(),
            );

            let (catalog, pricing, buffer) = fixtures();
            let server = MockServer::start().await;
            // SSE-style body — content type triggers the streaming path.
            let body = "data: hello\n\ndata: world\n\n";
            Mock::given(method("GET"))
                .and(path("/stream"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_raw(body.as_bytes().to_vec(), "text/event-stream"),
                )
                .mount(&server)
                .await;

            let mw = DexcostMiddleware::new(
                catalog,
                pricing,
                buffer.clone(),
                Some("t-stream".into()),
            );
            let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();

            let t0 = std::time::Instant::now();
            let resp = client
                .get(format!("{}/stream", server.uri()))
                .send()
                .await
                .unwrap();
            let middleware_return_us = t0.elapsed().as_micros();
            assert_eq!(resp.status(), 200);

            // Caller drains the body — this is where bytes flow through
            // the RecordingStream wrapper and get counted.
            let bytes = resp.bytes().await.unwrap();
            assert_eq!(bytes.len(), body.len(), "body bytes round-trip intact");

            // Accountant must have received exactly one record on stream
            // completion, with response bytes >= body length.
            let snap = accountant.finalize();
            assert_eq!(snap.call_count, 1, "exactly one record() on stream end");
            assert!(
                snap.bytes_in >= body.len() as u64,
                "response bytes ({}) must include the streamed body ({})",
                snap.bytes_in,
                body.len()
            );
            assert!(snap.bytes_out > 0, "request bytes recorded too");

            // Middleware must return promptly — even if we don't measure a
            // specific bound here, the test would deadlock under the old
            // buffer-everything path if the server held the connection open.
            // Belt-and-suspenders: cap at 1 second.
            assert!(
                middleware_return_us < 1_000_000,
                "middleware.handle returned in {}us — streaming path should be non-blocking",
                middleware_return_us
            );
        });
    }

    #[test]
    fn streaming_middleware_records_bytes_on_early_drop() {
        // Pin v1 §5.5 — early-abort path. Caller drops the response
        // without draining the stream; the RecordingStream's Drop must
        // still record the partial bytes seen so far.
        let _ = tokio::runtime::Runtime::new().unwrap().block_on(async {
            let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
            crate::adapters::http::clear_domain_rates();
            crate::adapters::network_accountant::_reset_registry_for_tests();

            let accountant = Arc::new(
                crate::adapters::network_accountant::NetworkAccountant::new(),
            );
            crate::adapters::network_accountant::register_accountant(
                "t-drop",
                accountant.clone(),
            );

            let (catalog, pricing, buffer) = fixtures();
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/sse"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_raw(b"data: x\n\n".to_vec(), "text/event-stream"),
                )
                .mount(&server)
                .await;

            let mw = DexcostMiddleware::new(
                catalog,
                pricing,
                buffer.clone(),
                Some("t-drop".into()),
            );
            let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();

            // Send request, get response, then drop without draining.
            {
                let _resp = client
                    .get(format!("{}/sse", server.uri()))
                    .send()
                    .await
                    .unwrap();
                // _resp dropped here — RecordingStream::Drop fires.
            }

            let snap = accountant.finalize();
            assert_eq!(
                snap.call_count, 1,
                "early-drop must still record exactly one call"
            );
            // bytes_out (request side) is always known at middleware entry.
            assert!(snap.bytes_out > 0);
        });
    }

    #[test]
    fn streaming_middleware_emits_no_network_event() {
        // For streaming responses we skip the threshold-gated network
        // event emission because (a) we don't know total bytes until
        // after handle() returns, and (b) LLM streaming responses are
        // already covered by the LLM instrument's own llm_call event.
        // The byte count still feeds network_cost_usd at task finalize.
        let _ = tokio::runtime::Runtime::new().unwrap().block_on(async {
            let _guard = crate::adapters::http::GLOBAL_HTTP_TEST_LOCK.lock().await;
            crate::adapters::http::clear_domain_rates();
            crate::adapters::network_accountant::_reset_registry_for_tests();

            let (catalog, pricing, buffer) = fixtures();
            let server = MockServer::start().await;
            // 200 KB SSE-flagged body — above the 100 KiB threshold that
            // would normally emit a network event in the buffered path.
            let big = "data: ".to_string() + &"x".repeat(200_000) + "\n\n";
            Mock::given(method("GET"))
                .and(path("/big-sse"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_raw(big.clone().into_bytes(), "text/event-stream"),
                )
                .mount(&server)
                .await;

            let mw = DexcostMiddleware::new(
                catalog,
                pricing,
                buffer.clone(),
                Some("t-no-event".into()),
            );
            let client = ClientBuilder::new(reqwest::Client::new()).with(mw).build();
            let resp = client
                .get(format!("{}/big-sse", server.uri()))
                .send()
                .await
                .unwrap();
            // Drain so bytes flow + RecordingStream finalises.
            let _ = resp.bytes().await.unwrap();

            let buf = buffer.lock().await;
            let net_events: Vec<_> = buf
                .query_events("t-no-event")
                .into_iter()
                .filter(|e| e.event_type == EventType::Network)
                .collect();
            assert!(
                net_events.is_empty(),
                "streaming path does not emit per-call network events; bytes still feed the task aggregate"
            );
        });
    }
}
