/**
 * CostTracker — the main entry point for recording AI agent costs.
 *
 * Wraps business logic in tracked tasks, records cost events, and
 * manages background flushing to a remote endpoint.
 */

import { randomUUID } from "node:crypto";
import { Decimal, toDecimal } from "./models.js";
import type {
  Task,
  CostEvent,
  EventType,
  CostConfidence,
  PricingSource,
  DecimalLike,
} from "./models.js";

/**
 * Decimal-based addition to defeat floating-point drift in cost
 * accumulation. Sprint 2 Theme E / §3.3.1 (B3).
 *
 * Native `a + b` on `number` accumulates ~2e-16 of error per add; over
 * 10 000 events that adds up to a visible drift in the per-task total.
 * Money fields are now `Decimal` end-to-end, so this stays entirely in the
 * Decimal domain (`a.plus(toDecimal(b))`) — no float round-trip. The
 * tiny-decimal accumulation invariant (1.23e-8 × 10000 == 0.000123 exactly)
 * is the regression this guards.
 */
function decAdd(a: Decimal, b: DecimalLike): Decimal {
  return a.plus(toDecimal(b));
}
import { createTask, createCostEvent } from "./models.js";
import { getCurrentTask, runWithTask } from "./context.js";
import { EventBuffer } from "../transport/buffer.js";
import {
  NetworkAccountant,
  registerAccountant,
} from "../adapters/network-accountant.js";
import { ComputePricingEngine } from "../pricing/compute-pricing.js";
import { ComputeAccountant } from "./compute-accountant.js";
import { RuntimeKind } from "./compute-runtime.js";
import { GpuPricingEngine } from "../pricing/gpu-pricing.js";
import { GpuAccountant } from "./gpu-accountant.js";
import { GpuRuntimeKind } from "./gpu-runtime.js";
import { getCloudEnv } from "../cloud-detect.js";
import { EventPusher } from "../transport/pusher.js";
import { PricingEngine } from "../pricing/engine.js";
import { RateRegistry } from "../pricing/rates.js";
import { RetryHeuristicEngine } from "./heuristics.js";
import { resolveConfig } from "./config.js";
import type { ResolvedConfig } from "./config.js";
import { DEFAULT_ENDPOINT, resolveEndpoint } from "./endpoint.js";
import { finalizeTaskNetwork } from "./network-finalize.js";
import { setDebugMode, debugLog } from "./debug.js";
import { registerLlmCapture } from "./llm-dedup.js";
import {
  trackHttp as _adapterTrackHttp,
  untrackHttp as _adapterUntrackHttp,
  getServiceCatalog as _adapterGetServiceCatalog,
  getSessionManager as _adapterGetSessionManager,
  registerInternalHost as _adapterRegisterInternalHost,
} from "../adapters/http.js";
import {
  ALL_SUPPORTED_INSTRUMENTS,
  instrumentProvider,
  uninstrumentProvider,
  provideInstrumentModule,
  canonicalInstrumentName,
} from "../instruments/index.js";

// Endpoint resolution lives in ./endpoint.js (single source of truth) so that
// both the pricing refresher here and the telemetry pusher route through the
// same https:// allow-list. Re-exported so external consumers (and existing
// tests that import from ../src/core/tracker.js) keep resolving these names.
export { DEFAULT_ENDPOINT, resolveEndpoint };

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

/**
 * Sprint 2 Theme E / §3.3.2 (B9) — exit-time flush handlers.
 *
 * Pre-fix events recorded just before `process.exit(0)` were lost:
 * the buffered in-memory queue and the not-yet-flushed pusher batch
 * both died with the process. These handlers run on process tear-
 * down (graceful exit, SIGTERM, SIGINT) and synchronously close the
 * tracker. closeAsync() flushes the pending push first.
 *
 * The handlers are stored so `close()` can unregister them — avoids
 * cross-test listener-leak when init/close cycles repeatedly.
 */
let _exitHandlers: {
  beforeExit?: (code: number) => void;
  sigterm?: NodeJS.SignalsListener;
  sigint?: NodeJS.SignalsListener;
} | null = null;

