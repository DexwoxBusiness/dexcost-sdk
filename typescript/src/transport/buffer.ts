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
import { isBun, isDeno } from "../core/runtime.js";
import { loadBunSqliteCompat } from "./bun-sqlite.js";
import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { Decimal, eventToDict } from "../core/models.js";
import type { CostEvent, Task } from "../core/models.js";
import { equivalentIdempotentEvent } from "../core/idempotency.js";
import { OutcomeRevision, RevenueRevision } from "../core/business.js";
import { providerJobFromDict, type ProviderJobRevision } from "../core/provider-jobs.js";
import { applyEventPricingProvenance } from "../pricing/explain.js";

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
  root_task_id: string | null;
  agent_id: string | null;
  agent_version: string | null;
  workflow_id: string | null;
  workflow_session_id: string | null;
  user_id: string | null;
  product_id: string | null;
  experiment_id: string | null;
  variant: string | null;
  network_bytes_in: number | null;
  network_bytes_out: number | null;
  network_call_count: number | null;
  network_by_host: string | null;
  network_cost_usd: string | null;
  gpu_cost_usd: string | null;
  sync_status: string;
}

interface CountRow {
  count: number;
}

interface LedgerRow {
  kind: "outcome" | "revenue" | "provider_job";
  entity_id: string;
  revision: number;
  task_id: string;
  provider: string | null;
  service: string | null;
  record_id: string | null;
  payload: string;
  sync_status: string;
  captured_at: string;
}

export interface DeliveryCounts {
  pendingEvents: number;
  quarantinedEvents: number;
  pendingTasks: number;
  pendingOutcomes: number;
  quarantinedOutcomes: number;
  pendingRevenues: number;
  quarantinedRevenues: number;
  pendingProviderJobs: number;
  quarantinedProviderJobs: number;
  oldestPendingAt?: Date;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Re-hydrate a cost column (TEXT, canonical decimal string) into an exact
 * `Decimal`. Falls back to `Decimal(0)` for null / malformed values so a
 * corrupt row never crashes the buffer read path.
 */
function _rowDecimal(value: string | number | null | undefined): Decimal {
  if (value == null) return new Decimal(0);
  try {
    return new Decimal(typeof value === "number" ? String(value) : value);
  } catch {
    return new Decimal(0);
  }
}

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
    costUsd: _rowDecimal(row.cost_usd),
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
    llmCostUsd: _rowDecimal(row.llm_cost_usd),
    externalCostUsd: _rowDecimal(row.external_cost_usd),
    computeCostUsd: _rowDecimal(row.compute_cost_usd),
    totalCostUsd: _rowDecimal(row.total_cost_usd),
    totalInputTokens: row.total_input_tokens ?? 0,
    totalOutputTokens: row.total_output_tokens ?? 0,
    totalCachedTokens: row.total_cached_tokens ?? 0,
    retryCount: row.retry_count ?? 0,
    retryCostUsd: _rowDecimal(row.retry_cost_usd),
    failureCount: row.failure_count ?? 0,
    customerId: row.customer_id ?? undefined,
    projectId: row.project_id ?? undefined,
    parentTaskId: row.parent_task_id ?? undefined,
    rootTaskId: row.root_task_id ?? undefined,
    agentId: row.agent_id ?? undefined,
    agentVersion: row.agent_version ?? undefined,
    workflowId: row.workflow_id ?? undefined,
    workflowSessionId: row.workflow_session_id ?? undefined,
    userId: row.user_id ?? undefined,
    productId: row.product_id ?? undefined,
    experimentId: row.experiment_id ?? undefined,
    variant: row.variant ?? undefined,
    // Network + GPU capture fields. Persisted since the network-columns
    // migration below; legacy rows (columns backfilled as NULL) read back
    // as fresh zero, matching Python's from_dict defaults. Pre-fix these
    // were never persisted at all, so every task read back from SQLite —
    // including the pusher's outbound payloads — carried
    // network_cost_usd = 0 and zero byte aggregates even when egress had
    // been computed at finalize.
    networkBytesIn: row.network_bytes_in ?? 0,
    networkBytesOut: row.network_bytes_out ?? 0,
    networkCallCount: row.network_call_count ?? 0,
    networkByHost: (() => {
      try {
        return row.network_by_host != null
          ? (JSON.parse(row.network_by_host) as Record<string, unknown>)
          : { hosts: [] };
      } catch {
        return { hosts: [] };
      }
    })(),
    networkCostUsd: _rowDecimal(row.network_cost_usd),
    gpuCostUsd: _rowDecimal(row.gpu_cost_usd),
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
    root_task_id        TEXT,
    agent_id            TEXT,
    agent_version       TEXT,
    workflow_id         TEXT,
    workflow_session_id TEXT,
    user_id             TEXT,
    product_id          TEXT,
    experiment_id       TEXT,
    variant             TEXT,
    network_bytes_in    INTEGER DEFAULT 0,
    network_bytes_out   INTEGER DEFAULT 0,
    network_call_count  INTEGER DEFAULT 0,
    network_by_host     TEXT,
    network_cost_usd    TEXT DEFAULT '0',
    gpu_cost_usd        TEXT DEFAULT '0',
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

const CREATE_LEDGER_REVISIONS = `
CREATE TABLE IF NOT EXISTS ledger_revisions (
    kind          TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    task_id       TEXT NOT NULL,
    provider      TEXT,
    service       TEXT,
    record_id     TEXT,
    payload       TEXT NOT NULL,
    sync_status   TEXT NOT NULL DEFAULT 'pending',
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (kind, entity_id, revision)
)`;

const INDEXES = [
  `CREATE INDEX IF NOT EXISTS idx_tasks_customer ON tasks(customer_id, started_at)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type, started_at)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_period ON tasks(started_at)`,
  `CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)`,
  `CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp)`,
  `CREATE INDEX IF NOT EXISTS idx_events_sync ON events(sync_status, timestamp)`,
  `CREATE INDEX IF NOT EXISTS idx_tasks_sync ON tasks(sync_status, started_at)`,
  `CREATE INDEX IF NOT EXISTS idx_ledger_pending ON ledger_revisions(sync_status, captured_at)`,
  `CREATE INDEX IF NOT EXISTS idx_ledger_task ON ledger_revisions(kind, task_id, captured_at)`,
  `CREATE INDEX IF NOT EXISTS idx_provider_job_lookup ON ledger_revisions(kind, provider, service, record_id, revision)`,
];

// ---------------------------------------------------------------------------
// MemoryBufferStore — in-memory fallback when better-sqlite3 is unavailable
// (Vercel Edge, Cloudflare Workers, Bun without bindings).
//
// Sprint 1 Theme B / §2.2.3 (B8 follow-on). The audit-minimum no-op
// fallback (commit a6eb6db) kept customer apps alive but silently
// dropped events. This store provides durable in-memory buffering with
// a hard 10k-entry cap per kind (events, tasks) and FIFO eviction
// (Map iteration order = insertion order). Events still don't survive
// process restarts — that's the SQLite path's job — but they're now
// available to the sync pusher within the process lifetime.
// ---------------------------------------------------------------------------

const MEM_BUFFER_MAX_EVENTS = 10_000;
const MEM_BUFFER_MAX_TASKS = 10_000;

interface MemEventEntry {
  event: CostEvent;
  syncStatus: "pending" | "quarantined" | "synced";
  capturedAt: Date;
  syncedAt: Date | null;
}

interface MemTaskEntry {
  task: Task;
  syncStatus: "pending" | "synced";
  capturedAt: Date;
  syncedAt: Date | null;
}

class MemoryBufferStore {
  private _events = new Map<string, MemEventEntry>();
  private _tasks = new Map<string, MemTaskEntry>();

