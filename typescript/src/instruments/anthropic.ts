/**
 * Anthropic auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `Anthropic.Messages.prototype.create` to automatically
 * record cost events and aggregate token usage on the active task context.
 *
 * Supports both non-streaming and streaming responses.
 */

import { randomUUID } from "node:crypto";
import { createCostEvent, Decimal } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import { getCurrentTask, runWithTask, suppressNetworkEvent } from "../core/context.js";
import { createAutoTask, finalizeAutoTask } from "../core/auto-task.js";
import { registerLlmCapture } from "../core/llm-dedup.js";
import { getAmbientSessionTask } from "../core/session.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine, CostResult } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import { stampAmbientAttribution } from "../core/capabilities.js";
import {
  ProviderOperationSession,
  mapProviderResult,
  providerJobMeasurementFields,
  recordProviderFailure,
} from "./provider-metering.js";
import type { OperationMeasurement } from "./provider-metering.js";
import { ProviderJobRevision, providerJobFromDict } from "../core/provider-jobs.js";
import { currentProviderCaptureOwner, runWithProviderCapture } from "./provider-capture.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _original: Function | null = null;
let _patchedPrototype: any = null;
let _messagesClass: any = null;
const _messageBatchClasses: Array<{ cls: any; service: string }> = [];
const _batchPatches: Array<{ prototype: any; name: string; original: Function }> = [];
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;

function providerForMessagesResource(resource: any): "anthropic" | "moonshot" {
  try {
    const raw = resource?._client?.baseURL ?? resource?._client?.base_url ??
      resource?._client?._baseURL ?? resource?._client?._base_url;
    if (typeof raw === "string" && new URL(raw).hostname.toLowerCase() === "api.moonshot.ai") {
      return "moonshot";
    }
  } catch {
    // Malformed or unavailable client metadata falls back to Anthropic.
  }
  return "anthropic";
}

/** Test helper: inject a mock Messages class so tests avoid importing @anthropic-ai/sdk. */
export function _setMessagesClass(cls: any): void {
  _messagesClass = cls;
}

/** Test/provider helper: add a stable or beta Message Batches resource class. */
export function _setMessageBatchesClass(cls: any, service = "message_batches"): void {
  if (cls) _messageBatchClasses.push({ cls, service });
}

/** Test helper: reset to real module resolution. */
export function _resetMessagesClass(): void {
  _messagesClass = null;
  _messageBatchClasses.length = 0;
}

/**
 * Patch `Anthropic.Messages.prototype.create` to record cost events.
 *
 * If `@anthropic-ai/sdk` is not installed and no mock class is injected, the
 * dynamic import will throw and the function will reject.
 */
