/**
 * CostTracker — the main entry point for recording AI agent costs.
 *
 * Wraps business logic in tracked tasks, records cost events, and
 * manages background flushing to a remote endpoint.
 */

import { randomUUID } from "node:crypto";
import type { Task, CostEvent, EventType, CostConfidence, PricingSource } from "./models.js";
import { createTask, createCostEvent } from "./models.js";
import { getCurrentTask, runWithTask } from "./context.js";
import { EventBuffer } from "../transport/buffer.js";
import { EventPusher } from "../transport/pusher.js";
import { PricingEngine } from "../pricing/engine.js";
import { RateRegistry } from "../pricing/rates.js";
import { RetryHeuristicEngine } from "./heuristics.js";
import { resolveConfig } from "./config.js";
import type { ResolvedConfig } from "./config.js";
import {
  ALL_SUPPORTED_INSTRUMENTS,
  instrumentProvider,
  uninstrumentProvider,
} from "../instruments/index.js";

const DEFAULT_ENDPOINT = "https://api.dexcost.io";

/** Event types accepted by `recordCost` (non-LLM cost events). */
const NON_LLM_EVENT_TYPES = new Set<EventType>(["external_cost", "compute_cost"]);
import { isDevMode, enableDevMode, logEvent, logTaskComplete } from "../dev-console.js";
// Side-effect imports to register instruments
import "../instruments/openai.js";
import "../instruments/anthropic.js";
import "../instruments/vercel-ai.js";
import "../instruments/gemini.js";
import "../instruments/bedrock.js";
import "../instruments/cohere.js";
import "../instruments/mcp.js";

// ---------------------------------------------------------------------------
// Singleton / init() factory
// ---------------------------------------------------------------------------

let _instance: CostTracker | null = null;

export function init(options: TrackerOptions = {}): CostTracker {
  if (_instance !== null) {
    throw new Error("dexcost already initialized — call close() first to reset");
  }
  _instance = new CostTracker(options);
  return _instance;
}

export function getTracker(): CostTracker {
  if (_instance === null) {
    throw new Error("dexcost not initialized — call init() first");
  }
  return _instance;
}

export async function globalTrack<T>(
  opts: { taskType: string; customerId?: string; projectId?: string; metadata?: Record<string, unknown>; experimentId?: string; variant?: string },
  fn: (task: TrackedTask) => Promise<T>,
): Promise<T> {
  return getTracker().track(opts, fn);
}

export async function globalFlush(): Promise<void> {
  return getTracker().flush();
}

export function globalClose(): void {
  if (_instance !== null) {
    _instance.close();
    _instance = null;
  }
}

export async function globalCloseAsync(): Promise<void> {
  if (_instance !== null) {
    await _instance.closeAsync();
    _instance = null;
  }
}

/** Configuration options for a CostTracker instance. */
export interface TrackerOptions {
  /** API key for authenticating with the remote endpoint. */
  apiKey?: string;
  /** Maximum number of events per batch push. Defaults to 100. */
  batchSize?: number;
  /** Interval in milliseconds between background flushes. Defaults to 30000. */
  flushIntervalMs?: number;
  /** Field names to redact from event details. */
  redactFields?: string[];
  /** Whether to hash customer IDs before storing/sending. */
  hashCustomerId?: boolean;
  /** Which LLM SDKs to auto-instrument. Defaults to all supported. Set to [] to disable. */
  autoInstrument?: string[];
  /**
   * Path to the SQLite database file. Defaults to ~/.dexcost/buffer.db.
   * Override in tests to get per-test isolation.
   */
  dbPath?: string;
  /** Set to "development" to enable dev mode console output and disable cloud push. */
  environment?: string;
  /** Enable automatic retry detection via sliding-window heuristics. */
  enableRetryHeuristics?: boolean;
  /** Sliding window size in seconds for heuristic retry detection. Defaults to 30. */
  retryHeuristicWindow?: number;
  /** Minimum confidence threshold (0–1) to flag a heuristic retry. Defaults to 0.8. */
  retryHeuristicThreshold?: number;
  /**
   * Explicit storage mode. `"local"` forces local-only mode regardless of
   * whether an API key is present; `"cloud"` (the default when a valid key
   * is set) enables background sync.
   */
  storage?: "local" | "cloud";
  /**
   * Automatically track outgoing HTTP calls via the HTTP adapter.
   * Defaults to `true` (matches Python `init(track_http=True)`).
   */
  trackHttp?: boolean;
  /**
   * Optional URL to refresh the HTTP service catalog from on init.
   */
  serviceCatalogUrl?: string;
}

