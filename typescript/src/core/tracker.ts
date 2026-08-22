/**
 * CostTracker — the main entry point for recording AI agent costs.
 *
 * Wraps business logic in tracked tasks, records cost events, and
 * manages background flushing to a remote endpoint.
 */

import { randomUUID } from "node:crypto";
import { Decimal, canonicalDecimal, toDecimal } from "./models.js";
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
import { getContext } from "./context.js";
import { createAutoTask, finalizeAutoTask } from "./auto-task.js";
import {
  applyEventCapability,
  defaultToolCapability,
  getCapability,
  type CapabilityIdentity,
} from "./capabilities.js";
import { applyEventIdempotency } from "./idempotency.js";
import {
  OutcomeRevision,
  RevenueRevision,
  outcomeValue,
  revenueAmount,
  type OutcomeRevisionOptions,
  type RevenueInput,
  type RevenueRevisionOptions,
} from "./business.js";
import { ProviderJobRevision, type ProviderJobRevisionOptions } from "./provider-jobs.js";
import {
  ToolUsage,
  type ToolCostInput,
  type ToolDimensionInput,
  type ToolOperationStatus,
} from "./tool.js";
import { decorateTool } from "./tool-tracking.js";
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
import { GpuRuntimeKind, resolveGpuRuntime } from "./gpu-runtime.js";
import { getCloudEnv } from "../cloud-detect.js";
import { EventPusher } from "../transport/pusher.js";
import { localDeliveryStatus, type DeliveryStatus } from "../transport/delivery.js";
import { PricingEngine } from "../pricing/engine.js";
import { CatalogRuntime, type CatalogRuntimeStatus } from "../pricing/catalog-runtime.js";
import { explainEventPricing, type PricingExplanation } from "../pricing/explain.js";
import { RateRegistry } from "../pricing/rates.js";
import { RetryHeuristicEngine } from "./heuristics.js";
import { resolveCatalogTrustPolicy, resolveConfig } from "./config.js";
import type { ResolvedConfig } from "./config.js";
import { DEFAULT_ENDPOINT, resolveEndpoint } from "./endpoint.js";
import { finalizeTaskNetwork, setNetworkRateRegistry } from "./network-finalize.js";
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

function toolTaskType(toolId: string): string {
  const normalized = toolId.trim().toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "_").replace(/^[._-]+|[._-]+$/g, "");
  return `tool.${(normalized || "unknown").slice(0, 122)}`;
}

import { isDevMode, enableDevMode, logEvent, logTaskComplete } from "../dev-console.js";
// Side-effect imports to register instruments
import "../instruments/openai.js";
import "../instruments/anthropic.js";
import "../instruments/vercel-ai.js";
import "../instruments/gemini.js";
import "../instruments/google-genai.js";
import "../instruments/bedrock.js";
import "../instruments/cohere.js";
import "../instruments/mcp.js";
import "../instruments/litellm.js";
import "../instruments/ollama.js";
import "../instruments/openrouter.js";
import "../instruments/perplexity.js";
import "../instruments/fal.js";

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
  opts: TaskOptions & { taskType: string },
  fn: (task: TrackedTask) => Promise<T>,
): Promise<T> {
  return getTracker().track(opts, fn);
}

export function globalAttachTask(
  taskId: string,
  options: { taskType?: string; rootTaskId?: string; parentTaskId?: string } = {},
): TrackedTask {
  return getTracker().attachTask(taskId, options);
}

export function globalReportToolCall(
  toolId: string,
  options: ToolCallOptions & { taskId?: string } = {},
): CostEvent {
  const tracker = getTracker();
  const current = getCurrentTask();
  const taskId = options.taskId ?? current?.taskId;
  if (taskId === undefined) {
    throw new Error("No active task — use track(), attachTask(), or provide taskId explicitly");
  }
  const { taskId: _ignored, ...callOptions } = options;
  if (current !== undefined && current.taskId.toLowerCase() === taskId.toLowerCase()) {
    return new TrackedTask(current, tracker.buffer, tracker, false, false, true)
      .recordToolCall(toolId, callOptions);
  }
  return tracker.attachTask(taskId).recordToolCall(toolId, callOptions);
}

export function globalStartTask(options: TaskOptions = {}): TrackedTask {
  return getTracker().startTask(options);
}

export function globalRecordCost(
  service: string,
  costUsd: DecimalLike,
  options: {
    eventType?: EventType;
    costConfidence?: CostConfidence;
    pricingSource?: PricingSource;
    pricingVersion?: string;
    details?: Record<string, unknown>;
    idempotencyKey?: string;
    capability?: CapabilityIdentity;
  } = {},
): CostEvent {
  const tracker = getTracker();
  const current = getCurrentTask();
  if (current === undefined) throw new Error("No active task — use track() or task() first");
  return new TrackedTask(current, tracker.buffer, tracker, false, false, true).recordCost(
    service,
    costUsd,
    options.details,
    options.eventType ?? "external_cost",
    options.costConfidence ?? "exact",
    options.pricingSource ?? "manual",
    options.pricingVersion,
    { idempotencyKey: options.idempotencyKey, capability: options.capability },
  );
}

export function globalRecordOutcome(
  name: string,
  options: Omit<OutcomeRevisionOptions, "name" | "taskId"> & { taskId?: string } = {},
): OutcomeRevision {
  const taskId = options.taskId ?? getCurrentTask()?.taskId;
  if (taskId === undefined) throw new Error("No active task — use track(), task(), or provide taskId");
  return getTracker().recordOutcome(name, { ...options, taskId });
}