export async function instrumentAnthropic(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  let MessagesProto: any;
  if (_messagesClass) {
    MessagesProto = _messagesClass.prototype;
  } else {
    // @anthropic-ai/sdk is an optional peer dependency; the dynamic import
    // only succeeds at runtime if the user has installed it.
    // @ts-ignore -- @anthropic-ai/sdk is an optional peer dependency
    const anthropicModule = await import("@anthropic-ai/sdk");
    const Anthropic = anthropicModule.default ?? anthropicModule;
    MessagesProto = Anthropic.Messages.prototype;
    for (const [cls, service] of [
      [Anthropic.Messages?.Batches, "message_batches"],
      [Anthropic.Beta?.Messages?.Batches, "beta_message_batches"],
    ] as const) {
      if (cls && !_messageBatchClasses.some((item) => item.cls === cls)) {
        _messageBatchClasses.push({ cls, service });
      }
    }
  }

  _original = MessagesProto.create;
  _patchedPrototype = MessagesProto;
  _buffer = buffer;
  _pricing = pricing;

  MessagesProto.create = async function (
    this: any,
    body: any,
    options?: any,
  ): Promise<any> {
    if (currentProviderCaptureOwner() !== undefined) {
      return _original!.call(this, body, options);
    }
    let task = getCurrentTask();
    let autoCreated = false;

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      // Join the ambient session (grouping with sibling HTTP/LLM calls
      // in the same context) when session tracking is active; the
      // session sweep owns its lifecycle. Otherwise fall back to a
      // per-call auto-task owned (and finalized) here.
      task = getAmbientSessionTask("anthropic.messages");
      if (!task) {
        task = createAutoTask("anthropic.messages");
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const startTime = performance.now();
    const provider = providerForMessagesResource(this);
    const operation = `${provider}.messages.create`;

    // Scope the SDK call inside runWithTask so the HTTP adapter's
    // _resolveHttpTask() finds this task via getCurrentTask() during
    // the underlying fetch — keeps llm_call and its network bytes
    // attributed to the same task.
    const self = this;

    if (body?.stream) {
      try {
        const rawStream = await suppressNetworkEvent(() =>
          runWithProviderCapture(provider, () =>
            runWithTask(task, () => _original!.call(self, body, options))),
        );
        return wrapStream(rawStream, task, startTime, autoCreated, provider);
      } catch (err) {
        if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
          taskType: `${provider}.messages`, provider, service: provider === "moonshot" ? "api" : "messages",
          operation, component: "llm", model: body?.model, eventType: "llm_call",
        }, err, startTime);
        if (autoCreated) {
          finalizeAutoTask(task, "failed", _buffer);
        }
        throw err;
      }
    }

    try {
      const response = await suppressNetworkEvent(() =>
        runWithProviderCapture(provider, () =>
          runWithTask(task, () => _original!.call(self, body, options))),
      );
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        recordEvent(response, task, latencyMs, provider);
      } catch {
        // dexcost errors must never crash user code
      }
      if (autoCreated) {
        finalizeAutoTask(task, "success", _buffer);
      }
      return response;
    } catch (err) {
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType: `${provider}.messages`, provider, service: provider === "moonshot" ? "api" : "messages",
        operation, component: "llm", model: body?.model, eventType: "llm_call",
      }, err, startTime);
      if (autoCreated) {
        finalizeAutoTask(task, "failed", _buffer);
      }
      throw err;
    }
  };

  for (const item of _messageBatchClasses) patchMessageBatches(item.cls.prototype, item.service);

  _patched = true;
}

/**
 * Remove the monkey-patch and restore the original `create` method.
 */
export function uninstrumentAnthropic(): void {
  if (!_patched) return;

  if (_patchedPrototype) {
    if (_original) _patchedPrototype.create = _original;
    else delete _patchedPrototype.create;
  }
  for (const patch of _batchPatches.splice(0)) patch.prototype[patch.name] = patch.original;

  _original = null;
  _patchedPrototype = null;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

type BatchStatus = "submitted" | "running" | "succeeded" | "failed" | "cancelled" | "unknown";

function boundedString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value.slice(0, 256) : undefined;
}

function batchCounts(resource: any): Record<string, number> {
  const raw = resource?.request_counts ?? resource?.requestCounts ?? {};
  const result: Record<string, number> = {};
  for (const name of ["processing", "succeeded", "errored", "canceled", "expired"]) {
    const value = Number(raw?.[name]);
    if (Number.isSafeInteger(value) && value >= 0) result[name] = value;
  }
  return result;
}

function batchStatus(resource: any, submission = false): BatchStatus {
  const status = resource?.processing_status ?? resource?.processingStatus;
  const counts = batchCounts(resource);
  if (status === "in_progress") return submission ? "submitted" : "running";
  if (status === "canceling" || (counts.processing ?? 0) > 0) return "running";
  if (status !== "ended") return "unknown";
  if ((counts.succeeded ?? 0) > 0) return "succeeded";
  if ((counts.errored ?? 0) > 0 || (counts.expired ?? 0) > 0) return "failed";
  if ((counts.canceled ?? 0) > 0) return "cancelled";
  return "unknown";
}

function batchCountMeasurement(resource: any, status: BatchStatus): OperationMeasurement | undefined {
  if (status === "submitted" || status === "running") return undefined;
  const usageLines = Object.entries(batchCounts(resource))
    .filter(([, quantity]) => quantity > 0)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, quantity]) => ({
      metric: `batch_${name}_request_count`, quantity, unit: "Requests",
    }));
  return { usageLines, pricingUsage: {} };
}