  addEvent(event: CostEvent): void {
    this._evict(this._events, MEM_BUFFER_MAX_EVENTS);
    // Clone to detach from caller mutations.
    this._events.set(event.eventId, {
      event: { ...event },
      syncStatus: "pending",
      capturedAt: new Date(),
      syncedAt: null,
    });
  }

  updateEvent(event: CostEvent): void {
    // Only update if entry exists — matches SQLite's UPDATE semantics
    // (no-op when no row matches).
    const existing = this._events.get(event.eventId);
    if (existing == null) return;
    existing.event = { ...event };
  }

  upsertTask(task: Task): void {
    const existing = this._tasks.get(task.taskId);
    if (existing != null) {
      existing.task = { ...task };
      return;
    }
    this._evict(this._tasks, MEM_BUFFER_MAX_TASKS);
    this._tasks.set(task.taskId, {
      task: { ...task },
      syncStatus: "pending",
      capturedAt: new Date(),
      syncedAt: null,
    });
  }

  getPendingEvents(limit: number): CostEvent[] {
    const out: CostEvent[] = [];
    for (const entry of this._events.values()) {
      if (entry.syncStatus !== "pending") continue;
      out.push(entry.event);
      if (out.length >= limit) break;
    }
    return out;
  }

  markSynced(eventIds: string[]): void {
    const now = new Date();
    for (const id of eventIds) {
      const entry = this._events.get(id);
      if (entry != null) {
        entry.syncStatus = "synced";
        entry.syncedAt = now;
      }
    }
  }

  markQuarantined(eventIds: string[]): void {
    for (const id of eventIds) {
      const entry = this._events.get(id);
      if (entry != null && entry.syncStatus === "pending") {
        entry.syncStatus = "quarantined";
      }
    }
  }

  getQuarantinedEvents(limit: number): CostEvent[] {
    const out: CostEvent[] = [];
    for (const entry of this._events.values()) {
      if (entry.syncStatus !== "quarantined") continue;
      out.push(entry.event);
      if (out.length >= limit) break;
    }
    return out;
  }

  requeueQuarantinedEvents(): number {
    let restored = 0;
    for (const entry of this._events.values()) {
      if (entry.syncStatus !== "quarantined") continue;
      entry.syncStatus = "pending";
      entry.syncedAt = null;
      restored += 1;
    }
    return restored;
  }

  getTask(taskId: string): Task | undefined {
    return this._tasks.get(taskId)?.task;
  }

  getAllTasks(): Task[] {
    return Array.from(this._tasks.values(), (e) => e.task);
  }

  getPendingTasks(): Task[] {
    const out: Task[] = [];
    for (const entry of this._tasks.values()) {
      if (entry.syncStatus === "pending") out.push(entry.task);
    }
    return out;
  }

  markTasksSynced(taskIds: string[]): void {
    const now = new Date();
    for (const id of taskIds) {
      const entry = this._tasks.get(id);
      if (entry != null) {
        entry.syncStatus = "synced";
        entry.syncedAt = now;
      }
    }
  }

