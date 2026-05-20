use std::collections::HashMap;
use std::sync::Arc;

use chrono::Utc;
use rust_decimal::Decimal;
use tokio::sync::Mutex;

use crate::adapters::network_accountant::{register_accountant, unregister_accountant};
use crate::cloud_detect::get_cloud_env;
use crate::core::heuristics::RetryHeuristicEngine;
use crate::core::models::{CostConfidence, CostEvent, EventType, PricingSource, Task, TaskStatus};
use crate::error::DexcostError;
use crate::pricing::compute_pricing::ComputePricingEngine;
use crate::pricing::egress_pricing::EgressPricingEngine;
use crate::pricing::engine::PricingEngine;
use crate::pricing::rates::RateRegistry;
use crate::transport::buffer::EventBuffer;

/// Heuristic configuration for automatic retry detection.
#[derive(Debug, Clone)]
pub struct HeuristicConfig {
    /// Rolling time window in seconds (default: 30).
    pub window_seconds: f64,
    /// Minimum confidence score required to flag a retry (default: 0.8).
    pub threshold: f64,
}

impl Default for HeuristicConfig {
    fn default() -> Self {
        use crate::core::heuristics::{DEFAULT_THRESHOLD, DEFAULT_WINDOW_SECONDS};
        Self {
            window_seconds: DEFAULT_WINDOW_SECONDS,
            threshold: DEFAULT_THRESHOLD,
        }
    }
}

/// Optional overrides for [`TrackedTask::record_cost`].
///
/// Mirrors the keyword arguments of Python `tracker.py` `record_cost`. When a
/// field is `None`, the default (`Exact` confidence / `Manual` source) applies.
#[derive(Debug, Clone, Default)]
pub struct RecordCostOptions {
    /// Confidence in the recorded cost. Defaults to `CostConfidence::Exact`.
    pub cost_confidence: Option<CostConfidence>,
    /// Source of the pricing data. Defaults to `PricingSource::Manual`.
    pub pricing_source: Option<PricingSource>,
    /// Optional hash referencing the rate snapshot used.
    pub pricing_version: Option<String>,
    /// Optional extra metadata for the event.
    pub details: Option<HashMap<String, serde_json::Value>>,
}

/// Optional fields for [`TrackedTask::record_llm_call`].
///
/// Mirrors the keyword-only arguments of Python `tracker.py` `record_llm_call`.
/// All fields default to `None` (no override / no extra detail).
#[derive(Debug, Clone, Default)]
pub struct RecordLlmCallOptions {
    /// Transient error type that caused this call to fail (e.g. `"rate_limit"`).
    /// Stored in `details["error_type"]`.
    pub error_type: Option<String>,
    /// Extra metadata merged into the event details.
    pub details: Option<HashMap<String, serde_json::Value>>,
    /// Overrides the auto-derived cost confidence when set.
    pub cost_confidence: Option<CostConfidence>,
    /// Overrides the auto-derived pricing source when set.
    pub pricing_source: Option<PricingSource>,
}

/// Options for creating a new task via `start_task`.
#[derive(Debug, Clone, Default)]
pub struct TaskOptions {
    pub customer_id: Option<String>,
    pub project_id: Option<String>,
    pub experiment_id: Option<String>,
    pub variant: Option<String>,
    pub metadata: Option<HashMap<String, serde_json::Value>>,
    pub parent_task_id: Option<String>,
    /// Enable heuristic retry detection. When `Some`, a `RetryHeuristicEngine`
    /// is created with the supplied config and wired into this task.
    pub heuristics: Option<HeuristicConfig>,
}

/// TrackedTask wraps a Task and provides methods to record costs and end the task.
/// Each method creates a CostEvent, adds it to the buffer, and aggregates into the task.
pub struct TrackedTask {
    task: Task,
    buffer: Arc<Mutex<EventBuffer>>,
    events: Vec<CostEvent>,
    ended: bool,
    pricing: Option<Arc<Mutex<PricingEngine>>>,
    rate_registry: Option<Arc<Mutex<RateRegistry>>>,
    heuristics: Option<Arc<std::sync::Mutex<RetryHeuristicEngine>>>,
    /// Compute pricing engine for Phase 1 deferred-cost back-fill at task
    /// finalize. Defaults to a fresh `ComputePricingEngine::new()` per task.
    compute_pricing: ComputePricingEngine,
    /// Per-billing-model rate overrides (e.g. `{"lambda_request_usd": "0.5"}`).
    /// Threaded through from `init()` config; empty by default.
    compute_billing_overrides: HashMap<String, String>,
    /// Opt-in K8s node-aware billing mode (Decision #11). False by default.
    k8s_node_aware: bool,
}

impl TrackedTask {
    /// Creates a new TrackedTask.
    pub fn new(
        task: Task,
        buffer: Arc<Mutex<EventBuffer>>,
        pricing: Option<Arc<Mutex<PricingEngine>>>,
    ) -> Self {
        Self::with_rate_registry(task, buffer, pricing, None)
    }