/**
 * A task that is currently being tracked.
 *
 * Provides methods to record cost events (LLM calls, external costs,
 * retries) against the task.
 */
export class TrackedTask {
  private _task: Task;
  private _buffer: EventBuffer;
  private _tracker: CostTracker;
  private _events: CostEvent[] = [];
  private _ended = false;

  constructor(task: Task, buffer: EventBuffer, tracker: CostTracker) {
    this._task = task;
    this._buffer = buffer;
    this._tracker = tracker;
  }

  /** The underlying Task data. */
  get task(): Task {
    return this._task;
  }

  /** All events recorded against this task. */
  get events(): ReadonlyArray<CostEvent> {
    return this._events;
  }

  /**
   * Record an LLM call event.
   *
   * When `cost` is omitted, the cost is auto-computed via the pricing
   * engine (mirrors Python `tracker.record_llm_call`). Accepts an
   * options object for `error_type`, `details`, `pricingSource`, and
   * `costConfidence`. `error_type` is stored in `details.error_type`.
   */
  recordLlmCall(
    provider: string,
    model: string,
    inputTokens: number,
    outputTokens: number,
    cost?: number,
    cachedTokens?: number,
    latencyMs?: number,
    options: {
      costConfidence?: CostConfidence;
      pricingSource?: PricingSource;
      pricingVersion?: string;
      details?: Record<string, unknown>;
      errorType?: string;
    } = {}
  ): CostEvent {
    let costUsd: number;
    let costConfidence: CostConfidence;
    let pricingSource: PricingSource | undefined;
    let pricingVersion: string | undefined = options.pricingVersion;

    if (cost === undefined) {
      // Auto-compute via the pricing engine (mirrors Python US-010).
      const result = this._tracker.pricing.getCost(
        model,
        inputTokens,
        outputTokens,
        cachedTokens ?? 0,
      );
      costUsd = result.costUsd;
      costConfidence = options.costConfidence ?? result.costConfidence;
      pricingSource = options.pricingSource ?? result.pricingSource;
      pricingVersion = pricingVersion ?? result.pricingVersion;
    } else {
      costUsd = cost;
      costConfidence = options.costConfidence ?? "exact";
      pricingSource = options.pricingSource ?? "manual";
    }

    const details: Record<string, unknown> = { ...(options.details ?? {}) };
    if (options.errorType !== undefined) {
      details.error_type = options.errorType;
    }

    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: this._task.taskId,
      eventType: "llm_call",
      costUsd,
      costConfidence,
      pricingSource,
      pricingVersion,
      provider,
      model,
      inputTokens,
      outputTokens,
      cachedTokens,
      latencyMs,
      isRetry: false,
      details,
    });

    // Heuristic retry detection — must run BEFORE the event is persisted so
    // the SQLite row reflects the detected retry. Mirrors the Python SDK,
    // which runs the heuristic engine before `insert_event` (sync.py /
    // tracker.py). Running it after `addEvent` would persist is_retry=0 and
    // any later update would be a separate, easy-to-drop write.
    const engine = this._tracker.heuristicEngine;
    if (engine && !event.isRetry) {
      const match = engine.check(event);
      if (match.isRetry) {
        event.isRetry = true;
        event.retryReason = match.reason || "heuristic";
        event.retryOf = match.matchedEventId;
        event.details = { ...event.details, retry_confidence: match.confidence };
        this._task.retryCount += 1;
        this._task.retryCostUsd += costUsd;
      }
    }

    // Persist only after the retry fields have been finalised on `event`.
    this._events.push(event);
    this._buffer.addEvent(event);
    logEvent(event, this._task.taskType);

    // Feed the persisted event into the engine's sliding window.
    if (engine) {
      engine.record(event);
    }

    // Aggregate into task
    this._task.llmCostUsd += costUsd;
    this._task.totalCostUsd += costUsd;
    this._task.totalInputTokens += inputTokens;
    this._task.totalOutputTokens += outputTokens;
    if (cachedTokens !== undefined) {
      this._task.totalCachedTokens += cachedTokens;
    }

    this._buffer.upsertTask(this._task);

    return event;
  }

  /**
   * Record a non-LLM cost event (external API call, compute, etc.).
   *
   * `eventType` must be `"external_cost"` or `"compute_cost"`; any other
   * value throws an Error (mirrors Python `tracker.record_cost`).
   */
  recordCost(
    service: string,
    costUsd: number,
    details?: Record<string, unknown>,
    eventType: EventType = "external_cost",
    costConfidence: CostConfidence = "exact",
    pricingSource: PricingSource = "manual",
    pricingVersion?: string
  ): CostEvent {
    if (!NON_LLM_EVENT_TYPES.has(eventType)) {
      throw new Error(
        `event_type must be one of ${[...NON_LLM_EVENT_TYPES].sort().join(", ")}, ` +
          `got "${eventType}"`,
      );
    }
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: this._task.taskId,
      eventType,
      costUsd,
      costConfidence,
      pricingSource,
      pricingVersion,
      serviceName: service,
      isRetry: false,
      details: details ?? {},
    });

    this._events.push(event);
    this._buffer.addEvent(event);
    logEvent(event, this._task.taskType);

    // Aggregate into task
    if (eventType === "external_cost") {
      this._task.externalCostUsd += costUsd;
    } else if (eventType === "compute_cost") {
      this._task.computeCostUsd += costUsd;
    }
    this._task.totalCostUsd += costUsd;

    this._buffer.upsertTask(this._task);
    return event;
  }

  /**
   * Record a retry event.
   */
  markRetry(
    reason: string,
    cost?: number,
    retryOf?: string
  ): CostEvent {
    const costUsd = cost ?? 0;
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: this._task.taskId,
      eventType: "retry_marker",
      costUsd,
      costConfidence: costUsd > 0 ? "exact" : "unknown",
      isRetry: true,
      retryReason: reason,
      retryOf,
    });

    this._events.push(event);
    this._buffer.addEvent(event);
    logEvent(event, this._task.taskType);

    // Aggregate into task
    this._task.retryCount += 1;
    this._task.retryCostUsd += costUsd;
    this._task.totalCostUsd += costUsd;

    this._buffer.upsertTask(this._task);
    return event;
  }

  /**
   * Link an external trace (e.g., Langfuse, LangSmith, Datadog) to this task.
   *
   * Stored under `metadata._trace_links` with `{ provider, trace_id }`
   * entries — the same shape the Python SDK uses, so cross-SDK buffers
   * interoperate.
   */
  linkTrace(provider: string, traceId: string): void {
    if (!this._task.metadata["_trace_links"]) {
      this._task.metadata["_trace_links"] = [];
    }
    (this._task.metadata["_trace_links"] as Array<{ provider: string; trace_id: string }>).push({
      provider,
      trace_id: traceId,
    });
    this._buffer.upsertTask(this._task);
  }

  /**
   * Return all linked traces for this task.
   *
   * Each entry is a `{ provider, trace_id }` object (mirrors Python
   * `TrackedTask.get_trace_links`).
   */
  getTraceLinks(): Array<{ provider: string; trace_id: string }> {
    const links = this._task.metadata["_trace_links"];
    if (Array.isArray(links)) {
      return links as Array<{ provider: string; trace_id: string }>;
    }
    return [];
  }

  /**
   * End the task, setting its status and ended_at timestamp.
   */
  end(status: "success" | "failed" = "success"): void {
    if (this._ended) {
      throw new Error(`Task ${this._task.taskId} has already been ended.`);
    }
    this._ended = true;
    this._task.status = status;
    this._task.endedAt = new Date();
    if (status === "failed") {
      this._task.failureCount += 1;
    }
    this._buffer.upsertTask(this._task);
    logTaskComplete(this._task);
  }

  /**
   * Record a usage event priced via the rate registry.
   */
  recordUsage(service: string, units: number = 1, details?: Record<string, unknown>): CostEvent {
    const rate = this._tracker.getRate(service);
    if (rate === undefined) {
      throw new Error(
        `No rate registered for service "${service}". Use tracker.registerRate("${service}", per, costUsd) first.`
      );
    }
    const costUsd = rate * units;
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: this._task.taskId,
      eventType: "external_cost",
      costUsd,
      costConfidence: "computed",
      pricingSource: "rate_registry",
      pricingVersion: this._tracker.rateRegistry.pricingVersion,
      serviceName: service,
      isRetry: false,
      details: details ?? {},
    });
    this._events.push(event);
    this._buffer.addEvent(event);
    logEvent(event, this._task.taskType);
    this._task.externalCostUsd += costUsd;
    this._task.totalCostUsd += costUsd;
    this._buffer.upsertTask(this._task);
    return event;
  }

  /**
   * Un-flag a retry event as non-retry, reversing the retry accounting.
   * If eventId is provided, targets that specific event; otherwise targets
   * the most recent retry event.
   */
  markNotRetry(eventId?: string): CostEvent | undefined {
    let target: CostEvent | undefined;
    if (eventId) {
      target = this._events.find((e) => e.eventId === eventId && e.isRetry);
    } else {
      for (let i = this._events.length - 1; i >= 0; i--) {
        if (this._events[i].isRetry) {
          target = this._events[i];
          break;
        }
      }
    }
    if (!target) return undefined;
    target.isRetry = false;
    target.retryReason = undefined;
    target.retryOf = undefined;
    this._task.retryCount = Math.max(0, this._task.retryCount - 1);
    this._task.retryCostUsd = Math.max(0, this._task.retryCostUsd - target.costUsd);
    this._buffer.upsertTask(this._task);
    return target;
  }
}

