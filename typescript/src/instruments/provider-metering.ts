import { randomUUID } from "node:crypto";
import { Decimal, canonicalDecimal, createCostEvent, toDecimal } from "../core/models.js";
import type { CostEvent, EventType, Task } from "../core/models.js";
import { getCurrentTask, runWithTask, suppressNetworkEvent } from "../core/context.js";
import { createAutoTask, finalizeAutoTask } from "../core/auto-task.js";
import { getAmbientSessionTask } from "../core/session.js";
import { applyEventCapability, getCapability } from "../core/capabilities.js";
import type { CapabilityIdentity } from "../core/capabilities.js";
import {
  applyEventIdempotency,
  captureIdempotencyKey,
  type CapturedIdempotencyKey,
} from "../core/idempotency.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerLlmCapture } from "../core/llm-dedup.js";
import { finalizeTaskNetwork } from "../core/network-finalize.js";
import type { ProviderJobRevisionOptions } from "../core/provider-jobs.js";
import {
  currentProviderCaptureOwner,
  runWithProviderCapture,
} from "./provider-capture.js";

export type ProviderOperationStatus = "succeeded" | "failed" | "cancelled" | "unknown";

export interface ProviderUsageLine {
  metric: string;
  quantity: string | number | bigint | Decimal;
  unit: string;
}

export interface OperationMeasurement {
  usageLines?: ProviderUsageLine[];
  /** Exact catalog-billable meters; defaults to the canonical usage lines. */
  pricingUsage?: Readonly<Record<string, string | number | bigint | Decimal>>;
  /** Canonical provider service used to correlate provider-owned request records. */
  providerService?: string;
  providerRecordId?: string;
  providerCostUsd?: string | number | Decimal;
  providerUpstreamCostUsd?: string | number | Decimal;
  /** Gateway-computed cost (for example LiteLLM), never provider-authoritative. */
  gatewayCalculatedCostUsd?: string | number | Decimal;
  responseModel?: string;
  modelCandidates?: string[];
  billingDimensions?: Array<readonly [string, string]>;
  inputTokens?: number;
  outputTokens?: number;
  cachedTokens?: number;
  cacheWriteTokens?: number;
  reasoningTokens?: number;
}

export interface ProviderOperationOptions {
  taskType: string;
  provider: string;
  service: string;
  operation: string;
  component: string;
  model?: string;
  eventType?: EventType;
}

function providerQuantity(name: string, value: unknown): Decimal {
  if (typeof value === "boolean" || (typeof value === "number" && !Number.isSafeInteger(value))) {
    throw new TypeError(`${name} must be an integer, Decimal, bigint, or decimal string`);
  }
  let parsed: Decimal;
  try {
    parsed = value instanceof Decimal ? value : new Decimal(String(value));
  } catch {
    throw new TypeError(`${name} is not a plain decimal`);
  }
  if (!parsed.isFinite() || parsed.isNegative()) {
    throw new RangeError(`${name} must be finite and non-negative`);
  }
  return parsed;
}