    /// Creates a new TrackedTask with an optional RateRegistry for `record_usage`.
    pub fn with_rate_registry(
        task: Task,
        buffer: Arc<Mutex<EventBuffer>>,
        pricing: Option<Arc<Mutex<PricingEngine>>>,
        rate_registry: Option<Arc<Mutex<RateRegistry>>>,
    ) -> Self {
        // Register the task's NetworkAccountant so the HTTP middleware can
        // find it via task_id lookup (mirrors Python's _resolveTask path).
        register_accountant(&task.task_id, task.network_accountant.clone());
        Self {
            task,
            buffer,
            events: Vec::new(),
            ended: false,
            pricing,
            rate_registry,
            heuristics: None,
            compute_pricing: ComputePricingEngine::new(),
            compute_billing_overrides: HashMap::new(),
            k8s_node_aware: false,
        }
    }

    /// Creates a new TrackedTask with a heuristic engine for automatic retry detection.
    pub fn with_heuristics(
        task: Task,
        buffer: Arc<Mutex<EventBuffer>>,
        pricing: Option<Arc<Mutex<PricingEngine>>>,
        rate_registry: Option<Arc<Mutex<RateRegistry>>>,
        heuristics: Arc<std::sync::Mutex<RetryHeuristicEngine>>,
    ) -> Self {
        register_accountant(&task.task_id, task.network_accountant.clone());
        Self {
            task,
            buffer,
            events: Vec::new(),
            ended: false,
            pricing,
            rate_registry,
            heuristics: Some(heuristics),
            compute_pricing: ComputePricingEngine::new(),
            compute_billing_overrides: HashMap::new(),
            k8s_node_aware: false,
        }
    }

    /// Configure compute billing knobs for this task. Used by handler wraps
    /// and init() config plumbing.
    pub fn with_compute_config(
        mut self,
        overrides: HashMap<String, String>,
        k8s_node_aware: bool,
    ) -> Self {
        self.compute_billing_overrides = overrides;
        self.k8s_node_aware = k8s_node_aware;
        self
    }

    /// Attach a ComputeAccountant to this task. Used by the handler wraps
    /// (adapters::compute_wrap::wrap_lambda_handler etc.) — the SDK public
    /// API for compute capture goes through these wraps.
    pub fn attach_compute_for_tests(
        &mut self,
        accountant: std::sync::Arc<crate::core::compute_accountant::ComputeAccountant>,
    ) {
        self.task.compute = Some(accountant);
    }

    /// Access the buffer handle. Used by handler wraps to insert compute
    /// events under the cost_pending deferred-cost pattern.
    pub fn buffer_handle_for_tests(&self) -> Arc<Mutex<EventBuffer>> {
        self.buffer.clone()
    }

    /// Records an LLM call event on this task.
    /// If `cost_usd` is None or zero, the pricing engine auto-computes the cost.
    ///
    /// This is a thin wrapper over [`record_llm_call_with`] with default
    /// options, kept for backward compatibility.
    #[allow(clippy::too_many_arguments)]
    pub async fn record_llm_call(
        &mut self,
        provider: &str,
        model: &str,
        input_tokens: i64,
        output_tokens: i64,
        cost_usd: Option<Decimal>,
        cached_tokens: Option<i64>,
        latency_ms: Option<i64>,
    ) -> Result<CostEvent, DexcostError> {
        self.record_llm_call_with(
            provider,
            model,
            input_tokens,
            output_tokens,
            cost_usd,
            cached_tokens,
            latency_ms,
            RecordLlmCallOptions::default(),
        )
        .await
    }

