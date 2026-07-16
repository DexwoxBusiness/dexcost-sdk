/**
 * Core data models for dexcost TypeScript SDK.
 *
 * These interfaces match the Dexcost Standard Event Schema v1
 * and mirror the Python SDK's Task and Event dataclasses.
 */

import { Decimal as DecimalBase } from "decimal.js";

// Money fields are stored as exact decimals (decimal.js), never float64.
// We use a CLONED Decimal constructor — NOT `DecimalBase.set(...)` on the
// global — so the SDK's no-exponential config can never alter a consumer
// app that also depends on decimal.js. `toExpNeg`/`toExpPos` are pushed to
// the extremes so `.toString()` ALWAYS produces a plain decimal string —
// the canonical wire form: trailing zeros stripped, `"0"` for zero, full
// precision, no `e`.
//
//   new Decimal("0.0000000123").toString() === "0.0000000123"   (not 1.23e-8)
//   new Decimal("0.00").toString()         === "0"
//   new Decimal("2.00").toString()         === "2"
//
// Every `new Decimal` across the SDK (pricing engines, tracker, buffer)
// imports THIS constructor, so the no-exp guarantee holds everywhere a cost
// is serialized — without mutating any global state.
export const Decimal = DecimalBase.clone({ toExpNeg: -9e15, toExpPos: 9e15 });
export type Decimal = InstanceType<typeof Decimal>;

/** Anything that can be coerced to an exact decimal cost. */
export type DecimalLike = number | string | Decimal;

/**
 * Coerce a number / string / Decimal into an exact `Decimal`.
 *
 * Numbers are routed through `String(...)` first so we never inherit a
 * float64 artifact (mirrors Python's `Decimal(str(x))`). This is the single
 * chokepoint user-supplied costs pass through before the SDK stores or sums
 * them.
 */
export function toDecimal(value: DecimalLike): Decimal {
  if (value instanceof Decimal) return value;
  let d: Decimal;
  try {
    // Route numbers through String(...) so we never inherit a float64 artifact.
    d = typeof value === "number" ? new Decimal(String(value)) : new Decimal(value);
  } catch {
    // Unparseable input (e.g. "abc") — never throw from the cost-ingest path.
    console.warn(`dexcost: invalid cost value ${String(value)} — coercing to 0`);
    return new Decimal(0);
  }
  if (!d.isFinite()) {
    // NaN / Infinity must never reach the wire as "NaN"/"Infinity".
    console.warn(`dexcost: non-finite cost value ${String(value)} — coercing to 0`);
    return new Decimal(0);
  }
  return d;
}

/**
 * Render a `Decimal` as the canonical plain-decimal wire string: trailing
 * zeros stripped, `"0"` for zero, never scientific notation, full precision.
 * Single serialization chokepoint — `toDict` uses this instead of `String(...)`.
 */
export function canonicalDecimal(d: Decimal): string {
  return d.toString();
}

/** Decimal addition helper for cost accumulation (`a + b` with exactness). */
export function addCost(a: Decimal, b: DecimalLike): Decimal {
  return a.plus(toDecimal(b));
}

/**
 * Serialise a Date to the canonical wire format. Sprint 3 Theme F /
 * §4.1.1 (P1): RFC3339 with microsecond precision (6 fractional
 * digits) + "Z" suffix, matching the Python canonical.
 *
 * `Date.toISOString()` only gives 3-digit millisecond precision, so
 * we pad with three trailing zeros. Customer-visible Date objects
 * carry no sub-millisecond data anyway, so the pad is information-
 * preserving — it just aligns the wire string for cross-SDK parity.
 */
export function isoCanonical(d: Date): string {
  const iso = d.toISOString();
  // iso is `YYYY-MM-DDTHH:mm:ss.sssZ` (24 chars); pad to .sssNNNZ
  return iso.slice(0, -1) + "000Z";
}