function batchRequestMetadata(body: any): {
  resourceId: string;
  billingDimensions: Array<readonly [string, string]>;
} {
  const requests = Array.isArray(body?.requests) ? body.requests : undefined;
  if (requests === undefined) return { resourceId: "anthropic-message-batch", billingDimensions: [] };
  const modelSet = new Set<string>();
  for (const request of requests) {
    const model = boundedString(request?.params?.model);
    if (model !== undefined) modelSet.add(model);
  }
  const models = [...modelSet];
  const billingDimensions: Array<readonly [string, string]> = [
    ["batch_request_count", String(requests.length)],
  ];
  if (models.length > 0) billingDimensions.push(["batch_model_count", String(models.length)]);
  return {
    resourceId: models.length === 1 ? models[0]! : "anthropic-message-batch",
    billingDimensions,
  };
}

function insertBatchSubmission(
  session: ProviderOperationSession,
  service: string,
  resource: any,
  resourceId: string,
  billingDimensions: Array<readonly [string, string]>,
): void {
  const recordId = boundedString(resource?.id);
  if (recordId === undefined) {
    session.fail(new Error("Anthropic batch response omitted id"));
    return;
  }
  const status = batchStatus(resource, true);
  const measurement = batchCountMeasurement(resource, status);
  const now = new Date();
  session.releaseForProviderJob();
  _buffer!.insertProviderJobRevision(new ProviderJobRevision({
    taskId: session.task.taskId,
    provider: "anthropic",
    service,
    providerRecordId: recordId,
    operation: `anthropic.${service}.create`,
    component: "llm",
    eventType: "llm_call",
    resourceType: "model",
    resourceId,
    status,
    submittedAt: now,
    observedAt: now,
    ownsTask: session.autoCreated,
    billingDimensions,
    ...providerJobMeasurementFields(_pricing!, resourceId, measurement),
    capability: session.capability,
  }));
}

function reconcileBatch(service: string, recordId: string, status: BatchStatus, measurement?: OperationMeasurement): void {
  if (!_buffer || !_pricing) return;
  const stored = _buffer.getProviderJob("anthropic", service, recordId);
  if (stored === undefined) return;
  const previous = providerJobFromDict(stored);
  const observedAt = new Date(Math.max(Date.now(), previous.observedAt.getTime()));
  _buffer.insertProviderJobRevision(new ProviderJobRevision({
    eventId: previous.eventId,
    revision: previous.revision + 1,
    taskId: previous.taskId,
    provider: previous.provider,
    service: previous.service,
    providerRecordId: previous.providerRecordId,
    operation: previous.operation,
    component: previous.component,
    eventType: previous.eventType,
    resourceType: previous.resourceType,
    resourceId: previous.resourceId,
    status,
    submittedAt: previous.submittedAt,
    observedAt,
    ownsTask: previous.ownsTask,
    billingDimensions: previous.billingDimensions,
    ...providerJobMeasurementFields(_pricing, previous.resourceId, measurement),
    errorType: status === "failed" ? "anthropic_batch_failed" : undefined,
    capability: previous.capability,
  }));
}

function observeOrAdoptBatch(resource: any, service: string): void {
  const recordId = boundedString(resource?.id);
  if (recordId === undefined || !_buffer || !_pricing) return;
  const status = batchStatus(resource);
  const measurement = batchCountMeasurement(resource, status);
  if (_buffer.getProviderJob("anthropic", service, recordId) !== undefined) {
    reconcileBatch(service, recordId, status, measurement);
    return;
  }
  const session = new ProviderOperationSession(_pricing, _buffer, {
    taskType: `anthropic.${service}.retrieve`,
    provider: "anthropic",
    service,
    operation: `anthropic.${service}.create`,
    component: "llm",
    model: "anthropic-message-batch",
    eventType: "llm_call",
  });
  insertBatchSubmission(session, service, resource, "anthropic-message-batch", []);
}

class BatchResultsMeter {
  private readonly usage = new Map<string, number>();
  private readonly counts = new Map<string, number>();
  inputTokens = 0;
  outputTokens = 0;
  cachedTokens = 0;