  get pendingTaskCount(): number {
    let n = 0;
    for (const e of this._tasks.values()) if (e.syncStatus === "pending") n += 1;
    return n;
  }

  get pendingCount(): number {
    let n = 0;
    for (const e of this._events.values()) if (e.syncStatus === "pending") n += 1;
    return n;
  }

  getAllEvents(): CostEvent[] {
    return Array.from(this._events.values(), (e) => e.event);
  }

  getEvent(eventId: string): CostEvent | undefined {
    return this._events.get(eventId)?.event;
  }

  deliveryCounts(): Pick<DeliveryCounts,
    "pendingEvents" | "quarantinedEvents" | "pendingTasks" | "oldestPendingAt"> {
    let pendingEvents = 0; let quarantinedEvents = 0; let pendingTasks = 0;
    const pendingDates: Date[] = [];
    for (const item of this._events.values()) {
      if (item.syncStatus === "pending") { pendingEvents++; pendingDates.push(item.capturedAt); }
      else if (item.syncStatus === "quarantined") quarantinedEvents++;
    }
    for (const item of this._tasks.values()) {
      if (item.syncStatus === "pending") { pendingTasks++; pendingDates.push(item.capturedAt); }
    }
    const oldestPendingAt = pendingDates.sort((a, b) => a.getTime() - b.getTime())[0];
    return { pendingEvents, quarantinedEvents, pendingTasks, oldestPendingAt };
  }

  queryEvents(taskId: string): CostEvent[] {
    const out: CostEvent[] = [];
    for (const entry of this._events.values()) {
      if (entry.event.taskId === taskId) out.push(entry.event);
    }
    return out;
  }

  purgeSynced(retentionHours: number): number {
    const cutoff = Date.now() - retentionHours * 3600 * 1000;
    let removed = 0;
    for (const [id, entry] of this._events) {
      if (entry.syncStatus === "synced" && entry.syncedAt != null &&
          entry.syncedAt.getTime() < cutoff) {
        this._events.delete(id);
        removed += 1;
      }
    }
    return removed;
  }

  purgeOldPending(maxAgeDays: number): number {
    const cutoff = Date.now() - maxAgeDays * 24 * 3600 * 1000;
    let removed = 0;
    for (const [id, entry] of this._events) {
      if ((entry.syncStatus === "pending" || entry.syncStatus === "quarantined") &&
          entry.capturedAt.getTime() < cutoff) {
        this._events.delete(id);
        removed += 1;
      }
    }
    return removed;
  }

  close(): void {
    this._events.clear();
    this._tasks.clear();
  }

  // Test-only: total entry counts (used by buffer regression tests to
  // exercise the FIFO eviction cap without going through every getter).
  _eventCount(): number { return this._events.size; }
  _taskCount(): number { return this._tasks.size; }

  private _evict<V>(map: Map<string, V>, max: number): void {
    while (map.size >= max) {
      // Map iteration order = insertion order, so first key is oldest.
      const oldestKey = map.keys().next().value;
      if (oldestKey === undefined) break;
      map.delete(oldestKey);
    }
  }
}

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
  // null when better-sqlite3 is unavailable; in that case `_mem` holds
  // the in-memory fallback store and every method delegates to it.
  private _db: Database.Database | null;
  private _mem: MemoryBufferStore | null = null;
  private _ledgerMemory = new Map<string, LedgerRow>();

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
    // back to a Map-based in-memory buffer with a 10k-entry cap so
    // init() doesn't crash the customer app and events are still
    // available to the sync pusher within the process lifetime.
    let DatabaseCtor: typeof Database | null = null;
    if (EventBuffer._forceFallbackForTest) {
      this._db = null;
      this._mem = new MemoryBufferStore();
      console.warn(
        "dexcost: EventBuffer._forceFallbackForTest is set — using in-memory buffer (events do not survive process restart)",
      );
      return;
    }
    if (isBun()) {
      // better-sqlite3's native binding is unsupported on Bun (bun#4290),
      // but Bun ships a built-in SQLite driver — use it for durable
      // buffering instead of degrading to memory.
      const BunCompat = loadBunSqliteCompat();
      if (BunCompat) {
        DatabaseCtor = BunCompat as unknown as typeof Database;
      }
      // If bun:sqlite is somehow unavailable, fall through to the
      // better-sqlite3 attempt below (whose construction-time failure is
      // catchable on Bun and degrades to the memory buffer).
    }