/** Lifecycle status of a tracked task. */
export type TaskStatus = "pending" | "success" | "failed";

/** Discriminator for cost-generating events. */
export type EventType =
  | "llm_call"
  | "external_cost"
  | "compute_cost"
  | "retry_marker"
  | "network"
  | "gpu_cost"
  | "gpu_utilization_signal";

/** How trustworthy the reported costUsd value is. */
export type CostConfidence = "exact" | "computed" | "estimated" | "unknown";

/** Where the costUsd figure was derived from. */
/**
 * Sprint 3 Theme F / §4.1.3 (P3): canonical 8-value set aligned
 * across all 4 SDKs. Adding new values requires a coordinated wire-
 * contract change — bump schema_version.
 */
export type PricingSource =
  | "litellm"
  | "tokencost"
  | "provider_response"
  | "manual"
  | "custom"
  | "rate_registry"
  | "service_catalog"
  | "unknown"
  | `compute_catalog:${string}`
  | `gpu_catalog:${string}`
  | `egress_catalog:${string}`;

/**
 * A tracked business task (e.g., "resolve support ticket").
 *
 * All downstream events roll up into the aggregated cost and token fields.
 * `metadata` is an open record for caller-defined context.
 */
export interface Task {
  taskId: string;
  taskType: string;
  status: TaskStatus;
  startedAt: Date;
  endedAt?: Date;
  metadata: Record<string, unknown>;
  customerId?: string;
  projectId?: string;
  parentTaskId?: string;
  experimentId?: string;
  variant?: string;
  // Aggregated costs (exact decimals — never float64)
  llmCostUsd: Decimal;
  externalCostUsd: Decimal;
  computeCostUsd: Decimal;
  /**
   * v2 cloud-egress cost in USD, computed at task finalize from the
   * accountant's canonical external_bytes_out scalar. Distinct from
   * externalCostUsd (vendor API charges) — see Decision #7.
   */
  networkCostUsd: Decimal;
  /**
   * v2 GPU cost in USD, computed at task finalize from the GpuAccountant's
   * NVML diff (per-PID SM-time) + cgroup-walk PID filter. Distinct from
   * computeCostUsd (CPU/memory rollup) — GPU billing is a separate dimension.
   * Mirrors the Python SDK's Task.gpu_cost_usd field.
   */
  gpuCostUsd: Decimal;
  totalCostUsd: Decimal;
  totalInputTokens: number;
  totalOutputTokens: number;
  totalCachedTokens: number;
  retryCount: number;
  retryCostUsd: Decimal;
  failureCount: number;
  // Network capture aggregates (v1 — bytes only).
  networkBytesIn: number;
  networkBytesOut: number;
  networkCallCount: number;
  /**
   * Per-host network breakdown, shape `{ hosts: Array<...> }`. Capped at 20
   * entries plus an `_other` overflow bucket during finalize.
   */
  networkByHost: Record<string, unknown>;
  schemaVersion: string;
  /**
   * In-memory only. The per-task ComputeAccountant (cgroup start/end
   * snapshots + runtime context). Never serialized — the buffer's
   * upsertTask() writes named columns only, so this field cannot leak to
   * SQLite or the wire payload. Compatible with the Python SDK's
   * Task._compute pattern.
   *
   * Typed as unknown to avoid a circular import from core/compute-
   * accountant.ts (which imports from cgroup-reader / compute-runtime).
   */
  _compute?: unknown;
  /**
   * In-memory only. The per-task GpuAccountant (NVML start snapshot +
   * cgroup PID scope + per-device handles). Never serialized — same
   * contract as _compute. Mirrors Python's Task._gpu.
   */
  _gpu?: unknown;
}

/**
 * A single cost event (LLM call, external API, compute, retry).
 *
 * Matches the Dexcost Standard Event Schema v1.  LLM-specific fields are
 * undefined for non-LLM event types.
 */