export function globalGetOutcomeHistory(outcomeId: string): Array<Record<string, unknown>> {
  return getTracker().getOutcomeHistory(outcomeId);
}

export function globalRecordRevenue(
  amount?: RevenueInput,
  options: Omit<RevenueRevisionOptions, "amount" | "taskId"> &
    { taskId?: string; currency?: string } = { state: "recognized" },
): RevenueRevision {
  const taskId = options.taskId ?? getCurrentTask()?.taskId;
  if (taskId === undefined) throw new Error("No active task — use track(), task(), or provide taskId");
  return getTracker().recordRevenue(amount, { ...options, taskId });
}

export function globalGetRevenueHistory(revenueId: string): Array<Record<string, unknown>> {
  return getTracker().getRevenueHistory(revenueId);
}

export function globalExplainPricing(eventOrId: CostEvent | string): PricingExplanation {
  return getTracker().explainPricing(eventOrId);
}

export async function globalInstrument(name: string): Promise<void> {
  return getTracker().instrument(canonicalInstrumentName(name));
}

export function globalUninstrument(name: string): void {
  getTracker().uninstrument(canonicalInstrumentName(name));
}

export function globalTrackTool<F extends (this: unknown, ...args: any[]) => any>(
  toolId: string,
  options: TrackToolOptions = {},
): (fn: F) => F {
  return (fn: F): F => function (this: unknown, ...args: Parameters<F>): ReturnType<F> {
    let decorated: F;
    try { decorated = getTracker().trackTool<F>(toolId, options)(fn); }
    catch { return fn.apply(this, args); }
    return decorated.apply(this, args);
  } as F;
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
  /** Field names to redact from event details and logical billing-dimension keys. */
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
   * Optional URL for a conformant HTTP service catalog. Cloud mode defaults
   * to the control-plane catalog endpoint.
   */
  serviceCatalogUrl?: string;
  /** Enable the atomic seven-artifact catalog release runtime (default true). */
  catalogReleases?: boolean;
  /** Durable active/previous LKG store path; defaults beside dbPath or under ~/.dexcost. */
  catalogReleaseStorePath?: string;
  /** Stable by default; canary is intended for controlled validation only. */
  catalogChannel?: "stable" | "canary";
  /** Background release refresh interval. Defaults to 24 hours. */
  catalogRefreshIntervalMs?: number;
  /** Random refresh spread from 0 to 0.5. Defaults to 0.1. */
  catalogRefreshJitterRatio?: number;
  /**
   * Ed25519 catalog public keys by manifest key_id (raw 32-byte base64url).
   * Falls back to strict JSON in `DEXCOST_CATALOG_TRUSTED_KEYS` when omitted.
   */
  catalogTrustedKeys?: Readonly<Record<string, string>>;
  /**
   * Reject unsigned catalog releases and unsigned durable cache entries.
   * Defaults to true when trusted keys exist and false otherwise; the
   * `DEXCOST_CATALOG_REQUIRE_SIGNATURE` environment value may be true/false.
   */
  catalogRequireSignature?: boolean;
  /** Catalog manifest/artifact request timeout. Defaults to 10000 ms; max 60000. */
  catalogTimeoutMs?: number;
  /** Optional explicit path to a versioned rates.yaml file (v1 or v2). */
  ratesPath?: string;

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

export function globalDeliveryStatus(): DeliveryStatus {
  return _instance?.deliveryStatus() ?? localDeliveryStatus();
}

export function globalCatalogStatus(): CatalogRuntimeStatus {
  return _instance?.catalogStatus ?? {
    source: "bootstrap", stale: false, overlayActive: false, overlayOverrideCount: 0,
  };
}

export function globalImportCatalogBundle(bundle: Uint8Array): CatalogRuntimeStatus {
  if (!_instance) throw new Error("init() must be called before importing a catalog bundle");
  return _instance.importCatalogBundle(bundle);
}

export function globalExportCatalogBundle(
  source: "active" | "previous" = "active",
): Uint8Array {
  if (!_instance) throw new Error("init() must be called before exporting a catalog bundle");
  return _instance.exportCatalogBundle(source);
}

export interface TaskOptions {
  taskType?: string;
  customerId?: string;
  projectId?: string;
  userId?: string;
  productId?: string;
  metadata?: Record<string, unknown>;
  experimentId?: string;
  variant?: string;
  taskId?: string;
  rootTaskId?: string;
  parentTaskId?: string;
  agentId?: string;
  agentVersion?: string;
  workflowId?: string;
  workflowSessionId?: string;
  trackGpu?: boolean;
}

export interface ToolCallOptions {
  operation?: string;
  status?: ToolOperationStatus;
  durationMs?: number;
  usage?: ToolUsage;
  costUsd?: ToolCostInput;
  provider?: string;
  providerRecordId?: string;
  errorType?: string;
  errorCode?: string | number;
  dimensions?: Record<string, ToolDimensionInput>;
  operationId?: string;
  attemptId?: string;
  attemptNumber?: number;
  retryOf?: string;
  idempotencyKey?: string;
  capability?: CapabilityIdentity;
}

export type TrackToolOptions = Pick<ToolCallOptions,
  "operation" | "usage" | "costUsd" | "provider" | "dimensions" | "capability">;

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
  private _ownsTask: boolean;
  private _persistTaskRollup: boolean;

  constructor(
    task: Task,
    buffer: EventBuffer,
    tracker: CostTracker,
    trackGpu = false,
    ownsTask = true,
    persistTaskRollup = ownsTask,
  ) {
    this._task = task;
    this._buffer = buffer;
    this._tracker = tracker;
    this._ownsTask = ownsTask;
    this._persistTaskRollup = persistTaskRollup;
    // Register a NetworkAccountant for this task so the patched
    // globalThis.fetch (which sees only the task_id via AsyncLocalStorage)
    // can record byte usage via core.getAccountant(taskId).
    // Unregistered in end().
    if (ownsTask) registerAccountant(task.taskId, new NetworkAccountant());
    if (trackGpu) {
      try {
        const cloudEnv = getCloudEnv();
        const runtime = resolveGpuRuntime({ getCloudEnv: () => cloudEnv });
        if (runtime === GpuRuntimeKind.LocalGpu) {
          const accountant = new GpuAccountant(runtime, cloudEnv);
          accountant.snapshotStart();
          (task as any)._gpu = accountant;
        }
      } catch {
        // Optional instrumentation is fail-open.
      }
    }
  }

  /** The underlying Task data. */
  get task(): Task {
    return this._task;
  }

  /** All events recorded against this task. */
  get events(): ReadonlyArray<CostEvent> {
    return this._events;
  }

  private _persistTask(): void {
    if (this._persistTaskRollup) this._buffer.upsertTask(this._task);
  }

  private _stampAmbient(event: CostEvent, capability?: CapabilityIdentity, idempotencyKey?: string): void {
    applyEventCapability(event, capability ?? getCapability());
    applyEventIdempotency(event, idempotencyKey);
  }

  /** Persist retry-root identity so later background conversion never guesses lineage. */
  private _stampRetryLineage(event: CostEvent): void {
    if (!event.isRetry || typeof event.retryOf !== "string" || event.retryOf.length === 0) return;

    const priorEvents = new Map<string, CostEvent>();
    try {
      for (const prior of this._buffer.queryEvents(event.taskId)) {
        priorEvents.set(prior.eventId, prior);
      }
    } catch {
      // Instrumentation remains fail-open; in-memory history is still usable.
    }
    for (const prior of this._events) priorEvents.set(prior.eventId, prior);

    let operationId = event.retryOf;
    let attemptNumber = 2;
    let prior = priorEvents.get(event.retryOf);
    const visited = new Set<string>([event.eventId]);
    while (prior !== undefined && !visited.has(prior.eventId)) {
      visited.add(prior.eventId);
      const storedOperationId = prior.details.attribution_operation_id;
      const storedAttemptNumber = prior.details.attribution_attempt_number;
      const hasStoredOperationId = typeof storedOperationId === "string" &&
        storedOperationId.length > 0;
      if (hasStoredOperationId) operationId = storedOperationId;
      if (typeof storedAttemptNumber === "number" &&
          Number.isInteger(storedAttemptNumber) && storedAttemptNumber > 0) {
        attemptNumber = storedAttemptNumber + 1;
        break;
      }
      if (typeof prior.retryOf !== "string" || prior.retryOf.length === 0) {
        if (!hasStoredOperationId) operationId = prior.eventId;
        break;
      }
      if (!hasStoredOperationId) operationId = prior.retryOf;
      attemptNumber += 1;
      prior = priorEvents.get(prior.retryOf);
    }

    const explicitOperationId = event.details.attribution_operation_id;
    const explicitAttemptNumber = event.details.attribution_attempt_number;
    event.details = {
      ...event.details,
      attribution_operation_id: typeof explicitOperationId === "string"
        ? explicitOperationId
        : operationId,
      attribution_attempt_number: typeof explicitAttemptNumber === "number" &&
        Number.isInteger(explicitAttemptNumber) && explicitAttemptNumber > 0
        ? explicitAttemptNumber
        : attemptNumber,
    };
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
      idempotencyKey?: string;
      capability?: CapabilityIdentity;
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

    this._stampRetryLineage(event);
    this._stampAmbient(event, options.capability, options.idempotencyKey);

    // Persist only after the retry fields have been finalised on `event`.
    const inserted = this._buffer.addEvent(event);
    if (!inserted) {
      if (event.isRetry) {
        this._task.retryCount = Math.max(0, this._task.retryCount - 1);
        const rolledBack = this._task.retryCostUsd.minus(costUsd);
        this._task.retryCostUsd = rolledBack.lt(0) ? new Decimal(0) : rolledBack;
      }
      return event;
    }
    this._events.push(event);
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

    this._persistTask();

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
    pricingVersion?: string,
    attribution: { idempotencyKey?: string; capability?: CapabilityIdentity } = {},
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
    this._stampAmbient(event, attribution.capability, attribution.idempotencyKey);

    if (!this._buffer.addEvent(event)) return event;
    this._events.push(event);
    logEvent(event, this._task.taskType);

    // Aggregate into task
    if (eventType === "external_cost") {
      this._task.externalCostUsd = decAdd(this._task.externalCostUsd, costUsd);
    } else if (eventType === "compute_cost") {
      this._task.computeCostUsd = decAdd(this._task.computeCostUsd, costUsd);
    }
    this._task.totalCostUsd = decAdd(this._task.totalCostUsd, costUsd);

    this._persistTask();
    return event;
  }

  /** Record a privacy-safe tool invocation without capturing its inputs or outputs. */
  recordToolCall(
    toolId: string,
    options: ToolCallOptions = {},
  ): CostEvent {
    if (toolId.trim() !== toolId || toolId.length < 1 || toolId.length > 256) {
      throw new Error("toolId must contain 1 to 256 characters");
    }
    const operation = options.operation ?? "execute";
    const operationName = `tool.${operation}`;
    if (!/^[a-z0-9][a-z0-9._-]{0,127}$/.test(operationName)) {
      throw new Error("tool operation must be a canonical lowercase identifier");
    }
    const status = options.status ?? "succeeded";
    if (!["succeeded", "failed", "cancelled", "unknown"].includes(status)) {
      throw new Error(`unsupported tool status ${String(status)}`);
    }
    const durationMs = options.durationMs ?? 0;
    if (!Number.isInteger(durationMs) || durationMs < 0 || durationMs > 86_400_000) {
      throw new Error("durationMs must be an integer between 0 and 86400000");
    }
    const rawCost = options.costUsd ?? 0;
    if (typeof rawCost === "number" && !Number.isSafeInteger(rawCost)) {
      throw new TypeError("tool cost must be a Decimal, safe integer, bigint, or decimal string");
    }
    let exactCost: Decimal;
    try { exactCost = rawCost instanceof Decimal ? rawCost : new Decimal(String(rawCost)); }
    catch { throw new Error("tool cost is not a plain decimal"); }
    if (!exactCost.isFinite() || exactCost.lt(0)) {
      throw new Error("tool cost must be finite and non-negative");
    }
    if (status === "succeeded" && options.errorType !== undefined) {
      throw new Error("a succeeded tool call cannot carry an error");
    }
    if (options.providerRecordId !== undefined &&
        (options.providerRecordId.length < 1 || options.providerRecordId.length > 256)) {
      throw new Error("providerRecordId must contain 1 to 256 characters");
    }
    const attemptNumber = options.attemptNumber ?? 1;
    if (!Number.isInteger(attemptNumber) || attemptNumber < 1 || attemptNumber > 2_147_483_647) {
      throw new Error("attemptNumber must be between 1 and 2147483647");
    }
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    for (const [name, value] of [
      ["attemptId", options.attemptId], ["operationId", options.operationId], ["retryOf", options.retryOf],
    ] as const) if (value !== undefined && !uuid.test(value)) throw new Error(`${name} must be a valid UUID`);
    if (attemptNumber === 1 && options.retryOf !== undefined) throw new Error("attempt 1 cannot retry another attempt");
    if (attemptNumber > 1 && options.retryOf === undefined) throw new Error("later attempts require retryOf");
    const attemptId = (options.attemptId ?? randomUUID()).toLowerCase();
    const operationId = (options.operationId ?? attemptId).toLowerCase();
    const retryOf = options.retryOf?.toLowerCase();
    if (retryOf === attemptId) throw new Error("a tool attempt cannot retry itself");
    const meter = options.usage ?? new ToolUsage();
    if (!(meter instanceof ToolUsage)) throw new TypeError("usage must be a ToolUsage");
    if (Object.keys(options.dimensions ?? {}).length > 24) {
      throw new Error("tool dimensions support at most 24 entries");
    }
    const dimensions = Object.entries(options.dimensions ?? {}).map(([key, raw]) => {
      if (!/^[a-z0-9][a-z0-9._-]{0,127}$/.test(key)) {
        throw new Error(`tool dimension ${key} must be a canonical identifier`);
      }
      const value = outcomeValue(raw);
      if (value.type === "string" && String(value.value).length > 256) {
        throw new Error(`tool dimension ${key} string exceeds 256 characters`);
      }
      return { key, value };
    }).sort((left, right) => left.key.localeCompare(right.key));
    const details: Record<string, unknown> = {
      attribution_operation_id: operationId,
      attribution_attempt_id: attemptId,
      attribution_attempt_number: attemptNumber,
      attribution_operation_name: operationName,
      attribution_operation_status: status,
      attribution_resource_type: "tool",
      attribution_resource_id: toolId,
      attribution_usage_metric: meter.metric,
      attribution_usage_quantity: canonicalDecimal(meter.quantity),
      attribution_usage_unit: meter.unit,
      attribution_usage_duration_seconds: canonicalDecimal(new Decimal(durationMs).div(1000)),
      attribution_dimensions: dimensions,
    };
    if (options.providerRecordId !== undefined) details["provider_record_id"] = options.providerRecordId;
    if (options.errorType !== undefined) details["attribution_error_type"] = options.errorType;
    if (options.errorCode !== undefined) details["attribution_error_code"] = String(options.errorCode);
    const event = createCostEvent({
      eventId: attemptId, taskId: this._task.taskId, eventType: "external_cost",
      costUsd: exactCost, costConfidence: "exact", pricingSource: "manual",
      provider: options.provider ?? "tool", serviceName: toolId, latencyMs: durationMs,
      isRetry: retryOf !== undefined, retryOf,
      retryReason: options.errorType, details,
    });
    this._stampAmbient(
      event,
      options.capability ?? getCapability() ?? defaultToolCapability(toolId),
      options.idempotencyKey,
    );
    if (!this._buffer.addEvent(event)) return event;
    this._events.push(event);
    this._task.externalCostUsd = decAdd(this._task.externalCostUsd, event.costUsd);
    this._task.totalCostUsd = decAdd(this._task.totalCostUsd, event.costUsd);
    if (event.isRetry) {
      this._task.retryCount += 1;
      this._task.retryCostUsd = decAdd(this._task.retryCostUsd, event.costUsd);
    }
    this._persistTask();
    return event;
  }

  /** Decorate a sync, Promise, generator, or async-generator tool on this task. */
  trackTool<F extends (this: unknown, ...args: any[]) => any>(
    toolId: string,
    options: TrackToolOptions = {},
  ): (fn: F) => F {
    return (fn: F): F => decorateTool(fn, {
      begin: () => ({ capability: options.capability ?? getCapability() }),
      run: (_state, action) => runWithTask(this._task, action),
      finish: (state, status, durationMs, error) => {
        this.recordToolCall(toolId, {
          ...options, status, durationMs, capability: state.capability,
          errorType: error instanceof Error ? error.name : error === undefined ? undefined : typeof error,
        });
      },
    });
  }

  /** Run work inside this task identity; useful for non-owning attachments. */
  run<T>(fn: () => T): T {
    return runWithTask(this._task, fn);
  }

  recordOutcome(
    name: string,
    options: Omit<ConstructorParameters<typeof OutcomeRevision>[0], "taskId" | "name"> = {},
  ): OutcomeRevision {
    return this._tracker.recordOutcome(name, { ...options, taskId: this._task.taskId });
  }

  recordRevenue(
    amount?: RevenueInput,
    options: Omit<ConstructorParameters<typeof RevenueRevision>[0], "taskId" | "amount"> &
      { currency?: string } = { state: "recognized" },
  ): RevenueRevision {
    return this._tracker.recordRevenue(amount, { ...options, taskId: this._task.taskId });
  }

  recordProviderJob(options: Omit<ProviderJobRevisionOptions, "taskId">): ProviderJobRevision {
    return this._tracker.recordProviderJob({ ...options, taskId: this._task.taskId });
  }

  explainPricing(eventOrId: CostEvent | string): PricingExplanation {
    const explanation = this._tracker.explainPricing(eventOrId);
    if (explanation.taskId !== this._task.taskId) {
      throw new Error(`event ${explanation.eventId} does not belong to task ${this._task.taskId}`);
    }
    return explanation;
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

    this._stampRetryLineage(event);
    this._stampAmbient(event);

    if (!this._buffer.addEvent(event)) return event;
    this._events.push(event);
    logEvent(event, this._task.taskType);

    // Aggregate into task
    this._task.retryCount += 1;
    this._task.retryCostUsd = decAdd(this._task.retryCostUsd, costUsd);
    this._task.totalCostUsd = decAdd(this._task.totalCostUsd, costUsd);

    this._persistTask();
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
    this._persistTask();
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
    if (!this._ownsTask) {
      throw new Error("Attached task handles do not own task lifecycle and cannot end the task");
    }
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

    this._persistTask();
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
      GpuRuntimeKind.LocalGpu,
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
      let customRate = undefined;
      if (details.billing_model === "local_gpu_usage_only") {
        for (const key of [details.gpu_sku, details._nvml_product_name_lower]) {
          if (typeof key !== "string" || !key.trim()) continue;
          customRate = this._tracker.rateRegistry.getInfrastructure("gpu", key);
          if (customRate !== undefined) break;
        }
      }
      let gpuSeconds: Decimal | undefined;
      if (customRate !== undefined) {
        try {
          gpuSeconds = new Decimal(String(details.gpu_seconds_used ?? "0"));
          if (!gpuSeconds.isFinite() || gpuSeconds.lte(0)) customRate = undefined;
        } catch {
          customRate = undefined;
        }
      }
      if (customRate !== undefined && gpuSeconds !== undefined) {
        const ratePerSecond = customRate.per === "gpu_hour"
          ? customRate.costUsd.dividedBy(3600)
          : customRate.costUsd;
        ev.costUsd = gpuSeconds.times(ratePerSecond);
        ev.pricingSource = "rate_registry";
        ev.costConfidence = "computed";
        ev.pricingVersion = this._tracker.rateRegistry.pricingVersion;
      } else {
        const priced = engine.resolveGpuCost(
          details as Record<string, any>,
          cloudEnv,
          windowS,
        );
        ev.costUsd = priced.costUsd;
        ev.pricingSource = priced.pricingSource as any;
        ev.costConfidence = priced.costConfidence;
        ev.pricingVersion = details.billing_model === "local_gpu_usage_only"
          ? undefined
          : `gpu:${engine.catalogVersion}`;
      }
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

      const delta = ev.costUsd.minus(oldCost);
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
    finalizeTaskNetwork(this._task, this._buffer, this._tracker.rateRegistry);
  }

  /**
   * Record a usage event priced via the rate registry.
   */
  recordUsage(service: string, units: number = 1, details?: Record<string, unknown>): CostEvent {
    const rateEntry = this._tracker.rateRegistry.get(service);
    if (rateEntry === undefined) {
      throw new Error(
        `No rate registered for service "${service}". Use tracker.registerRate("${service}", per, costUsd) first.`
      );
    }
    const costUsd = toDecimal(rateEntry.costUsd).times(units);
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
      details: {
        ...(details ?? {}),
        attribution_usage_quantity: units,
        attribution_usage_per: rateEntry.per,
      },
    });
    if (!this._buffer.addEvent(event)) return event;
    this._events.push(event);
    logEvent(event, this._task.taskType);
    this._task.externalCostUsd = decAdd(this._task.externalCostUsd, costUsd);
    this._task.totalCostUsd = decAdd(this._task.totalCostUsd, costUsd);
    this._persistTask();
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
    target.details = {
      ...target.details,
      attribution_operation_id: target.eventId,
      attribution_attempt_number: 1,
    };
    this._task.retryCount = Math.max(0, this._task.retryCount - 1);
    const reversed = this._task.retryCostUsd.minus(target.costUsd);
    this._task.retryCostUsd = reversed.lt(0) ? new Decimal(0) : reversed;
    this._buffer.updateEvent(target);
    this._persistTask();
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
  private _catalogRuntime: CatalogRuntime | null = null;
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

    // Validate customer-owned pricing before opening storage or starting a
    // background worker. Invalid YAML cannot leave a partial tracker alive.
    this._rateRegistry = new RateRegistry();
    if (this._options.ratesPath !== undefined) {
      this._rateRegistry.load(this._options.ratesPath);
    }

    // Resolve API key (explicit arg → DEXCOST_API_KEY env var) and storage
    // mode. Throws InvalidAPIKeyError for a malformed key.
    // Debug mode: explicit option wins; otherwise DEXCOST_DEBUG decides.
    if (options.debug !== undefined) {
      setDebugMode(options.debug);
    }

    this._config = resolveConfig(this._options.apiKey, this._options.storage);
    // Use the resolved key everywhere downstream (env-var fallback included).
    this._options.apiKey = this._config.apiKey;

    // Security configuration is resolved before opening storage or starting
    // workers. Invalid trust settings are startup errors, not a reason to
    // silently disable catalog signature enforcement.
    const catalogTrustPolicy = this._options.catalogReleases === false
      ? undefined
      : resolveCatalogTrustPolicy(
        this._options.catalogTrustedKeys,
        this._options.catalogRequireSignature,
      );

    this._buffer = new EventBuffer(this._options.dbPath);
    setNetworkRateRegistry(this._buffer, this._rateRegistry);

    // Dev mode detection
    const env = options?.environment ?? process.env.DEXCOST_ENV;
    if (env === "development") {
      enableDevMode();
    }

    // Endpoint comes ONLY from the explicit in-code option (or the hardcoded
    // default) — never from the process env. Threaded to both consumers below:
    // the pusher (telemetry POST) and the pricing refresher.
    const endpoint = resolveEndpoint(this._options.endpoint);
    const cloudMode = this._config.storageMode === "cloud" && !isDevMode();
    // The legacy single service-catalog URL remains an explicit compatibility
    // escape hatch. Normal cloud operation uses one atomic seven-artifact release.
    const serviceCatalogUrl = this._options.serviceCatalogUrl;
    let serviceCatalogApiKey: string | undefined;
    if (serviceCatalogUrl && this._config.apiKey) {
      try {
        if (new URL(serviceCatalogUrl).origin === new URL(endpoint).origin) {
          serviceCatalogApiKey = this._config.apiKey;
        }
      } catch {
        // Invalid custom URLs fail open during refresh and never receive credentials.
      }
    }

    // The SDK's own traffic (pusher, pricing refresh, catalog refresh)
    // must be invisible to capture — register the hosts it talks to
    // BEFORE HTTP tracking patches fetch.
    try {
      _adapterRegisterInternalHost(new URL(endpoint).hostname);
    } catch {
      // endpoint already validated by resolveEndpoint; never fatal here
    }
    if (serviceCatalogUrl) {
      try {
        _adapterRegisterInternalHost(new URL(serviceCatalogUrl).hostname);
      } catch {
        // invalid catalog URL fails later in refresh; not fatal here
      }
    }

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

    if (this._options.catalogReleases !== false) {
      try {
        this._catalogRuntime = new CatalogRuntime({
          pricing: this._pricing,
          replaceCompute: (engine) => { this._computePricing = engine; },
          replaceGpu: (engine) => { this._gpuPricing = engine; },
          trackHttp: this._options.trackHttp !== false,
        }, {
          endpoint,
          storePath: this._options.catalogReleaseStorePath
            ?? (this._options.dbPath ? `${this._options.dbPath}.catalog-releases.json` : undefined),
          apiKey: this._config.apiKey,
          refreshIntervalMs: this._options.catalogRefreshIntervalMs,
          refreshJitterRatio: this._options.catalogRefreshJitterRatio,
          channel: this._options.catalogChannel,
          trustedKeys: catalogTrustPolicy?.trustedKeys,
          requireSignature: catalogTrustPolicy?.requireSignature,
          timeoutMs: this._options.catalogTimeoutMs,
        });
        // Durable LKG is applied synchronously before any provider is patched.
        this._catalogRuntime.loadCached();
        if (cloudMode) this._catalogRuntime.start();
      } catch (error) {
        debugLog("catalog", `catalog release runtime disabled: ${String(error)}`);
        this._catalogRuntime = null;
      }
    }

    // Compatibility fallback for installations that explicitly disable the
    // atomic release contract.
    if (cloudMode && this._config.apiKey && this._catalogRuntime === null) {
      this._pricing.setApiKey(this._config.apiKey);
      this._pricing.startBackgroundRefresh(endpoint);
    }

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
      this._enableHttpTracking(serviceCatalogUrl, serviceCatalogApiKey);
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
   * refresh the service catalog from the configured control-plane envelope.
   */
  private _enableHttpTracking(serviceCatalogUrl?: string, serviceCatalogApiKey?: string): void {
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
          void catalog.refreshFromUrl(serviceCatalogUrl, serviceCatalogApiKey).catch(() => {
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

  get catalogStatus(): CatalogRuntimeStatus | null {
    return this._catalogRuntime?.status() ?? null;
  }

  importCatalogBundle(bundle: Uint8Array): CatalogRuntimeStatus {
    if (this._catalogRuntime === null) {
      throw new Error("catalog releases are disabled for this tracker");
    }
    this._catalogRuntime.importBundle(bundle);
    return this._catalogRuntime.status();
  }

  exportCatalogBundle(source: "active" | "previous" = "active"): Uint8Array {
    if (this._catalogRuntime === null) {
      throw new Error("catalog releases are disabled for this tracker");
    }
    return this._catalogRuntime.exportBundle(source);
  }

  deliveryStatus(): DeliveryStatus {
    return this._pusher?.status() ?? localDeliveryStatus(this._buffer);
  }

  private _createTask(options: TaskOptions): Task {
    const current = getCurrentTask();
    const context = getContext();
    const taskId = options.taskId ?? randomUUID();
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    for (const [name, value] of [
      ["taskId", taskId], ["rootTaskId", options.rootTaskId], ["parentTaskId", options.parentTaskId],
    ] as const) if (value !== undefined && !uuid.test(value)) throw new Error(`${name} must be a UUID`);

    const inheritsCurrent = options.parentTaskId === undefined && current !== undefined;
    const parentTaskId = options.parentTaskId ?? current?.taskId;
    let rootTaskId = options.rootTaskId ?? (inheritsCurrent ? current?.rootTaskId : undefined);
    const customerId = options.customerId ?? (inheritsCurrent ? current?.customerId : undefined) ?? context?.customerId;
    const projectId = options.projectId ?? (inheritsCurrent ? current?.projectId : undefined) ?? context?.projectId;
    const userId = options.userId ?? (inheritsCurrent ? current?.userId : undefined) ?? context?.userId;
    const productId = options.productId ?? (inheritsCurrent ? current?.productId : undefined) ?? context?.productId;
    const experimentId = options.experimentId ?? (inheritsCurrent ? current?.experimentId : undefined);
    const variant = options.variant ?? (inheritsCurrent ? current?.variant : undefined);
    const agentId = options.agentId ?? (inheritsCurrent ? current?.agentId : undefined) ?? context?.agent;
    const agentVersion = options.agentVersion ?? (inheritsCurrent ? current?.agentVersion : undefined) ?? context?.agentVersion;
    const workflowId = options.workflowId ?? (inheritsCurrent ? current?.workflowId : undefined) ?? context?.workflowId;
    const workflowSessionId = options.workflowSessionId ??
      (inheritsCurrent ? current?.workflowSessionId : undefined) ?? context?.workflowSessionId;
    if ((agentId === undefined) !== (agentVersion === undefined)) {
      throw new Error("agentId and agentVersion must be supplied together");
    }
    if (workflowSessionId !== undefined && workflowId === undefined) {
      throw new Error("workflowSessionId requires workflowId");
    }
    const hasBusinessIdentity = [
      customerId, projectId, userId, productId, experimentId, variant, agentId, workflowId,
    ].some((value) => value !== undefined);
    if (rootTaskId === undefined && parentTaskId === undefined && hasBusinessIdentity) rootTaskId = taskId;
    const taskType = options.taskType ?? "";
    if (rootTaskId !== undefined) {
      if (!/^[a-z0-9][a-z0-9._-]{0,127}$/.test(taskType)) {
        throw new Error("business-attributed taskType must be canonical lowercase");
      }
      if (variant !== undefined && experimentId === undefined) throw new Error("variant requires experimentId");
      if (parentTaskId === undefined && rootTaskId !== taskId) {
        throw new Error("a root task must use its own taskId as rootTaskId");
      }
      if (parentTaskId === taskId || (parentTaskId !== undefined && rootTaskId === taskId)) {
        throw new Error("a child task cannot be its own root or parent");
      }
      for (const [name, value] of Object.entries({ customerId, projectId, userId, productId,
        experimentId, variant, agentId, agentVersion, workflowId, workflowSessionId })) {
        if (value !== undefined && (value.trim() !== value || value.length < 1 || value.length > 256)) {
          throw new Error(`${name} must contain 1 to 256 characters`);
        }
      }
    }
    return createTask({
      taskId, taskType, customerId, projectId, userId, productId,
      metadata: { ...(context?.metadata ?? {}), ...(options.metadata ?? {}) },
      parentTaskId, rootTaskId, agentId, agentVersion, workflowId, workflowSessionId,
      experimentId, variant,
    });
  }

  /** Register a per-unit rate for a named service. */
  registerRate(service: string, per: string, costUsd: DecimalLike): void {
    this._rateRegistry.register(service, per, costUsd);
  }

  /** Get the per-unit cost (in USD) for a named service, or undefined if not registered. */
  getRate(service: string): Decimal | undefined {
    return this._rateRegistry.get(service)?.costUsd;
  }

  /** Register an explicit user-owned GPU or network rate. */
  registerInfrastructureRate(
    kind: string,
    key: string,
    per: string,
    costUsd: DecimalLike,
  ): void {
    this._rateRegistry.registerInfrastructure(kind, key, per, costUsd);
  }

  /** Get an exact normalized user-owned infrastructure rate. */
  getInfrastructureRate(kind: string, key: string): Decimal | undefined {
    return this._rateRegistry.getInfrastructure(kind, key)?.costUsd;
  }

  /** Atomically merge rates from a versioned YAML file. */
  loadRates(path: string): void {
    this._rateRegistry.load(path);
  }

  /** Export a deterministic version-2 rates.yaml snapshot. */
  exportRates(path: string): void {
    this._rateRegistry.export(path);
  }

  recordOutcome(
    name: string,
    options: Omit<OutcomeRevisionOptions, "name">,
  ): OutcomeRevision {
    const revision = new OutcomeRevision({ ...options, name });
    this._buffer.insertOutcomeRevision(revision);
    return revision;
  }

  getOutcomeHistory(outcomeId: string): Array<Record<string, unknown>> {
    return this._buffer.getOutcomeHistory(outcomeId);
  }

  recordRevenue(
    amount: RevenueInput | undefined,
    options: Omit<RevenueRevisionOptions, "amount"> & { currency?: string },
  ): RevenueRevision {
    const revision = new RevenueRevision({
      ...options,
      source: options.source ?? { type: "sdk" },
      amount: amount === undefined ? undefined : revenueAmount(amount, options.currency ?? "USD"),
    });
    this._buffer.insertRevenueRevision(revision);
    return revision;
  }

  getRevenueHistory(revenueId: string): Array<Record<string, unknown>> {
    return this._buffer.getRevenueHistory(revenueId);
  }

  recordProviderJob(options: ProviderJobRevisionOptions): ProviderJobRevision {
    const revision = new ProviderJobRevision(options);
    this._buffer.insertProviderJobRevision(revision);
    return revision;
  }

  getProviderJob(provider: string, service: string, recordId: string): Record<string, unknown> | undefined {
    return this._buffer.getProviderJob(provider, service, recordId);
  }

  getProviderJobHistory(eventId: string): Array<Record<string, unknown>> {
    return this._buffer.getProviderJobHistory(eventId);
  }

  explainPricing(eventOrId: CostEvent | string): PricingExplanation {
    if (typeof eventOrId !== "string") return explainEventPricing(eventOrId);
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(eventOrId)) {
      throw new Error("eventId must be a valid UUID");
    }
    const event = this._buffer.getEvent(eventOrId);
    if (event === undefined) throw new Error(`event ${eventOrId} was not found in local storage`);
    return explainEventPricing(event);
  }

  /** Decorate any JavaScript execution shape with content-free tool metering. */
  trackTool<F extends (this: unknown, ...args: any[]) => any>(
    toolId: string,
    options: TrackToolOptions = {},
  ): (fn: F) => F {
    type Invocation = { task: Task; auto: boolean; capability?: CapabilityIdentity };
    return (fn: F): F => decorateTool(fn, {
      begin: (): Invocation => {
        const current = getCurrentTask();
        const auto = current === undefined;
        const task = current ?? createAutoTask(toolTaskType(toolId));
        if (auto) this._buffer.upsertTask(task);
        return { task, auto, capability: options.capability ?? getCapability() };
      },
      run: (state, action) => runWithTask(state.task, action),
      finish: (state, status, durationMs, error) => {
        try {
          new TrackedTask(state.task, this._buffer, this, false, false, true).recordToolCall(toolId, {
            ...options, status, durationMs, capability: state.capability,
            errorType: error instanceof Error ? error.name : error === undefined ? undefined : typeof error,
          });
        } finally {
          if (state.auto) {
            finalizeAutoTask(
              state.task,
              status === "succeeded" ? "success" : "failed",
              this._buffer,
            );
          }
        }
      },
    });
  }

  /** Return a non-owning handle for cross-process work associated with an existing task UUID. */
  attachTask(
    taskId: string,
    options: { taskType?: string; rootTaskId?: string; parentTaskId?: string } = {},
  ): TrackedTask {
    const existing = this._buffer.getTask(taskId);
    if (existing !== undefined) {
      if (options.rootTaskId !== undefined && options.rootTaskId !== existing.rootTaskId) {
        throw new Error("attached rootTaskId does not match the locally stored task");
      }
      if (options.parentTaskId !== undefined && options.parentTaskId !== existing.parentTaskId) {
        throw new Error("attached parentTaskId does not match the locally stored task");
      }
      return new TrackedTask(existing, this._buffer, this, false, false);
    }
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    if (!uuid.test(taskId)) throw new Error("taskId must be a valid UUID");
    if (options.rootTaskId !== undefined && !uuid.test(options.rootTaskId)) {
      throw new Error("rootTaskId must be a valid UUID");
    }
    if (options.parentTaskId !== undefined && !uuid.test(options.parentTaskId)) {
      throw new Error("parentTaskId must be a valid UUID");
    }
    const taskType = options.taskType ?? "attached";
    if (taskType.trim().length === 0) throw new Error("taskType must be a non-empty string");
    const task = createTask({
      taskId: taskId.toLowerCase(), taskType,
      rootTaskId: options.rootTaskId?.toLowerCase(), parentTaskId: options.parentTaskId?.toLowerCase(),
    });
    return new TrackedTask(task, this._buffer, this, false, false);
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
   * Manually start a task and return a `TrackedTask` handle.
   *
   * Use this when callbacks/context managers don't fit your architecture
   * (e.g. Celery-style workers, multi-process pipelines). The caller
   * **must** call `TrackedTask.end()` when the task is complete.
   * Set `trackGpu: true` only on the leaf task that owns local NVIDIA GPU
   * work; owned hardware is emitted as usage-only, never a synthetic price.
   * Mirrors the Python SDK's `CostTracker.start_task`.
   */
  startTask(opts: TaskOptions = {}): TrackedTask {
    const task = this._createTask(opts);
    this._buffer.upsertTask(task);
    return new TrackedTask(task, this._buffer, this, opts.trackGpu === true);
  }

  /**
   * Execute `fn` inside a tracked task context.
   *
   * Creates a new task, runs the function within an AsyncLocalStorage
   * context, and ends the task on completion (or failure). Set
   * `trackGpu: true` only on the leaf task that owns local NVIDIA GPU work.
   */
  async track<T>(
    opts: TaskOptions & { taskType: string },
    fn: (task: TrackedTask) => Promise<T>
  ): Promise<T> {
    const task = this._createTask(opts);

    this._buffer.upsertTask(task);

    const trackedTask = new TrackedTask(task, this._buffer, this, opts.trackGpu === true);

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
    this._catalogRuntime?.setApiKey(newKey);
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
    this._catalogRuntime?.stop();
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
    this._catalogRuntime?.stop();
    this._buffer.close();
  }
}