    /// Records an LLM call event with optional `error_type`, extra `details`,
    /// and `cost_confidence` / `pricing_source` overrides.
    ///
    /// Mirrors Python `tracker.py` `record_llm_call` (`tracker.py:292-371`):
    /// `error_type` is merged into `details["error_type"]`, and the
    /// confidence / source overrides take precedence over the auto-derived
    /// values when set.
    #[allow(clippy::too_many_arguments)]
    pub async fn record_llm_call_with(
        &mut self,
        provider: &str,
        model: &str,
        input_tokens: i64,
        output_tokens: i64,
        cost_usd: Option<Decimal>,
        cached_tokens: Option<i64>,
        latency_ms: Option<i64>,
        opts: RecordLlmCallOptions,
    ) -> Result<CostEvent, DexcostError> {
        if self.ended {
            return Err(DexcostError::TaskAlreadyEnded);
        }

        let mut event = CostEvent::new(&self.task.task_id, EventType::LlmCall);
        event.provider = Some(provider.to_string());
        event.model = Some(model.to_string());
        event.input_tokens = Some(input_tokens);
        event.output_tokens = Some(output_tokens);
        event.cached_tokens = cached_tokens;
        event.latency_ms = latency_ms;

        // Merge caller-supplied details, then the error_type marker.
        if let Some(d) = opts.details {
            event.details = d;
        }
        if let Some(ref et) = opts.error_type {
            event.details.insert(
                "error_type".to_string(),
                serde_json::Value::String(et.clone()),
            );
        }

        if let Some(cost) = cost_usd {
            if !cost.is_zero() {
                event.cost_usd = cost;
                event.cost_confidence = CostConfidence::Exact;
                event.pricing_source = Some(PricingSource::Manual);
            } else {
                self.auto_price(
                    &mut event,
                    model,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                )
                .await;
            }
        } else {
            self.auto_price(
                &mut event,
                model,
                input_tokens,
                output_tokens,
                cached_tokens,
            )
            .await;
        }

        // Caller overrides take precedence over the auto-derived values.
        if let Some(cc) = opts.cost_confidence {
            event.cost_confidence = cc;
        }
        if let Some(ps) = opts.pricing_source {
            event.pricing_source = Some(ps);
        }

        // Aggregate into task
        self.task.llm_cost_usd += event.cost_usd;
        self.task.total_input_tokens += input_tokens;
        self.task.total_output_tokens += output_tokens;
        if let Some(ct) = cached_tokens {
            self.task.total_cached_tokens += ct;
        }
        self.task.total_cost_usd =
            self.task.llm_cost_usd + self.task.external_cost_usd + self.task.compute_cost_usd;

        // Run heuristic retry detection before recording.
        if let Some(ref heuristics) = self.heuristics {
            let mut engine = heuristics.lock().unwrap_or_else(|e| {
                eprintln!("[dexcost] mutex poisoned, recovering: {}", e);
                e.into_inner()
            });
            let result = engine.check(&event);
            if result.is_retry {
                event.is_retry = true;
                event.retry_reason = Some(result.reason.clone());
                event.retry_of = result.matched_event_id.clone();
                self.task.retry_count += 1;
                self.task.retry_cost_usd += event.cost_usd;
            }
            engine.record(event.clone());
        }

        // Dev console output
        crate::dev_console::log_event(&event, &self.task.task_type);

        self.events.push(event.clone());
        let mut buf = self.buffer.lock().await;
        buf.add_event(event.clone());
        buf.upsert_task(self.task.clone());

        Ok(event)
    }

    /// Auto-price using the pricing engine.
    async fn auto_price(
        &self,
        event: &mut CostEvent,
        model: &str,
        input_tokens: i64,
        output_tokens: i64,
        cached_tokens: Option<i64>,
    ) {
        if let Some(ref pricing) = self.pricing {
            let engine = pricing.lock().await;
            let result = engine
                .get_cost(
                    model,
                    input_tokens,
                    output_tokens,
                    cached_tokens.unwrap_or(0),
                    0,
                )
                .await;
            event.cost_usd = result.cost_usd;
            event.cost_confidence = result.cost_confidence;
            event.pricing_source = Some(result.pricing_source);
            event.pricing_version = Some(result.pricing_version.clone());
        } else {
            event.cost_confidence = CostConfidence::Unknown;
            event.pricing_source = Some(PricingSource::Unknown);
        }
    }

    /// Records a non-LLM cost event on this task.
    /// If `event_type` is `None`, defaults to `EventType::ExternalCost`.
    ///
    /// Thin wrapper over [`record_cost_with`] using default options
    /// (`Exact` confidence, `Manual` source). Kept for backward compatibility.
    pub async fn record_cost(
        &mut self,
        service: &str,
        cost_usd: Decimal,
        details: Option<HashMap<String, serde_json::Value>>,
        event_type: Option<EventType>,
    ) -> Result<CostEvent, DexcostError> {
        self.record_cost_with(
            service,
            cost_usd,
            event_type,
            RecordCostOptions {
                details,
                ..Default::default()
            },
        )
        .await
    }

    /// Records a non-LLM cost event with optional `cost_confidence`,
    /// `pricing_source`, and `pricing_version` overrides.
    ///
    /// Mirrors Python `tracker.py` `record_cost`, which accepts these as
    /// keyword arguments defaulting to `exact` / `manual`. When an option is
    /// not supplied the default behavior is preserved.
    pub async fn record_cost_with(
        &mut self,
        service: &str,
        cost_usd: Decimal,
        event_type: Option<EventType>,
        opts: RecordCostOptions,
    ) -> Result<CostEvent, DexcostError> {
        if self.ended {
            return Err(DexcostError::TaskAlreadyEnded);
        }

        let ev_type = event_type.unwrap_or(EventType::ExternalCost);
        let mut event = CostEvent::new(&self.task.task_id, ev_type.clone());
        event.service_name = Some(service.to_string());
        event.cost_usd = cost_usd;
        event.cost_confidence = opts.cost_confidence.unwrap_or(CostConfidence::Exact);
        event.pricing_source = Some(opts.pricing_source.unwrap_or(PricingSource::Manual));
        event.pricing_version = opts.pricing_version;

        if let Some(d) = opts.details {
            event.details = d;
        }

        // Aggregate based on type
        match ev_type {
            EventType::ExternalCost => self.task.external_cost_usd += event.cost_usd,
            EventType::ComputeCost => self.task.compute_cost_usd += event.cost_usd,
            _ => self.task.external_cost_usd += event.cost_usd,
        }
        self.task.total_cost_usd =
            self.task.llm_cost_usd + self.task.external_cost_usd + self.task.compute_cost_usd;

        // Dev console output
        crate::dev_console::log_event(&event, &self.task.task_type);

        self.events.push(event.clone());
        let mut buf = self.buffer.lock().await;
        buf.add_event(event.clone());
        buf.upsert_task(self.task.clone());

        Ok(event)
    }

