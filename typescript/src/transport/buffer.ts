/**
 * SQLite-backed event buffer for dexcost.
 *
 * Persists cost events and tasks to a local SQLite database using the exact
 * same schema as the Python SDK. Data survives process restarts.
 *
 * Uses better-sqlite3 for synchronous, high-performance SQLite access.
 */

// Type-only import — keeps the static reference for TS without pulling
// the runtime binding in. The runtime side is loaded dynamically via
// createRequire inside the constructor so module load doesn't crash
// when better-sqlite3 is unavailable (Vercel Edge, Cloudflare Workers,
// Bun configurations without the native binding). Sprint 1 Theme B /
// §2.2.3 (B8).
import type Database from "better-sqlite3";
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import type { CostEvent, Task } from "../core/models.js";

// ---------------------------------------------------------------------------
// Row types — what better-sqlite3 returns from the DB
// ---------------------------------------------------------------------------

interface EventRow {
  event_id: string;
  task_id: string;
  event_type: string;
  provider: string | null;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  service_name: string | null;
  cost_usd: string;
  latency_ms: number | null;
  cost_confidence: string;
  pricing_source: string | null;
  pricing_version: string | null;
  is_retry: number;
  retry_reason: string | null;
  retry_of: string | null;
  details: string | null;
  timestamp: string;
  sync_status: string;
}

interface TaskRow {
  task_id: string;
  task_type: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  metadata: string | null;
  llm_cost_usd: string | null;
  external_cost_usd: string | null;
  compute_cost_usd: string | null;
  total_cost_usd: string | null;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  total_cached_tokens: number | null;
  retry_count: number | null;
  retry_cost_usd: string | null;
  failure_count: number | null;
  customer_id: string | null;
  project_id: string | null;
  parent_task_id: string | null;
  experiment_id: string | null;
  variant: string | null;
  sync_status: string;
}