export interface CostEvent {
  eventId: string;
  taskId: string;
  eventType: EventType;
  occurredAt: Date;
  costUsd: Decimal;
  costConfidence: CostConfidence;
  pricingSource?: PricingSource;
  pricingVersion?: string;
  provider?: string;
  model?: string;
  inputTokens?: number;
  outputTokens?: number;
  cachedTokens?: number;
  latencyMs?: number;
  serviceName?: string;
  isRetry: boolean;
  retryReason?: string;
  retryOf?: string;
  details: Record<string, unknown>;
  schemaVersion: string;
}

/**
 * Override map for `createTask`. Cost fields accept any `DecimalLike`
 * (`number | string | Decimal`) for caller ergonomics; they are coerced to
 * `Decimal` below so the resulting `Task` always holds exact decimals.
 */
type TaskOverrides = Partial<Omit<Task, TaskCostField>> &
  Partial<Record<TaskCostField, DecimalLike>> & { taskId: string };

type TaskCostField =
  | "llmCostUsd"
  | "externalCostUsd"
  | "computeCostUsd"
  | "networkCostUsd"
  | "gpuCostUsd"
  | "totalCostUsd"
  | "retryCostUsd";

const TASK_COST_FIELDS: readonly TaskCostField[] = [
  "llmCostUsd",
  "externalCostUsd",
  "computeCostUsd",
  "networkCostUsd",
  "gpuCostUsd",
  "totalCostUsd",
  "retryCostUsd",
];

/** Create a default Task with required fields. */
export function createTask(overrides: TaskOverrides): Task {
  const task: Task = {
    taskType: "",
    status: "pending",
    startedAt: new Date(),
    metadata: {},
    llmCostUsd: new Decimal(0),
    externalCostUsd: new Decimal(0),
    computeCostUsd: new Decimal(0),
    networkCostUsd: new Decimal(0),
    gpuCostUsd: new Decimal(0),
    totalCostUsd: new Decimal(0),
    totalInputTokens: 0,
    totalOutputTokens: 0,
    totalCachedTokens: 0,
    retryCount: 0,
    retryCostUsd: new Decimal(0),
    failureCount: 0,
    networkBytesIn: 0,
    networkBytesOut: 0,
    networkCallCount: 0,
    networkByHost: { hosts: [] },
    schemaVersion: "1",
    ...(overrides as Partial<Task> & { taskId: string }),
  };
  // Coerce any caller-supplied DecimalLike cost overrides to Decimal.
  for (const field of TASK_COST_FIELDS) {
    const raw = (overrides as Record<string, unknown>)[field];
    if (raw !== undefined) task[field] = toDecimal(raw as DecimalLike);
  }
  return task;
}

/**
 * Override map for `createCostEvent`. `costUsd` accepts any `DecimalLike`
 * for ergonomics and is coerced to `Decimal`.
 */
type CostEventOverrides = Partial<Omit<CostEvent, "costUsd">> & {
  eventId: string;
  taskId: string;
  costUsd?: DecimalLike;
};

/** Create a default CostEvent with required fields. */
export function createCostEvent(overrides: CostEventOverrides): CostEvent {
  const { costUsd, ...rest } = overrides;
  return {
    eventType: "llm_call",
    occurredAt: new Date(),
    costUsd: costUsd === undefined ? new Decimal(0) : toDecimal(costUsd),
    costConfidence: "exact",
    isRetry: false,
    details: {},
    schemaVersion: "1",
    ...(rest as Partial<CostEvent> & { eventId: string; taskId: string }),
  };
}

/**
 * Serialise a Task to a JSON-safe dictionary matching the Standard Event Schema v1.
 * Costs are serialised as strings to preserve precision.
 */