    /// Records a retry marker event on this task.
    pub async fn mark_retry(
        &mut self,
        reason: &str,
        cost_usd: Decimal,
    ) -> Result<CostEvent, DexcostError> {
        if self.ended {
            return Err(DexcostError::TaskAlreadyEnded);
        }

        let mut event = CostEvent::new(&self.task.task_id, EventType::RetryMarker);
        event.is_retry = true;
        event.retry_reason = Some(reason.to_string());
        event.cost_usd = cost_usd;

        // Aggregate retry metrics
        self.task.retry_count += 1;
        self.task.retry_cost_usd += event.cost_usd;

        // Dev console output
        crate::dev_console::log_event(&event, &self.task.task_type);

        self.events.push(event.clone());
        let mut buf = self.buffer.lock().await;
        buf.add_event(event.clone());
        buf.upsert_task(self.task.clone());

        Ok(event)
    }

    /// Records a non-LLM service cost event using a rate from the RateRegistry.
    /// The cost is computed as `rate.cost_usd * units`. Requires a RateRegistry
    /// to have been provided via `with_rate_registry`.
    pub async fn record_usage(
        &mut self,
        service: &str,
        units: i64,
    ) -> Result<CostEvent, DexcostError> {
        if self.ended {
            return Err(DexcostError::TaskAlreadyEnded);
        }

        let (cost_usd, pricing_version) = if let Some(ref registry) = self.rate_registry {
            let mut reg = registry.lock().await;
            match reg.get(service) {
                Some(entry) => {
                    let cost = entry.cost_usd * Decimal::from(units);
                    let version = reg.pricing_version();
                    (cost, version)
                }
                None => {
                    return Err(DexcostError::Config(format!(
                        "No rate registered for service: {}",
                        service
                    )))
                }
            }
        } else {
            return Err(DexcostError::Config("No rate registry configured".into()));
        };

        let mut event = CostEvent::new(&self.task.task_id, EventType::ExternalCost);
        event.service_name = Some(service.to_string());
        event.cost_usd = cost_usd;
        event.cost_confidence = CostConfidence::Computed;
        event.pricing_source = Some(PricingSource::RateRegistry);
        event
            .details
            .insert("units".to_string(), serde_json::Value::Number(units.into()));
        event.details.insert(
            "pricing_version".to_string(),
            serde_json::Value::String(pricing_version),
        );

        // Aggregate into task
        self.task.external_cost_usd += event.cost_usd;
        self.task.total_cost_usd =
            self.task.llm_cost_usd + self.task.external_cost_usd + self.task.compute_cost_usd;

        // Dev console output
        crate::dev_console::log_event(&event, &self.task.task_type);

        self.events.push(event.clone());
        let mut buf = self.buffer.lock().await;
        buf.add_event(event.clone());
        buf.upsert_task(self.task.clone());
        drop(buf);

        Ok(event)
    }

    /// Clears the retry flag on an event.
    ///
    /// If `event_id` is `Some`, finds the most-recent event with that ID that
    /// has `is_retry == true`. If `event_id` is `None`, finds the most-recent
    /// retry event on this task. Returns `None` if no matching event exists.
    pub async fn mark_not_retry(
        &mut self,
        event_id: Option<&str>,
    ) -> Result<Option<CostEvent>, DexcostError> {
        if self.ended {
            return Err(DexcostError::TaskAlreadyEnded);
        }

        let target_idx = if let Some(id) = event_id {
            self.events
                .iter()
                .rposition(|e| e.event_id == id && e.is_retry)
        } else {
            self.events.iter().rposition(|e| e.is_retry)
        };

        let target_idx = match target_idx {
            Some(idx) => idx,
            None => return Ok(None),
        };

        self.events[target_idx].is_retry = false;
        self.events[target_idx].retry_reason = None;
        self.events[target_idx].retry_of = None;

        let mut buf = self.buffer.lock().await;
        buf.update_event(&self.events[target_idx]);
        drop(buf);

        Ok(Some(self.events[target_idx].clone()))
    }