interface CountRow {
  count: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function rowToEvent(row: EventRow): CostEvent {
  return {
    eventId: row.event_id,
    taskId: row.task_id,
    eventType: row.event_type as CostEvent["eventType"],
    provider: row.provider ?? undefined,
    model: row.model ?? undefined,
    inputTokens: row.input_tokens ?? undefined,
    outputTokens: row.output_tokens ?? undefined,
    cachedTokens: row.cached_tokens ?? undefined,
    serviceName: row.service_name ?? undefined,
    costUsd: Number(row.cost_usd),
    latencyMs: row.latency_ms ?? undefined,
    costConfidence: row.cost_confidence as CostEvent["costConfidence"],
    pricingSource: row.pricing_source as CostEvent["pricingSource"] ?? undefined,
    pricingVersion: row.pricing_version ?? undefined,
    isRetry: Boolean(row.is_retry),
    retryReason: row.retry_reason ?? undefined,
    retryOf: row.retry_of ?? undefined,
    details: (() => {
      let d: Record<string, unknown> = {};
      try {
        d = row.details != null ? (JSON.parse(row.details) as Record<string, unknown>) : {};
      } catch {
        d = {};
      }
      return d;
    })(),
    occurredAt: new Date(row.timestamp),
    schemaVersion: "1",
  };
}

function rowToTask(row: TaskRow): Task {
  return {
    taskId: row.task_id,
    taskType: row.task_type,
    status: row.status as Task["status"],
    startedAt: new Date(row.started_at),
    endedAt: row.ended_at != null ? new Date(row.ended_at) : undefined,
    metadata: (() => {
      let m: Record<string, unknown> = {};
      try {
        m = row.metadata != null ? (JSON.parse(row.metadata) as Record<string, unknown>) : {};
      } catch {
        m = {};
      }
      return m;
    })(),
    llmCostUsd: Number(row.llm_cost_usd ?? "0"),
    externalCostUsd: Number(row.external_cost_usd ?? "0"),
    computeCostUsd: Number(row.compute_cost_usd ?? "0"),
    totalCostUsd: Number(row.total_cost_usd ?? "0"),
    totalInputTokens: row.total_input_tokens ?? 0,
    totalOutputTokens: row.total_output_tokens ?? 0,
    totalCachedTokens: row.total_cached_tokens ?? 0,
    retryCount: row.retry_count ?? 0,
    retryCostUsd: Number(row.retry_cost_usd ?? "0"),
    failureCount: row.failure_count ?? 0,
    customerId: row.customer_id ?? undefined,
    projectId: row.project_id ?? undefined,
    parentTaskId: row.parent_task_id ?? undefined,
    experimentId: row.experiment_id ?? undefined,
    variant: row.variant ?? undefined,
    // Network capture fields default to zero / empty for rows that
    // pre-date the v1 migration. Phase D wires the SQLite columns +
    // serialisation/deserialisation; for now legacy rows read back as
    // fresh, matching Python's from_dict defaults.
    networkBytesIn: 0,
    networkBytesOut: 0,
    networkCallCount: 0,
    networkByHost: { hosts: [] },
    networkCostUsd: 0,
    schemaVersion: "1",
  };
}

// ---------------------------------------------------------------------------
// DDL
// ---------------------------------------------------------------------------

const CREATE_TASKS = `
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
    total_cost_usd      TEXT,
    total_input_tokens   INTEGER,
    total_output_tokens  INTEGER,
    total_cached_tokens  INTEGER,
    retry_count         INTEGER DEFAULT 0,
    retry_cost_usd      TEXT DEFAULT '0',
    failure_count       INTEGER DEFAULT 0,
    customer_id         TEXT,
    project_id          TEXT,
    parent_task_id      TEXT,
    experiment_id       TEXT,
    variant             TEXT,
    sync_status         TEXT NOT NULL DEFAULT 'pending'
)`;

const CREATE_EVENTS = `
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    provider        TEXT,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cached_tokens   INTEGER,
    service_name    TEXT,
    cost_usd        TEXT NOT NULL,
    latency_ms      INTEGER,
    cost_confidence TEXT NOT NULL DEFAULT 'exact',
    pricing_source  TEXT,
    pricing_version TEXT,
    is_retry        INTEGER DEFAULT 0,
    retry_reason    TEXT,
    retry_of        TEXT,
    details         TEXT,
    timestamp       TEXT NOT NULL,
    sync_status     TEXT NOT NULL DEFAULT 'pending'
)`;

const CREATE_SCHEMA_VERSION = `
CREATE TABLE IF NOT EXISTS schema_version (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    version_number  INTEGER NOT NULL,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    migration_name  TEXT
)`;

const INDEXES = [
  `CREATE INDEX IF NOT EXISTS idx_tasks_customer ON tasks(customer_id, started_at)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type, started_at)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_period ON tasks(started_at)`,
  `CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)`,
  `CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp)`,
  `CREATE INDEX IF NOT EXISTS idx_events_sync ON events(sync_status, timestamp)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_sync ON tasks(sync_status, started_at)`,
];

// ---------------------------------------------------------------------------
// EventBuffer
// ---------------------------------------------------------------------------

/**
 * SQLite-backed buffer that persists events and tasks across process restarts.
 *
 * Schema is identical to the Python SDK so both SDKs can share a database file.
 * Costs are stored as TEXT strings to avoid floating-point precision loss.
 */
export class EventBuffer {
  // null when better-sqlite3 is unavailable; every method short-circuits.
  private _db: Database.Database | null;

  /**
   * Test-only seam. When `true`, the constructor takes the no-binding
   * fallback path without attempting the require — used by
   * tests/runtime-fallback.test.ts to simulate Vercel Edge / Cloudflare
   * Workers behaviour without touching the real native module. Do NOT
   * set this in production code. Sprint 1 Theme B / §2.2.3 (B8).
   */
  static _forceFallbackForTest = false;