export function taskToDict(task: Task): Record<string, unknown> {
  return {
    task_id: task.taskId,
    task_type: task.taskType,
    status: task.status,
    started_at: isoCanonical(task.startedAt),
    ended_at: task.endedAt ? isoCanonical(task.endedAt) : null,
    metadata: task.metadata,
    customer_id: task.customerId ?? null,
    project_id: task.projectId ?? null,
    parent_task_id: task.parentTaskId ?? null,
    experiment_id: task.experimentId ?? null,
    variant: task.variant ?? null,
    llm_cost_usd: canonicalDecimal(task.llmCostUsd),
    external_cost_usd: canonicalDecimal(task.externalCostUsd),
    compute_cost_usd: canonicalDecimal(task.computeCostUsd),
    network_cost_usd: canonicalDecimal(task.networkCostUsd),
    gpu_cost_usd: canonicalDecimal(task.gpuCostUsd),
    total_cost_usd: canonicalDecimal(task.totalCostUsd),
    total_input_tokens: task.totalInputTokens,
    total_output_tokens: task.totalOutputTokens,
    total_cached_tokens: task.totalCachedTokens,
    retry_count: task.retryCount,
    retry_cost_usd: canonicalDecimal(task.retryCostUsd),
    failure_count: task.failureCount,
    network_bytes_in: task.networkBytesIn,
    network_bytes_out: task.networkBytesOut,
    network_call_count: task.networkCallCount,
    network_by_host: task.networkByHost,
    schema_version: task.schemaVersion,
  };
}

/**
 * Serialise a CostEvent to a JSON-safe dictionary matching the Standard Event Schema v1.
 * Costs are serialised as strings to preserve precision.
 */
export function eventToDict(event: CostEvent): Record<string, unknown> {
  return {
    event_id: event.eventId,
    task_id: event.taskId,
    event_type: event.eventType,
    occurred_at: isoCanonical(event.occurredAt),
    cost_usd: canonicalDecimal(event.costUsd),
    cost_confidence: event.costConfidence,
    pricing_source: event.pricingSource ?? null,
    pricing_version: event.pricingVersion ?? null,
    provider: event.provider ?? null,
    model: event.model ?? null,
    input_tokens: event.inputTokens ?? null,
    output_tokens: event.outputTokens ?? null,
    cached_tokens: event.cachedTokens ?? null,
    latency_ms: event.latencyMs ?? null,
    service_name: event.serviceName ?? null,
    is_retry: event.isRetry,
    retry_reason: event.retryReason ?? null,
    retry_of: event.retryOf ?? null,
    details: event.details,
    schema_version: event.schemaVersion,
  };
}

// ---------------------------------------------------------------------------
// Deserialisation helpers (inverse of taskToDict / eventToDict)
// ---------------------------------------------------------------------------

function _requireString(data: Record<string, unknown>, key: string): string {
  const value = data[key];
  if (typeof value !== "string") {
    throw new Error(`Invalid task/event data: missing or non-string field "${key}"`);
  }
  return value;
}

function _optString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function _toNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number") return value;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    if (!Number.isNaN(n)) return n;
  }
  return fallback;
}

/**
 * Parse a serialized cost field (canonical decimal string, or legacy
 * number) into an exact `Decimal`. Inverse of `canonicalDecimal`. Falls back
 * to `Decimal(0)` for missing / malformed input, matching the old
 * `_toNumber(...)` defaulting behaviour.
 */
function _toDecimal(value: unknown): Decimal {
  if (value instanceof Decimal) return value;
  if (typeof value === "number") {
    return Number.isFinite(value) ? new Decimal(String(value)) : new Decimal(0);
  }
  if (typeof value === "string" && value.trim() !== "") {
    try {
      return new Decimal(value);
    } catch {
      return new Decimal(0);
    }
  }
  return new Decimal(0);
}

/**
 * Deserialise a Task from a JSON-safe dictionary (inverse of `taskToDict`).
 *
 * Mirrors the Python SDK's `Task.from_dict`. Throws an Error when required
 * fields are missing or malformed.
 */