    /// Links a trace from an external provider for observability.
    /// Stores trace links in a `_trace_links` array for consistency with
    /// the Python, TypeScript, and Go SDKs.
    pub fn link_trace(&mut self, provider: &str, trace_id: &str) {
        let links = self
            .task
            .metadata
            .entry("_trace_links".to_string())
            .or_insert_with(|| serde_json::Value::Array(Vec::new()));

        if let serde_json::Value::Array(ref mut arr) = links {
            arr.push(serde_json::json!({
                "provider": provider,
                "trace_id": trace_id
            }));
        }
    }

    /// Returns all linked traces for this task.
    ///
    /// Reads the `_trace_links` array from the task metadata. Each entry is a
    /// JSON object with `"provider"` and `"trace_id"` keys. Returns an empty
    /// vec when no traces have been linked. Mirrors Python `tracker.py`
    /// `get_trace_links` (`tracker.py:284-290`).
    pub fn get_trace_links(&self) -> Vec<serde_json::Value> {
        match self.task.metadata.get("_trace_links") {
            Some(serde_json::Value::Array(arr)) => arr.clone(),
            _ => Vec::new(),
        }
    }

    /// Ends the task with the given status.
    pub async fn end(&mut self, status: TaskStatus) -> Result<(), DexcostError> {
        if self.ended {
            return Err(DexcostError::TaskAlreadyEnded);
        }
        self.ended = true;

        self.task.ended_at = Some(Utc::now());
        self.task.status = status.clone();

        if status == TaskStatus::Failed {
            self.task.failure_count = 1;
        }

        // ── Network finalize — v1 byte aggregates + v2 egress pricing ──
        //
        // Mirrors python/src/dexcost/tracker.py:_aggregate_costs. The
        // accountant snapshot drives both:
        //   - v1 task fields (network_bytes_in/out/call_count/network_by_host)
        //   - v2 task.network_cost_usd from the canonical external_bytes_out
        //     scalar (Decimal(bytes) / 10^9 * resolved_rate)
        //   - v2 per-host egress_cost_usd stamped into network_by_host
        //   - v2 per-event back-fill: every cost_pending network event for
        //     this task gets its cost_usd / pricing_source / pricing_version
        //     stamped and the cost_pending marker removed
        //
        // Wrapped in `let result = (...)` so a Tier-5 failure (spec §7.1)
        // never breaks task finalization — the task still ships with
        // llm/external/compute costs intact.
        let finalize_result = self.finalize_network().await;
        if let Err(e) = finalize_result {
            eprintln!(
                "[dexcost] WARNING: egress cost computation failed for task {}: {}",
                self.task.task_id, e
            );
            // Tier 5: zero out the v2 fields but preserve the v1 aggregates.
            self.task.network_cost_usd = Decimal::ZERO;
        }

        // ── Compute finalize — auto-emit long-running event + back-fill ──
        // Wrapped in a fail-silent shell (Tier 5 of the §7.1 ladder).
        let compute_result = self.finalize_compute().await;
        if let Err(e) = compute_result {
            eprintln!(
                "[dexcost] WARNING: compute cost computation failed for task {}: {}",
                self.task.task_id, e
            );
        }

        // Remove the accountant from the registry; subsequent HTTP calls
        // attributed to this task_id won't find one (matches Python's
        // frozen-then-snapshot semantics — late bytes are dropped, not
        // recorded against the wrong task).
        unregister_accountant(&self.task.task_id);

        let mut buf = self.buffer.lock().await;
        buf.upsert_task(self.task.clone());
        drop(buf);

        // Dev console output
        crate::dev_console::log_task_complete(&self.task);

        Ok(())
    }

    /// Snapshots the NetworkAccountant onto the task's v1 fields and
    /// (if a CloudEnv has been resolved) computes v2 egress dollars +
    /// back-fills the cost_pending network events for this task.
    ///
    /// Returns Err only on truly exceptional conditions; the caller wraps
    /// this in a fail-silent shell (Tier 5 of the §7.1 ladder).
    async fn finalize_network(&mut self) -> Result<(), DexcostError> {
        // v1 — drain the accountant into task fields.
        let snapshot = self.task.network_accountant.finalize();
        self.task.network_bytes_in = snapshot.bytes_in as i64;
        self.task.network_bytes_out = snapshot.bytes_out as i64;
        self.task.network_call_count = snapshot.call_count as i64;
        self.task.network_by_host = snapshot.by_host;

        // v2 — egress pricing.
        let env = get_cloud_env();
        let engine = EgressPricingEngine::new();
        let rate = engine.resolve_rate(env.provider.as_deref(), env.region.as_deref());
        let pricing_version = format!("egress:{}", engine.catalog_version());

        // Convert external_bytes_out scalar to GB (decimal — never float
        // — per spec §6.3). 1 GB = 10^9 bytes, NOT 2^30.
        let divisor = Decimal::from(1_000_000_000_u64);
        let external_gb = Decimal::from(snapshot.external_bytes_out) / divisor;
        self.task.network_cost_usd = external_gb * rate.rate_per_gb;

        // Stamp per-host egress_cost_usd into network_by_host. Per-host
        // external_bytes_out survives the LIVE_CAP overflow + top-N cap;
        // sum(per-host egress_cost_usd) == network_cost_usd by construction
        // (the v2 §10.3 property invariant).
        if let Some(hosts) = self
            .task
            .network_by_host
            .get_mut("hosts")
            .and_then(|v| v.as_array_mut())
        {
            for host in hosts.iter_mut() {
                let host_external = host
                    .get("external_bytes_out")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0);
                let host_gb = Decimal::from(host_external) / divisor;
                let host_cost = host_gb * rate.rate_per_gb;
                if let Some(obj) = host.as_object_mut() {
                    obj.insert(
                        "egress_cost_usd".to_string(),
                        serde_json::Value::String(host_cost.to_string()),
                    );
                }
            }
        }

