use std::str::FromStr;

use chrono::{DateTime, Utc};
use rusqlite::{params, Connection, Result as SqlResult};
use rust_decimal::Decimal;

use crate::core::models::{CostConfidence, CostEvent, EventType, PricingSource, Task, TaskStatus};

// ---------------------------------------------------------------------------
// Schema DDL
// ---------------------------------------------------------------------------

const DDL_TASKS: &str = "
CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    task_type           TEXT NOT NULL,
    status              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    metadata            TEXT,
    llm_cost_usd        TEXT,
    external_cost_usd   TEXT,
    compute_cost_usd    TEXT,
    network_cost_usd    TEXT NOT NULL DEFAULT '0',
    gpu_cost_usd        TEXT NOT NULL DEFAULT '0',
    total_cost_usd      TEXT,
    total_input_tokens  INTEGER,
    total_output_tokens INTEGER,
    total_cached_tokens INTEGER,
    retry_count         INTEGER DEFAULT 0,
    retry_cost_usd      TEXT    DEFAULT '0',
    failure_count       INTEGER DEFAULT 0,
    customer_id         TEXT,
    project_id          TEXT,
    parent_task_id      TEXT,
    experiment_id       TEXT,
    variant             TEXT,
    sync_status         TEXT NOT NULL DEFAULT 'pending'
);";

const DDL_EVENTS: &str = "
CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    provider         TEXT,
    model            TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cached_tokens    INTEGER,
    service_name     TEXT,
    cost_usd         TEXT NOT NULL,
    latency_ms       INTEGER,
    cost_confidence  TEXT NOT NULL DEFAULT 'exact',
    pricing_source   TEXT,
    pricing_version  TEXT,
    is_retry         INTEGER DEFAULT 0,
    retry_reason     TEXT,
    retry_of         TEXT,
    details          TEXT,
    timestamp        TEXT NOT NULL,
    sync_status      TEXT NOT NULL DEFAULT 'pending'
);";

const DDL_SCHEMA_VERSION: &str = "
CREATE TABLE IF NOT EXISTS schema_version (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    version_number  INTEGER NOT NULL,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    migration_name  TEXT
);";

const DDL_INDEXES: &str = "
CREATE INDEX IF NOT EXISTS idx_events_task_id       ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_sync_status   ON events(sync_status);
CREATE INDEX IF NOT EXISTS idx_events_timestamp     ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_tasks_customer_id    ON tasks(customer_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project_id     ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_started_at     ON tasks(started_at);
CREATE INDEX IF NOT EXISTS idx_tasks_sync_status    ON tasks(sync_status);
";

// ---------------------------------------------------------------------------
// Helper: serialise / deserialise enum variants
// ---------------------------------------------------------------------------

fn task_status_to_str(s: &TaskStatus) -> &'static str {
    match s {
        TaskStatus::Pending => "pending",
        TaskStatus::Success => "success",
        TaskStatus::Failed => "failed",
        TaskStatus::Running => "running",
    }
}

fn task_status_from_str(s: &str) -> TaskStatus {
    match s {
        "running" => TaskStatus::Running,
        "success" => TaskStatus::Success,
        "failed" => TaskStatus::Failed,
        _ => TaskStatus::Pending,
    }
}

fn event_type_to_str(et: &EventType) -> &'static str {
    match et {
        EventType::LlmCall => "llm_call",
        EventType::ExternalCost => "external_cost",
        EventType::ComputeCost => "compute_cost",
        EventType::RetryMarker => "retry_marker",
        EventType::Network => "network",
        EventType::GpuCost => "gpu_cost",
        EventType::GpuUtilizationSignal => "gpu_utilization_signal",
    }
}

fn event_type_from_str(s: &str) -> EventType {
    match s {
        "external_cost" => EventType::ExternalCost,
        "compute_cost" => EventType::ComputeCost,
        "retry_marker" => EventType::RetryMarker,
        "network" => EventType::Network,
        "gpu_cost" => EventType::GpuCost,
        "gpu_utilization_signal" => EventType::GpuUtilizationSignal,
        _ => EventType::LlmCall,
    }
}

fn cost_confidence_to_str(cc: &CostConfidence) -> &'static str {
    match cc {
        CostConfidence::Exact => "exact",
        CostConfidence::Computed => "computed",
        CostConfidence::Estimated => "estimated",
        CostConfidence::Unknown => "unknown",
    }
}

fn cost_confidence_from_str(s: &str) -> CostConfidence {
    match s {
        "computed" => CostConfidence::Computed,
        "estimated" => CostConfidence::Estimated,
        "unknown" => CostConfidence::Unknown,
        _ => CostConfidence::Exact,
    }
}