  constructor(dbPath?: string) {
    // Sprint 1 Theme B / §2.2.3 (B8): try to load better-sqlite3
    // dynamically. If the native binding is absent or fails to load
    // (Vercel Edge, Cloudflare Workers, Bun without bindings), fall
    // back to a no-op buffer so init() doesn't crash the customer app.
    // Events recorded in this mode are silently dropped.
    let DatabaseCtor: typeof Database | null = null;
    if (EventBuffer._forceFallbackForTest) {
      this._db = null;
      console.warn(
        "dexcost: EventBuffer._forceFallbackForTest is set — using no-op buffer",
      );
      return;
    }
    try {
      const require = createRequire(import.meta.url);
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      DatabaseCtor = require("better-sqlite3") as typeof Database;
    } catch (err) {
      console.warn(
        "dexcost: better-sqlite3 not available in this runtime; events " +
          "will not be persisted locally. Install better-sqlite3 as a " +
          "peer dependency for durable buffering. Cause: " +
          (err instanceof Error ? err.message : String(err)),
      );
      this._db = null;
      return;
    }

    const resolvedPath = dbPath ?? join(homedir(), ".dexcost", "buffer.db");
    try {
      mkdirSync(dirname(resolvedPath), { recursive: true });
    } catch (err) {
      throw new Error(`Cannot create dexcost storage directory: ${err instanceof Error ? err.message : err}`);
    }

    this._db = new DatabaseCtor(resolvedPath);

    // PRAGMAs and DDL
    try {
      this._db.pragma("journal_mode=WAL");
      this._db.pragma("synchronous=NORMAL");
      this._db.pragma("foreign_keys=ON");

      this._db.exec(CREATE_TASKS);
      this._db.exec(CREATE_EVENTS);
      this._db.exec(CREATE_SCHEMA_VERSION);
      // Migrate older databases that pre-date the tasks.sync_status column.
      // CREATE TABLE IF NOT EXISTS won't add columns to an existing table,
      // so add it explicitly; ignore the "duplicate column" error when the
      // column already exists.
      this._migrateAddColumn("tasks", "sync_status", "TEXT NOT NULL DEFAULT 'pending'");
      for (const idx of INDEXES) {
        this._db.exec(idx);
      }
    } catch (err) {
      throw new Error(`Cannot initialize dexcost database: ${err instanceof Error ? err.message : err}`);
    }

    // Seed schema_version if empty
    const versionCount = (
      this._db.prepare("SELECT COUNT(*) AS count FROM schema_version").get() as CountRow
    ).count;
    if (versionCount === 0) {
      try {
        this._db
          .prepare(
            `INSERT INTO schema_version (version_number, migration_name)
             VALUES (1, 'initial')`
          )
          .run();
      } catch {
        // SQLite error — skip seeding, don't crash
      }
    }
  }

  /**
   * Add `column` to `table` if it does not already exist.
   *
   * SQLite has no `ADD COLUMN IF NOT EXISTS`, so a duplicate-column error
   * is the expected signal that the migration has already been applied and
   * is swallowed. Any other failure is also tolerated so init never crashes.
   */
  private _migrateAddColumn(table: string, column: string, definition: string): void {
    if (!this._db) return;
    try {
      this._db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
    } catch {
      // Column already exists (duplicate column name) or other benign error.
    }
  }

  /**
   * Add a cost event to the buffer with sync_status = 'pending'.
   */
  addEvent(event: CostEvent): void {
    if (!this._db) return;
    try {
      this._db
        .prepare(
          `INSERT INTO events (
            event_id, task_id, event_type, provider, model,
            input_tokens, output_tokens, cached_tokens, service_name,
            cost_usd, latency_ms, cost_confidence, pricing_source, pricing_version,
            is_retry, retry_reason, retry_of, details, timestamp, sync_status
          ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, 'pending'
          )`
        )
        .run(
          event.eventId,
          event.taskId,
          event.eventType,
          event.provider ?? null,
          event.model ?? null,
          event.inputTokens ?? null,
          event.outputTokens ?? null,
          event.cachedTokens ?? null,
          event.serviceName ?? null,
          event.costUsd.toString(),
          event.latencyMs ?? null,
          event.costConfidence,
          event.pricingSource ?? null,
          event.pricingVersion ?? null,
          event.isRetry ? 1 : 0,
          event.retryReason ?? null,
          event.retryOf ?? null,
          JSON.stringify(event.details),
          event.occurredAt.toISOString()
        );
    } catch {
      // SQLite error (disk full, locked) — skip this event, don't crash
    }
  }