/** Validate the runtime boundary shared by every provider adapter. */
export function validateOperationMeasurement(measurement: OperationMeasurement): void {
  if (measurement.providerService !== undefined && !CANONICAL.test(measurement.providerService)) {
    throw new TypeError(`invalid provider service '${measurement.providerService}'`);
  }
  const positiveLines = new Set<string>();
  for (const line of measurement.usageLines ?? []) {
    if (!CANONICAL.test(line.metric)) throw new TypeError(`invalid provider usage metric '${line.metric}'`);
    if (!UNIT.test(line.unit)) throw new TypeError(`invalid provider usage unit '${line.unit}'`);
    const quantity = providerQuantity(line.metric, line.quantity);
    if (quantity.gt(0)) {
      const identity = `${line.metric}\0${line.unit}`;
      if (positiveLines.has(identity)) throw new TypeError(`duplicate provider usage line '${line.metric}/${line.unit}'`);
      positiveLines.add(identity);
    }
  }
  for (const [name, value] of [
    ["providerCostUsd", measurement.providerCostUsd],
    ["providerUpstreamCostUsd", measurement.providerUpstreamCostUsd],
    ["gatewayCalculatedCostUsd", measurement.gatewayCalculatedCostUsd],
  ] as const) {
    if (value !== undefined) providerQuantity(name, value);
  }
  const dimensions = measurement.billingDimensions ?? [];
  if (dimensions.length > 24) throw new RangeError("provider billing dimensions cannot exceed 24 entries");
  const dimensionKeys = new Set<string>();
  for (const [key, value] of dimensions) {
    if (!CANONICAL.test(key)) throw new TypeError(`invalid provider billing dimension '${key}'`);
    if (typeof value !== "string" || value.length < 1 || value.length > 256) {
      throw new RangeError("provider billing dimension values must contain 1-256 characters");
    }
    if (dimensionKeys.has(key)) throw new TypeError(`duplicate provider billing dimension '${key}'`);
    dimensionKeys.add(key);
  }
  for (const [name, value] of [
    ["inputTokens", measurement.inputTokens],
    ["outputTokens", measurement.outputTokens],
    ["cachedTokens", measurement.cachedTokens],
  ] as const) {
    if (value !== undefined && (!Number.isSafeInteger(value) || value < 0)) {
      throw new RangeError(`${name} must be a non-negative safe integer or undefined`);
    }
  }
}

export function providerJobMeasurementFields(
  pricing: PricingEngine,
  resourceId: string,
  measurement?: OperationMeasurement,
): Pick<ProviderJobRevisionOptions,
  "usage" | "costAmount" | "costSource" | "costConfidence" | "pricingVersion" |
  "taskInputTokens" | "taskOutputTokens" | "taskCachedTokens"> {
  if (measurement === undefined) return { usage: [] };
  validateOperationMeasurement(measurement);
  const usage = (measurement.usageLines ?? []).flatMap((item) => {
    const quantity = providerQuantity(item.metric, item.quantity);
    return quantity.gt(0) ? [{ metric: item.metric, quantity, unit: item.unit }] : [];
  });
  let costAmount: Decimal | undefined;
  let costSource: ProviderJobRevisionOptions["costSource"];
  let costConfidence: ProviderJobRevisionOptions["costConfidence"];
  let pricingVersion: string | undefined;
  if (measurement.providerCostUsd !== undefined) {
    const amount = toDecimal(measurement.providerCostUsd);
    if (amount.gt(0)) {
      costAmount = amount;
      costSource = "provider_reported";
      costConfidence = "exact";
    }
  } else {
    const pricingUsage = measurement.pricingUsage ?? Object.fromEntries(
      (measurement.usageLines ?? []).map((line) => [line.metric, line.quantity]),
    );
    const result = pricing.getMeteredCost(
      measurement.responseModel ?? resourceId,
      pricingUsage,
      measurement.modelCandidates ?? [],
    );
    if (result.costUsd.gt(0) && result.pricingSource !== "unknown") {
      costAmount = result.costUsd;
      costSource = "sdk_catalog";
      costConfidence = result.costConfidence;
      pricingVersion = result.pricingVersion;
    }
  }
  return {
    usage,
    costAmount,
    costSource,
    costConfidence,
    pricingVersion,
    taskInputTokens: measurement.inputTokens,
    taskOutputTokens: measurement.outputTokens,
    taskCachedTokens: measurement.cachedTokens,
  };
}

/**
 * Map a provider SDK result without replacing rich promise implementations.
 *
 * OpenAI-style SDKs expose helpers such as `withResponse()` and `asResponse()`
 * on their promise objects. Returning `result.then(...)` discards those APIs.
 * A transparent proxy keeps the original surface and instruments normal
 * `await`/`then` consumption. `withResponse()` is also mapped because it
 * contains the parsed response body; `asResponse()` remains untouched because
 * consuming its body would change application behaviour.
 */