fn pricing_source_to_str(ps: &PricingSource) -> &'static str {
    match ps {
        PricingSource::Litellm => "litellm",
        PricingSource::Tokencost => "tokencost",
        PricingSource::ProviderResponse => "provider_response",
        PricingSource::Manual => "manual",
        PricingSource::Custom => "custom",
        PricingSource::RateRegistry => "rate_registry",
        PricingSource::ServiceCatalog => "service_catalog",
        PricingSource::UserOverride => "user_override",
        PricingSource::Unknown => "unknown",
    }
}

fn pricing_source_from_str(s: &str) -> PricingSource {
    match s {
        "litellm" => PricingSource::Litellm,
        "tokencost" => PricingSource::Tokencost,
        "provider_response" => PricingSource::ProviderResponse,
        "manual" => PricingSource::Manual,
        "custom" => PricingSource::Custom,
        "rate_registry" => PricingSource::RateRegistry,
        "service_catalog" => PricingSource::ServiceCatalog,
        "user_override" => PricingSource::UserOverride,
        _ => PricingSource::Unknown,
    }
}

fn decimal_to_str(d: &Decimal) -> String {
    d.to_string()
}

fn decimal_from_str(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap_or_else(|e| {
        eprintln!("[dexcost] invalid decimal '{}': {}", s, e);
        Decimal::ZERO
    })
}

fn dt_to_str(dt: &DateTime<Utc>) -> String {
    dt.to_rfc3339()
}

fn dt_from_str(s: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(s)
        .map(|d| d.with_timezone(&Utc))
        .unwrap_or_else(|e| {
            eprintln!("[dexcost] invalid timestamp '{}': {}", s, e);
            Utc::now()
        })
}

// ---------------------------------------------------------------------------
// EventBuffer
// ---------------------------------------------------------------------------

/// SQLite-backed event buffer.
/// `new()` creates an in-memory database (suitable for tests / offline use).
/// `open(path)` opens or creates a file-backed database.
pub struct EventBuffer {
    conn: Connection,
}

impl EventBuffer {
    // ------------------------------------------------------------------
    // Constructors
    // ------------------------------------------------------------------

    /// Creates an in-memory SQLite buffer. Suitable for tests.
    /// Returns an error if SQLite cannot be opened or schema initialisation fails.
    pub fn new() -> Result<Self, crate::error::DexcostError> {
        let conn = Connection::open_in_memory().map_err(|e| {
            crate::error::DexcostError::Storage(format!("failed to open SQLite: {}", e))
        })?;
        let mut buf = Self { conn };
        buf.init_schema().map_err(|e| {
            crate::error::DexcostError::Storage(format!("failed to init schema: {}", e))
        })?;
        Ok(buf)
    }