  private add(metric: string, quantity: unknown): void {
    const parsed = Number(quantity);
    if (!Number.isSafeInteger(parsed) || parsed <= 0) return;
    this.usage.set(metric, (this.usage.get(metric) ?? 0) + parsed);
  }

  observe(item: any): void {
    let resultType = boundedString(item?.result?.type) ?? "errored";
    if (!["succeeded", "errored", "canceled", "expired"].includes(resultType)) resultType = "errored";
    this.counts.set(resultType, (this.counts.get(resultType) ?? 0) + 1);
    if (resultType !== "succeeded") return;
    const usage = item?.result?.message?.usage;
    if (!usage) return;
    const input = Number(usage.input_tokens ?? 0);
    const output = Number(usage.output_tokens ?? 0);
    const cacheRead = Number(usage.cache_read_input_tokens ?? 0);
    const cacheWrite = Number(usage.cache_creation_input_tokens ?? 0);
    const cacheWrite1h = Number(
      usage.cache_creation?.ephemeral_1h_input_tokens ?? usage.cache_creation_input_tokens_1h ?? 0,
    );
    this.add("anthropic_batch_input_tokens", input);
    this.add("anthropic_batch_output_tokens", output);
    this.add("anthropic_batch_cache_read_input_tokens", cacheRead);
    this.add("anthropic_batch_cache_write_input_tokens", Math.max(0, cacheWrite - cacheWrite1h));
    this.add("anthropic_batch_cache_write_input_tokens_1h", cacheWrite1h);
    this.inputTokens += Number.isSafeInteger(input) && input > 0 ? input : 0;
    this.outputTokens += Number.isSafeInteger(output) && output > 0 ? output : 0;
    this.cachedTokens += Number.isSafeInteger(cacheRead) && cacheRead > 0 ? cacheRead : 0;
  }

  measurement(): { status: BatchStatus; measurement: OperationMeasurement } {
    for (const [name, quantity] of this.counts) {
      this.usage.set(`batch_${name}_request_count`, quantity);
    }
    const succeeded = this.counts.get("succeeded") ?? 0;
    const status: BatchStatus = succeeded > 0 ? "succeeded"
      : (this.counts.get("errored") ?? 0) > 0 || (this.counts.get("expired") ?? 0) > 0 ? "failed"
        : (this.counts.get("canceled") ?? 0) > 0 ? "cancelled" : "unknown";
    const usageLines = [...this.usage.entries()].sort(([left], [right]) => left.localeCompare(right)).map(
      ([metric, quantity]) => ({
        metric,
        quantity,
        unit: metric.endsWith("request_count") ? "Requests" : "Tokens",
      }),
    );
    const pricingUsage = Object.fromEntries(
      [...this.usage.entries()].filter(([metric]) => metric.startsWith("anthropic_batch_")),
    );
    return {
      status,
      measurement: {
        usageLines,
        pricingUsage,
        inputTokens: this.inputTokens,
        outputTokens: this.outputTokens,
        cachedTokens: this.cachedTokens,
      },
    };
  }
}

