/**
 * Core data models for dexcost TypeScript SDK.
 *
 * These interfaces match the Dexcost Standard Event Schema v1
 * and mirror the Python SDK's Task and Event dataclasses.
 */

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
export type PricingSource =
  | "litellm"
  | "tokencost"
  | "provider_response"
  | "manual"
  | "custom"
  | "rate_registry"
  | "service_catalog"
  | "unknown";

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
  // Aggregated costs
  llmCostUsd: number;
  externalCostUsd: number;
  computeCostUsd: number;
  /**
   * v2 cloud-egress cost in USD, computed at task finalize from the
   * accountant's canonical external_bytes_out scalar. Distinct from
   * externalCostUsd (vendor API charges) — see Decision #7.
   */
  networkCostUsd: number;
  /**
   * v2 GPU cost in USD, computed at task finalize from the GpuAccountant's
   * NVML diff (per-PID SM-time) + cgroup-walk PID filter. Distinct from
   * computeCostUsd (CPU/memory rollup) — GPU billing is a separate dimension.
   * Mirrors the Python SDK's Task.gpu_cost_usd field.
   */
  gpuCostUsd: number;
  totalCostUsd: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  totalCachedTokens: number;
  retryCount: number;
  retryCostUsd: number;
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
  costUsd: number;
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

/** Create a default Task with required fields. */
export function createTask(overrides: Partial<Task> & { taskId: string }): Task {
  return {
    taskType: "",
    status: "pending",
    startedAt: new Date(),
    metadata: {},
    llmCostUsd: 0,
    externalCostUsd: 0,
    computeCostUsd: 0,
    networkCostUsd: 0,
    gpuCostUsd: 0,
    totalCostUsd: 0,
    totalInputTokens: 0,
    totalOutputTokens: 0,
    totalCachedTokens: 0,
    retryCount: 0,
    retryCostUsd: 0,
    failureCount: 0,
    networkBytesIn: 0,
    networkBytesOut: 0,
    networkCallCount: 0,
    networkByHost: { hosts: [] },
    schemaVersion: "1",
    ...overrides,
  };
}

/** Create a default CostEvent with required fields. */
export function createCostEvent(
  overrides: Partial<CostEvent> & { eventId: string; taskId: string }
): CostEvent {
  return {
    eventType: "llm_call",
    occurredAt: new Date(),
    costUsd: 0,
    costConfidence: "exact",
    isRetry: false,
    details: {},
    schemaVersion: "1",
    ...overrides,
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
    started_at: task.startedAt.toISOString(),
    ended_at: task.endedAt ? task.endedAt.toISOString() : null,
    metadata: task.metadata,
    customer_id: task.customerId ?? null,
    project_id: task.projectId ?? null,
    parent_task_id: task.parentTaskId ?? null,
    experiment_id: task.experimentId ?? null,
    variant: task.variant ?? null,
    llm_cost_usd: String(task.llmCostUsd),
    external_cost_usd: String(task.externalCostUsd),
    compute_cost_usd: String(task.computeCostUsd),
    network_cost_usd: String(task.networkCostUsd),
    gpu_cost_usd: String(task.gpuCostUsd),
    total_cost_usd: String(task.totalCostUsd),
    total_input_tokens: task.totalInputTokens,
    total_output_tokens: task.totalOutputTokens,
    total_cached_tokens: task.totalCachedTokens,
    retry_count: task.retryCount,
    retry_cost_usd: String(task.retryCostUsd),
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
    occurred_at: event.occurredAt.toISOString(),
    cost_usd: String(event.costUsd),
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
    llmCostUsd: _toNumber(data["llm_cost_usd"]),
    externalCostUsd: _toNumber(data["external_cost_usd"]),
    computeCostUsd: _toNumber(data["compute_cost_usd"]),
    networkCostUsd: _toNumber(data["network_cost_usd"]),
    gpuCostUsd: _toNumber(data["gpu_cost_usd"]),
    totalCostUsd: _toNumber(data["total_cost_usd"]),
    totalInputTokens: _toNumber(data["total_input_tokens"]),
    totalOutputTokens: _toNumber(data["total_output_tokens"]),
    totalCachedTokens: _toNumber(data["total_cached_tokens"]),
    retryCount: _toNumber(data["retry_count"]),
    retryCostUsd: _toNumber(data["retry_cost_usd"]),
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
    costUsd: _toNumber(data["cost_usd"]),
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