/**
 * Main cost tracker for recording AI agent unit economics.
 *
 * Manages task lifecycle, event recording, and background push to
 * a remote endpoint.
 */
export class CostTracker {
  private _buffer: EventBuffer;
  private _pusher: EventPusher | null = null;
  private _options: TrackerOptions;
  private _pricing: PricingEngine;
  private _rateRegistry: RateRegistry;
  private _heuristicEngine: RetryHeuristicEngine | null;
  private _instrumented: Set<string> = new Set();
  private _config: ResolvedConfig;
  private _httpTracked = false;

  constructor(options: TrackerOptions = {}) {
    this._options = {
      batchSize: 100,
      flushIntervalMs: 30000,
      ...options,
    };

    // Resolve API key (explicit arg → DEXCOST_API_KEY env var) and storage
    // mode. Throws InvalidAPIKeyError for a malformed key.
    this._config = resolveConfig(this._options.apiKey, this._options.storage);
    // Use the resolved key everywhere downstream (env-var fallback included).
    this._options.apiKey = this._config.apiKey;

    this._buffer = new EventBuffer(this._options.dbPath);

    // Dev mode detection
    const env = options?.environment ?? process.env.DEXCOST_ENV;
    if (env === "development") {
      enableDevMode();
    }

    const endpoint = process.env.DEXCOST_ENDPOINT ?? DEFAULT_ENDPOINT;

    const cloudMode = this._config.storageMode === "cloud" && !isDevMode();

    if (cloudMode) {
      this._pusher = new EventPusher(this._buffer, this._options);
      this._pusher.start();
    }

    this._pricing = new PricingEngine();

    // Start background pricing refresh in cloud mode
    if (cloudMode && this._config.apiKey) {
      this._pricing.setApiKey(this._config.apiKey);
      this._pricing.startBackgroundRefresh(endpoint);
    }

    this._rateRegistry = new RateRegistry();
    this._heuristicEngine = options.enableRetryHeuristics
      ? new RetryHeuristicEngine(options.retryHeuristicWindow, options.retryHeuristicThreshold)
      : null;

    const instruments = options.autoInstrument ?? [...ALL_SUPPORTED_INSTRUMENTS];
    for (const name of instruments) {
      void this.instrument(name);
    }

    // Auto-track outgoing HTTP calls (default on, matches Python).
    if (this._options.trackHttp !== false) {
      void this._enableHttpTracking(this._options.serviceCatalogUrl);
    }

    // Wire the browser adapter to durable storage so trackBrowser() cost
    // events are persisted and shipped by the pusher. Browser tracking is
    // opt-in via the trackBrowser() wrapper (no init flag), so the buffer is
    // wired unconditionally and used only if trackBrowser actually runs.
    void import("../adapters/browser.js").then(({ setBrowserBuffer }) =>
      setBrowserBuffer(this._buffer),
    );
  }

