use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::{Mutex, Notify};
use tokio::task::JoinHandle;

use crate::attribution::{to_attribution_event_v2, to_attribution_task_ingest_v1};
use crate::config::Config;
use crate::error::DexcostError;
use crate::security::redaction::{enforce_metadata_limit, hash_value, redact_map};
use crate::transport::buffer::EventBuffer;

const MAX_PAYLOAD_BYTES: usize = 120_000;
const MAX_CONVERSION_SCAN: usize = 1_000;
const CONVERSION_SCAN_MULTIPLIER: usize = 10;
const CONVERSION_WARNING_INTERVAL: Duration = Duration::from_secs(3600);

/// How often the sync loop runs buffer purges (mirrors Python `_PURGE_INTERVAL`).
const PURGE_INTERVAL: Duration = Duration::from_secs(3600);
/// Synced events older than this are purged.
const PURGE_RETENTION_HOURS: i64 = 48;
/// Pending events older than this are purged as a safety net.
const PURGE_MAX_PENDING_DAYS: i64 = 7;

/// Background task that flushes the event buffer to the control layer API.
/// Uses exponential backoff on failures.
pub struct EventPusher {
    buffer: Arc<Mutex<EventBuffer>>,
    config: Config,
    client: reqwest::Client,
    stop: Arc<Notify>,
    flush_notify: Arc<Notify>,
    /// Set permanently when the API key is rejected (HTTP 401/403). Once set,
    /// the sync loop stops and never retries — mirrors Python `sync.py:325-328`.
    auth_failed: Arc<AtomicBool>,
    /// Sprint 2 Theme D / §3.2.3 (B14): runtime override for the API key
    /// set via `set_api_key`. When `Some`, takes precedence over
    /// `config.api_key` in the Bearer header. Lets customers recover
    /// from 401/403 without restarting the process.
    api_key_override: Arc<parking_lot::RwLock<Option<String>>>,
}