    /// Opens (or creates) a file-backed SQLite buffer.
    pub fn open(db_path: &str) -> SqlResult<Self> {
        // Ensure parent directory exists
        if let Some(parent) = std::path::Path::new(db_path).parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|e| {
                    rusqlite::Error::InvalidPath(std::path::PathBuf::from(format!(
                        "create_dir_all failed: {e}"
                    )))
                })?;
            }
        }
        let conn = Connection::open(db_path)?;
        let mut buf = Self { conn };
        buf.init_schema()?;
        Ok(buf)
    }

    // ------------------------------------------------------------------
    // Schema initialisation
    // ------------------------------------------------------------------

    fn init_schema(&mut self) -> SqlResult<()> {
        self.conn.execute_batch(
            "
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;
        ",
        )?;
        self.conn.execute_batch(DDL_TASKS)?;
        self.conn.execute_batch(DDL_EVENTS)?;
        self.conn.execute_batch(DDL_SCHEMA_VERSION)?;

        // Migration v2: tasks.sync_status — added DEX-297. The CREATE TABLE
        // above includes the column for fresh DBs; older buffers need an
        // idempotent ALTER. SQLite returns a "duplicate column" error when
        // the column already exists; we ignore that specific case.
        if let Err(e) = self.conn.execute(
            "ALTER TABLE tasks ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'pending'",
            [],
        ) {
            let msg = e.to_string();
            if !msg.contains("duplicate column") {
                return Err(e);
            }
        }

        // Migration v5: tasks.network_cost_usd. Idempotent ALTER for legacy DBs.
        if let Err(e) = self.conn.execute(
            "ALTER TABLE tasks ADD COLUMN network_cost_usd TEXT NOT NULL DEFAULT '0'",
            [],
        ) {
            let msg = e.to_string();
            if !msg.contains("duplicate column") {
                return Err(e);
            }
        }

        // Migration v6 (Phase 2 GPU foundation): tasks.gpu_cost_usd.
        // Idempotent ALTER for legacy DBs. Mirrors Python v5 → v6 migration
        // (commit 2785158). Total cost becomes
        // llm + external + compute + network + gpu.
        if let Err(e) = self.conn.execute(
            "ALTER TABLE tasks ADD COLUMN gpu_cost_usd TEXT NOT NULL DEFAULT '0'",
            [],
        ) {
            let msg = e.to_string();
            if !msg.contains("duplicate column") {
                return Err(e);
            }
        }

        self.conn.execute_batch(DDL_INDEXES)?;

        // Record schema version if not already present
        let count: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM schema_version WHERE version_number = 1",
            [],
            |r| r.get(0),
        )?;
        if count == 0 {
            self.conn.execute(
                "INSERT INTO schema_version (version_number, migration_name) VALUES (1, 'initial')",
                [],
            )?;
        }
        let v2_count: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM schema_version WHERE version_number = 2",
            [],
            |r| r.get(0),
        )?;
        if v2_count == 0 {
            self.conn.execute(
                "INSERT INTO schema_version (version_number, migration_name) VALUES (2, 'tasks_sync_status')",
                [],
            )?;
        }
        Ok(())
    }

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    /// Persists (inserts or replaces) an event.
    pub fn add_event(&mut self, event: CostEvent) {
        let details_json =
            serde_json::to_string(&event.details).unwrap_or_else(|_| "{}".to_string());
        let ps = event.pricing_source.as_ref().map(pricing_source_to_str);

        if let Err(e) = self.conn.execute(
            "INSERT OR REPLACE INTO events
             (event_id, task_id, event_type, provider, model,
              input_tokens, output_tokens, cached_tokens, service_name,
              cost_usd, latency_ms, cost_confidence, pricing_source,
              pricing_version, is_retry, retry_reason, retry_of,
              details, timestamp, sync_status)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,'pending')",
            params![
                event.event_id,
                event.task_id,
                event_type_to_str(&event.event_type),
                event.provider,
                event.model,
                event.input_tokens,
                event.output_tokens,
                event.cached_tokens,
                event.service_name,
                decimal_to_str(&event.cost_usd),
                event.latency_ms,
                cost_confidence_to_str(&event.cost_confidence),
                ps,
                event.pricing_version,
                if event.is_retry { 1i64 } else { 0i64 },
                event.retry_reason,
                event.retry_of,
                details_json,
                dt_to_str(&event.occurred_at),
            ],
        ) {
            eprintln!("[dexcost] failed to persist event: {}", e);
        }
    }

    /// Inserts or replaces a task.
    pub fn upsert_task(&mut self, task: Task) {
        let metadata_json =
            serde_json::to_string(&task.metadata).unwrap_or_else(|_| "{}".to_string());

        if let Err(e) = self.conn.execute(
            "INSERT OR REPLACE INTO tasks
             (task_id, task_type, status, started_at, ended_at, metadata,
              llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
              total_input_tokens, total_output_tokens, total_cached_tokens,
              retry_count, retry_cost_usd, failure_count,
              customer_id, project_id, parent_task_id, experiment_id, variant,
              sync_status)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,'pending')",
            params![
                task.task_id,
                task.task_type,
                task_status_to_str(&task.status),
                dt_to_str(&task.started_at),
                task.ended_at.as_ref().map(dt_to_str),
                metadata_json,
                decimal_to_str(&task.llm_cost_usd),
                decimal_to_str(&task.external_cost_usd),
                decimal_to_str(&task.compute_cost_usd),
                decimal_to_str(&task.total_cost_usd),
                task.total_input_tokens,
                task.total_output_tokens,
                task.total_cached_tokens,
                task.retry_count,
                decimal_to_str(&task.retry_cost_usd),
                task.failure_count,
                task.customer_id,
                task.project_id,
                task.parent_task_id,
                task.experiment_id,
                task.variant,
            ],
        ) {
            eprintln!("[dexcost] failed to upsert task: {}", e);
        }
    }

    /// Returns up to `limit` events whose sync_status is 'pending'.
    pub fn get_pending_events(&self, limit: usize) -> Vec<CostEvent> {
        let mut stmt = match self.conn.prepare(
            "SELECT event_id, task_id, event_type, provider, model,
                    input_tokens, output_tokens, cached_tokens, service_name,
                    cost_usd, latency_ms, cost_confidence, pricing_source,
                    pricing_version, is_retry, retry_reason, retry_of,
                    details, timestamp
             FROM events WHERE sync_status = 'pending'
             ORDER BY timestamp ASC
             LIMIT ?1",
        ) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[dexcost] query prepare failed (get_pending_events): {}", e);
                return vec![];
            }
        };

        stmt.query_map(params![limit as i64], row_to_event)
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
    }

    /// Marks the given event IDs as synced.
    pub fn mark_synced(&mut self, event_ids: &[String]) {
        for id in event_ids {
            if let Err(e) = self.conn.execute(
                "UPDATE events SET sync_status = 'synced' WHERE event_id = ?1",
                params![id],
            ) {
                eprintln!("[dexcost] failed to mark event synced: {}", e);
            }
        }
    }

    /// Moves unrepresentable events out of the normal pending delivery window
    /// without claiming that the control plane accepted them. Returns the
    /// number of rows transitioned so the pusher can detect storage failures
    /// instead of spinning on the same malformed prefix.
    pub fn mark_quarantined(&mut self, event_ids: &[String]) -> usize {
        let mut updated = 0;
        for id in event_ids {
            match self.conn.execute(
                "UPDATE events SET sync_status = 'quarantined' \
                 WHERE sync_status = 'pending' AND event_id = ?1",
                params![id],
            ) {
                Ok(count) => updated += count,
                Err(e) => eprintln!("[dexcost] failed to quarantine event: {}", e),
            }
        }
        updated
    }

    /// Returns quarantined attribution conversion failures, oldest first, for
    /// diagnostics. Quarantined rows remain durable until retention cleanup.
    pub fn get_quarantined_events(&self, limit: usize) -> Vec<CostEvent> {
        let mut stmt = match self.conn.prepare(
            "SELECT event_id, task_id, event_type, provider, model,
                    input_tokens, output_tokens, cached_tokens, service_name,
                    cost_usd, latency_ms, cost_confidence, pricing_source,
                    pricing_version, is_retry, retry_reason, retry_of,
                    details, timestamp
             FROM events WHERE sync_status = 'quarantined'
             ORDER BY timestamp ASC
             LIMIT ?1",
        ) {
            Ok(s) => s,
            Err(e) => {
                eprintln!(
                    "[dexcost] query prepare failed (get_quarantined_events): {}",
                    e
                );
                return vec![];
            }
        };

        stmt.query_map(params![limit as i64], row_to_event)
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
    }

    /// Returns a Task by ID, or None.
    pub fn get_task(&self, task_id: &str) -> Option<Task> {
        self.conn
            .query_row(
                "SELECT task_id, task_type, status, started_at, ended_at, metadata,
                    llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
                    total_input_tokens, total_output_tokens, total_cached_tokens,
                    retry_count, retry_cost_usd, failure_count,
                    customer_id, project_id, parent_task_id, experiment_id, variant
             FROM tasks WHERE task_id = ?1",
                params![task_id],
                row_to_task,
            )
            .ok()
    }

    /// Returns all tasks.
    pub fn all_tasks(&self) -> Vec<Task> {
        let mut stmt = match self.conn.prepare(
            "SELECT task_id, task_type, status, started_at, ended_at, metadata,
                    llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
                    total_input_tokens, total_output_tokens, total_cached_tokens,
                    retry_count, retry_cost_usd, failure_count,
                    customer_id, project_id, parent_task_id, experiment_id, variant
             FROM tasks ORDER BY started_at ASC",
        ) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[dexcost] query prepare failed (all_tasks): {}", e);
                return vec![];
            }
        };

        stmt.query_map([], row_to_task)
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
    }

    /// Returns all events (any sync status).
    pub fn all_events(&self) -> Vec<CostEvent> {
        let mut stmt = match self.conn.prepare(
            "SELECT event_id, task_id, event_type, provider, model,
                    input_tokens, output_tokens, cached_tokens, service_name,
                    cost_usd, latency_ms, cost_confidence, pricing_source,
                    pricing_version, is_retry, retry_reason, retry_of,
                    details, timestamp
             FROM events ORDER BY timestamp ASC",
        ) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[dexcost] query prepare failed (all_events): {}", e);
                return vec![];
            }
        };

        stmt.query_map([], row_to_event)
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
    }

    /// Returns the number of pending events.
    pub fn pending_count(&self) -> usize {
        self.conn
            .query_row(
                "SELECT COUNT(*) FROM events WHERE sync_status = 'pending'",
                [],
                |r| r.get::<_, i64>(0),
            )
            .unwrap_or(0) as usize
    }

    /// Returns all events for a specific task_id.
    pub fn query_events(&self, task_id: &str) -> Vec<CostEvent> {
        let mut stmt = match self.conn.prepare(
            "SELECT event_id, task_id, event_type, provider, model,
                    input_tokens, output_tokens, cached_tokens, service_name,
                    cost_usd, latency_ms, cost_confidence, pricing_source,
                    pricing_version, is_retry, retry_reason, retry_of,
                    details, timestamp
             FROM events WHERE task_id = ?1 ORDER BY timestamp ASC",
        ) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[dexcost] query prepare failed (query_events): {}", e);
                return vec![];
            }
        };

        stmt.query_map(params![task_id], row_to_event)
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
    }

    /// Updates an existing event (INSERT OR REPLACE based on event_id).
    pub fn update_event(&mut self, event: &CostEvent) {
        let details_json =
            serde_json::to_string(&event.details).unwrap_or_else(|_| "{}".to_string());
        let ps = event.pricing_source.as_ref().map(pricing_source_to_str);

        // Preserve existing sync_status during update
        if let Err(e) = self.conn.execute(
            "UPDATE events SET
                task_id         = ?2,
                event_type      = ?3,
                provider        = ?4,
                model           = ?5,
                input_tokens    = ?6,
                output_tokens   = ?7,
                cached_tokens   = ?8,
                service_name    = ?9,
                cost_usd        = ?10,
                latency_ms      = ?11,
                cost_confidence = ?12,
                pricing_source  = ?13,
                pricing_version = ?14,
                is_retry        = ?15,
                retry_reason    = ?16,
                retry_of        = ?17,
                details         = ?18,
                timestamp       = ?19
             WHERE event_id = ?1",
            params![
                event.event_id,
                event.task_id,
                event_type_to_str(&event.event_type),
                event.provider,
                event.model,
                event.input_tokens,
                event.output_tokens,
                event.cached_tokens,
                event.service_name,
                decimal_to_str(&event.cost_usd),
                event.latency_ms,
                cost_confidence_to_str(&event.cost_confidence),
                ps,
                event.pricing_version,
                if event.is_retry { 1i64 } else { 0i64 },
                event.retry_reason,
                event.retry_of,
                details_json,
                dt_to_str(&event.occurred_at),
            ],
        ) {
            eprintln!("[dexcost] failed to update event: {}", e);
        }
    }

    /// Returns total number of events (any sync status).
    pub fn event_count(&self) -> usize {
        self.conn
            .query_row("SELECT COUNT(*) FROM events", [], |r| r.get::<_, i64>(0))
            .unwrap_or(0) as usize
    }

    /// Returns total number of tasks.
    pub fn task_count(&self) -> usize {
        self.conn
            .query_row("SELECT COUNT(*) FROM tasks", [], |r| r.get::<_, i64>(0))
            .unwrap_or(0) as usize
    }

    /// Returns up to `limit` tasks whose sync_status is 'pending'.
    /// Tasks become pending when first inserted and whenever they are
    /// re-upserted (e.g. status transitions, end_task, total recomputation).
    pub fn get_pending_tasks(&self, limit: usize) -> Vec<Task> {
        let mut stmt = match self.conn.prepare(
            "SELECT task_id, task_type, status, started_at, ended_at, metadata,
                    llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
                    total_input_tokens, total_output_tokens, total_cached_tokens,
                    retry_count, retry_cost_usd, failure_count,
                    customer_id, project_id, parent_task_id, experiment_id, variant
             FROM tasks WHERE sync_status = 'pending'
             ORDER BY started_at ASC
             LIMIT ?1",
        ) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[dexcost] query prepare failed (get_pending_tasks): {}", e);
                return vec![];
            }
        };

        stmt.query_map(params![limit as i64], row_to_task)
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
    }

    /// Marks the given task IDs as synced.
    pub fn mark_tasks_synced(&mut self, task_ids: &[String]) {
        for id in task_ids {
            if let Err(e) = self.conn.execute(
                "UPDATE tasks SET sync_status = 'synced' WHERE task_id = ?1",
                params![id],
            ) {
                eprintln!("[dexcost] failed to mark task synced: {}", e);
            }
        }
    }

    /// Returns the number of tasks whose sync_status is 'pending'.
    pub fn pending_task_count(&self) -> usize {
        self.conn
            .query_row(
                "SELECT COUNT(*) FROM tasks WHERE sync_status = 'pending'",
                [],
                |r| r.get::<_, i64>(0),
            )
            .unwrap_or(0) as usize
    }

    /// Returns pending tasks matching the given IDs. Tasks already accepted by
    /// ingestion must not be resent merely because a later event is retried.
    pub fn get_tasks_by_ids(&self, task_ids: &[String]) -> Vec<Task> {
        if task_ids.is_empty() {
            return vec![];
        }
        let placeholders: Vec<String> = task_ids
            .iter()
            .enumerate()
            .map(|(i, _)| format!("?{}", i + 1))
            .collect();
        let sql = format!(
            "SELECT task_id, task_type, status, started_at, ended_at, metadata,
                    llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
                    total_input_tokens, total_output_tokens, total_cached_tokens,
                    retry_count, retry_cost_usd, failure_count,
                    customer_id, project_id, parent_task_id, experiment_id, variant
             FROM tasks
             WHERE task_id IN ({}) AND sync_status = 'pending'
             ORDER BY started_at ASC",
            placeholders.join(", ")
        );
        let mut stmt = match self.conn.prepare(&sql) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("[dexcost] query prepare failed (get_tasks_by_ids): {}", e);
                return vec![];
            }
        };
        let params: Vec<&dyn rusqlite::ToSql> = task_ids
            .iter()
            .map(|id| id as &dyn rusqlite::ToSql)
            .collect();
        stmt.query_map(rusqlite::params_from_iter(params.iter()), row_to_task)
            .map(|rows| rows.filter_map(|r| r.ok()).collect())
            .unwrap_or_default()
    }

    /// Deletes synced events older than `retention_hours` and runs `VACUUM`
    /// to reclaim disk space. Returns the number of deleted rows.
    ///
    /// Mirrors Python `storage/sqlite.py` `purge_synced` (default 48 hours).
    pub fn purge_synced(&mut self, retention_hours: i64) -> usize {
        let deleted = match self.conn.execute(
            "DELETE FROM events WHERE sync_status = 'synced' \
             AND timestamp < datetime('now', ?1 || ' hours')",
            params![(-retention_hours).to_string()],
        ) {
            Ok(n) => n,
            Err(e) => {
                eprintln!("[dexcost] purge_synced failed: {}", e);
                return 0;
            }
        };
        if let Err(e) = self.conn.execute_batch("VACUUM") {
            eprintln!("[dexcost] VACUUM after purge_synced failed: {}", e);
        }
        deleted
    }

    /// Deletes pending or quarantined events older than `max_age_days` and
    /// runs `VACUUM` when rows were removed. Quarantined rows remain available
    /// for diagnosis during the normal retention window.
    ///
    /// Mirrors Python `storage/sqlite.py` `purge_old_pending` (default 7 days).
    pub fn purge_old_pending(&mut self, max_age_days: i64) -> usize {
        let cutoff = (Utc::now() - chrono::Duration::days(max_age_days)).to_rfc3339();
        let deleted = match self.conn.execute(
            "DELETE FROM events WHERE sync_status IN ('pending', 'quarantined') \
             AND timestamp < ?1",
            params![cutoff],
        ) {
            Ok(n) => n,
            Err(e) => {
                eprintln!("[dexcost] purge_old_pending failed: {}", e);
                return 0;
            }
        };
        if deleted > 0 {
            if let Err(e) = self.conn.execute_batch("VACUUM") {
                eprintln!("[dexcost] VACUUM after purge_old_pending failed: {}", e);
            }
        }
        deleted
    }

    /// Closes the connection explicitly (connection also closes on drop).
    pub fn close(&mut self) {
        // rusqlite::Connection doesn't expose a close() that takes &mut self;
        // dropping is sufficient. This is a no-op placeholder kept for API
        // compatibility described in the task spec.
    }
}