  /** The resolved API-key / storage configuration. */
  get config(): ResolvedConfig {
    return this._config;
  }

  /**
   * Patch outgoing HTTP transports to auto-record external costs and,
   * when a catalog URL is provided, refresh the service catalog.
   */
  private async _enableHttpTracking(serviceCatalogUrl?: string): Promise<void> {
    try {
      const { trackHttp, getServiceCatalog } = await import("../adapters/http.js");
      trackHttp(this._buffer);
      this._httpTracked = true;
      if (serviceCatalogUrl) {
        const catalog = getServiceCatalog();
        if (catalog) {
          await catalog.refreshFromUrl(serviceCatalogUrl);
        }
      }
    } catch {
      // HTTP tracking is best-effort — never crash init.
    }
  }

  /** The underlying event buffer. */
  get buffer(): EventBuffer {
    return this._buffer;
  }

  /** The pricing engine used for cost calculations. */
  get pricing(): PricingEngine {
    return this._pricing;
  }

  /** The rate registry for service-based cost calculations. */
  get rateRegistry(): RateRegistry {
    return this._rateRegistry;
  }

  /** The heuristic retry engine, or null if heuristics are disabled. */
  get heuristicEngine(): RetryHeuristicEngine | null {
    return this._heuristicEngine;
  }