function _registerExitHandlers(): void {
  if (_exitHandlers !== null) return;
  const beforeExit = (_code: number): void => {
    // Synchronous best-effort flush on graceful exit. Node will wait
    // for any returned promise from `beforeExit` (unlike `exit`), so
    // closeAsync's in-flight push has a chance to land.
    void globalCloseAsync();
  };
  const sigterm: NodeJS.SignalsListener = () => {
    // SIGTERM: containerized environments (k8s, docker stop) deliver
    // this 30s before SIGKILL. Run closeAsync to flush, then let the
    // default handler take over (re-emit so other listeners run).
    void globalCloseAsync();
  };
  const sigint: NodeJS.SignalsListener = () => {
    // SIGINT (Ctrl+C in dev): same flush guarantee.
    void globalCloseAsync();
  };
  process.on("beforeExit", beforeExit);
  process.on("SIGTERM", sigterm);
  process.on("SIGINT", sigint);
  _exitHandlers = { beforeExit, sigterm, sigint };
}

function _unregisterExitHandlers(): void {
  if (_exitHandlers === null) return;
  if (_exitHandlers.beforeExit) process.off("beforeExit", _exitHandlers.beforeExit);
  if (_exitHandlers.sigterm) process.off("SIGTERM", _exitHandlers.sigterm);
  if (_exitHandlers.sigint) process.off("SIGINT", _exitHandlers.sigint);
  _exitHandlers = null;
}

export function init(options: TrackerOptions = {}): CostTracker {
  if (_instance !== null) {
    throw new Error("dexcost already initialized — call close() first to reset");
  }
  _instance = new CostTracker(options);
  _registerExitHandlers();
  return _instance;
}

export function getTracker(): CostTracker {
  if (_instance === null) {
    throw new Error("dexcost not initialized — call init() first");
  }
  return _instance;
}

/**
 * Update the SDK's API key and resume sync after auth failure.
 *
 * Sprint 2 Theme D / §3.2.3 (B14). When the Control Layer returns
 * 401/403 the pusher sets `_authFailed=true` and stops; without this
 * function the only recovery is restarting the customer's process.
 *
 * Returns `true` on success, `false` if `init()` has not been called
 * (logs a console warning).
 */
export function setApiKey(newKey: string): boolean {
  if (_instance === null) {
    console.warn(
      "dexcost: setApiKey called before init(); ignoring. " +
        "Call dexcost.init({apiKey:...}) first.",
    );
    return false;
  }
  _instance.setApiKey(newKey);
  return true;
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

/**
 * Best-effort, bounded flush for freeze-prone environments (Lambda, Cloud
 * Functions, Vercel, Cloud Run without always-on CPU): serverless runtimes
 * give NO background CPU after the handler returns, so the pusher's
 * interval may never fire and buffered events sit undelivered until the
 * next (possibly never-coming) invocation.
 *
 * Never throws and never hangs the handler: resolves after `timeoutMs`
 * even if the push is still in flight, and is a no-op when the SDK is not
 * initialized or runs in local mode.
 *
 * Next.js route handlers: pair it with `after()` so the flush runs outside
 * the response's critical path:
 *
 *   import { after } from "next/server";
 *   after(() => flushBeforeFreeze());
 */
export async function flushBeforeFreeze(timeoutMs: number = 3_000): Promise<void> {
  let tracker: CostTracker;
  try {
    tracker = getTracker();
  } catch {
    return; // not initialized — nothing to flush
  }
  try {
    await Promise.race([
      tracker.flush(),
      new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, timeoutMs);
        // Never keep the event loop (and a serverless bill) alive for this.
        if (typeof timer.unref === "function") timer.unref();
      }),
    ]);
  } catch (err) {
    // A failed push stays buffered for the next cycle — log in debug only.
    debugLog("flush", `flushBeforeFreeze push failed (events remain buffered): ${String(err)}`);
  }
}

export function globalClose(): void {
  if (_instance !== null) {
    _instance.close();
    _instance = null;
  }
  _unregisterExitHandlers();
}

export async function globalCloseAsync(): Promise<void> {
  if (_instance !== null) {
    await _instance.closeAsync();
    _instance = null;
  }
  _unregisterExitHandlers();
}