        // v2 §6.4 — back-fill each network event for this task. Walk the
        // buffer's stored events for this task_id, find any with
        // details.cost_pending == true, compute their cost, and
        // update_event to re-sync.
        let mut buf = self.buffer.lock().await;
        let pending = buf
            .query_events(&self.task.task_id)
            .into_iter()
            .filter(|e| {
                e.event_type == EventType::Network
                    && e.details
                        .get("cost_pending")
                        .and_then(|v| v.as_bool())
                        == Some(true)
            })
            .collect::<Vec<_>>();

        for mut ev in pending {
            let resp_bytes = ev
                .details
                .get("response_bytes")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let req_bytes = ev
                .details
                .get("request_bytes")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let is_internal_val = ev.details.get("is_internal_traffic");
            let is_internal = matches!(is_internal_val, Some(v) if v.as_bool() == Some(true));

            let billable_bytes = if is_internal {
                0_u64
            } else {
                resp_bytes.saturating_add(req_bytes)
            };
            let ev_gb = Decimal::from(billable_bytes) / divisor;
            let ev_cost = ev_gb * rate.rate_per_gb;

            ev.cost_usd = ev_cost;
            ev.cost_confidence = if is_internal {
                CostConfidence::Exact
            } else {
                match rate.cost_confidence.as_str() {
                    "computed" => CostConfidence::Computed,
                    "estimated" => CostConfidence::Estimated,
                    "exact" => CostConfidence::Exact,
                    _ => CostConfidence::Unknown,
                }
            };
            ev.pricing_source = Some(if is_internal {
                PricingSource::ServiceCatalog
            } else {
                // No dedicated enum variant for egress catalog yet — reuse
                // ServiceCatalog. The pricing_source string on the wire
                // carries the full detail via details/pricing_version.
                PricingSource::ServiceCatalog
            });
            ev.pricing_version = Some(pricing_version.clone());
            // Strip cost_pending marker so the back-filled event is no
            // longer "deferred-cost".
            ev.details.remove("cost_pending");
            // Stamp egress_pricing_source string so the wire payload
            // carries the v2 source detail (egress_catalog:aws:us-east-1).
            ev.details.insert(
                "egress_pricing_source".to_string(),
                serde_json::Value::String(if is_internal {
                    "egress_catalog:internal".to_string()
                } else {
                    rate.pricing_source.clone()
                }),
            );

            buf.update_event(&ev);

            // The first-pass total_cost_usd summed this event as 0 (per
            // v2 §6.4 cost_pending); add the back-filled cost.
            self.task.total_cost_usd += ev_cost;
        }

        // Add network_cost_usd to total — the canonical scalar that
        // captures every external byte (including those from cataloged
        // and below-threshold un-cataloged calls that never emitted a
        // network event).
        self.task.total_cost_usd += self.task.network_cost_usd;