  /**
   * Insert or replace a task in the buffer.
   *
   * The task is (re)marked `sync_status = 'pending'`: an upsert means the
   * task's data changed (new cost rolled up, status flipped, etc.), so it
   * must be re-sent on the next push. `markTasksSynced` flips it to
   * `'synced'` after a successful POST so unchanged tasks are not re-sent.
   */
  upsertTask(task: Task): void {
    if (!this._db) return;
    try {
      this._db
        .prepare(
          `INSERT OR REPLACE INTO tasks (
            task_id, task_type, status, started_at, ended_at, metadata,
            llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
            total_input_tokens, total_output_tokens, total_cached_tokens,
            retry_count, retry_cost_usd, failure_count,
            customer_id, project_id, parent_task_id, experiment_id, variant,
            sync_status
          ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?, ?,
            'pending'
          )`
        )
        .run(
          task.taskId,
          task.taskType,
          task.status,
          task.startedAt.toISOString(),
          task.endedAt ? task.endedAt.toISOString() : null,
          JSON.stringify(task.metadata),
          task.llmCostUsd.toString(),
          task.externalCostUsd.toString(),
          task.computeCostUsd.toString(),
          task.totalCostUsd.toString(),
          task.totalInputTokens,
          task.totalOutputTokens,
          task.totalCachedTokens,
          task.retryCount,
          task.retryCostUsd.toString(),
          task.failureCount,
          task.customerId ?? null,
          task.projectId ?? null,
          task.parentTaskId ?? null,
          task.experimentId ?? null,
          task.variant ?? null
        );
    } catch {
      // SQLite error (disk full, locked) — skip this upsert, don't crash
    }
  }

  /**
   * Return up to `limit` pending events, ordered by timestamp ASC.
   */
  getPendingEvents(limit: number = 100): CostEvent[] {
    if (!this._db) return [];
    const rows = this._db
      .prepare(
        `SELECT * FROM events WHERE sync_status = 'pending' ORDER BY timestamp ASC LIMIT ?`
      )
      .all(limit) as EventRow[];
    return rows.map(rowToEvent);
  }

  /**
   * Mark the given event IDs as synced.
   */
  markSynced(eventIds: string[]): void {
    if (!this._db) return;
    if (eventIds.length === 0) return;
    try {
      const placeholders = eventIds.map(() => "?").join(", ");
      this._db
        .prepare(`UPDATE events SET sync_status = 'synced' WHERE event_id IN (${placeholders})`)
        .run(...eventIds);
    } catch {
      // SQLite error (disk full, locked) — skip marking, don't crash
    }
  }

  /**
   * Retrieve a task by ID, or undefined if not found.
   */
  getTask(taskId: string): Task | undefined {
    if (!this._db) return undefined;
    const row = this._db
      .prepare("SELECT * FROM tasks WHERE task_id = ?")
      .get(taskId) as TaskRow | undefined;
    return row != null ? rowToTask(row) : undefined;
  }

  /**
   * Return all tasks in the buffer.
   */
  getAllTasks(): Task[] {
    if (!this._db) return [];
    const rows = this._db.prepare("SELECT * FROM tasks").all() as TaskRow[];
    return rows.map(rowToTask);
  }

  /**
   * Return all tasks awaiting sync (`sync_status = 'pending'`).
   *
   * The pusher sends only these so unchanged tasks are not re-POSTed on
   * every push cycle.
   */
  getPendingTasks(): Task[] {
    if (!this._db) return [];
    const rows = this._db
      .prepare("SELECT * FROM tasks WHERE sync_status = 'pending'")
      .all() as TaskRow[];
    return rows.map(rowToTask);
  }

  /**
   * Mark the given task IDs as synced.
   *
   * Called by the pusher after a successful POST so the tasks are excluded
   * from subsequent pushes until they are upserted again.
   */
  markTasksSynced(taskIds: string[]): void {
    if (!this._db) return;
    if (taskIds.length === 0) return;
    try {
      const placeholders = taskIds.map(() => "?").join(", ");
      this._db
        .prepare(`UPDATE tasks SET sync_status = 'synced' WHERE task_id IN (${placeholders})`)
        .run(...taskIds);
    } catch {
      // SQLite error (disk full, locked) — skip marking, don't crash
    }
  }

  /** The number of tasks awaiting sync (`sync_status = 'pending'`). */
  get pendingTaskCount(): number {
    if (!this._db) return 0;
    const row = this._db
      .prepare("SELECT COUNT(*) AS count FROM tasks WHERE sync_status = 'pending'")
      .get() as CountRow;
    return row.count;
  }