  /** Register a per-unit rate for a named service. */
  registerRate(service: string, per: string, costUsd: number): void {
    this._rateRegistry.register(service, per, costUsd);
  }

  /** Get the per-unit cost (in USD) for a named service, or undefined if not registered. */
  getRate(service: string): number | undefined {
    return this._rateRegistry.get(service)?.costUsd;
  }

  /**
   * Activate the named instrument, monkey-patching the provider library.
   */
  async instrument(name: string): Promise<void> {
    if (this._instrumented.has(name)) return;
    const success = await instrumentProvider(name, this._pricing, this._buffer);
    if (success) this._instrumented.add(name);
  }

  /**
   * Deactivate the named instrument, restoring the original library methods.
   */
  uninstrument(name: string): void {
    uninstrumentProvider(name);
    this._instrumented.delete(name);
  }

  /**
   * Execute `fn` inside a tracked task context.
   *
   * Creates a new task, runs the function within an AsyncLocalStorage
   * context, and ends the task on completion (or failure).
   */
  /**
   * Manually start a task and return a `TrackedTask` handle.
   *
   * Use this when callbacks/context managers don't fit your architecture
   * (e.g. Celery-style workers, multi-process pipelines). The caller
   * **must** call `TrackedTask.end()` when the task is complete.
   * Mirrors the Python SDK's `CostTracker.start_task`.
   */
  startTask(
    opts: {
      taskType?: string;
      customerId?: string;
      projectId?: string;
      metadata?: Record<string, unknown>;
      experimentId?: string;
      variant?: string;
    } = {}
  ): TrackedTask {
    const parentTask = getCurrentTask();
    const task = createTask({
      taskId: randomUUID(),
      taskType: opts.taskType ?? "",
      customerId: opts.customerId,
      projectId: opts.projectId,
      metadata: opts.metadata ? { ...opts.metadata } : {},
      parentTaskId: parentTask?.taskId,
      experimentId: opts.experimentId,
      variant: opts.variant,
    });
    this._buffer.upsertTask(task);
    return new TrackedTask(task, this._buffer, this);
  }

  async track<T>(
    opts: {
      taskType: string;
      customerId?: string;
      projectId?: string;
      metadata?: Record<string, unknown>;
      experimentId?: string;
      variant?: string;
    },
    fn: (task: TrackedTask) => Promise<T>
  ): Promise<T> {
    const parentTask = getCurrentTask();

    const task = createTask({
      taskId: randomUUID(),
      taskType: opts.taskType,
      customerId: opts.customerId,
      projectId: opts.projectId,
      metadata: opts.metadata ? { ...opts.metadata } : {},
      parentTaskId: parentTask?.taskId,
      experimentId: opts.experimentId,
      variant: opts.variant,
    });

    this._buffer.upsertTask(task);

    const trackedTask = new TrackedTask(task, this._buffer, this);

    try {
      const result = await runWithTask(task, () => fn(trackedTask));
      if (task.status === "pending") {
        trackedTask.end("success");
      }
      return result;
    } catch (error) {
      trackedTask.end("failed");
      throw error;
    }
  }

  /**
   * Force an immediate flush of all buffered events to the remote endpoint.
   */
  async flush(): Promise<void> {
    if (this._pusher) {
      await this._pusher.flush();
    }
  }

  /**
   * Stop the background pusher and release resources.
   */
  close(): void {
    for (const name of this._instrumented) {
      uninstrumentProvider(name);
    }
    this._instrumented.clear();
    this._disableHttpTracking();
    if (this._pusher) {
      // Note: flush() is async but close() is sync by contract.
      // We call stop() which clears the interval; any in-flight push
      // completes naturally. Use flush() before close() for guaranteed delivery.
      this._pusher.stop();
    }
    this._pricing.stopBackgroundRefresh();
    this._buffer.close();
  }

  /** Restore patched HTTP transports if HTTP tracking was enabled. */
  private _disableHttpTracking(): void {
    if (!this._httpTracked) return;
    this._httpTracked = false;
    void import("../adapters/http.js")
      .then(({ untrackHttp }) => untrackHttp())
      .catch(() => {
        // best-effort
      });
  }

  /**
   * Flush pending events and then stop the background pusher and release resources.
   * Prefer this over close() when you need to guarantee all events are delivered.
   */
  async closeAsync(): Promise<void> {
    for (const name of this._instrumented) {
      uninstrumentProvider(name);
    }
    this._instrumented.clear();
    this._disableHttpTracking();
    if (this._pusher) {
      await this._pusher.flush();
      this._pusher.stop();
    }
    this._pricing.stopBackgroundRefresh();
    this._buffer.close();
  }
}