// Note: EventBuffer intentionally does not implement Default because `new()`
// returns `Result`. Use `EventBuffer::new()` directly and handle the error.

// ---------------------------------------------------------------------------
// Row mappers
// ---------------------------------------------------------------------------

/// Maps a SQLite row to a Task struct.
///
/// Expected column order (must match SELECT in get_task / all_tasks):
///   0: task_id, 1: task_type, 2: status, 3: started_at, 4: ended_at,
///   5: metadata, 6: llm_cost_usd, 7: external_cost_usd, 8: compute_cost_usd,
///   9: total_cost_usd, 10: total_input_tokens, 11: total_output_tokens,
///   12: total_cached_tokens, 13: retry_count, 14: retry_cost_usd,
///   15: failure_count, 16: customer_id, 17: project_id, 18: parent_task_id,
///   19: experiment_id, 20: variant
fn row_to_task(row: &rusqlite::Row<'_>) -> SqlResult<Task> {
    let metadata_str: Option<String> = row.get(5)?;
    let metadata = metadata_str
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or_default();

    Ok(Task {
        task_id: row.get(0)?,
        task_type: row.get(1)?,
        status: task_status_from_str(&row.get::<_, String>(2)?),
        started_at: dt_from_str(&row.get::<_, String>(3)?),
        ended_at: row.get::<_, Option<String>>(4)?.as_deref().map(dt_from_str),
        metadata,
        llm_cost_usd: decimal_from_str(&row.get::<_, String>(6).unwrap_or_else(|_| "0".into())),
        external_cost_usd: decimal_from_str(
            &row.get::<_, String>(7).unwrap_or_else(|_| "0".into()),
        ),
        compute_cost_usd: decimal_from_str(&row.get::<_, String>(8).unwrap_or_else(|_| "0".into())),
        total_cost_usd: decimal_from_str(&row.get::<_, String>(9).unwrap_or_else(|_| "0".into())),
        total_input_tokens: row.get::<_, Option<i64>>(10)?.unwrap_or(0),
        total_output_tokens: row.get::<_, Option<i64>>(11)?.unwrap_or(0),
        total_cached_tokens: row.get::<_, Option<i64>>(12)?.unwrap_or(0),
        retry_count: row.get::<_, Option<i32>>(13)?.unwrap_or(0),
        retry_cost_usd: decimal_from_str(&row.get::<_, String>(14).unwrap_or_else(|_| "0".into())),
        failure_count: row.get::<_, Option<i32>>(15)?.unwrap_or(0),
        customer_id: row.get(16)?,
        project_id: row.get(17)?,
        parent_task_id: row.get(18)?,
        experiment_id: row.get(19)?,
        variant: row.get(20)?,
        // Network capture v1 — not yet persisted to SQLite; defaults reapplied on load.
        network_bytes_in: 0,
        network_bytes_out: 0,
        network_call_count: 0,
        network_by_host: serde_json::json!({"hosts": []}),
        network_cost_usd: rust_decimal::Decimal::ZERO,
        gpu_cost_usd: rust_decimal::Decimal::ZERO,
        network_accountant: std::sync::Arc::default(),
        compute: None,
        gpu: None,
        schema_version: "1".to_string(),
    })
}