export function taskFromDict(data: Record<string, unknown>): Task {
  const startedAtRaw = _requireString(data, "started_at");
  const endedAtRaw = data["ended_at"];
  return {
    taskId: _requireString(data, "task_id"),
    taskType: typeof data["task_type"] === "string" ? data["task_type"] : "",
    status: _requireString(data, "status") as TaskStatus,
    startedAt: new Date(startedAtRaw),
    endedAt: typeof endedAtRaw === "string" ? new Date(endedAtRaw) : undefined,
    metadata:
      data["metadata"] && typeof data["metadata"] === "object"
        ? (data["metadata"] as Record<string, unknown>)
        : {},
    customerId: _optString(data["customer_id"]),
    projectId: _optString(data["project_id"]),
    parentTaskId: _optString(data["parent_task_id"]),
    experimentId: _optString(data["experiment_id"]),
    variant: _optString(data["variant"]),
    llmCostUsd: _toDecimal(data["llm_cost_usd"]),
    externalCostUsd: _toDecimal(data["external_cost_usd"]),
    computeCostUsd: _toDecimal(data["compute_cost_usd"]),
    networkCostUsd: _toDecimal(data["network_cost_usd"]),
    gpuCostUsd: _toDecimal(data["gpu_cost_usd"]),
    totalCostUsd: _toDecimal(data["total_cost_usd"]),
    totalInputTokens: _toNumber(data["total_input_tokens"]),
    totalOutputTokens: _toNumber(data["total_output_tokens"]),
    totalCachedTokens: _toNumber(data["total_cached_tokens"]),
    retryCount: _toNumber(data["retry_count"]),
    retryCostUsd: _toDecimal(data["retry_cost_usd"]),
    failureCount: _toNumber(data["failure_count"]),
    networkBytesIn: _toNumber(data["network_bytes_in"]),
    networkBytesOut: _toNumber(data["network_bytes_out"]),
    networkCallCount: _toNumber(data["network_call_count"]),
    networkByHost:
      data["network_by_host"] && typeof data["network_by_host"] === "object"
        ? (data["network_by_host"] as Record<string, unknown>)
        : { hosts: [] },
    schemaVersion: _optString(data["schema_version"]) ?? "1",
  };
}

/**
 * Deserialise a CostEvent from a JSON-safe dictionary (inverse of `eventToDict`).
 *
 * Mirrors the Python SDK's `Event.from_dict`. Throws an Error when required
 * fields are missing or malformed.
 */
export function eventFromDict(data: Record<string, unknown>): CostEvent {
  const occurredAtRaw = _requireString(data, "occurred_at");
  return {
    eventId: _requireString(data, "event_id"),
    taskId: _requireString(data, "task_id"),
    eventType: _requireString(data, "event_type") as EventType,
    occurredAt: new Date(occurredAtRaw),
    costUsd: _toDecimal(data["cost_usd"]),
    costConfidence: _requireString(data, "cost_confidence") as CostConfidence,
    pricingSource: _optString(data["pricing_source"]) as PricingSource | undefined,
    pricingVersion: _optString(data["pricing_version"]),
    provider: _optString(data["provider"]),
    model: _optString(data["model"]),
    inputTokens:
      typeof data["input_tokens"] === "number" ? data["input_tokens"] : undefined,
    outputTokens:
      typeof data["output_tokens"] === "number" ? data["output_tokens"] : undefined,
    cachedTokens:
      typeof data["cached_tokens"] === "number" ? data["cached_tokens"] : undefined,
    latencyMs:
      typeof data["latency_ms"] === "number" ? data["latency_ms"] : undefined,
    serviceName: _optString(data["service_name"]),
    isRetry: data["is_retry"] === true,
    retryReason: _optString(data["retry_reason"]),
    retryOf: _optString(data["retry_of"]),
    details:
      data["details"] && typeof data["details"] === "object"
        ? (data["details"] as Record<string, unknown>)
        : {},
    schemaVersion: _optString(data["schema_version"]) ?? "1",
  };
}