function wrapBatchResults(raw: any, service: string, recordId: string): any {
  const meter = new BatchResultsMeter();
  let completed = false;
  const complete = (): void => {
    if (completed) return;
    completed = true;
    try {
      const result = meter.measurement();
      reconcileBatch(service, recordId, result.status, result.measurement);
    } catch {
      // Result parsing is telemetry-only.
    }
  };
  const candidate = raw as Record<PropertyKey, unknown>;
  if (typeof candidate?.[Symbol.asyncIterator] === "function") {
    return new Proxy(candidate, {
      get(target, property, receiver): unknown {
        if (property === Symbol.asyncIterator) return () => {
          const iterator = (target[Symbol.asyncIterator] as () => AsyncIterator<any>).call(target);
          return {
            async next(): Promise<IteratorResult<any>> {
              const result = await iterator.next();
              if (result.done) complete(); else meter.observe(result.value);
              return result;
            },
            async return(value?: any): Promise<IteratorResult<any>> {
              return iterator.return ? iterator.return(value) : { done: true, value };
            },
            async throw(error?: any): Promise<IteratorResult<any>> {
              if (iterator.throw) return iterator.throw(error);
              throw error;
            },
            [Symbol.asyncIterator](): AsyncIterator<any> { return this; },
          };
        };
        const value = Reflect.get(target, property, receiver);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
  }
  if (typeof candidate?.[Symbol.iterator] === "function") {
    return new Proxy(candidate, {
      get(target, property, receiver): unknown {
        if (property === Symbol.iterator) return () => {
          const iterator = (target[Symbol.iterator] as () => Iterator<any>).call(target);
          return {
            next(): IteratorResult<any> {
              const result = iterator.next();
              if (result.done) complete(); else meter.observe(result.value);
              return result;
            },
            return(value?: any): IteratorResult<any> {
              return iterator.return ? iterator.return(value) : { done: true, value };
            },
            throw(error?: any): IteratorResult<any> {
              if (iterator.throw) return iterator.throw(error);
              throw error;
            },
            [Symbol.iterator](): Iterator<any> { return this; },
          };
        };
        const value = Reflect.get(target, property, receiver);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
  }
  return raw;
}

function patchMessageBatches(prototype: any, service: string): void {
  if (!prototype) return;
  for (const name of ["create", "retrieve", "cancel", "results"] as const) {
    if (typeof prototype[name] !== "function") continue;
    const original = prototype[name] as Function;
    _batchPatches.push({ prototype, name, original });
    prototype[name] = function (this: any, ...args: any[]): any {
      if (currentProviderCaptureOwner() !== undefined) return original.apply(this, args);
      if (name === "create") {
        const metadata = batchRequestMetadata(args[0]);
        const session = new ProviderOperationSession(_pricing!, _buffer!, {
          taskType: `anthropic.${service}.create`,
          provider: "anthropic",
          service,
          operation: `anthropic.${service}.create`,
          component: "llm",
          model: metadata.resourceId,
          eventType: "llm_call",
        });
        let raw: any;
        try { raw = session.invoke(() => original.apply(this, args)); }
        catch (error) { session.fail(error); throw error; }
        return mapProviderResult(raw, (resource) => {
          try {
            insertBatchSubmission(session, service, resource, metadata.resourceId, metadata.billingDimensions);
          } catch {
            session.finalizeWithoutEvent("failed");
          }
          return resource;
        }, (error) => { session.fail(error); throw error; });
      }

      const recordId = boundedString(args[0]);
      let raw: any;
      try {
        raw = suppressNetworkEvent(() => runWithProviderCapture(
          "anthropic", () => original.apply(this, args),
        ));
      } catch (error) { throw error; }
      return mapProviderResult(raw, (resource) => {
        if (recordId !== undefined) {
          try {
            if (name === "results") {
              if (_buffer?.getProviderJob("anthropic", service, recordId) === undefined) {
                const session = new ProviderOperationSession(_pricing!, _buffer!, {
                  taskType: `anthropic.${service}.results`, provider: "anthropic", service,
                  operation: `anthropic.${service}.create`, component: "llm",
                  model: "anthropic-message-batch", eventType: "llm_call",
                });
                const now = new Date();
                session.releaseForProviderJob();
                _buffer!.insertProviderJobRevision(new ProviderJobRevision({
                  taskId: session.task.taskId, provider: "anthropic", service,
                  providerRecordId: recordId, operation: `anthropic.${service}.create`,
                  component: "llm", eventType: "llm_call", resourceType: "model",
                  resourceId: "anthropic-message-batch", status: "running",
                  submittedAt: now, observedAt: now, ownsTask: session.autoCreated,
                  capability: session.capability,
                }));
              }
              return wrapBatchResults(resource, service, recordId);
            }
            observeOrAdoptBatch(resource, service);
          } catch {
            // Reconciliation is telemetry-only.
          }
        }
        return resource;
      }, (error) => { throw error; });
    };
  }
}

function recordEvent(
  response: any,
  task: Task,
  latencyMs: number,
  provider: "anthropic" | "moonshot" = "anthropic",
): void {
  if (!_buffer || !_pricing) return;

  const model: string = response?.model ?? "unknown";
  const usage = response?.usage;
  const hasUsage = usage != null;

  const inputTokens: number = usage?.input_tokens ?? 0;
  const outputTokens: number = usage?.output_tokens ?? 0;
  const cachedTokens: number = usage?.cache_read_input_tokens ?? 0;
  const cacheCreationTokens: number = usage?.cache_creation_input_tokens ?? 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage && provider === "anthropic") {
    const result: CostResult = _pricing.getCost(
      model,
      inputTokens,
      outputTokens,
      cachedTokens,
      cacheCreationTokens,
    );
    costUsd = result.costUsd;
    costConfidence = result.costConfidence;
    pricingSource = result.pricingSource;
  }

  const usageLines = [
    ...(inputTokens > 0
      ? [{ metric: "input_tokens", quantity: String(inputTokens), unit: "Tokens" }]
      : []),
    ...(outputTokens > 0
      ? [{ metric: "output_tokens", quantity: String(outputTokens), unit: "Tokens" }]
      : []),
    ...(cachedTokens > 0
      ? [{ metric: "cache_read_input_tokens", quantity: String(cachedTokens), unit: "Tokens" }]
      : []),
    ...(cacheCreationTokens > 0
      ? [{ metric: "cache_write_input_tokens", quantity: String(cacheCreationTokens), unit: "Tokens" }]
      : []),
  ];
  const details: Record<string, unknown> = {
    attribution_component: "llm",
    attribution_operation_name: `${provider}.messages.create`,
    attribution_operation_status: "succeeded",
    attribution_resource_type: "model",
    attribution_resource_id: model,
    attribution_provider_service: provider === "moonshot" ? "api" : "messages",
    attribution_usage_lines: usageLines.length > 0
      ? usageLines
      : [{ metric: "request_count", quantity: "1", unit: "Requests" }],
  };
  if (cacheCreationTokens > 0) {
    details["cache_creation_input_tokens"] = cacheCreationTokens;
  }

  const event = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
    eventType: "llm_call",
    costUsd,
    costConfidence,
    pricingSource,
    provider,
    model,
    inputTokens,
    outputTokens,
    cachedTokens,
    latencyMs,
    isRetry: false,
    details,
  });
  stampAmbientAttribution(event);

  _buffer.addEvent(event);
  registerLlmCapture(task.taskId, event.inputTokens ?? 0, event.outputTokens ?? 0);

  task.llmCostUsd = task.llmCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  task.totalInputTokens += inputTokens;
  task.totalOutputTokens += outputTokens;
  task.totalCachedTokens += cachedTokens;
  _buffer.upsertTask(task);
}

function wrapStream(
  rawStream: any,
  task: Task,
  startTime: number,
  autoCreated: boolean = false,
  provider: "anthropic" | "moonshot" = "anthropic",
): AsyncIterable<any> {
  let model = "unknown";
  let inputTokens = 0;
  let outputTokens = 0;
  let cachedTokens = 0;
  let cacheCreationTokens = 0;
  let hasUsage = false;
  let finalized = false;

  const finalize = (status: "succeeded" | "failed" | "cancelled", error?: unknown): void => {
    if (finalized) return;
    finalized = true;
    try {
      const costResult = hasUsage && _pricing && provider === "anthropic"
        ? _pricing.getCost(model, inputTokens, outputTokens, cachedTokens, cacheCreationTokens)
        : { costUsd: new Decimal(0), costConfidence: "estimated" as const, pricingSource: "unknown" as const };
      const usageLines = [
        ...(inputTokens > 0 ? [{ metric: "input_tokens", quantity: String(inputTokens), unit: "Tokens" }] : []),
        ...(outputTokens > 0 ? [{ metric: "output_tokens", quantity: String(outputTokens), unit: "Tokens" }] : []),
        ...(cachedTokens > 0 ? [{ metric: "cache_read_input_tokens", quantity: String(cachedTokens), unit: "Tokens" }] : []),
        ...(cacheCreationTokens > 0 ? [{ metric: "cache_write_input_tokens", quantity: String(cacheCreationTokens), unit: "Tokens" }] : []),
      ];
      const event = createCostEvent({
        eventId: randomUUID(), taskId: task.taskId, eventType: "llm_call",
        costUsd: costResult.costUsd, costConfidence: costResult.costConfidence,
        pricingSource: costResult.pricingSource, provider, model,
        inputTokens, outputTokens, cachedTokens,
        latencyMs: Math.round(performance.now() - startTime), isRetry: false,
        details: {
          attribution_component: "llm",
          attribution_operation_name: `${provider}.messages.create`,
          attribution_operation_status: status,
          attribution_resource_type: "model",
          attribution_resource_id: model,
          attribution_provider_service: provider === "moonshot" ? "api" : "messages",
          attribution_usage_lines: usageLines.length > 0
            ? usageLines
            : [{ metric: "request_count", quantity: "1", unit: "Requests" }],
          ...(cacheCreationTokens > 0 ? { cache_creation_input_tokens: cacheCreationTokens } : {}),
          ...(error === undefined ? {} : {
            attribution_error_type: error instanceof Error ? error.name.toLowerCase() : typeof error,
          }),
        },
      });
      stampAmbientAttribution(event);
      if (_buffer?.addEvent(event) !== false) {
        registerLlmCapture(task.taskId, inputTokens, outputTokens);
        task.llmCostUsd = task.llmCostUsd.plus(costResult.costUsd);
        task.totalCostUsd = task.totalCostUsd.plus(costResult.costUsd);
        task.totalInputTokens += inputTokens;
        task.totalOutputTokens += outputTokens;
        task.totalCachedTokens += cachedTokens;
        _buffer?.upsertTask(task);
      }
    } catch {
      // dexcost errors must never crash the provider stream
    }
    if (autoCreated) finalizeAutoTask(task, status === "succeeded" ? "success" : "failed", _buffer);
  };

  return {
    [Symbol.asyncIterator]() {
      const iter = rawStream[Symbol.asyncIterator]();
      return {
        async next(): Promise<IteratorResult<any>> {
          let result: IteratorResult<any>;
          try {
            result = await iter.next();
          } catch (err) {
            finalize("failed", err);
            throw err;
          }
          if (result.done) {
            finalize("succeeded");
            return result;
          }

          const chunk = result.value;

          // Anthropic streaming event types
          if (chunk?.type === "message_start" && chunk?.message) {
            if (chunk.message.model) model = chunk.message.model;
            if (chunk.message.usage) {
              hasUsage = true;
              inputTokens = chunk.message.usage.input_tokens ?? inputTokens;
              cachedTokens =
                chunk.message.usage.cache_read_input_tokens ?? cachedTokens;
              cacheCreationTokens =
                chunk.message.usage.cache_creation_input_tokens ?? cacheCreationTokens;
            }
          }

          if (chunk?.type === "message_delta" && chunk?.usage) {
            hasUsage = true;
            outputTokens = chunk.usage.output_tokens ?? outputTokens;
          }

          return result;
        },
        async return(value?: any): Promise<IteratorResult<any>> {
          try {
            const result = iter.return ? await iter.return(value) : { done: true as const, value };
            finalize("cancelled");
            return result;
          } catch (error) {
            finalize("failed", error);
            throw error;
          }
        },
        async throw(error?: any): Promise<IteratorResult<any>> {
          try {
            if (!iter.throw) throw error;
            const result = await iter.throw(error);
            if (result.done) finalize("failed", error);
            return result;
          } catch (raised) {
            finalize("failed", raised);
            throw raised;
          }
        },
      };
    },
  };
}

// Self-register so importing this module is enough to make the instrument available.
registerInstrument("anthropic", instrumentAnthropic, uninstrumentAnthropic, (ref: any) => {
  // Accept the Anthropic class, the module namespace, or Messages directly.
  const mod = ref?.default ?? ref;
  _setMessagesClass(mod?.Messages ?? mod);
  _setMessageBatchesClass(mod?.Messages?.Batches, "message_batches");
  _setMessageBatchesClass(mod?.Beta?.Messages?.Batches, "beta_message_batches");
});