/// Maps a SQLite row to a CostEvent struct.
///
/// Expected column order (must match SELECT in get_pending_events / all_events / query_events):
///   0: event_id, 1: task_id, 2: event_type, 3: provider, 4: model,
///   5: input_tokens, 6: output_tokens, 7: cached_tokens, 8: service_name,
///   9: cost_usd, 10: latency_ms, 11: cost_confidence, 12: pricing_source,
///   13: pricing_version, 14: is_retry, 15: retry_reason, 16: retry_of,
///   17: details, 18: timestamp
fn row_to_event(row: &rusqlite::Row<'_>) -> SqlResult<CostEvent> {
    let details_str: Option<String> = row.get(17)?;
    let details = details_str
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or_default();

    let is_retry_int: i64 = row.get::<_, Option<i64>>(14)?.unwrap_or(0);
    let ps_str: Option<String> = row.get(12)?;
    let pricing_source = ps_str.as_deref().map(pricing_source_from_str);

    Ok(CostEvent {
        event_id: row.get(0)?,
        task_id: row.get(1)?,
        event_type: event_type_from_str(&row.get::<_, String>(2)?),
        provider: row.get(3)?,
        model: row.get(4)?,
        input_tokens: row.get(5)?,
        output_tokens: row.get(6)?,
        cached_tokens: row.get(7)?,
        service_name: row.get(8)?,
        cost_usd: decimal_from_str(&row.get::<_, String>(9)?),
        latency_ms: row.get(10)?,
        cost_confidence: cost_confidence_from_str(
            &row.get::<_, String>(11).unwrap_or_else(|_| "exact".into()),
        ),
        pricing_source,
        pricing_version: row.get(13)?,
        is_retry: is_retry_int != 0,
        retry_reason: row.get(15)?,
        retry_of: row.get(16)?,
        details,
        occurred_at: dt_from_str(&row.get::<_, String>(18)?),
        schema_version: "1".to_string(),
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::models::{CostEvent, EventType, Task};
    use rust_decimal::Decimal;

    // ------------------------------------------------------------------
    // Existing tests (updated to work with SQLite-backed in-memory buffer)
    // ------------------------------------------------------------------

    #[test]
    fn test_add_event() {
        let mut buffer = EventBuffer::new().unwrap();
        let event = CostEvent::new("task-1", EventType::LlmCall);
        buffer.add_event(event);
        assert_eq!(buffer.event_count(), 1);
    }

    #[test]
    fn test_upsert_task() {
        let mut buffer = EventBuffer::new().unwrap();
        let task = Task::new("test_type");
        let task_id = task.task_id.clone();
        buffer.upsert_task(task);
        assert_eq!(buffer.task_count(), 1);
        assert!(buffer.get_task(&task_id).is_some());
    }

    #[test]
    fn test_get_pending_events() {
        let mut buffer = EventBuffer::new().unwrap();

        let e1 = CostEvent::new("task-1", EventType::LlmCall);
        let e2 = CostEvent::new("task-1", EventType::ExternalCost);
        let e3 = CostEvent::new("task-1", EventType::ComputeCost);

        buffer.add_event(e1.clone());
        buffer.add_event(e2.clone());
        buffer.add_event(e3);

        // All 3 are pending
        assert_eq!(buffer.pending_count(), 3);
        let pending = buffer.get_pending_events(2);
        assert_eq!(pending.len(), 2);

        // Mark first two as synced
        buffer.mark_synced(&[e1.event_id, e2.event_id]);
        assert_eq!(buffer.pending_count(), 1);
    }

    #[test]
    fn test_upsert_task_updates() {
        let mut buffer = EventBuffer::new().unwrap();
        let mut task = Task::new("test_type");
        let task_id = task.task_id.clone();

        buffer.upsert_task(task.clone());
        assert_eq!(
            buffer.get_task(&task_id).unwrap().llm_cost_usd,
            Decimal::ZERO
        );

        task.llm_cost_usd = Decimal::new(5, 2);
        buffer.upsert_task(task);
        assert_eq!(
            buffer.get_task(&task_id).unwrap().llm_cost_usd,
            Decimal::new(5, 2)
        );
    }

    // ------------------------------------------------------------------
    // New tests
    // ------------------------------------------------------------------

    #[test]
    fn test_persistence_reopen() {
        let dir = tempfile::tempdir().expect("tempdir");
        let db_path = dir.path().join("test_buffer.db");
        let db_path_str = db_path.to_str().unwrap();

        // Open, insert, close
        {
            let mut buf = EventBuffer::open(db_path_str).expect("open");
            let event = CostEvent::new("task-persist", EventType::LlmCall);
            buf.add_event(event);
            assert_eq!(buf.event_count(), 1);
        }

        // Reopen — event must still be there
        {
            let buf = EventBuffer::open(db_path_str).expect("reopen");
            assert_eq!(buf.event_count(), 1);
            assert_eq!(buf.pending_count(), 1);
        }
    }

    #[test]
    fn test_query_events_by_task() {
        let mut buffer = EventBuffer::new().unwrap();
        let e1 = CostEvent::new("task-A", EventType::LlmCall);
        let e2 = CostEvent::new("task-A", EventType::ExternalCost);
        let e3 = CostEvent::new("task-B", EventType::ComputeCost);

        buffer.add_event(e1.clone());
        buffer.add_event(e2.clone());
        buffer.add_event(e3.clone());

        let task_a_events = buffer.query_events("task-A");
        assert_eq!(task_a_events.len(), 2);
        let ids: Vec<_> = task_a_events.iter().map(|e| &e.event_id).collect();
        assert!(ids.contains(&&e1.event_id));
        assert!(ids.contains(&&e2.event_id));

        let task_b_events = buffer.query_events("task-B");
        assert_eq!(task_b_events.len(), 1);
        assert_eq!(task_b_events[0].event_id, e3.event_id);
    }

    #[test]
    fn test_update_event() {
        let mut buffer = EventBuffer::new().unwrap();
        let mut event = CostEvent::new("task-upd", EventType::LlmCall);
        buffer.add_event(event.clone());

        // Modify and update
        event.cost_usd = Decimal::new(42, 2); // 0.42
        event.is_retry = true;
        event.retry_reason = Some("rate_limit".to_string());
        buffer.update_event(&event);

        let all = buffer.all_events();
        assert_eq!(all.len(), 1);
        assert_eq!(all[0].cost_usd, Decimal::new(42, 2));
        assert!(all[0].is_retry);
        assert_eq!(all[0].retry_reason.as_deref(), Some("rate_limit"));
    }

    // Gap 4: purge_synced deletes old synced events but keeps recent / pending ones.
    #[test]
    fn test_purge_synced() {
        let mut buffer = EventBuffer::new().unwrap();

        // One old synced event, one recent synced event, one pending event.
        let old = CostEvent::new("task-1", EventType::LlmCall);
        let recent = CostEvent::new("task-1", EventType::LlmCall);
        let pending = CostEvent::new("task-1", EventType::LlmCall);
        buffer.add_event(old.clone());
        buffer.add_event(recent.clone());
        buffer.add_event(pending.clone());

        // Backdate `old`'s timestamp 100 hours into the past.
        let old_ts = (Utc::now() - chrono::Duration::hours(100)).to_rfc3339();
        buffer
            .conn
            .execute(
                "UPDATE events SET timestamp = ?1 WHERE event_id = ?2",
                params![old_ts, old.event_id],
            )
            .unwrap();

        buffer.mark_synced(&[old.event_id.clone(), recent.event_id.clone()]);

        let deleted = buffer.purge_synced(48);
        assert_eq!(
            deleted, 1,
            "only the 100h-old synced event should be purged"
        );
        assert_eq!(buffer.event_count(), 2);
        // The pending event is untouched.
        assert_eq!(buffer.pending_count(), 1);
    }

    // Gap 4: purge_old_pending removes very old pending/quarantined events.
    #[test]
    fn test_purge_old_pending() {
        let mut buffer = EventBuffer::new().unwrap();

        let old = CostEvent::new("task-1", EventType::LlmCall);
        let old_quarantined = CostEvent::new("task-1", EventType::LlmCall);
        let fresh = CostEvent::new("task-1", EventType::LlmCall);
        buffer.add_event(old.clone());
        buffer.add_event(old_quarantined.clone());
        buffer.add_event(fresh.clone());

        // Backdate `old` 10 days into the past.
        let old_ts = (Utc::now() - chrono::Duration::days(10)).to_rfc3339();
        buffer
            .conn
            .execute(
                "UPDATE events SET timestamp = ?1 WHERE event_id = ?2",
                params![old_ts, old.event_id],
            )
            .unwrap();
        buffer
            .conn
            .execute(
                "UPDATE events SET timestamp = ?1 WHERE event_id = ?2",
                params![old_ts, old_quarantined.event_id],
            )
            .unwrap();
        assert_eq!(
            buffer.mark_quarantined(std::slice::from_ref(&old_quarantined.event_id)),
            1
        );
        assert_eq!(buffer.get_quarantined_events(10).len(), 1);

        let deleted = buffer.purge_old_pending(7);
        assert_eq!(deleted, 2);
        assert_eq!(buffer.event_count(), 1);
        assert_eq!(buffer.pending_count(), 1);
    }

    #[test]
    fn test_all_tasks_and_events() {
        let mut buffer = EventBuffer::new().unwrap();
        let task = Task::new("report");
        buffer.upsert_task(task.clone());
        let event = CostEvent::new(&task.task_id, EventType::ComputeCost);
        buffer.add_event(event);

        assert_eq!(buffer.all_tasks().len(), 1);
        assert_eq!(buffer.all_events().len(), 1);
    }
}
