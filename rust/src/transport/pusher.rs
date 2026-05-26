use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::{Mutex, Notify};
use tokio::task::JoinHandle;

use crate::config::Config;
use crate::error::DexcostError;
use crate::security::redaction::{enforce_metadata_limit, hash_value, redact_map};
use crate::transport::buffer::EventBuffer;

const MAX_PAYLOAD_BYTES: usize = 200_000; // 200KB — well under SQS 256KB limit
const MAX_SPLIT_DEPTH: usize = 5;

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
                            Ok(_) => backoff = Duration::from_secs(0),
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

    /// Triggers an immediate flush.
    pub async fn flush(&self) -> Result<(), DexcostError> {
        Self::push_batch(&self.buffer, &self.config, &self.client, &self.auth_failed, &self.api_key_override).await
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
        let buf = buffer.lock().await;
        // Log a warning if the buffer is growing unboundedly (backpressure signal).
        let pending = buf.pending_count();
        if pending > 10_000 {
            eprintln!(
                "[dexcost] WARNING: {} pending events in buffer — push may be failing or too slow",
                pending,
            );
        }
        let mut events = buf.get_pending_events(config.batch_size);
        let pending_tasks = buf.get_pending_tasks(config.batch_size);
        drop(buf);

        // Skip flush only when both queues are empty.
        if events.is_empty() && pending_tasks.is_empty() {
            return Ok(());
        }

        let event_ids: Vec<String> = events.iter().map(|e| e.event_id.clone()).collect();

        // Apply redaction / hashing / metadata limits before serialization.
        let redact_refs: Vec<&str> = config.redact_fields.iter().map(|s| s.as_str()).collect();
        const MAX_METADATA_BYTES: usize = 10240;

        for event in &mut events {
            if event.details.is_empty() {
                continue;
            }

            // Convert HashMap -> serde_json::Map for the redaction API
            let mut map: serde_json::Map<String, serde_json::Value> = event
                .details
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();

            // 1. Redact configured fields
            if !config.redact_fields.is_empty() {
                map = redact_map(&map, &redact_refs);
            }

            // 2. Hash customer_id if present
            if config.hash_customer_id {
                if let Some(cid) = map.get("customer_id") {
                    if let Some(s) = cid.as_str() {
                        map.insert(
                            "customer_id".to_string(),
                            serde_json::Value::String(hash_value(s)),
                        );
                    }
                }
            }

            // 3. Enforce metadata size limit
            map = enforce_metadata_limit(&map, MAX_METADATA_BYTES);

            // Convert back to HashMap
            event.details = map.into_iter().collect();
        }

        let event_dicts: Vec<serde_json::Value> = events.iter().map(|e| e.to_dict()).collect();

        // Build the union of (pending tasks) and (tasks referenced by pending
        // events that are not already in pending_tasks). The latter covers
        // resilience: if an earlier task flush failed, the next event flush
        // re-includes the relevant tasks.
        use std::collections::HashSet;
        let mut tasks_to_send: Vec<crate::core::models::Task> = pending_tasks;
        let already_included: HashSet<String> =
            tasks_to_send.iter().map(|t| t.task_id.clone()).collect();

        let event_task_ids: Vec<String> = events
            .iter()
            .map(|e| e.task_id.clone())
            .collect::<HashSet<_>>()
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

        let task_dicts: Vec<serde_json::Value> =
            tasks_to_send.iter().map(|t| t.to_dict()).collect();

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
            0,
        )
        .await?;

        // Outer mark_synced retained as a defensive idempotent
        // no-op safety net for any future code path that returns Ok
        // without recursing into the leaf.
        let mut buf = buffer.lock().await;
        buf.mark_synced(&event_ids);
        buf.mark_tasks_synced(&task_ids_sent);
        Ok(())
    }

    /// Recursively splits oversized payloads until they fit within
    /// MAX_PAYLOAD_BYTES. Tasks are only sent with the first half to avoid
    /// duplication. Uses `Box::pin` because recursive async fns require
    /// indirection to avoid infinitely-sized futures.
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
        depth: usize,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), DexcostError>> + Send + 'a>>
    {
        Box::pin(async move {
            let payload = serde_json::to_string(&serde_json::json!({
                "events": events,
                "tasks": tasks,
            }))?;

            if payload.len() <= MAX_PAYLOAD_BYTES || depth >= MAX_SPLIT_DEPTH {
                Self::post_raw(&payload, config, client, auth_failed, api_key_override).await?;
                // Sprint 2 Theme D / §3.2.1 (B12) — mark synced at the
                // leaf so a sibling-half failure does not re-send
                // already-POSTed events.
                let mut buf = buffer.lock().await;
                buf.mark_synced(event_ids);
                buf.mark_tasks_synced(task_ids);
                return Ok(());
            }

            if events.len() <= 1 {
                eprintln!(
                    "[dexcost] Single event exceeds payload limit ({} bytes), skipping",
                    payload.len()
                );
                return Ok(());
            }

            let mid = events.len() / 2;
            let mid_id = event_ids.len() / 2;
            eprintln!(
                "[dexcost] Batch too large ({} bytes, {} events), splitting",
                payload.len(),
                events.len()
            );

            // First half carries the tasks (Tasks are only sent with
            // the first chunk to avoid duplication); second half: no
            // tasks (so empty task_ids for the leaf mark).
            Self::push_with_split(
                &events[..mid],
                tasks,
                &event_ids[..mid_id],
                task_ids,
                buffer,
                config,
                client,
                auth_failed,
                api_key_override,
                depth + 1,
            )
            .await?;
            Self::push_with_split(
                &events[mid..],
                &[],
                &event_ids[mid_id..],
                &[],
                buffer,
                config,
                client,
                auth_failed,
                api_key_override,
                depth + 1,
            )
            .await
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
        if status == reqwest::StatusCode::UNAUTHORIZED
            || status == reqwest::StatusCode::FORBIDDEN
        {
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
            Ok(())
        } else {
            Err(DexcostError::Transport(format!(
                "push failed with status {}",
                status
            )))
        }
    }
}