    if (isDeno()) {
      // Deno's dlopen of a non-NAPI (V8-ABI) native addon is a FATAL
      // process-level symbol-lookup error — it kills the process before
      // any JS catch can run. better-sqlite3 is such an addon, so on Deno
      // we must not even attempt to load it.
      console.warn(
        "dexcost: running on Deno — better-sqlite3 (a V8-ABI native addon) " +
          "cannot be loaded here; cost tracking continues on an in-memory " +
          "buffer (events are NOT persisted across process restarts; hard " +
          "cap 10k entries).",
      );
      this._db = null;
      this._mem = new MemoryBufferStore();
      return;
    }
    if (DatabaseCtor === null) {
      try {
        const require = createRequire(import.meta.url);
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        DatabaseCtor = require("better-sqlite3") as typeof Database;
      } catch (err) {
        const cause = err instanceof Error ? err.message : String(err);
        // `better-sqlite3` is a native module — it must be compiled for the
        // running Node version/platform. Two distinct failure modes land here:
        //   1. Not installed at all (optional dependency skipped).
        //   2. Installed but the native .node binding is missing/mismatched
        //      ("Could not locate the bindings file"), common in Docker
        //      multi-stage builds where the postinstall compile step was
        //      skipped or run against a different Node ABI.
        // Either way the SDK stays alive on the in-memory fallback; the message
        // tells the user exactly how to restore durable buffering.
        const bindingsIssue = cause.includes("bindings") || cause.includes(".node");
        const remedy = bindingsIssue
        ? "Rebuild the native binding with `npm rebuild better-sqlite3` " +
          "(ensure python3, make and a C++ compiler are available during install — " +
          "in Docker, run the rebuild in the same stage that runs your app)."
        : "Install it for durable buffering: `npm install better-sqlite3` " +
          "(requires python3, make and a C++ compiler to build the native binding).";
        console.warn(
        "dexcost: better-sqlite3 is unavailable — cost tracking continues on an " +
          "in-memory buffer (events are NOT persisted across process restarts; " +
          "hard cap 10k entries). " +
          remedy +
          " Cause: " +
          cause,
        );
        this._db = null;
        this._mem = new MemoryBufferStore();
        return;
      }
    }

    const resolvedPath = dbPath ?? join(homedir(), ".dexcost", "buffer.db");
    try {
      mkdirSync(dirname(resolvedPath), { recursive: true });
    } catch (err) {
      throw new Error(`Cannot create dexcost storage directory: ${err instanceof Error ? err.message : err}`);
    }

    try {
      this._db = new DatabaseCtor(resolvedPath);
    } catch (err) {
      // require("better-sqlite3") can SUCCEED while opening the database
      // still fails: Bun loads the JS wrapper but rejects the native
      // binding at dlopen time (ERR_DLOPEN_FAILED, bun#4290), and a
      // corrupt/locked db file lands here too. Pre-fix this crashed the
      // customer app inside init(); it must degrade to the in-memory
      // fallback exactly like a failed require.
      const cause = err instanceof Error ? err.message.split("\n")[0] : String(err);
      console.warn(
        "dexcost: better-sqlite3 loaded but the database could not be opened — " +
          "cost tracking continues on an in-memory buffer (events are NOT " +
          "persisted across process restarts; hard cap 10k entries). " +
          "On Bun, better-sqlite3's native binding is unsupported (bun#4290) " +
          "and this fallback is expected. Cause: " +
          cause,
      );
      this._db = null;
      this._mem = new MemoryBufferStore();
      return;
    }