        Ok(())
    }

    /// Compute finalize: auto-emit the long-running compute event (if
    /// applicable) and back-fill `cost_pending=true` compute_cost events
    /// for this task. Tracks DELTAs (new - old) and adds to
    /// `task.compute_cost_usd` / `task.total_cost_usd` to preserve any
    /// `retry_marker` costs already summed by the main aggregation.
    ///
    /// Mirrors python/src/dexcost/tracker.py `_finalize_compute`.
    async fn finalize_compute(&mut self) -> Result<(), DexcostError> {
        use crate::core::compute_runtime::RuntimeKind;
        let cloud_env = get_cloud_env();
        let pricing_version = format!("compute:{}", self.compute_pricing.catalog_version());

        // Stage 1 — long-running runtimes auto-emit a single compute_cost
        // event with cost_pending: true. The compute accountant decides
        // whether to emit (via snapshot_end_and_build returning Some).
        if let Some(acc) = self.task.compute.clone() {
            let is_long_running = matches!(
                acc.runtime,
                RuntimeKind::Ec2
                    | RuntimeKind::Gce
                    | RuntimeKind::AzureVm
                    | RuntimeKind::K8sPod
                    | RuntimeKind::Fargate
            );
            if is_long_running {
                let duration_ms = match (self.task.ended_at, Some(self.task.started_at)) {
                    (Some(end), Some(start)) => {
                        (end - start).num_milliseconds().max(0)
                    }
                    _ => 0,
                };
                if let Some(details_value) = acc.snapshot_end_and_build(duration_ms) {
                    let mut event = CostEvent::new(&self.task.task_id, EventType::ComputeCost);
                    event.cost_usd = Decimal::ZERO;
                    event.cost_confidence = CostConfidence::Estimated;
                    event.pricing_source = Some(PricingSource::ServiceCatalog);
                    event.pricing_version = Some(pricing_version.clone());
                    if let serde_json::Value::Object(map) = details_value {
                        for (k, v) in map {
                            event.details.insert(k, v);
                        }
                    }
                    let mut buf = self.buffer.lock().await;
                    buf.add_event(event);
                    drop(buf);
                }
            }
        }

        // Stage 2 — back-fill cost_pending compute_cost events.
        let mut buf = self.buffer.lock().await;
        let pending: Vec<CostEvent> = buf
            .query_events(&self.task.task_id)
            .into_iter()
            .filter(|e| {
                e.event_type == EventType::ComputeCost
                    && e.details
                        .get("cost_pending")
                        .and_then(|v| v.as_bool())
                        == Some(true)
            })
            .collect();

        for mut ev in pending {
            let old_cost = ev.cost_usd;
            // Reconstruct a details Value for the engine.
            let details_value = serde_json::Value::Object(
                ev.details.clone().into_iter().collect(),
            );
            let result = self.compute_pricing.resolve_compute_cost(
                &details_value,
                &cloud_env,
                &self.compute_billing_overrides,
                None,
            );
            ev.cost_usd = result.cost_usd;
            ev.cost_confidence = match result.cost_confidence.as_str() {
                "computed" => CostConfidence::Computed,
                "estimated" => CostConfidence::Estimated,
                "exact" => CostConfidence::Exact,
                _ => CostConfidence::Unknown,
            };
            ev.pricing_source = Some(PricingSource::ServiceCatalog);
            ev.pricing_version = Some(pricing_version.clone());
            ev.details.insert(
                "compute_pricing_source".to_string(),
                serde_json::Value::String(result.pricing_source),
            );
            ev.details.remove("cost_pending");
            buf.update_event(&ev);

            // Track DELTA (new - old). Do NOT recompute total_cost_usd from
            // scratch — preserves retry_marker costs already summed by the
            // main aggregation loop.
            let delta = result.cost_usd - old_cost;
            self.task.compute_cost_usd += delta;
            self.task.total_cost_usd += delta;
        }

        Ok(())
    }

    /// Returns a reference to the underlying Task.
    pub fn task(&self) -> &Task {
        &self.task
    }

    /// Test-only: mutable access to the underlying Task. Used by Phase D
    /// finalize tests to seed cost aggregates before driving end().
    /// `_for_tests` + `#[doc(hidden)]` mark this as not-public-API.
    #[doc(hidden)]
    pub fn task_mut_for_tests(&mut self) -> &mut Task {
        &mut self.task
    }

    /// Returns the events recorded on this task.
    pub fn events(&self) -> &[CostEvent] {
        &self.events
    }

    /// Runs `f` with this task established in the task-local context.
    ///
    /// Any task created via [`crate::start_task`] *inside* `f` automatically
    /// discovers this task as its parent and sets `parent_task_id`.
    ///
    /// This is the handle-based counterpart to
    /// [`crate::core::context::with_task`]: `tokio::task_local!` scopes are
    /// tied to a running future, so a long-lived `TrackedTask` handle cannot
    /// "enter" the scope by itself — callers wrap their work with `scope`.
    ///
    /// ```no_run
    /// # use dexcost::{start_task, TaskOptions, TaskStatus};
    /// # async fn _example() -> Result<(), dexcost::DexcostError> {
    /// let mut parent = start_task("parent", TaskOptions::default()).await?;
    /// parent
    ///     .scope(async {
    ///         // `child.parent_task_id` is set to `parent`'s id automatically.
    ///         let mut child = start_task("child", TaskOptions::default()).await?;
    ///         child.end(TaskStatus::Success).await?;
    ///         Ok::<(), dexcost::DexcostError>(())
    ///     })
    ///     .await?;
    /// parent.end(TaskStatus::Success).await?;
    /// # Ok(())
    /// # }
    /// ```
    pub async fn scope<F, T>(&self, f: F) -> T
    where
        F: std::future::Future<Output = T>,
    {
        crate::core::context::with_task(self.task.clone(), f).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_record_llm_call_aggregates() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        let cost = Decimal::new(5, 2); // 0.05
        let event = tt
            .record_llm_call("openai", "gpt-4o", 1000, 500, Some(cost), None, None)
            .await
            .unwrap();

        assert_eq!(event.cost_usd, cost);
        assert_eq!(tt.task().llm_cost_usd, cost);
        assert_eq!(tt.task().total_input_tokens, 1000);
        assert_eq!(tt.task().total_output_tokens, 500);
        assert_eq!(tt.task().total_cost_usd, cost);
    }

    #[tokio::test]
    async fn test_record_cost_aggregates() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        let cost = Decimal::new(1, 2); // 0.01
        let event = tt
            .record_cost("google_maps", cost, None, None)
            .await
            .unwrap();

        assert_eq!(event.cost_usd, cost);
        assert_eq!(tt.task().external_cost_usd, cost);
    }

    #[tokio::test]
    async fn test_mark_retry() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        let cost = Decimal::new(2, 2); // 0.02
        let event = tt.mark_retry("rate_limit", cost).await.unwrap();

        assert!(event.is_retry);
        assert_eq!(event.retry_reason, Some("rate_limit".to_string()));
        assert_eq!(tt.task().retry_count, 1);
        assert_eq!(tt.task().retry_cost_usd, cost);
    }

    #[tokio::test]
    async fn test_end_task() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        tt.end(TaskStatus::Success).await.unwrap();
        assert_eq!(tt.task().status, TaskStatus::Success);
        assert!(tt.task().ended_at.is_some());
        assert_eq!(tt.task().failure_count, 0);
    }

    #[tokio::test]
    async fn test_end_task_failed() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        tt.end(TaskStatus::Failed).await.unwrap();
        assert_eq!(tt.task().status, TaskStatus::Failed);
        assert_eq!(tt.task().failure_count, 1);
    }

    #[tokio::test]
    async fn test_end_already_ended() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        tt.end(TaskStatus::Success).await.unwrap();
        let result = tt.end(TaskStatus::Failed).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_link_trace() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        tt.link_trace("langfuse", "trace-abc-123");
        let meta = &tt.task().metadata;
        let links = meta.get("_trace_links").unwrap();
        let arr = links.as_array().unwrap();
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["provider"], "langfuse");
        assert_eq!(arr[0]["trace_id"], "trace-abc-123");
    }

    // Gap 10: get_trace_links returns the linked traces, empty when none.
    #[tokio::test]
    async fn test_get_trace_links() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        assert!(tt.get_trace_links().is_empty());

        tt.link_trace("langfuse", "trace-1");
        tt.link_trace("langsmith", "run-2");

        let links = tt.get_trace_links();
        assert_eq!(links.len(), 2);
        assert_eq!(links[0]["provider"], "langfuse");
        assert_eq!(links[1]["trace_id"], "run-2");
    }

    // Gap 3: record_cost_with honours cost_confidence / pricing_source overrides.
    #[tokio::test]
    async fn test_record_cost_with_overrides() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        let event = tt
            .record_cost_with(
                "google_maps",
                Decimal::new(1, 2),
                None,
                RecordCostOptions {
                    cost_confidence: Some(CostConfidence::Estimated),
                    pricing_source: Some(PricingSource::ServiceCatalog),
                    pricing_version: Some("v-abc".to_string()),
                    details: None,
                },
            )
            .await
            .unwrap();

        assert_eq!(event.cost_confidence, CostConfidence::Estimated);
        assert_eq!(event.pricing_source, Some(PricingSource::ServiceCatalog));
        assert_eq!(event.pricing_version.as_deref(), Some("v-abc"));
    }

    // Gap 3: record_cost default behavior remains exact / manual.
    #[tokio::test]
    async fn test_record_cost_default_confidence_and_source() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        let event = tt
            .record_cost("twilio", Decimal::new(5, 3), None, None)
            .await
            .unwrap();

        assert_eq!(event.cost_confidence, CostConfidence::Exact);
        assert_eq!(event.pricing_source, Some(PricingSource::Manual));
    }

    // Gap 6: record_llm_call_with merges error_type into details and applies overrides.
    #[tokio::test]
    async fn test_record_llm_call_with_error_type_and_overrides() {
        let buffer = Arc::new(Mutex::new(EventBuffer::new().unwrap()));
        let task = Task::new("test_task");
        let mut tt = TrackedTask::new(task, buffer.clone(), None);

        let event = tt
            .record_llm_call_with(
                "openai",
                "gpt-4o",
                1000,
                500,
                Some(Decimal::new(3, 2)),
                None,
                None,
                RecordLlmCallOptions {
                    error_type: Some("rate_limit".to_string()),
                    details: None,
                    cost_confidence: Some(CostConfidence::Estimated),
                    pricing_source: Some(PricingSource::ProviderResponse),
                },
            )
            .await
            .unwrap();

        assert_eq!(
            event.details.get("error_type"),
            Some(&serde_json::Value::String("rate_limit".to_string()))
        );
        assert_eq!(event.cost_confidence, CostConfidence::Estimated);
        assert_eq!(event.pricing_source, Some(PricingSource::ProviderResponse));
    }
}