/** Configuration options for a CostTracker instance. */
export interface TrackerOptions {
  /** API key for authenticating with the remote endpoint. */
  apiKey?: string;
  /**
   * Control Layer endpoint, supplied explicitly in code. Defaults to the
   * hardcoded production URL (`https://api.dexcost.io`). This is the ONLY way
   * to override the endpoint — it is never read from the process environment,
   * so a hostile env (`DEXCOST_ENDPOINT=http://attacker/`) cannot redirect
   * telemetry or the Bearer API key. Must start with `http://` or `https://`
   * (otherwise it is ignored with a warning and the default is used). `http://`
   * is accepted (e.g. `http://localhost` for e2e) since it is not
   * env-controllable.
   */
  endpoint?: string;
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
   * Explicit module/class references for bundled apps (Next.js, webpack,
   * esbuild) where runtime resolution finds a DIFFERENT package copy than
   * the one your code calls — the classic "instrumented but captures
   * nothing" failure. Keys: openai, anthropic, ai, gemini, bedrock,
   * cohere, mcp. Providing a module implies instrumenting it.
   *
   *   import OpenAI from "openai";
   *   import * as ai from "ai";
   *   init({ instrumentModules: { openai: OpenAI, ai } });
   */
  instrumentModules?: Record<string, unknown>;
  /**
   * Path to the SQLite database file. Defaults to ~/.dexcost/buffer.db.
   * Override in tests to get per-test isolation.
   */
  dbPath?: string;
  /** Set to "development" to enable dev mode console output and disable cloud push. */
  environment?: string;
  /** Enable automatic retry detection via sliding-window heuristics. */
  enableRetryHeuristics?: boolean;
  /**
   * Log every capture decision to stderr (instrument activation, HTTP
   * fallback classification, session lifecycle) — answers "why wasn't
   * this call captured?". Also enabled by `DEXCOST_DEBUG=1`.
   */
  debug?: boolean;
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