    // PRAGMAs and DDL
    try {
      this._db.pragma("journal_mode=WAL");
      this._db.pragma("synchronous=NORMAL");
      this._db.pragma("foreign_keys=ON");

      this._db.exec(CREATE_TASKS);
      this._db.exec(CREATE_EVENTS);
      this._db.exec(CREATE_SCHEMA_VERSION);
      this._db.exec(CREATE_LEDGER_REVISIONS);
      // Migrate older databases that pre-date the tasks.sync_status column.
      // CREATE TABLE IF NOT EXISTS won't add columns to an existing table,
      // so add it explicitly; ignore the "duplicate column" error when the
      // column already exists.
      this._migrateAddColumn("tasks", "sync_status", "TEXT NOT NULL DEFAULT 'pending'");
      // Network + GPU task dimensions (previously computed in memory but
      // never persisted — read-back and pushed payloads showed $0).
      this._migrateAddColumn("tasks", "network_bytes_in", "INTEGER DEFAULT 0");
      this._migrateAddColumn("tasks", "network_bytes_out", "INTEGER DEFAULT 0");
      this._migrateAddColumn("tasks", "network_call_count", "INTEGER DEFAULT 0");
      this._migrateAddColumn("tasks", "network_by_host", "TEXT");
      this._migrateAddColumn("tasks", "network_cost_usd", "TEXT DEFAULT '0'");
      this._migrateAddColumn("tasks", "gpu_cost_usd", "TEXT DEFAULT '0'");
      this._migrateAddColumn("tasks", "root_task_id", "TEXT");
      this._migrateAddColumn("tasks", "agent_id", "TEXT");
      this._migrateAddColumn("tasks", "agent_version", "TEXT");
      this._migrateAddColumn("tasks", "workflow_id", "TEXT");
      this._migrateAddColumn("tasks", "workflow_session_id", "TEXT");
      this._migrateAddColumn("tasks", "user_id", "TEXT");
      this._migrateAddColumn("tasks", "product_id", "TEXT");
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
  addEvent(event: CostEvent): boolean {
    applyEventPricingProvenance(event);
    const existing = this.getEvent(event.eventId);
    if (existing !== undefined) {
      const exactlyEqual = JSON.stringify(eventToDict(existing)) === JSON.stringify(eventToDict(event));
      if (!exactlyEqual && !equivalentIdempotentEvent(existing, event)) {
        throw new Error(`event ${event.eventId} already exists with different economic facts`);
      }
      event.occurredAt = existing.occurredAt;
      return false;
    }
    if (this._mem) { this._mem.addEvent(event); return true; }
    if (!this._db) return false;
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
      return true;
    } catch {
      // SQLite error (disk full, locked) — skip this event, don't crash
      return false;
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
    if (this._mem) { this._mem.upsertTask(task); return; }
    if (!this._db) return;
    try {
      this._db
        .prepare(
          `INSERT OR REPLACE INTO tasks (
            task_id, task_type, status, started_at, ended_at, metadata,
            llm_cost_usd, external_cost_usd, compute_cost_usd, total_cost_usd,
            total_input_tokens, total_output_tokens, total_cached_tokens,
            retry_count, retry_cost_usd, failure_count,
            customer_id, project_id, parent_task_id, root_task_id,
            agent_id, agent_version, workflow_id, workflow_session_id,
            user_id, product_id, experiment_id, variant,
            network_bytes_in, network_bytes_out, network_call_count,
            network_by_host, network_cost_usd, gpu_cost_usd,
            sync_status
          ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
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
          task.rootTaskId ?? null,
          task.agentId ?? null,
          task.agentVersion ?? null,
          task.workflowId ?? null,
          task.workflowSessionId ?? null,
          task.userId ?? null,
          task.productId ?? null,
          task.experimentId ?? null,
          task.variant ?? null,
          task.networkBytesIn,
          task.networkBytesOut,
          task.networkCallCount,
          JSON.stringify(task.networkByHost ?? { hosts: [] }),
          task.networkCostUsd.toString(),
          task.gpuCostUsd.toString()
        );
    } catch {
      // SQLite error (disk full, locked) — skip this upsert, don't crash
    }
  }

  /**
   * Return up to `limit` pending events, ordered by timestamp ASC.
   */
  getPendingEvents(limit: number = 100): CostEvent[] {
    if (this._mem) return this._mem.getPendingEvents(limit);
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
    if (this._mem) { this._mem.markSynced(eventIds); return; }
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
   * Retain unrepresentable events outside the normal pending scan.
   *
   * Quarantine is deliberately distinct from `synced`: the event was not
   * delivered, remains inspectable through `queryEvents`/`getQuarantinedEvents`,
   * and is removed only by the same age-based safety purge as stale pending
   * records. Keeping it out of `getPendingEvents` prevents head-of-line
   * blocking without pretending ingestion succeeded.
   */
  markQuarantined(eventIds: string[]): void {
    if (this._mem) { this._mem.markQuarantined(eventIds); return; }
    if (!this._db || eventIds.length === 0) return;
    try {
      const placeholders = eventIds.map(() => "?").join(", ");
      this._db
        .prepare(
          `UPDATE events SET sync_status = 'quarantined'
           WHERE sync_status = 'pending' AND event_id IN (${placeholders})`,
        )
        .run(...eventIds);
    } catch {
      // SQLite error (disk full, locked) — keep rows pending and retry later.
    }
  }

  /** Return quarantined conversion failures for diagnostics. */
  getQuarantinedEvents(limit: number = 100): CostEvent[] {
    if (this._mem) return this._mem.getQuarantinedEvents(limit);
    if (!this._db) return [];
    const rows = this._db
      .prepare(
        `SELECT * FROM events WHERE sync_status = 'quarantined' ORDER BY timestamp ASC LIMIT ?`,
      )
      .all(limit) as EventRow[];
    return rows.map(rowToEvent);
  }

  /** Requeue retained converter failures after an SDK upgrade. */
  requeueQuarantinedEvents(): number {
    if (this._mem) return this._mem.requeueQuarantinedEvents();
    if (!this._db) return 0;
    try {
      return this._db
        .prepare("UPDATE events SET sync_status = 'pending' WHERE sync_status = 'quarantined'")
        .run().changes;
    } catch {
      return 0;
    }
  }

  /**
   * Retrieve a task by ID, or undefined if not found.
   */
  getTask(taskId: string): Task | undefined {
    if (this._mem) return this._mem.getTask(taskId);
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
    if (this._mem) return this._mem.getAllTasks();
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
    if (this._mem) return this._mem.getPendingTasks();
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
    if (this._mem) { this._mem.markTasksSynced(taskIds); return; }
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
    if (this._mem) return this._mem.pendingTaskCount;
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
    if (this._mem) return this._mem.getAllEvents();
    if (!this._db) return [];
    const rows = this._db.prepare("SELECT * FROM events").all() as EventRow[];
    return rows.map(rowToEvent);
  }

  /**
   * Return events for a specific task, ordered by timestamp DESC.
   */
  queryEvents(taskId: string): CostEvent[] {
    if (this._mem) return this._mem.queryEvents(taskId);
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
    if (this._mem) { this._mem.updateEvent(event); return; }
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
    if (this._mem) return this._mem.pendingCount;
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
    if (this._mem) return this._mem.purgeSynced(retentionHours);
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
   * Explicitly delete pending or quarantined events older than `maxAgeDays`.
   * Background delivery never calls this destructive maintenance operation:
   * financial attribution waiting on credentials, transport recovery, or a
   * converter upgrade remains durable until the operator chooses otherwise.
   */
  purgeOldPending(maxAgeDays: number = 7): number {
    if (this._mem) return this._mem.purgeOldPending(maxAgeDays);
    if (!this._db) return 0;
    try {
      const cutoff = new Date(Date.now() - maxAgeDays * 86_400_000).toISOString();
      const result = this._db
        .prepare(
          `DELETE FROM events
           WHERE sync_status IN ('pending', 'quarantined') AND timestamp < ?`,
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

  getEvent(eventId: string): CostEvent | undefined {
    if (this._mem) return this._mem.getEvent(eventId);
    if (!this._db) return undefined;
    const row = this._db.prepare("SELECT * FROM events WHERE event_id = ?").get(eventId) as EventRow | null | undefined;
    return row == null ? undefined : rowToEvent(row);
  }

  private _ledgerKey(kind: LedgerRow["kind"], entityId: string, revision: number): string {
    return `${kind}\0${entityId}\0${revision}`;
  }

  private _ledgerRows(kind?: LedgerRow["kind"], entityId?: string): LedgerRow[] {
    if (this._mem || !this._db) {
      return Array.from(this._ledgerMemory.values())
        .filter((row) => (kind === undefined || row.kind === kind) &&
          (entityId === undefined || row.entity_id === entityId))
        .sort((a, b) => a.revision - b.revision);
    }
    const clauses: string[] = [];
    const values: string[] = [];
    if (kind !== undefined) { clauses.push("kind = ?"); values.push(kind); }
    if (entityId !== undefined) { clauses.push("entity_id = ?"); values.push(entityId); }
    const where = clauses.length === 0 ? "" : ` WHERE ${clauses.join(" AND ")}`;
    return this._db.prepare(`SELECT * FROM ledger_revisions${where} ORDER BY revision ASC`)
      .all(...values) as LedgerRow[];
  }

  private _insertLedger(row: LedgerRow): boolean {
    const history = this._ledgerRows(row.kind, row.entity_id);
    const sameRevision = history.find((item) => item.revision === row.revision);
    if (sameRevision !== undefined) {
      if (sameRevision.payload === row.payload) return false;
      throw new Error(`${row.kind} revision already exists with different content`);
    }
    const latest = history.at(-1);
    const expected = latest === undefined ? 1 : latest.revision + 1;
    if (row.revision !== expected) {
      throw new Error(`${row.kind} expected revision ${expected}, got ${row.revision}`);
    }
    if (this._mem || !this._db) {
      this._ledgerMemory.set(this._ledgerKey(row.kind, row.entity_id, row.revision), row);
      return true;
    }
    this._db.prepare(
      `INSERT INTO ledger_revisions
       (kind, entity_id, revision, task_id, provider, service, record_id, payload, sync_status, captured_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)`,
    ).run(
      row.kind, row.entity_id, row.revision, row.task_id, row.provider,
      row.service, row.record_id, row.payload, row.captured_at,
    );
    return true;
  }

  insertOutcomeRevision(revision: OutcomeRevision): void {
    const row: LedgerRow = {
      kind: "outcome", entity_id: revision.outcomeId, revision: revision.revision,
      task_id: revision.taskId, provider: null, service: null, record_id: null,
      payload: JSON.stringify(revision.toDict()), sync_status: "pending",
      captured_at: revision.observedAt.toISOString(),
    };
    const history = this._ledgerRows("outcome", revision.outcomeId);
    if (history.some((item) => item.revision === revision.revision)) {
      this._insertLedger(row);
      return;
    }
    const previousRow = history.at(-1);
    if (previousRow !== undefined) {
      const previous = OutcomeRevision.fromDict(
        JSON.parse(previousRow.payload) as Record<string, unknown>,
      );
      if (previous.taskId !== revision.taskId || previous.name !== revision.name) {
        throw new Error("an outcome cannot change taskId or name across revisions");
      }
      const allowed: Record<string, ReadonlySet<string>> = {
        pending: new Set(["pending", "achieved", "missed", "voided"]),
        achieved: new Set(["achieved", "missed", "voided"]),
        missed: new Set(["missed", "achieved", "voided"]),
        voided: new Set(),
      };
      if (!allowed[previous.state]?.has(revision.state)) {
        throw new Error(`invalid outcome lifecycle transition ${previous.state} -> ${revision.state}`);
      }
    }
    this._insertLedger(row);
  }

  insertRevenueRevision(revision: RevenueRevision): void {
    const row: LedgerRow = {
      kind: "revenue", entity_id: revision.revenueId, revision: revision.revision,
      task_id: revision.taskId, provider: null, service: null, record_id: null,
      payload: JSON.stringify(revision.toDict()), sync_status: "pending",
      captured_at: revision.observedAt.toISOString(),
    };
    const history = this._ledgerRows("revenue", revision.revenueId);
    if (history.some((item) => item.revision === revision.revision)) {
      this._insertLedger(row);
      return;
    }
    const previousRow = history.at(-1);
    if (previousRow !== undefined) {
      const previous = RevenueRevision.fromDict(
        JSON.parse(previousRow.payload) as Record<string, unknown>,
      );
      const immutableChanged = previous.taskId !== revision.taskId
        || previous.outcomeId !== revision.outcomeId
        || previous.source.type !== revision.source.type
        || previous.source.recordId !== revision.source.recordId;
      if (immutableChanged) {
        throw new Error("revenue cannot change taskId, outcomeId, or source across revisions");
      }
      const allowed: Record<string, ReadonlySet<string>> = {
        pending: new Set(["pending", "provisional", "recognized", "voided"]),
        provisional: new Set(["provisional", "recognized", "voided"]),
        recognized: new Set(["recognized", "voided"]),
        voided: new Set(),
      };
      if (!allowed[previous.state]?.has(revision.state)) {
        throw new Error(`invalid revenue lifecycle transition ${previous.state} -> ${revision.state}`);
      }
      if (previous.amount !== undefined && revision.amount !== undefined &&
          previous.amount.currency !== revision.amount.currency) {
        throw new Error("revenue currency cannot change across revisions; void it and create a new revenueId");
      }
    }
    this._insertLedger(row);
  }

  insertProviderJobRevision(revision: ProviderJobRevision): void {
    const history = this._ledgerRows("provider_job", revision.eventId);
    const previousRow = history.at(-1);
    const previous = previousRow === undefined
      ? undefined
      : providerJobFromDict(JSON.parse(previousRow.payload) as Record<string, unknown>);
    if (previous !== undefined) {
      const immutableEqual = previous.taskId === revision.taskId
        && previous.provider === revision.provider
        && previous.service === revision.service
        && previous.providerRecordId === revision.providerRecordId
        && previous.operation === revision.operation
        && previous.component === revision.component
        && previous.eventType === revision.eventType
        && previous.resourceType === revision.resourceType
        && previous.resourceId === revision.resourceId
        && previous.submittedAt.getTime() === revision.submittedAt.getTime()
        && previous.ownsTask === revision.ownsTask
        && JSON.stringify(previous.billingDimensions) === JSON.stringify(revision.billingDimensions)
        && JSON.stringify(previous.capability) === JSON.stringify(revision.capability);
      if (!immutableEqual) {
        throw new Error("a provider job cannot change task, provider, resource, operation, or capability identity across revisions");
      }
      if (revision.observedAt.getTime() < previous.observedAt.getTime()) {
        throw new Error("provider job observedAt cannot move backwards across revisions");
      }
      if (previous.terminal && !revision.terminal) {
        throw new Error("a terminal provider job cannot return to pending");
      }
      if (previous.status === "running" && revision.status === "submitted") {
        throw new Error("a running provider job cannot return to submitted");
      }
    }
    const inserted = this._insertLedger({
      kind: "provider_job", entity_id: revision.eventId, revision: revision.revision,
      task_id: revision.taskId, provider: revision.provider, service: revision.service,
      record_id: revision.providerRecordId, payload: JSON.stringify(revision.toDict()),
      sync_status: "pending", captured_at: revision.observedAt.toISOString(),
    });
    if (!inserted || !revision.terminal) return;
    const task = this.getTask(revision.taskId);
    if (task === undefined) return;
    const previousCost = previous?.terminal ? previous.costAmount ?? new Decimal(0) : new Decimal(0);
    const nextCost = revision.costAmount ?? new Decimal(0);
    const costDelta = nextCost.minus(previousCost);
    if (revision.eventType === "llm_call") {
      task.llmCostUsd = task.llmCostUsd.plus(costDelta);
      task.totalInputTokens += (revision.taskInputTokens ?? 0) - (previous?.terminal ? previous.taskInputTokens ?? 0 : 0);
      task.totalOutputTokens += (revision.taskOutputTokens ?? 0) - (previous?.terminal ? previous.taskOutputTokens ?? 0 : 0);
      task.totalCachedTokens += (revision.taskCachedTokens ?? 0) - (previous?.terminal ? previous.taskCachedTokens ?? 0 : 0);
    } else if (revision.eventType === "external_cost") {
      task.externalCostUsd = task.externalCostUsd.plus(costDelta);
    } else {
      task.computeCostUsd = task.computeCostUsd.plus(costDelta);
    }
    task.totalCostUsd = task.totalCostUsd.plus(costDelta);
    if (revision.ownsTask) {
      task.status = revision.status === "succeeded" ? "success" : "failed";
      task.endedAt = revision.observedAt;
      task.failureCount = revision.status === "succeeded" ? 0 : 1;
    }
    this.upsertTask(task);
  }

  getProviderJobHistory(eventId: string): Array<Record<string, unknown>> {
    return this._ledgerRows("provider_job", eventId)
      .map((row) => JSON.parse(row.payload) as Record<string, unknown>);
  }

  getOutcomeHistory(outcomeId: string): Array<Record<string, unknown>> {
    return this._ledgerRows("outcome", outcomeId).map((row) => JSON.parse(row.payload) as Record<string, unknown>);
  }

  getRevenueHistory(revenueId: string): Array<Record<string, unknown>> {
    return this._ledgerRows("revenue", revenueId).map((row) => JSON.parse(row.payload) as Record<string, unknown>);
  }

  getProviderJob(
    provider: string, service: string, recordId: string,
  ): Record<string, unknown> | undefined {
    const rows = this._ledgerRows("provider_job")
      .filter((row) => row.provider === provider && row.service === service && row.record_id === recordId);
    const latest = rows.at(-1);
    return latest === undefined ? undefined : JSON.parse(latest.payload) as Record<string, unknown>;
  }

  getPendingLedger(kind?: LedgerRow["kind"], limit = 100): Array<Record<string, unknown>> {
    const rows = this._ledgerRows(kind)
      .filter((row) => row.sync_status === "pending")
      .sort((a, b) => a.captured_at.localeCompare(b.captured_at))
      .slice(0, limit);
    return rows.map((row) => JSON.parse(row.payload) as Record<string, unknown>);
  }

  markLedgerSynced(kind: LedgerRow["kind"], identities: ReadonlyArray<readonly [string, number]>): void {
    if (identities.length === 0) return;
    if (this._mem || !this._db) {
      for (const [entityId, revision] of identities) {
        const row = this._ledgerMemory.get(this._ledgerKey(kind, entityId, revision));
        if (row !== undefined) row.sync_status = "synced";
      }
      return;
    }
    const statement = this._db.prepare(
      "UPDATE ledger_revisions SET sync_status = 'synced' WHERE kind = ? AND entity_id = ? AND revision = ?",
    );
    const transaction = this._db.transaction((items: ReadonlyArray<readonly [string, number]>) => {
      for (const [entityId, revision] of items) statement.run(kind, entityId, revision);
    });
    transaction(identities);
  }

  markLedgerQuarantined(kind: LedgerRow["kind"], identities: ReadonlyArray<readonly [string, number]>): void {
    if (identities.length === 0) return;
    if (this._mem || !this._db) {
      for (const [entityId, revision] of identities) {
        const row = this._ledgerMemory.get(this._ledgerKey(kind, entityId, revision));
        if (row?.sync_status === "pending") row.sync_status = "quarantined";
      }
      return;
    }
    const statement = this._db.prepare(
      "UPDATE ledger_revisions SET sync_status = 'quarantined' WHERE kind = ? AND entity_id = ? AND revision = ? AND sync_status = 'pending'",
    );
    const transaction = this._db.transaction((items: ReadonlyArray<readonly [string, number]>) => {
      for (const [entityId, revision] of items) statement.run(kind, entityId, revision);
    });
    transaction(identities);
  }

  deliveryCounts(): DeliveryCounts {
    const empty = {
      pendingEvents: 0, quarantinedEvents: 0, pendingTasks: 0,
      pendingOutcomes: 0, quarantinedOutcomes: 0,
      pendingRevenues: 0, quarantinedRevenues: 0,
      pendingProviderJobs: 0, quarantinedProviderJobs: 0,
    };
    if (this._mem || !this._db) {
      const base = this._mem?.deliveryCounts() ?? {
        pendingEvents: 0, quarantinedEvents: 0, pendingTasks: 0, oldestPendingAt: undefined,
      };
      const counts: DeliveryCounts = { ...empty, ...base };
      const dates: Date[] = base.oldestPendingAt === undefined ? [] : [base.oldestPendingAt];
      for (const row of this._ledgerMemory.values()) {
        const pending = row.sync_status === "pending";
        const quarantined = row.sync_status === "quarantined";
        if (row.kind === "outcome") {
          if (pending) counts.pendingOutcomes++; else if (quarantined) counts.quarantinedOutcomes++;
        } else if (row.kind === "revenue") {
          if (pending) counts.pendingRevenues++; else if (quarantined) counts.quarantinedRevenues++;
        } else {
          if (pending) counts.pendingProviderJobs++; else if (quarantined) counts.quarantinedProviderJobs++;
        }
        if (pending) dates.push(new Date(row.captured_at));
      }
      const oldest = dates.filter((item) => Number.isFinite(item.getTime()))
        .sort((a, b) => a.getTime() - b.getTime())[0];
      return { ...counts, ...(oldest === undefined ? {} : { oldestPendingAt: oldest }) };
    }
    const row = this._db.prepare(`
      SELECT
        (SELECT COUNT(*) FROM events WHERE sync_status='pending') AS pending_events,
        (SELECT COUNT(*) FROM events WHERE sync_status='quarantined') AS quarantined_events,
        (SELECT COUNT(*) FROM tasks WHERE sync_status='pending') AS pending_tasks,
        (SELECT COUNT(*) FROM ledger_revisions WHERE kind='outcome' AND sync_status='pending') AS pending_outcomes,
        (SELECT COUNT(*) FROM ledger_revisions WHERE kind='outcome' AND sync_status='quarantined') AS quarantined_outcomes,
        (SELECT COUNT(*) FROM ledger_revisions WHERE kind='revenue' AND sync_status='pending') AS pending_revenues,
        (SELECT COUNT(*) FROM ledger_revisions WHERE kind='revenue' AND sync_status='quarantined') AS quarantined_revenues,
        (SELECT COUNT(*) FROM ledger_revisions WHERE kind='provider_job' AND sync_status='pending') AS pending_provider_jobs,
        (SELECT COUNT(*) FROM ledger_revisions WHERE kind='provider_job' AND sync_status='quarantined') AS quarantined_provider_jobs,
        (SELECT MIN(value) FROM (
          SELECT timestamp AS value FROM events WHERE sync_status='pending'
          UNION ALL SELECT started_at FROM tasks WHERE sync_status='pending'
          UNION ALL SELECT captured_at FROM ledger_revisions WHERE sync_status='pending'
        )) AS oldest_pending_at
    `).get() as Record<string, number | string | null>;
    const oldest = typeof row.oldest_pending_at === "string" ? new Date(row.oldest_pending_at) : undefined;
    return {
      pendingEvents: Number(row.pending_events), quarantinedEvents: Number(row.quarantined_events),
      pendingTasks: Number(row.pending_tasks), pendingOutcomes: Number(row.pending_outcomes),
      quarantinedOutcomes: Number(row.quarantined_outcomes), pendingRevenues: Number(row.pending_revenues),
      quarantinedRevenues: Number(row.quarantined_revenues), pendingProviderJobs: Number(row.pending_provider_jobs),
      quarantinedProviderJobs: Number(row.quarantined_provider_jobs),
      ...(oldest !== undefined && Number.isFinite(oldest.getTime()) ? { oldestPendingAt: oldest } : {}),
    };
  }

  /**
   * Close the underlying database connection.
   */
  close(): void {
    if (this._mem) { this._mem.close(); this._mem = null; this._ledgerMemory.clear(); return; }
    if (!this._db) return;
    this._db.close();
  }
}