export function mapProviderResult<T>(
  raw: T,
  fulfilled: (value: unknown) => unknown,
  rejected: (error: unknown) => never,
): T {
  const candidate = raw as unknown as Record<PropertyKey, unknown>;
  if (raw === null || raw === undefined || typeof candidate.then !== "function") {
    return fulfilled(raw) as T;
  }
  return new Proxy(candidate, {
    get(target, property, receiver): unknown {
      if (property === "then") {
        return (onFulfilled?: (value: unknown) => unknown, onRejected?: (error: unknown) => unknown) =>
          (target.then as (...args: unknown[]) => unknown).call(
            target,
            (value: unknown) => {
              const mapped = fulfilled(value);
              return onFulfilled === undefined ? mapped : onFulfilled(mapped);
            },
            (error: unknown) => {
              try {
                rejected(error);
              } catch (mappedError) {
                if (onRejected !== undefined) return onRejected(mappedError);
                throw mappedError;
              }
            },
          );
      }
      if (property === "withResponse" && typeof target[property] === "function") {
        return (...args: unknown[]) => {
          const result = (target[property] as (...values: unknown[]) => unknown).apply(target, args);
          const value = result as Record<PropertyKey, unknown>;
          if (result === null || result === undefined || typeof value.then !== "function") return result;
          return (value.then as (...values: unknown[]) => unknown).call(
            result,
            (payload: unknown) => {
              const row = payload as Record<string, unknown> | null;
              if (row !== null && typeof row === "object" && "data" in row) {
                const mapped = fulfilled(row.data);
                if (mapped !== row.data) return { ...row, data: mapped };
              }
              return payload;
            },
            rejected,
          );
        };
      }
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) as T;
}

const CANONICAL = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const UNIT = /^[A-Za-z0-9][A-Za-z0-9._{}/*^+-]{0,63}$/;

function canonicalErrorName(error: unknown): string {
  const raw = error instanceof Error ? error.name : typeof error;
  const value = raw.trim().toLowerCase().replace(/[^a-z0-9._-]+/g, "_").replace(/^[_-]+|[_-]+$/g, "");
  return value.slice(0, 128) || "provider_error";
}

function normalizedUsage(lines: ProviderUsageLine[] | undefined): Array<Record<string, string>> {
  const positive = (lines ?? []).flatMap((line) => {
    const quantity = providerQuantity(line.metric, line.quantity);
    if (!quantity.gt(0)) return [];
    return [{ metric: line.metric, quantity: canonicalDecimal(quantity), unit: line.unit }];
  });
  const result = positive.length === 0
    ? [{ metric: "request_count", quantity: "1", unit: "Requests" }]
    : positive;
  const seen = new Set<string>();
  return result.filter((line) => {
    const key = `${line.metric}\0${line.unit}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function recordProviderOperation(
  pricing: PricingEngine,
  buffer: EventBuffer,
  task: Task,
  options: ProviderOperationOptions,
  measurement: OperationMeasurement,
  latencyMs: number,
  status: ProviderOperationStatus,
  error: unknown,
  capability?: CapabilityIdentity,
  idempotencyKey?: string | CapturedIdempotencyKey,
): CostEvent {
  validateOperationMeasurement(measurement);
  const model = measurement.responseModel ?? options.model ?? "unknown";
  const pricingUsage = measurement.pricingUsage ?? Object.fromEntries(
    (measurement.usageLines ?? []).map((line) => [line.metric, line.quantity]),
  );
  const priced = pricing.getMeteredCost(model, pricingUsage, measurement.modelCandidates ?? []);
  let costUsd = priced.costUsd;
  let costConfidence: CostEvent["costConfidence"] = priced.costConfidence;
  let pricingSource: CostEvent["pricingSource"] = priced.pricingSource;
  let pricingVersion: string | undefined = priced.pricingVersion;
  if (measurement.providerCostUsd !== undefined) {
    costUsd = toDecimal(measurement.providerCostUsd);
    costConfidence = "exact";
    pricingSource = "provider_response";
    pricingVersion = undefined;
  } else if (measurement.gatewayCalculatedCostUsd !== undefined) {
    costUsd = toDecimal(measurement.gatewayCalculatedCostUsd);
    costConfidence = "computed";
    pricingSource = "litellm";
    pricingVersion = undefined;
  }
  const details: Record<string, unknown> = {
    attribution_component: options.component,
    attribution_operation_name: options.operation,
    attribution_operation_status: status,
    attribution_resource_type: "model",
    attribution_resource_id: model,
    attribution_usage_lines: normalizedUsage(measurement.usageLines),
    provider_usage_privacy: "quantities_only",
  };
  if (measurement.providerService !== undefined) {
    details["attribution_provider_service"] = measurement.providerService;
  }
  if (measurement.billingDimensions?.length) {
    details["attribution_dimensions"] = measurement.billingDimensions.map(([key, value]) => ({
      key, value: { type: "string", value },
    }));
  }
  if (priced.resolvedModel !== undefined) details["pricing_resolved_model"] = priced.resolvedModel;
  if (priced.lines.length > 0) {
    details["pricing_breakdown"] = priced.lines.map((item) => ({
      dimension: item.dimension,
      quantity: canonicalDecimal(item.quantity),
      rate_field: item.rateField,
      rate_usd: canonicalDecimal(item.rateUsd),
      cost_usd: canonicalDecimal(item.costUsd),
    }));
  }
  if (priced.unpricedDimensions.length > 0) {
    details["pricing_unpriced_dimensions"] = priced.unpricedDimensions;
  }
  if (measurement.providerRecordId) details["provider_record_id"] = measurement.providerRecordId.slice(0, 256);
  if (measurement.providerCostUsd !== undefined) {
    details["provider_reported_cost_usd"] = canonicalDecimal(toDecimal(measurement.providerCostUsd));
  }
  if (measurement.providerUpstreamCostUsd !== undefined) {
    details["provider_upstream_cost_usd"] = canonicalDecimal(toDecimal(measurement.providerUpstreamCostUsd));
  }
  if (measurement.gatewayCalculatedCostUsd !== undefined) {
    details["gateway_calculated_cost_usd"] = canonicalDecimal(toDecimal(measurement.gatewayCalculatedCostUsd));
  }
  if ((measurement.cacheWriteTokens ?? 0) > 0) details["cache_write_input_tokens"] = measurement.cacheWriteTokens;
  if ((measurement.reasoningTokens ?? 0) > 0) details["reasoning_output_tokens"] = measurement.reasoningTokens;
  if (error !== undefined) {
    details["attribution_error_type"] = canonicalErrorName(error);
    const candidate = error as { code?: unknown };
    if (candidate?.code !== undefined) details["attribution_error_code"] = String(candidate.code).slice(0, 64);
  }
  const event = createCostEvent({
    eventId: randomUUID(), taskId: task.taskId,
    eventType: options.eventType ?? "external_cost",
    costUsd, costConfidence, pricingSource, pricingVersion,
    provider: options.provider, serviceName: options.service, model,
    inputTokens: measurement.inputTokens, outputTokens: measurement.outputTokens,
    cachedTokens: measurement.cachedTokens, latencyMs, details,
    isRetry: false,
  });
  applyEventCapability(event, capability);
  applyEventIdempotency(event, idempotencyKey);
  if (!buffer.addEvent(event)) return event;
  if (event.eventType === "llm_call") {
    task.llmCostUsd = task.llmCostUsd.plus(costUsd);
    task.totalInputTokens += event.inputTokens ?? 0;
    task.totalOutputTokens += event.outputTokens ?? 0;
    task.totalCachedTokens += event.cachedTokens ?? 0;
    registerLlmCapture(task.taskId, event.inputTokens ?? 0, event.outputTokens ?? 0);
  } else if (event.eventType === "compute_cost") task.computeCostUsd = task.computeCostUsd.plus(costUsd);
  else task.externalCostUsd = task.externalCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  buffer.upsertTask(task);
  return event;
}

export function recordProviderFailure(
  pricing: PricingEngine,
  buffer: EventBuffer,
  task: Task,
  options: ProviderOperationOptions,
  error: unknown,
  startedAt: number,
  capability: CapabilityIdentity | undefined = getCapability(),
  idempotencyKey: CapturedIdempotencyKey | undefined = captureIdempotencyKey(),
): CostEvent | undefined {
  try {
    return recordProviderOperation(
      pricing, buffer, task, options, { usageLines: [] },
      Math.max(0, Math.round(performance.now() - startedAt)), "failed", error,
      capability, idempotencyKey,
    );
  } catch {
    return undefined;
  }
}

export class ProviderOperationSession {
  readonly task: Task;
  readonly autoCreated: boolean;
  private readonly startedAt = performance.now();
  readonly capability = getCapability();
  private readonly captureClaimed = currentProviderCaptureOwner() === undefined;
  private readonly idempotencyKey = this.captureClaimed ? captureIdempotencyKey() : undefined;
  private finalized = false;

  constructor(
    private readonly pricing: PricingEngine,
    private readonly buffer: EventBuffer,
    private readonly options: ProviderOperationOptions,
  ) {
    const current = getCurrentTask();
    const ambient = current ?? getAmbientSessionTask(options.taskType);
    this.autoCreated = ambient === undefined;
    this.task = ambient ?? createAutoTask(options.taskType);
    if (this.autoCreated) buffer.upsertTask(this.task);
  }

  invoke<T>(fn: () => T): T {
    if (!this.captureClaimed) return runWithTask(this.task, fn);
    return suppressNetworkEvent(() => runWithProviderCapture(
      this.options.provider,
      () => runWithTask(this.task, fn),
    ));
  }

  finish(
    measurement: OperationMeasurement = {},
    status: ProviderOperationStatus = "succeeded",
    error?: unknown,
  ): CostEvent | undefined {
    if (this.finalized) return undefined;
    this.finalized = true;
    if (!this.captureClaimed) return undefined;
    try {
      const event = recordProviderOperation(
        this.pricing, this.buffer, this.task, this.options, measurement,
        Math.max(0, Math.round(performance.now() - this.startedAt)), status, error,
        this.capability, this.idempotencyKey,
      );
      if (this.autoCreated) {
        finalizeAutoTask(this.task, status === "succeeded" ? "success" : "failed", this.buffer);
      }
      return event;
    } catch {
      if (this.autoCreated) finalizeAutoTask(this.task, "failed", this.buffer);
      return undefined;
    }
  }

  fail(error: unknown): CostEvent | undefined {
    return this.finish({ usageLines: [] }, "failed", error);
  }

  cancel(measurement: OperationMeasurement = { usageLines: [], pricingUsage: {} }): CostEvent | undefined {
    return this.finish(measurement, "cancelled");
  }

  finalizeWithoutEvent(status: "success" | "failed" = "success"): void {
    if (this.finalized) return;
    this.finalized = true;
    if (this.autoCreated) finalizeAutoTask(this.task, status, this.buffer);
  }

  /** Transfer an auto-created task to a durable provider-job ledger. */
  releaseForProviderJob(): void {
    if (this.finalized) return;
    this.finalized = true;
    if (this.autoCreated) {
      try { finalizeTaskNetwork(this.task, this.buffer); } catch { /* provider hot path */ }
      this.buffer.upsertTask(this.task);
    }
  }
}

export function wrapProviderStream<T>(
  raw: T,
  session: ProviderOperationSession,
  observe: (item: unknown) => void,
  measurement: () => OperationMeasurement,
  completionStatus: () => ProviderOperationStatus = () => "succeeded",
): T {
  const safeMeasurement = (): OperationMeasurement => {
    try {
      const extracted = measurement();
      validateOperationMeasurement(extracted);
      return extracted;
    } catch {
      // Provider response parsing is telemetry-only. Unknown or newly added
      // response shapes must not replace valid provider results with SDK errors.
      return { usageLines: [], pricingUsage: {} };
    }
  };
  const safeCompletionStatus = (): ProviderOperationStatus => {
    try {
      const status = completionStatus();
      return ["succeeded", "failed", "cancelled", "unknown"].includes(status)
        ? status
        : "unknown";
    } catch {
      return "unknown";
    }
  };
  const safeObserve = (item: unknown): void => {
    try {
      observe(item);
    } catch {
      // Preserve the provider item even when usage extraction cannot parse it.
    }
  };
  const candidate = raw as unknown as Record<PropertyKey, unknown>;
  if (typeof candidate[Symbol.asyncIterator] === "function") {
    return new Proxy(candidate, {
      get(target, property, receiver): unknown {
        if (property === Symbol.asyncIterator) {
          return () => {
            const iterator = (target[Symbol.asyncIterator] as () => AsyncIterator<unknown>).call(target);
            return {
              async next(...args: [] | [unknown]): Promise<IteratorResult<unknown>> {
                let result: IteratorResult<unknown>;
                try {
                  result = await (iterator.next as (...values: unknown[]) => Promise<IteratorResult<unknown>>).apply(iterator, args);
                } catch (error) {
                  session.finish(safeMeasurement(), "failed", error);
                  throw error;
                }
                if (result.done) session.finish(safeMeasurement(), safeCompletionStatus());
                else safeObserve(result.value);
                return result;
              },
              async return(value?: unknown): Promise<IteratorResult<unknown>> {
                let result: IteratorResult<unknown>;
                try {
                  result = iterator.return === undefined
                    ? { done: true, value }
                    : await iterator.return(value);
                } catch (error) {
                  session.finish(safeMeasurement(), "failed", error);
                  throw error;
                }
                session.cancel(safeMeasurement());
                return result;
              },
              async throw(error?: unknown): Promise<IteratorResult<unknown>> {
                if (iterator.throw === undefined) {
                  session.finish(safeMeasurement(), "failed", error);
                  throw error;
                }
                let result: IteratorResult<unknown>;
                try {
                  result = await iterator.throw(error);
                } catch (raised) {
                  session.finish(safeMeasurement(), "failed", raised);
                  throw raised;
                }
                if (result.done) session.finish(safeMeasurement(), "failed", error);
                else safeObserve(result.value);
                return result;
              },
              [Symbol.asyncIterator](): AsyncIterator<unknown> { return this; },
            };
          };
        }
        const value = Reflect.get(target, property, receiver);
        return typeof value === "function" ? value.bind(target) : value;
      },
    }) as T;
  }
  if (typeof candidate[Symbol.iterator] === "function") {
    return new Proxy(candidate, {
      get(target, property, receiver): unknown {
        if (property === Symbol.iterator) {
          return () => {
            const iterator = (target[Symbol.iterator] as () => Iterator<unknown>).call(target);
            return {
              next(...args: [] | [unknown]): IteratorResult<unknown> {
                let result: IteratorResult<unknown>;
                try {
                  result = (iterator.next as (...values: unknown[]) => IteratorResult<unknown>).apply(iterator, args);
                } catch (error) {
                  session.finish(safeMeasurement(), "failed", error);
                  throw error;
                }
                if (result.done) session.finish(safeMeasurement(), safeCompletionStatus());
                else safeObserve(result.value);
                return result;
              },
              return(value?: unknown): IteratorResult<unknown> {
                let result: IteratorResult<unknown>;
                try {
                  result = iterator.return === undefined ? { done: true, value } : iterator.return(value);
                } catch (error) {
                  session.finish(safeMeasurement(), "failed", error);
                  throw error;
                }
                session.cancel(safeMeasurement());
                return result;
              },
              throw(error?: unknown): IteratorResult<unknown> {
                if (iterator.throw === undefined) {
                  session.finish(safeMeasurement(), "failed", error);
                  throw error;
                }
                let result: IteratorResult<unknown>;
                try {
                  result = iterator.throw(error);
                } catch (raised) {
                  session.finish(safeMeasurement(), "failed", raised);
                  throw raised;
                }
                if (result.done) session.finish(safeMeasurement(), "failed", error);
                else safeObserve(result.value);
                return result;
              },
              [Symbol.iterator](): Iterator<unknown> { return this; },
            };
          };
        }
        const value = Reflect.get(target, property, receiver);
        return typeof value === "function" ? value.bind(target) : value;
      },
    }) as T;
  }
  session.finish(safeMeasurement(), safeCompletionStatus());
  return raw;
}