  /**
   * Sprint 3 Theme F / §4.1.3 (P4): network-event emission knobs,
   * parity with Python `init(network_event_*)`. The HTTP adapter
   * reads these to decide whether a captured call deserves an
   * emitted `network` event (in addition to the always-emitted
   * `external_cost`). Defaults match Python.
   *
   * Emit when combined request+response bytes exceed this.
   * Default 102_400 (100 KiB). Set 0 to disable.
   */
  networkEventThresholdBytes?: number;
  /** Emit on response status >= 400. Default true. */
  networkEventOnError?: boolean;
  /** Emit when call latency exceeds this many ms. Default 0 (off). */
  networkEventLatencyMs?: number;
  /**
   * Per-billing-model dispatch overrides for the compute pricing engine.
   * Currently used to switch Cloud Run from request-based to instance-
   * based billing: `{ cloud_run: "instance" }`. Mirrors the Python
   * `compute_billing_overrides` option.
   */
  computeBillingOverrides?: Record<string, string>;
  /**
   * Enable K8s node-aware pricing. Reserved for follow-up — currently
   * threaded through but unused; the default k8s_pod billing model uses
   * pod-limits × duration × hourly default. Mirrors the Python
   * `k8s_node_aware` option.
   */
  k8sNodeAware?: boolean;
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
    // Register a NetworkAccountant for this task so the patched
    // globalThis.fetch (which sees only the task_id via AsyncLocalStorage)
    // can record byte usage via core.getAccountant(taskId).
    // Unregistered in end().
    registerAccountant(task.taskId, new NetworkAccountant());
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
    cost?: DecimalLike,
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
    let costUsd: Decimal;
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
      costUsd = toDecimal(cost);
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
        this._task.retryCostUsd = decAdd(this._task.retryCostUsd, costUsd);
      }
    }

    // Persist only after the retry fields have been finalised on `event`.
    this._events.push(event);
    this._buffer.addEvent(event);
    registerLlmCapture(this._task.taskId, event.inputTokens ?? 0, event.outputTokens ?? 0);
    logEvent(event, this._task.taskType);

    // Feed the persisted event into the engine's sliding window.
    if (engine) {
      engine.record(event);
    }

    // Aggregate into task
    this._task.llmCostUsd = decAdd(this._task.llmCostUsd, costUsd);
    this._task.totalCostUsd = decAdd(this._task.totalCostUsd, costUsd);
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
    cost: DecimalLike,
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
    const costUsd = toDecimal(cost);
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
      this._task.externalCostUsd = decAdd(this._task.externalCostUsd, costUsd);
    } else if (eventType === "compute_cost") {
      this._task.computeCostUsd = decAdd(this._task.computeCostUsd, costUsd);
    }
    this._task.totalCostUsd = decAdd(this._task.totalCostUsd, costUsd);

    this._buffer.upsertTask(this._task);
    return event;
  }

  /**
   * Record a retry event.
   */
  markRetry(
    reason: string,
    cost?: DecimalLike,
    retryOf?: string
  ): CostEvent {
    const costUsd = cost === undefined ? new Decimal(0) : toDecimal(cost);
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: this._task.taskId,
      eventType: "retry_marker",
      costUsd,
      costConfidence: costUsd.gt(0) ? "exact" : "unknown",
      isRetry: true,
      retryReason: reason,
      retryOf,
    });

    this._events.push(event);
    this._buffer.addEvent(event);
    logEvent(event, this._task.taskType);

    // Aggregate into task
    this._task.retryCount += 1;
    this._task.retryCostUsd = decAdd(this._task.retryCostUsd, costUsd);
    this._task.totalCostUsd = decAdd(this._task.totalCostUsd, costUsd);

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

    // ── Network finalize — v1 byte aggregates + v2 egress pricing ────
    // Mirrors python tracker.py:_aggregate_costs + rust TrackedTask::
    // finalize_network + go finalizeNetwork. Tier-5 fail-silent: any
    // throw in the egress block is logged and swallowed so a pricing
    // bug never breaks task finalization (the task still ships with
    // v1 + LLM/external/compute costs intact).
    try {
      this._finalizeNetwork();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(
        `[dexcost] egress cost computation failed for task ${this._task.taskId}:`,
        err,
      );
      this._task.networkCostUsd = new Decimal(0);
    }

    // ── Compute capture (v1 + v2 cost) ───────────────────────────────────
    // Long-running runtimes emit their compute_cost event at task finalize
    // from the cgroup diff; serverless runtimes have already emitted from
    // the handler wrap with cost_pending=true. Either way, the v2 pricing
    // engine back-fills cost_usd here via the deferred-cost pattern.
    // Wrapped in Tier-5 fail-silent so a pricing throw never breaks
    // finalize (mirrors python tracker.py:_aggregate_costs +
    // _finalize_compute).
    try {
      this._finalizeCompute();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(
        `[dexcost] compute cost computation failed for task ${this._task.taskId}:`,
        err,
      );
    }

    // ── GPU capture (Phase 2 v1 + v2) ─────────────────────────────────────
    // Long-running GPU runtimes (AWS_EC2_GPU / GCP_GCE_BUNDLED / etc.) emit
    // 1 gpu_cost + N gpu_utilization_signal at task finalize from the cgroup
    // walk + NVML snapshot diff. Serverless runtimes (Modal / RunPod /
    // Replicate) have already emitted via the handler wrap. Either way the
    // GpuPricingEngine back-fills gpu_cost.costUsd here via the deferred-
    // cost pattern. gpu_utilization_signal events are NEVER priced
    // (Decision #3 observability carve-out). Tier-5 fail-silent.
    try {
      this._finalizeGpu();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn(
        `[dexcost] gpu cost computation failed for task ${this._task.taskId}:`,
        err,
      );
    }

    this._buffer.upsertTask(this._task);
    logTaskComplete(this._task);
  }

  /**
   * Compute auto-emission + back-fill at task finalize.
   *
   * Mirrors python tracker.py:_finalize_compute.
   *
   * Step 1: long-running runtime → call snapshotEndAndBuild and insert a
   *         compute_cost event with details.cost_pending=true.
   * Step 2: walk all compute_cost events with cost_pending=true, resolve
   *         their cost via the pricing engine, then updateEvent to strip
   *         the marker + stamp pricing source/confidence/version.
   * Step 3: apply DELTA-based total adjustment — never recompute total_
   *         cost_usd from scratch, which would blow away retry_marker
   *         and other costs accumulated by the main loop.
   */
  private _finalizeCompute(): void {
    const task = this._task;
    const accountant = task._compute as ComputeAccountant | undefined;
    const cloudEnv = getCloudEnv();
    const overrides = this._tracker.computeBillingOverrides;

    let durationMs = 0;
    let windowS = new Decimal(0);
    if (task.endedAt && task.startedAt) {
      const ms = task.endedAt.getTime() - task.startedAt.getTime();
      durationMs = Math.trunc(ms);
      windowS = new Decimal(ms).dividedBy(1000);
    }

    // 1. Long-running runtimes: build + persist the cgroup-diff event.
    const longRunning = new Set<RuntimeKind>([
      RuntimeKind.Fargate,
      RuntimeKind.Ec2,
      RuntimeKind.Gce,
      RuntimeKind.AzureVm,
      RuntimeKind.K8sPod,
    ]);
    const newEventIds = new Set<string>();
    if (accountant && longRunning.has(accountant.runtime)) {
      const details = accountant.snapshotEndAndBuild(durationMs);
      if (details !== null) {
        const ev = createCostEvent({
          eventId: randomUUID(),
          taskId: task.taskId,
          eventType: "compute_cost",
          costUsd: 0,
          costConfidence: "unknown",
          isRetry: false,
          details,
        });
        this._buffer.addEvent(ev);
        this._events.push(ev);
        newEventIds.add(ev.eventId);
      }
    }

    // 2. Back-fill cost on every compute_cost event with cost_pending=true.
    //    Track per-event delta so we adjust totals without blowing away the
    //    running totals already accumulated by the main loop.
    const engine = this._tracker.computePricing;
    const events = this._buffer.queryEvents(task.taskId);
    let costDelta = new Decimal(0);
    for (const ev of events) {
      if (ev.eventType !== "compute_cost") continue;
      const details = ev.details || {};
      if ((details as Record<string, unknown>).cost_pending !== true) continue;
      const oldCost = ev.costUsd;
      const priced = engine.resolveComputeCost(
        details as Record<string, any>,
        cloudEnv,
        overrides,
        windowS,
      );
      ev.costUsd = priced.costUsd;
      ev.pricingSource = priced.pricingSource as PricingSource;
      ev.costConfidence = priced.costConfidence;
      ev.pricingVersion = `compute:${engine.catalogVersion}`;
      const newDetails: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(details)) {
        if (k !== "cost_pending") newDetails[k] = v;
      }
      ev.details = newDetails;
      this._buffer.updateEvent(ev);

      // Delta = new - old. For newly-inserted long-running events the
      // main loop never saw them at all, so we add the original $0 too
      // (always 0 here, but explicit per python parity).
      const delta = priced.costUsd.minus(oldCost);
      costDelta = costDelta.plus(delta);
      if (newEventIds.has(ev.eventId)) {
        costDelta = costDelta.plus(oldCost);
      }
    }

    task.computeCostUsd = decAdd(task.computeCostUsd, costDelta);
    task.totalCostUsd = decAdd(task.totalCostUsd, costDelta);
  }

  /**
   * GPU auto-emission + back-fill at task finalize.
   *
   * Mirrors python tracker.py:_finalize_gpu. Three steps:
   *
   *  1. Long-running GPU runtimes (AwsEc2Gpu / GcpGceBundled /
   *     GcpGceN1Attached / AzureVmGpu / AzureVmVgpu / LambdaLabs /
   *     CoreWeave) call accountant.snapshotEndAndBuild(durationMs) and
   *     persist a gpu_cost event (cost_pending=true) plus N
   *     gpu_utilization_signal events. Serverless GPU runtimes (Modal /
   *     RunPod / Replicate) have already emitted via the handler wrap;
   *     this step is a no-op for them.
   *  2. Back-fills cost_usd on every gpu_cost event with cost_pending=true:
   *     resolves rate via GpuPricingEngine.resolveGpuCost, sets cost_usd,
   *     pricing_source, cost_confidence, pricing_version ("gpu:<version>"
   *     — distinct from compute / egress prefixes), and strips the
   *     internal cost_pending / _cgroup_scope_fallback /
   *     _nvml_product_name_lower hints from details before re-persisting.
   *  3. gpu_utilization_signal events are NEVER touched by the back-fill
   *     walker — they stay at cost_usd=0 (Decision #3 observability
   *     carve-out). Load-bearing convention §1 carve-out — see test
   *     gpu-auto-emission.test.ts.
   *
   * Delta-based total adjustment preserves any retry_marker costs already
   * accumulated by the main aggregation loop.
   */
  private _finalizeGpu(): void {
    const task = this._task;
    const accountant = (task as any)._gpu as GpuAccountant | undefined;
    const cloudEnv = getCloudEnv();

    let durationMs = 0;
    let windowS = new Decimal(0);
    if (task.endedAt && task.startedAt) {
      const ms = task.endedAt.getTime() - task.startedAt.getTime();
      durationMs = Math.trunc(ms);
      windowS = new Decimal(ms).dividedBy(1000);
    }

    // 1. Long-running GPU runtimes: snapshot + persist dual events.
    const longRunningGpu = new Set<string>([
      GpuRuntimeKind.AwsEc2Gpu,
      GpuRuntimeKind.GcpGceBundled,
      GpuRuntimeKind.GcpGceN1Attached,
      GpuRuntimeKind.AzureVmGpu,
      GpuRuntimeKind.AzureVmVgpu,
      GpuRuntimeKind.LambdaLabs,
      GpuRuntimeKind.CoreWeave,
    ]);
    const newEventIds = new Set<string>();
    if (accountant && longRunningGpu.has(accountant.runtime)) {
      const { costDetails, signalEvents } = accountant.snapshotEndAndBuild(
        durationMs,
      );
      if (costDetails !== null) {
        const ev = createCostEvent({
          eventId: randomUUID(),
          taskId: task.taskId,
          eventType: "gpu_cost",
          costUsd: 0,
          costConfidence: "unknown",
          isRetry: false,
          details: costDetails as unknown as Record<string, unknown>,
        });
        this._buffer.addEvent(ev);
        this._events.push(ev);
        newEventIds.add(ev.eventId);
      }
      if (signalEvents) {
        for (const sig of signalEvents) {
          const sev = createCostEvent({
            eventId: randomUUID(),
            taskId: task.taskId,
            eventType: "gpu_utilization_signal",
            costUsd: 0, // Decision #3 — observability only
            costConfidence: "unknown",
            isRetry: false,
            details: sig as unknown as Record<string, unknown>,
          });
          this._buffer.addEvent(sev);
          this._events.push(sev);
        }
      }
    }

    // 2. Back-fill cost on every gpu_cost event with cost_pending=true.
    //    Per Decision #3, gpu_utilization_signal events are NEVER priced.
    const engine = this._tracker.gpuPricing;
    const events = this._buffer.queryEvents(task.taskId);
    let costDelta = new Decimal(0);
    for (const ev of events) {
      if (ev.eventType !== "gpu_cost") continue;
      const details = (ev.details || {}) as Record<string, unknown>;
      if (details.cost_pending !== true) continue;
      const oldCost = ev.costUsd;
      const priced = engine.resolveGpuCost(
        details as Record<string, any>,
        cloudEnv,
        windowS,
      );
      ev.costUsd = priced.costUsd;
      ev.pricingSource = priced.pricingSource as any;
      ev.costConfidence = priced.costConfidence;
      ev.pricingVersion = `gpu:${engine.catalogVersion}`;
      const newDetails: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(details)) {
        if (
          k !== "cost_pending" &&
          k !== "_cgroup_scope_fallback" &&
          k !== "_nvml_product_name_lower"
        ) {
          newDetails[k] = v;
        }
      }
      ev.details = newDetails;
      this._buffer.updateEvent(ev);

      const delta = priced.costUsd.minus(oldCost);
      costDelta = costDelta.plus(delta);
      if (newEventIds.has(ev.eventId)) {
        costDelta = costDelta.plus(oldCost); // always 0; explicit
      }
    }

    task.gpuCostUsd = decAdd(task.gpuCostUsd, costDelta);
    task.totalCostUsd = decAdd(task.totalCostUsd, costDelta);
  }

  /**
   * Snapshot the NetworkAccountant onto the task's v1 fields and (if
   * a CloudEnv has been resolved) compute v2 egress dollars + back-fill
   * the cost_pending network events for this task.
   *
   * Caller (end) wraps this in a Tier-5 fail-silent shell.
   */
  private _finalizeNetwork(): void {
    // Delegates to the shared implementation so session tasks and
    // instrument auto-tasks (via finalizeAutoTask) run the exact same
    // drain + egress pricing + cost_pending back-fill path.
    finalizeTaskNetwork(this._task, this._buffer);
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
    const costUsd = toDecimal(rate).times(units);
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
    this._task.externalCostUsd = decAdd(this._task.externalCostUsd, costUsd);
    this._task.totalCostUsd = decAdd(this._task.totalCostUsd, costUsd);
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
    const reversed = this._task.retryCostUsd.minus(target.costUsd);
    this._task.retryCostUsd = reversed.lt(0) ? new Decimal(0) : reversed;
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
  private _computePricing: ComputePricingEngine;
  private _gpuPricing: GpuPricingEngine;
  private _computeBillingOverrides: Record<string, string>;
  private _k8sNodeAware: boolean;
  private _rateRegistry: RateRegistry;
  private _heuristicEngine: RetryHeuristicEngine | null;
  private _instrumented: Set<string> = new Set();
  private _config: ResolvedConfig;
  private _httpTracked = false;
  private _sessionTimer: ReturnType<typeof setInterval> | null = null;
  private _getSessionManager?: () => import("./session.js").SessionManager | null;

  constructor(options: TrackerOptions = {}) {
    this._options = {
      batchSize: 100,
      // Sprint 3 Theme F / §4.1.3 P5: default flush 5 s, matching
      // Python's `flush_interval=5.0`. Pre-fix the TS default was
      // 30 s, leaving up to 6× more time for events to be lost on
      // process exit (and inconsistent with Python's UX).
      flushIntervalMs: 5000,
      ...options,
    };

    // Resolve API key (explicit arg → DEXCOST_API_KEY env var) and storage
    // mode. Throws InvalidAPIKeyError for a malformed key.
    // Debug mode: explicit option wins; otherwise DEXCOST_DEBUG decides.
    if (options.debug !== undefined) {
      setDebugMode(options.debug);
    }

    this._config = resolveConfig(this._options.apiKey, this._options.storage);
    // Use the resolved key everywhere downstream (env-var fallback included).
    this._options.apiKey = this._config.apiKey;

    this._buffer = new EventBuffer(this._options.dbPath);

    // Dev mode detection
    const env = options?.environment ?? process.env.DEXCOST_ENV;
    if (env === "development") {
      enableDevMode();
    }

    // Endpoint comes ONLY from the explicit in-code option (or the hardcoded
    // default) — never from the process env. Threaded to both consumers below:
    // the pusher (telemetry POST) and the pricing refresher.
    const endpoint = resolveEndpoint(this._options.endpoint);

    // The SDK's own traffic (pusher, pricing refresh, catalog refresh)
    // must be invisible to capture — register the hosts it talks to
    // BEFORE HTTP tracking patches fetch.
    try {
      _adapterRegisterInternalHost(new URL(endpoint).hostname);
    } catch {
      // endpoint already validated by resolveEndpoint; never fatal here
    }
    if (this._options.serviceCatalogUrl) {
      try {
        _adapterRegisterInternalHost(new URL(this._options.serviceCatalogUrl).hostname);
      } catch {
        // invalid catalog URL fails later in refresh; not fatal here
      }
    }

    const cloudMode = this._config.storageMode === "cloud" && !isDevMode();
    debugLog(
      "init",
      `storage=${isDevMode() ? "dev-console" : cloudMode ? "cloud" : "local"} ` +
        `endpoint=${cloudMode ? endpoint : "n/a"} apiKey=${this._config.apiKey ? "present" : "absent"}`,
    );

    if (cloudMode) {
      this._pusher = new EventPusher(this._buffer, this._options, endpoint);
      this._pusher.start();
    }

    this._pricing = new PricingEngine();
    this._computePricing = new ComputePricingEngine();
    // GPU pricing engine (Phase 2 — bundled gpu_prices.json). No init knob
    // needed: GPU billing models are unambiguous per provider (Modal is
    // always per_gpu_second_active, etc.). Mirrors python tracker.py.
    this._gpuPricing = new GpuPricingEngine();
    this._computeBillingOverrides = { ...(options.computeBillingOverrides ?? {}) };
    this._k8sNodeAware = options.k8sNodeAware ?? false;

    // Start background pricing refresh in cloud mode
    if (cloudMode && this._config.apiKey) {
      this._pricing.setApiKey(this._config.apiKey);
      this._pricing.startBackgroundRefresh(endpoint);
    }

    this._rateRegistry = new RateRegistry();
    this._heuristicEngine = options.enableRetryHeuristics
      ? new RetryHeuristicEngine(options.retryHeuristicWindow, options.retryHeuristicThreshold)
      : null;

    // `explicit` is true only when the user listed providers themselves;
    // failures for the default full set stay quiet (issue: noisy warnings for
    // uninstalled providers), while failures for user-requested providers warn.
    // Explicit module references (bundler escape hatch) are handed to the
    // instruments BEFORE activation; providing a module implies wanting
    // that provider instrumented even under a narrowed autoInstrument list.
    const provided: string[] = [];
    for (const [name, ref] of Object.entries(options.instrumentModules ?? {})) {
      if (provideInstrumentModule(name, ref)) {
        provided.push(canonicalInstrumentName(name));
      }
    }

    const explicitInstruments = options.autoInstrument !== undefined;
    const instruments = new Set([
      ...(options.autoInstrument ?? [...ALL_SUPPORTED_INSTRUMENTS]),
      ...provided,
    ]);
    for (const name of instruments) {
      // Providers with an explicitly provided module are always "explicit":
      // the user asked for them by handing us the module, so activation
      // failures must be surfaced.
      void this.instrument(name, explicitInstruments || provided.includes(name));
    }

    // Auto-track outgoing HTTP calls (default on, matches Python).
    if (this._options.trackHttp !== false) {
      this._enableHttpTracking(this._options.serviceCatalogUrl);
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
  private _enableHttpTracking(serviceCatalogUrl?: string): void {
    try {
      // SYNCHRONOUS on purpose. This used to be fire-and-forget async with
      // a dynamic import, which meant init() returned BEFORE globalThis.fetch
      // was patched — LLM calls made immediately after init() (cold-start
      // requests, top-level awaits) escaped capture entirely. The fetch
      // patch must be in effect the moment init() returns.
      this._getSessionManager = _adapterGetSessionManager;
      _adapterTrackHttp(this._buffer, this._pricing);
      this._httpTracked = true;

      // Safety-net timer: finalize idle sessions every 30s so auto-created
      // session tasks don't stay "pending" forever if an instrument or
      // stream fails to end them (e.g. unhandled exception, aborted stream).
      const buffer = this._buffer;
      this._sessionTimer = setInterval(() => {
        try {
          const sm = _adapterGetSessionManager();
          if (sm) {
            sm.finalizeIdleSessions(buffer);
          }
        } catch {
          // Safety net must never crash the process
        }
      }, 30_000);
      if (this._sessionTimer.unref) {
        this._sessionTimer.unref();
      }

      if (serviceCatalogUrl) {
        // Catalog refresh is network I/O — the only part that stays async
        // (and best-effort). The patch above is already installed.
        const catalog = _adapterGetServiceCatalog();
        if (catalog) {
          void catalog.refreshFromUrl(serviceCatalogUrl).catch(() => {
            // best-effort refresh — bundled catalog remains in use
          });
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

  /** The compute pricing engine — wires through to TrackedTask.end finalize. */
  get computePricing(): ComputePricingEngine {
    return this._computePricing;
  }

  /** The GPU pricing engine — wires through to TrackedTask.end finalize. */
  get gpuPricing(): GpuPricingEngine {
    return this._gpuPricing;
  }

  /** Compute billing-model dispatch overrides (e.g. cloud_run=instance). */
  get computeBillingOverrides(): Record<string, string> {
    return this._computeBillingOverrides;
  }

  /** Whether K8s node-aware pricing is enabled (reserved for follow-up). */
  get k8sNodeAware(): boolean {
    return this._k8sNodeAware;
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
  async instrument(name: string, explicit: boolean = true): Promise<void> {
    if (this._instrumented.has(name)) return;
    const success = await instrumentProvider(name, this._pricing, this._buffer, explicit);
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
   * Update the API key on both pricing engine and pusher. Sprint 2
   * Theme D / §3.2.3 (B14) — entry point for `dexcost.setApiKey`.
   */
  setApiKey(newKey: string): void {
    this._config = { ...this._config, apiKey: newKey };
    this._pricing.setApiKey(newKey);
    if (this._pusher) {
      this._pusher.setApiKey(newKey);
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

    // Finalize all pending sessions before tearing down HTTP tracking
    this._finalizeAllSessionsSync();

    this._disableHttpTracking();
    if (this._sessionTimer) {
      clearInterval(this._sessionTimer);
      this._sessionTimer = null;
    }
    if (this._pusher) {
      // Note: flush() is async but close() is sync by contract.
      // We call stop() which clears the interval; any in-flight push
      // completes naturally. Use flush() before close() for guaranteed delivery.
      this._pusher.stop();
    }
    this._pricing.stopBackgroundRefresh();
    this._buffer.close();
  }

  /**
   * Force-finalize all active session tasks so none are left "pending"
   * on shutdown.  Synchronous — safe to call from both close() and
   * closeAsync().
   */
  private _finalizeAllSessionsSync(): void {
    if (!this._httpTracked) return;
    // Best-effort: session finalization must never abort shutdown. Any
    // exception from finalizeAllSessions is swallowed so close()/closeAsync()
    // still tear down the pusher and HTTP tracking.
    try {
      const sm = this._getSessionManager?.();
      if (sm) {
        sm.finalizeAllSessions(this._buffer);
      }
    } catch (err) {
      // Best-effort: never abort shutdown so the pusher/buffer still close,
      // but surface the failure so stuck-pending sessions (e.g. from
      // buffer.upsertTask throwing) stay observable rather than silently
      // swallowed.
      // eslint-disable-next-line no-console
      console.warn("[dexcost] session finalization failed during shutdown:", err);
    }
  }

  /** Restore patched HTTP transports if HTTP tracking was enabled. */
  private _disableHttpTracking(): void {
    if (!this._httpTracked) return;
    this._httpTracked = false;
    try {
      _adapterUntrackHttp();
    } catch {
      // best-effort
    }
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

    // Finalize all pending sessions before tearing down HTTP tracking
    this._finalizeAllSessionsSync();

    this._disableHttpTracking();
    if (this._sessionTimer) {
      clearInterval(this._sessionTimer);
      this._sessionTimer = null;
    }
    if (this._pusher) {
      await this._pusher.flush();
      this._pusher.stop();
    }
    this._pricing.stopBackgroundRefresh();
    this._buffer.close();
  }
}