impl EventPusher {
    /// Creates a new EventPusher.
    pub fn new(buffer: Arc<Mutex<EventBuffer>>, config: Config) -> Self {
        Self {
            buffer,
            config,
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(30))
                .build()
                .unwrap_or_default(),
            stop: Arc::new(Notify::new()),
            flush_notify: Arc::new(Notify::new()),
            auth_failed: Arc::new(AtomicBool::new(false)),
            api_key_override: Arc::new(parking_lot::RwLock::new(None)),
        }
    }

    /// Returns `true` if the pusher has permanently stopped due to a rejected
    /// API key (HTTP 401/403).
    pub fn is_auth_failed(&self) -> bool {
        self.auth_failed.load(Ordering::SeqCst)
    }

    /// Update the API key and clear the auth-failed flag so the push
    /// loop can resume. Sprint 2 Theme D / §3.2.3 (B14).
    ///
    /// Note: the spawned push task at `pusher.rs:73-77` returns early
    /// when `auth_failed` is true, so a fresh key alone is not enough
    /// to revive a dead loop — callers should also `start()` a new
    /// task if `is_auth_failed()` was previously true. For SDK-level
    /// orchestration the wrapping global init/set_api_key handles
    /// the restart.
    pub fn set_api_key(&self, new_key: String) {
        *self.api_key_override.write() = Some(new_key);
        self.auth_failed.store(false, Ordering::SeqCst);
    }

    /// Starts the background flush loop. Returns a JoinHandle for the spawned task.
    pub fn start(&self) -> JoinHandle<()> {
        let buffer = self.buffer.clone();
        let config = self.config.clone();
        let client = self.client.clone();
        let stop = self.stop.clone();
        let flush_notify = self.flush_notify.clone();
        let auth_failed = self.auth_failed.clone();
        let api_key_override = self.api_key_override.clone();

        tokio::spawn(async move {
            let interval = Duration::from_secs(config.flush_interval_secs);
            let mut backoff = Duration::from_secs(0);
            let mut last_conversion_warning: Option<(String, Instant)> = None;
            // Run an initial purge after one interval; tracked via Instant.
            let mut last_purge = Instant::now();

            loop {
                // Permanently stop once the API key has been rejected.
                if auth_failed.load(Ordering::SeqCst) {
                    return;
                }

                tokio::select! {
                    _ = stop.notified() => {
                        // Final flush before exiting
                        let _ = Self::push_batch(&buffer, &config, &client, &auth_failed, &api_key_override).await;
                        return;
                    }
                    _ = flush_notify.notified() => {
                        let _ = Self::push_batch(&buffer, &config, &client, &auth_failed, &api_key_override).await;
                    }
                    _ = tokio::time::sleep(interval + backoff) => {
                        match Self::push_batch(&buffer, &config, &client, &auth_failed, &api_key_override).await {
                            Ok(_) => {
                                backoff = Duration::from_secs(0);
                                last_conversion_warning = None;
                            }
                            Err(DexcostError::AttributionConversion(event_ids)) => {
                                // Local contract failures are quarantined and must not make a
                                // healthy control plane look like a transport outage.
                                backoff = Duration::from_secs(0);
                                if Self::should_warn_conversion(&mut last_conversion_warning, &event_ids) {
                                    eprintln!("[dexcost] {}", DexcostError::AttributionConversion(event_ids));
                                }
                            }
                            Err(_) => {
                                if backoff.is_zero() {
                                    backoff = Duration::from_secs(1);
                                } else {
                                    backoff = (backoff * 2).min(Duration::from_secs(300));
                                }
                            }
                        }
                    }
                }

                // Periodic buffer purge (throttled to once per hour), mirroring
                // Python `sync.py:236-253`.
                if last_purge.elapsed() >= PURGE_INTERVAL {
                    Self::run_purge(&buffer).await;
                    last_purge = Instant::now();
                }
            }
        })
    }

    /// Runs the periodic buffer purge: drop old synced events, then very old
    /// pending events as a safety net. Failures are logged and swallowed.
    async fn run_purge(buffer: &Arc<Mutex<EventBuffer>>) {
        let mut buf = buffer.lock().await;
        let synced = buf.purge_synced(PURGE_RETENTION_HOURS);
        if synced > 0 {
            eprintln!("[dexcost] purged {} old synced events", synced);
        }
        let pending = buf.purge_old_pending(PURGE_MAX_PENDING_DAYS);
        if pending > 0 {
            eprintln!(
                "[dexcost] purged {} old pending events (>{} days)",
                pending, PURGE_MAX_PENDING_DAYS
            );
        }
    }

    fn should_warn_conversion(
        last_warning: &mut Option<(String, Instant)>,
        event_ids: &[String],
    ) -> bool {
        let mut sorted = event_ids.to_vec();
        sorted.sort();
        let key = sorted.join("\0");
        let now = Instant::now();
        let should_warn = match last_warning {
            Some((previous_key, warned_at)) => {
                previous_key != &key
                    || now.duration_since(*warned_at) >= CONVERSION_WARNING_INTERVAL
            }
            None => true,
        };
        if should_warn {
            *last_warning = Some((key, now));
        }
        should_warn
    }

    /// Triggers an immediate flush.
    pub async fn flush(&self) -> Result<(), DexcostError> {
        Self::push_batch(
            &self.buffer,
            &self.config,
            &self.client,
            &self.auth_failed,
            &self.api_key_override,
        )
        .await
    }

    /// Signals the background loop to stop.
    pub fn stop(&self) {
        self.stop.notify_one();
    }

    /// Applies the configured PII controls to a single [`Task`] in place,
    /// before it is serialized for the ingest POST:
    ///
    /// 1. Redact configured fields from `task.metadata` (recursively).
    /// 2. When `hash_customer_id` is set, replace `customer_id` and
    ///    `project_id` with their SHA-256 hex digests.
    /// 3. Enforce the metadata size limit on `task.metadata`.
    ///
    /// This mirrors the redaction / hashing / size-limit pass applied to
    /// event details in [`Self::push_batch`], extending the same protections
    /// to task-level data (which was previously serialized raw).
    fn sanitize_task(
        task: &mut crate::core::models::Task,
        config: &Config,
        redact_refs: &[&str],
        max_metadata_bytes: usize,
    ) {
        // 1 + 3: redact + size-limit the metadata map.
        if !task.metadata.is_empty() {
            let mut map: serde_json::Map<String, serde_json::Value> = task
                .metadata
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();

            if !config.redact_fields.is_empty() {
                map = redact_map(&map, redact_refs);
            }
            map = enforce_metadata_limit(&map, max_metadata_bytes);

            task.metadata = map.into_iter().collect();
        }

        // 2: hash the customer/project attribution fields when configured.
        if config.hash_customer_id {
            if let Some(ref cid) = task.customer_id {
                task.customer_id = Some(hash_value(cid));
            }
            if let Some(ref pid) = task.project_id {
                task.project_id = Some(hash_value(pid));
            }
        }
    }

    fn sanitize_event(
        event: &mut crate::core::models::CostEvent,
        config: &Config,
        redact_refs: &[&str],
        max_metadata_bytes: usize,
    ) {
        if event.details.is_empty() {
            return;
        }
        let mut map: serde_json::Map<String, serde_json::Value> = event
            .details
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect();
        if !config.redact_fields.is_empty() {
            map = redact_map(&map, redact_refs);
        }
        if config.hash_customer_id {
            if let Some(customer_id) = map.get("customer_id").and_then(|value| value.as_str()) {
                map.insert(
                    "customer_id".to_string(),
                    serde_json::Value::String(hash_value(customer_id)),
                );
            }
        }
        event.details = enforce_metadata_limit(&map, max_metadata_bytes)
            .into_iter()
            .collect();
    }

    /// Pushes a batch of pending events and pending tasks to the control layer.
    /// Applies PII redaction, customer_id / project_id hashing, and metadata
    /// size limits to both events and tasks before serialization.
    ///
    /// Pending tasks are included so that task lifecycle changes (start_task,
    /// end_task, total recomputation) propagate to the server even when no
    /// new events accompany them. Tasks referenced by pending events are also
    /// fetched as a resilience measure (covers retries when an earlier task
    /// flush failed).
    async fn push_batch(
        buffer: &Arc<Mutex<EventBuffer>>,
        config: &Config,
        client: &reqwest::Client,
        auth_failed: &Arc<AtomicBool>,
        api_key_override: &Arc<parking_lot::RwLock<Option<String>>>,
    ) -> Result<(), DexcostError> {
        // Skip entirely once the API key has been permanently rejected.
        if auth_failed.load(Ordering::SeqCst) {
            return Ok(());
        }
        let batch_size = config.batch_size.max(1);
        let pending = buffer.lock().await.pending_count();
        // Log a warning if the buffer is growing unboundedly (backpressure signal).
        if pending > 10_000 {
            eprintln!(
                "[dexcost] WARNING: {} pending events in buffer — push may be failing or too slow",
                pending,
            );
        }
        let pending_tasks = buffer.lock().await.get_pending_tasks(batch_size);

        // Conversion happens only after redaction. Attribution v2 promotes
        // selected detail fields into typed provider/resource fields, so raw
        // details must never reach the converter.
        let redact_refs: Vec<&str> = config.redact_fields.iter().map(|s| s.as_str()).collect();
        const MAX_METADATA_BYTES: usize = 10240;
        let mut event_dicts = Vec::with_capacity(batch_size);
        let mut event_ids = Vec::with_capacity(batch_size);
        let mut event_task_ids = HashSet::new();
        let mut failed_event_ids = Vec::new();
        let mut seen_event_ids = HashSet::new();
        let scan_limit = batch_size
            .max(MAX_CONVERSION_SCAN.min(batch_size.saturating_mul(CONVERSION_SCAN_MULTIPLIER)));
        let mut scanned = 0;

        // Quarantine malformed pages as they are encountered, then fetch the
        // next pending page. A complete invalid prefix can no longer starve valid
        // attribution records that were captured later.
        while event_dicts.len() < batch_size && scanned < scan_limit {
            let page_limit = (batch_size - event_dicts.len()).min(scan_limit - scanned);
            let mut events = buffer.lock().await.get_pending_events(page_limit);
            if events.is_empty() {
                break;
            }
            let page_len = events.len();
            let mut newly_scanned = 0;
            let mut observability_event_ids = Vec::new();
            let mut page_failed_event_ids = Vec::new();
            for event in &mut events {
                if !seen_event_ids.insert(event.event_id.clone()) {
                    continue;
                }
                newly_scanned += 1;
                scanned += 1;
                Self::sanitize_event(event, config, &redact_refs, MAX_METADATA_BYTES);
                if event.event_type == crate::core::models::EventType::GpuUtilizationSignal {
                    observability_event_ids.push(event.event_id.clone());
                    continue;
                }
                match to_attribution_event_v2(event) {
                    Some(converted) => {
                        event_ids.push(event.event_id.clone());
                        event_task_ids.insert(event.task_id.clone());
                        event_dicts.push(serde_json::to_value(converted)?);
                    }
                    None => page_failed_event_ids.push(event.event_id.clone()),
                }
            }

            let mut buf = buffer.lock().await;
            buf.mark_synced(&observability_event_ids);
            if !page_failed_event_ids.is_empty() {
                let quarantined = buf.mark_quarantined(&page_failed_event_ids);
                if quarantined != page_failed_event_ids.len() {
                    return Err(DexcostError::Storage(format!(
                        "quarantined {quarantined} of {} attribution conversion failures",
                        page_failed_event_ids.len()
                    )));
                }
                failed_event_ids.extend(page_failed_event_ids);
            }
            drop(buf);
            if newly_scanned == 0 || page_len < page_limit {
                break;
            }
        }

        // Build the union of (pending tasks) and (tasks referenced by pending
        // events that are not already in pending_tasks). The latter covers
        // resilience: if an earlier task flush failed, the next event flush
        // re-includes the relevant tasks.
        let mut tasks_to_send: Vec<crate::core::models::Task> = pending_tasks;
        let already_included: HashSet<String> =
            tasks_to_send.iter().map(|t| t.task_id.clone()).collect();

        let event_task_ids: Vec<String> = event_task_ids
            .into_iter()
            .filter(|id| !already_included.contains(id))
            .collect();

        if !event_task_ids.is_empty() {
            let buf = buffer.lock().await;
            let extra = buf.get_tasks_by_ids(&event_task_ids);
            drop(buf);
            tasks_to_send.extend(extra);
        }

        // Capture the IDs of the tasks we are actually about to send so we
        // can mark them synced after the push succeeds. The id is never
        // redacted, so this is captured before the metadata pass below.
        let task_ids_sent: Vec<String> = tasks_to_send.iter().map(|t| t.task_id.clone()).collect();

        // Apply the same PII controls to task metadata that events receive:
        // redact configured fields, hash customer_id / project_id when
        // configured, and enforce the metadata size limit — all before the
        // task is serialized for the POST. Without this, `Task.metadata`
        // (and the customer/project attribution fields) would be pushed raw.
        for task in &mut tasks_to_send {
            Self::sanitize_task(task, config, &redact_refs, MAX_METADATA_BYTES);
        }

        let task_dicts: Vec<serde_json::Value> = tasks_to_send
            .iter()
            .map(to_attribution_task_ingest_v1)
            .map(serde_json::to_value)
            .collect::<Result<_, _>>()?;

        if event_dicts.is_empty() && task_dicts.is_empty() {
            return Self::conversion_failure(&failed_event_ids);
        }

        // Push with adaptive splitting for oversized payloads. Sprint 2
        // Theme D / §3.2.1 (B12): push_with_split marks events/tasks
        // synced at each leaf POST that succeeds, so a sibling-half
        // failure does not unwind work that already reached the
        // control plane.
        Self::push_with_split(
            &event_dicts,
            &task_dicts,
            &event_ids,
            &task_ids_sent,
            buffer,
            config,
            client,
            auth_failed,
            api_key_override,
        )
        .await?;

        Self::conversion_failure(&failed_event_ids)
    }

    fn conversion_failure(event_ids: &[String]) -> Result<(), DexcostError> {
        if event_ids.is_empty() {
            return Ok(());
        }
        Err(DexcostError::AttributionConversion(event_ids.to_vec()))
    }

    /// Recursively splits oversized payloads until they fit within the queue
    /// contract. When a mixed payload is too large, tasks are accepted first
    /// so no event can reference a task that has not reached ingestion yet.
    /// Successful leaves are acknowledged immediately and independently.
    #[allow(clippy::too_many_arguments)]
    fn push_with_split<'a>(
        events: &'a [serde_json::Value],
        tasks: &'a [serde_json::Value],
        event_ids: &'a [String],
        task_ids: &'a [String],
        buffer: &'a Arc<Mutex<EventBuffer>>,
        config: &'a Config,
        client: &'a reqwest::Client,
        auth_failed: &'a Arc<AtomicBool>,
        api_key_override: &'a Arc<parking_lot::RwLock<Option<String>>>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), DexcostError>> + Send + 'a>>
    {
        Box::pin(async move {
            let payload = serde_json::to_string(&serde_json::json!({
                "events": events,
                "tasks": tasks,
            }))?;

            if payload.len() <= MAX_PAYLOAD_BYTES {
                Self::post_raw(&payload, config, client, auth_failed, api_key_override).await?;
                let mut buf = buffer.lock().await;
                buf.mark_synced(event_ids);
                buf.mark_tasks_synced(task_ids);
                return Ok(());
            }

            if !events.is_empty() && !tasks.is_empty() {
                Self::push_with_split(
                    &[],
                    tasks,
                    &[],
                    task_ids,
                    buffer,
                    config,
                    client,
                    auth_failed,
                    api_key_override,
                )
                .await?;
                return Self::push_with_split(
                    events,
                    &[],
                    event_ids,
                    &[],
                    buffer,
                    config,
                    client,
                    auth_failed,
                    api_key_override,
                )
                .await;
            }

            if events.len() > 1 {
                let mid = events.len() / 2;
                eprintln!(
                    "[dexcost] Batch too large ({} bytes, {} events), splitting",
                    payload.len(),
                    events.len()
                );
                Self::push_with_split(
                    &events[..mid],
                    &[],
                    &event_ids[..mid],
                    &[],
                    buffer,
                    config,
                    client,
                    auth_failed,
                    api_key_override,
                )
                .await?;
                return Self::push_with_split(
                    &events[mid..],
                    &[],
                    &event_ids[mid..],
                    &[],
                    buffer,
                    config,
                    client,
                    auth_failed,
                    api_key_override,
                )
                .await;
            }

            if tasks.len() > 1 {
                let mid = tasks.len() / 2;
                eprintln!(
                    "[dexcost] Batch too large ({} bytes, {} tasks), splitting",
                    payload.len(),
                    tasks.len()
                );
                Self::push_with_split(
                    &[],
                    &tasks[..mid],
                    &[],
                    &task_ids[..mid],
                    buffer,
                    config,
                    client,
                    auth_failed,
                    api_key_override,
                )
                .await?;
                return Self::push_with_split(
                    &[],
                    &tasks[mid..],
                    &[],
                    &task_ids[mid..],
                    buffer,
                    config,
                    client,
                    auth_failed,
                    api_key_override,
                )
                .await;
            }

            // A single record cannot be made smaller without changing its
            // meaning. Drop it from the retry queue after retaining the local
            // durable copy, otherwise it blocks every subsequent batch.
            if !events.is_empty() {
                eprintln!(
                    "[dexcost] Single event exceeds payload limit ({} bytes), skipping",
                    payload.len()
                );
            } else {
                eprintln!(
                    "[dexcost] Single task exceeds payload limit ({} bytes), skipping",
                    payload.len()
                );
            }
            let mut buf = buffer.lock().await;
            buf.mark_synced(event_ids);
            buf.mark_tasks_synced(task_ids);
            Ok(())
        })
    }

    /// Sends a pre-serialized JSON payload to the ingestion endpoint.
    ///
    /// On HTTP 401/403 the API key is considered permanently rejected: the
    /// `auth_failed` flag is set so the sync loop stops and never retries the
    /// same key (mirrors Python `sync.py:325-328`). A non-retryable
    /// `Transport` error is returned without triggering backoff retries.
    async fn post_raw(
        body: &str,
        config: &Config,
        client: &reqwest::Client,
        auth_failed: &Arc<AtomicBool>,
        api_key_override: &Arc<parking_lot::RwLock<Option<String>>>,
    ) -> Result<(), DexcostError> {
        let url = format!("{}/v1/ingest", config.endpoint());
        let mut req = client
            .post(&url)
            .header("Content-Type", "application/json")
            .body(body.to_owned());

        // B14: prefer the runtime override populated by set_api_key,
        // fall back to the config value baked in at construction.
        let bearer = api_key_override
            .read()
            .clone()
            .or_else(|| config.api_key.clone());
        if let Some(ref key) = bearer {
            req = req.header("Authorization", format!("Bearer {}", key));
        }

        let resp = req.send().await?;
        let status = resp.status();

        if status == reqwest::StatusCode::PAYLOAD_TOO_LARGE {
            eprintln!("[dexcost] Server returned 413 — batch too large");
            return Err(DexcostError::Transport("payload too large".into()));
        }

        // 401/403: the API key is rejected. Stop the pusher permanently
        // instead of retrying a key the server will never accept.
        if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
            eprintln!(
                "[dexcost] ERROR: API key rejected (HTTP {}) — disabling sync permanently",
                status.as_u16()
            );
            auth_failed.store(true, Ordering::SeqCst);
            return Err(DexcostError::Transport(format!(
                "API key rejected (HTTP {}); sync disabled",
                status.as_u16()
            )));
        }

        if status.is_success() {
            let response_body = resp.text().await?;
            if !response_body.trim().is_empty() {
                if let Ok(value) = serde_json::from_str::<serde_json::Value>(&response_body) {
                    let rejected = value
                        .get("rejected")
                        .and_then(serde_json::Value::as_u64)
                        .unwrap_or(0);
                    if rejected > 0 {
                        return Err(DexcostError::Transport(format!(
                            "ingestion rejected {} record(s)",
                            rejected
                        )));
                    }
                }
            }
            Ok(())
        } else {
            Err(DexcostError::Transport(format!(
                "push failed with status {}",
                status
            )))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn conversion_warnings_are_keyed_throttled_and_expire() {
        let mut last_warning = None;
        let first = vec!["b".to_string(), "a".to_string()];
        assert!(EventPusher::should_warn_conversion(
            &mut last_warning,
            &first
        ));
        assert!(!EventPusher::should_warn_conversion(
            &mut last_warning,
            &["a".to_string(), "b".to_string()]
        ));

        last_warning.as_mut().expect("warning state").1 =
            Instant::now() - CONVERSION_WARNING_INTERVAL;
        assert!(EventPusher::should_warn_conversion(
            &mut last_warning,
            &first
        ));
        assert!(EventPusher::should_warn_conversion(
            &mut last_warning,
            &["different".to_string()]
        ));
    }
}