  /**
   * Return all events in the buffer (including synced).
   */
  getAllEvents(): CostEvent[] {
    if (!this._db) return [];
    const rows = this._db.prepare("SELECT * FROM events").all() as EventRow[];
    return rows.map(rowToEvent);
  }

  /**
   * Return events for a specific task, ordered by timestamp DESC.
   */
  queryEvents(taskId: string): CostEvent[] {
    if (!this._db) return [];
    const rows = this._db
      .prepare("SELECT * FROM events WHERE task_id = ? ORDER BY timestamp DESC")
      .all(taskId) as EventRow[];
    return rows.map(rowToEvent);
  }

  /**
   * Update all columns of an existing event in-place.
   */
  updateEvent(event: CostEvent): void {
    if (!this._db) return;
    try {
      this._db
        .prepare(
          `UPDATE events SET
            task_id = ?,
            event_type = ?,
            provider = ?,
            model = ?,
            input_tokens = ?,
            output_tokens = ?,
            cached_tokens = ?,
            service_name = ?,
            cost_usd = ?,
            latency_ms = ?,
            cost_confidence = ?,
            pricing_source = ?,
            pricing_version = ?,
            is_retry = ?,
            retry_reason = ?,
            retry_of = ?,
            details = ?,
            timestamp = ?
          WHERE event_id = ?`
        )
        .run(
          event.taskId,
          event.eventType,
          event.provider ?? null,
          event.model ?? null,
          event.inputTokens ?? null,
          event.outputTokens ?? null,
          event.cachedTokens ?? null,
          event.serviceName ?? null,
          event.costUsd.toString(),
          event.latencyMs ?? null,
          event.costConfidence,
          event.pricingSource ?? null,
          event.pricingVersion ?? null,
          event.isRetry ? 1 : 0,
          event.retryReason ?? null,
          event.retryOf ?? null,
          JSON.stringify(event.details),
          event.occurredAt.toISOString(),
          event.eventId
        );
    } catch {
      // SQLite error (disk full, locked) — skip this update, don't crash
    }
  }

  /**
   * Return the number of pending (unsynced) events.
   */
  get pendingCount(): number {
    if (!this._db) return 0;
    const row = this._db
      .prepare("SELECT COUNT(*) AS count FROM events WHERE sync_status = 'pending'")
      .get() as CountRow;
    return row.count;
  }

  /**
   * Delete synced events older than `retentionHours` and VACUUM.
   *
   * Returns the number of deleted rows.
   */
  purgeSynced(retentionHours: number = 48): number {
    if (!this._db) return 0;
    try {
      const cutoff = new Date(Date.now() - retentionHours * 3_600_000).toISOString();
      const result = this._db
        .prepare(
          `DELETE FROM events WHERE sync_status = 'synced' AND timestamp < ?`
        )
        .run(cutoff);
      const deleted = result.changes;
      if (deleted > 0) {
        this._db.pragma("wal_checkpoint(TRUNCATE)");
        this._db.exec("VACUUM");
      }
      return deleted;
    } catch {
      // SQLite error — skip purge, don't crash
      return 0;
    }
  }

  /**
   * Delete pending events older than `maxAgeDays` and VACUUM.
   *
   * Safety net for events that can never be synced (rejected API key,
   * permanently-down endpoint, etc.) so the local buffer cannot grow
   * unbounded. Mirrors the Python SDK's `purge_old_pending` (default 7
   * days). Returns the number of deleted rows.
   */
  purgeOldPending(maxAgeDays: number = 7): number {
    if (!this._db) return 0;
    try {
      const cutoff = new Date(Date.now() - maxAgeDays * 86_400_000).toISOString();
      const result = this._db
        .prepare(`DELETE FROM events WHERE sync_status = 'pending' AND timestamp < ?`)
        .run(cutoff);
      const deleted = result.changes;
      if (deleted > 0) {
        this._db.pragma("wal_checkpoint(TRUNCATE)");
        this._db.exec("VACUUM");
      }
      return deleted;
    } catch {
      // SQLite error — skip purge, don't crash
      return 0;
    }
  }

  /**
   * Close the underlying database connection.
   */
  close(): void {
    if (!this._db) return;
    this._db.close();
  }
}
