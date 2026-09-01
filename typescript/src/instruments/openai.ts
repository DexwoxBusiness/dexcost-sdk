/**
 * OpenAI auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches OpenAI Chat Completions and Responses `create` methods to
 * automatically record cost events and aggregate token usage on the active
 * task context.
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
import type { PricingEngine, MeteredCostResult } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import {
  applyEventCapability,
  getCapability,
  type CapabilityIdentity,
} from "../core/capabilities.js";
import {
  applyEventIdempotency,
  captureIdempotencyKey,
  type CapturedIdempotencyKey,
} from "../core/idempotency.js";
import {
  canonicalMistralModel,
  canonicalXaiModel,
  groqPricingLane,
  groqToolExecutionBlocksStaticPricing,
  mistralPricingLane,
  nonNegativeDecimal,
  nonNegativeInteger,
  prefixedModel,
  tokenMeasurement,
  xaiPricingLane,
} from "./provider-extract.js";
import { normalizeOpenAIUsage, OpenAIUsageError } from "./openai-usage.js";
import { mapProviderResult, recordProviderFailure } from "./provider-metering.js";
import { currentProviderCaptureOwner, runWithProviderCapture } from "./provider-capture.js";
import {
  canonicalLiteLlmModel,
  classifyLiteLlmProvider,
  isConfiguredLiteLlmProxyUrl,
} from "./litellm-routing.js";
import {
  installOpenAIModern,
  recordOpenAIResponseTools,
  uninstallOpenAIModern,
} from "./openai-modern.js";
import { installOpenAIRealtime, uninstallOpenAIRealtime } from "./openai-realtime.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
let _instrumenting: Promise<void> | null = null;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
const _patches: Array<{ prototype: any; original: Function }> = [];
let _completionsClass: any = null;
let _responsesClass: any = null;
let _providedModule: any = null;
let _modernInstalled = false;
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;

/** Test helper: inject a mock Completions class so tests avoid importing openai. */
export function _setCompletionsClass(cls: any): void {
  _completionsClass = cls;
}

/** Test helper: reset to real module resolution. */
export function _resetCompletionsClass(): void {
  _completionsClass = null;
}

/** Test helper: inject a mock Responses class. */
export function _setResponsesClass(cls: any): void {
  _responsesClass = cls;
}

/** Test helper: reset Responses module resolution. */
export function _resetResponsesClass(): void {
  _responsesClass = null;
}

/** Test/bundler reset helper for an explicitly supplied OpenAI module. */
export function _resetOpenAIModule(): void {
  _providedModule = null;
}

/**
 * Patch `OpenAI.Chat.Completions.prototype.create` to record cost events.
 *
 * If `openai` is not installed and no mock class is injected, the dynamic
 * import will throw and the function will reject.
 */
export function instrumentOpenai(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return Promise.resolve();
  if (_instrumenting) return _instrumenting;
  _instrumenting = installOpenai(pricing, buffer).finally(() => {
    _instrumenting = null;
  });
  return _instrumenting;
}

async function installOpenai(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  let CompletionsProto: any;
  let ResponsesProto: any;
  let OpenAIRoot: any = _providedModule;
  if (_completionsClass) {
    CompletionsProto = _completionsClass.prototype;
  } else {
    // openai is an optional peer dependency; the dynamic import only
    // succeeds at runtime if the user has installed it.
    // @ts-ignore -- openai is an optional peer dependency
    const openai = await import("openai");
    const OpenAI = openai.default ?? openai;
    OpenAIRoot = OpenAI;
    CompletionsProto = OpenAI.Chat.Completions.prototype;
    ResponsesProto = OpenAI.Responses?.prototype;
  }
  if (_responsesClass) ResponsesProto = _responsesClass.prototype;
  if (!ResponsesProto) {
    try {
      // @ts-ignore -- optional peer dependency and version-dependent export
      const responses = await import("openai/resources/responses/responses");
      ResponsesProto = responses.Responses?.prototype;
    } catch {
      // OpenAI SDK versions predating Responses remain Chat-compatible.
    }
  }

  _buffer = buffer;
  _pricing = pricing;

  if (OpenAIRoot) {
    _modernInstalled = installOpenAIModern(OpenAIRoot, pricing, buffer);
    installOpenAIRealtime(OpenAIRoot, pricing, buffer);
  }
  if (!_providedModule && !_completionsClass) {
    try {
      // @ts-ignore -- optional peer dependency and version-dependent export
      installOpenAIRealtime(await import("openai/realtime/ws"), pricing, buffer);
    } catch {
      // Older OpenAI SDKs do not expose the Realtime ws helper.
    }
    try {
      // @ts-ignore -- optional peer dependency and version-dependent export
      installOpenAIRealtime(await import("openai/realtime/websocket"), pricing, buffer);
    } catch {
      // Older OpenAI SDKs do not expose the native WebSocket helper.
    }
  }
  patchCreate(CompletionsProto, "openai.chat", false);
  if (ResponsesProto) patchCreate(ResponsesProto, "openai.responses", true);
  _patched = true;
}

function patchCreate(prototype: any, taskType: string, responsesApi: boolean): void {
  const original = prototype.create as Function;
  _patches.push({ prototype, original });
  prototype.create = function (
    this: any,
    body: any,
    options?: any,
  ): any {
    if (currentProviderCaptureOwner() !== undefined) {
      return original.call(this, body, options);
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
      task = getAmbientSessionTask(taskType);
      if (!task) {
        task = createAutoTask(taskType);
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const requestedModel = typeof body?.model === "string" ? body.model : "unknown";
    const startTime = performance.now();
    const self = this;
    const route = providerForResource(self, requestedModel);
    const serviceTier = requestServiceTier(route, body);
    const capability = getCapability();
    const idempotencyKey = captureIdempotencyKey();

    let result: any;
    try {
      result = suppressNetworkEvent(() =>
        runWithProviderCapture(route.provider, () =>
          runWithTask(task, () => original.call(self, body, options))),
      );
    } catch (err) {
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType, provider: route.provider, service: routedService(route, responsesApi),
        operation: routedOperation(route, responsesApi),
        component: "llm", model: routedModel(route, undefined, requestedModel), eventType: "llm_call",
      }, err, startTime, capability, idempotencyKey);
      if (autoCreated) finalizeAutoTask(task, "failed", _buffer);
      throw err;
    }
    const complete = (response: any): any => {
      if (body?.stream) {
        return wrapStream(
          response, task, startTime, autoCreated, responsesApi, route, requestedModel,
          capability, idempotencyKey, serviceTier,
        );
      }
      if (_modernInstalled && responsesApi && body?.background === true) {
        if (autoCreated) finalizeAutoTask(task, "success", _buffer);
        return response;
      }
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        recordEvent(
          response, task, latencyMs, route, requestedModel, responsesApi,
          capability, idempotencyKey, serviceTier,
        );
        if (responsesApi && route.provider === "openai" && route.gateway === undefined &&
            _pricing && _buffer) {
          recordOpenAIResponseTools(response, task, _pricing, _buffer);
        }
      } catch {
        // dexcost errors must never crash user code
      }
      if (autoCreated) {
        finalizeAutoTask(task, "success", _buffer);
      }
      return response;
    };
    return mapProviderResult(result, complete, (err) => {
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType, provider: route.provider, service: routedService(route, responsesApi),
        operation: routedOperation(route, responsesApi),
        component: "llm", model: routedModel(route, undefined, requestedModel), eventType: "llm_call",
      }, err, startTime, capability, idempotencyKey);
      if (autoCreated) {
        finalizeAutoTask(task, "failed", _buffer);
      }
      throw err;
    });
  };
}

/**
 * Remove the monkey-patch and restore the original `create` method.
 */
export function uninstrumentOpenai(): void {
  if (!_patched) return;
  for (const patch of _patches) patch.prototype.create = patch.original;
  _patches.length = 0;
  uninstallOpenAIModern();
  uninstallOpenAIRealtime();
  _modernInstalled = false;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

interface RoutedIdentity {
  provider: string;
  gateway?: "litellm";
}

function providerForResource(resource: any, requestedModel: string): RoutedIdentity {
  try {
    const raw = String(resource?._client?.baseURL ?? resource?._client?.base_url ?? "");
    const hostname = new URL(raw).hostname.toLowerCase();
    if (isConfiguredLiteLlmProxyUrl(raw)) {
      return { provider: classifyLiteLlmProvider(requestedModel), gateway: "litellm" };
    }
    if (hostname === "openrouter.ai" || hostname.endsWith(".openrouter.ai")) {
      return { provider: "openrouter" };
    }
    if (hostname === "api.perplexity.ai" || hostname.endsWith(".perplexity.ai")) {
      return { provider: "perplexity" };
    }
    if (hostname === "api.deepseek.com" || hostname.endsWith(".deepseek.com")) {
      return { provider: "deepseek" };
    }
    if (hostname === "api.fireworks.ai" || hostname.endsWith(".api.fireworks.ai")) {
      return { provider: "fireworks_ai" };
    }
    if (hostname === "api.x.ai" || hostname.endsWith(".api.x.ai")) {
      return { provider: "xai" };
    }
    if (hostname === "api.groq.com" || hostname.endsWith(".api.groq.com")) {
      return { provider: "groq" };
    }
    if (hostname === "api.mistral.ai") {
      return { provider: "mistral" };
    }
    if (hostname.endsWith(".openai.azure.com") || hostname.endsWith(".services.ai.azure.com")) {
      return { provider: "azure_openai" };
    }
  } catch { /* the default OpenAI client may expose a relative/opaque URL */ }
  return { provider: "openai" };
}

function requestServiceTier(route: RoutedIdentity, body: any): unknown {
  const value = body?.service_tier ?? body?.extra_body?.service_tier;
  if (route.provider === "fireworks_ai") return value === "priority" ? "priority" : "default";
  return route.provider === "groq" ? value : undefined;
}

function routedService(route: RoutedIdentity, responsesApi: boolean): string {
  return route.gateway ?? (responsesApi ? "responses" : "chat");
}

function routedOperation(route: RoutedIdentity, responsesApi: boolean): string {
  const owner = route.gateway ?? route.provider;
  return `${owner}.${responsesApi ? "responses" : "chat"}.create`;
}

function routedModel(route: RoutedIdentity, responseModel: unknown, requestedModel: string): string {
  if (route.gateway === "litellm") {
    return canonicalLiteLlmModel(route.provider, responseModel, requestedModel);
  }
  if (["openai", "deepseek", "fireworks_ai", "xai", "groq", "mistral"].includes(route.provider) && route.gateway === undefined) {
    const selected = typeof responseModel === "string" && responseModel.length > 0
      ? responseModel
      : requestedModel;
    return route.provider === "xai"
      ? canonicalXaiModel(selected)
      : route.provider === "mistral"
        ? canonicalMistralModel(selected)
        : selected;
  }
  const selected = route.provider === "azure_openai" || route.gateway !== undefined
    ? requestedModel
    : (typeof responseModel === "string" && responseModel.length > 0 ? responseModel : requestedModel);
  return prefixedModel(route.provider, selected);
}

function recordEvent(
  response: any,
  task: Task,
  latencyMs: number,
  route: RoutedIdentity,
  requestedModel: string,
  responsesApi: boolean,
  capability: CapabilityIdentity | undefined,
  idempotencyKey: CapturedIdempotencyKey | undefined,
  serviceTier?: unknown,
): void {
  if (!_buffer || !_pricing) return;

  const model = routedModel(route, response?.model, requestedModel);
  recordUsageEvent(
    task, model, response?.usage, latencyMs, response?.id, route,
    responsesApi, "succeeded", undefined, capability, idempotencyKey, response, serviceTier,
  );
}

function recordUsageEvent(
  task: Task,
  model: string,
  rawUsage: unknown,
  latencyMs: number,
  providerRecordId?: unknown,
  route: RoutedIdentity = { provider: "openai" },
  responsesApi: boolean = false,
  status: "succeeded" | "failed" | "cancelled" = "succeeded",
  operationError?: unknown,
  capability?: CapabilityIdentity,
  idempotencyKey?: CapturedIdempotencyKey,
  rawResponse?: unknown,
  serviceTier?: unknown,
): void {
  if (!_buffer || !_pricing) return;
  const provider = route.provider;
  const service = routedService(route, responsesApi);

  let inputTokens = 0;
  let outputTokens = 0;
  let billableInputTokens = 0;
  let billableOutputTokens = 0;
  let cachedTokens = 0;
  let cacheWriteTokens = 0;
  let reasoningTokens = 0;
  let serverToolCallsRequested = 0;
  let serverToolCallsExecuted = 0;
  let webSearchRequests = 0;
  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = status === "succeeded" ? "estimated" : "unknown";
  let pricingSource: PricingSource = "unknown";
  let pricingVersion: string | undefined;
  const details: Record<string, unknown> = {
    attribution_component: "llm",
    attribution_operation_name: routedOperation(route, responsesApi),
    attribution_operation_status: status,
    attribution_resource_type: "model",
    attribution_resource_id: model,
  };

  if (typeof providerRecordId === "string" && providerRecordId.length > 0) {
    details.provider_record_id = providerRecordId;
  }
  if (route.gateway !== undefined) {
    details.attribution_dimensions = [{
      key: "gateway",
      value: { type: "string", value: route.gateway },
    }];
  } else if (provider !== "openai") {
    details.attribution_dimensions = [{ key: "gateway", value: { type: "string", value: provider } }];
  }
  if (provider === "fireworks_ai" && typeof serviceTier === "string") {
    const dimensions = Array.isArray(details.attribution_dimensions)
      ? details.attribution_dimensions as Array<Record<string, unknown>>
      : [];
    details.attribution_dimensions = [
      ...dimensions,
      { key: "service_tier", value: { type: "string", value: serviceTier } },
    ];
  }
  if (provider === "azure_openai") {
    details.azure_deployment = model.replace(/^azure\//, "");
  }

  let providerCostUsd: Decimal | undefined;
  let providerUpstreamCostUsd: Decimal | undefined;
  if (rawUsage !== undefined && rawUsage !== null) {
    try {
      // Native OpenAI-compatible routes preserve the strict validation and
      // durable diagnostic used by the established contract. LiteLLM is
      // intentionally tolerant because some upstream providers expose only
      // ordinary totals plus non-OpenAI detail buckets.
      if (route.gateway === undefined) normalizeOpenAIUsage(rawUsage);
      const measurement = tokenMeasurement(rawResponse ?? { model, usage: rawUsage }, model, provider);
      inputTokens = measurement.inputTokens ?? 0;
      outputTokens = measurement.outputTokens ?? 0;
      cachedTokens = measurement.cachedTokens ?? 0;
      cacheWriteTokens = measurement.cacheWriteTokens ?? 0;
      reasoningTokens = measurement.reasoningTokens ?? 0;
      const quantities = new Map(
        (measurement.usageLines ?? []).map((line) => [line.metric, nonNegativeInteger(line.quantity)]),
      );
      billableInputTokens = quantities.get("input_tokens") ?? 0;
      billableOutputTokens = quantities.get("output_tokens") ?? 0;
      serverToolCallsRequested = quantities.get("server_tool_calls_requested") ?? 0;
      serverToolCallsExecuted = quantities.get("server_tool_calls_executed") ?? 0;
      webSearchRequests = quantities.get("web_search_requests") ?? 0;
      providerCostUsd = measurement.providerCostUsd === undefined
        ? undefined
        : nonNegativeDecimal(measurement.providerCostUsd);
      providerUpstreamCostUsd = measurement.providerUpstreamCostUsd === undefined
        ? undefined
        : nonNegativeDecimal(measurement.providerUpstreamCostUsd);
    } catch (error) {
      // Response parsing is telemetry-only. Preserve trustworthy ordinary
      // totals even if a newly added detail bucket is malformed.
      if (error instanceof OpenAIUsageError) details.openai_usage_error = error.message;
      const usage = rawUsage as any;
      inputTokens = nonNegativeInteger(usage?.prompt_tokens ?? usage?.input_tokens);
      outputTokens = nonNegativeInteger(usage?.completion_tokens ?? usage?.output_tokens);
      billableInputTokens = inputTokens;
      billableOutputTokens = outputTokens;
    }
    if (cacheWriteTokens > 0) details.cache_write_input_tokens = cacheWriteTokens;
    if (reasoningTokens > 0) details.reasoning_output_tokens = reasoningTokens;

    const result: MeteredCostResult = _pricing.getMeteredCost(model, {
      input_tokens: billableInputTokens,
      cache_read_input_tokens: cachedTokens,
      cache_write_input_tokens: cacheWriteTokens,
      output_tokens: billableOutputTokens,
      reasoning_output_tokens: reasoningTokens,
      server_tool_calls_requested: serverToolCallsRequested,
      server_tool_calls_executed: serverToolCallsExecuted,
      web_search_requests: webSearchRequests,
    });
    costUsd = result.costUsd;
    costConfidence = result.costConfidence;
    pricingSource = result.pricingSource;
    pricingVersion = result.pricingVersion;
    if (result.unpricedDimensions.length > 0) {
      details.pricing_unpriced_dimensions = result.unpricedDimensions;
    }
    if (providerCostUsd !== undefined) {
      costUsd = providerCostUsd;
      costConfidence = "exact";
      pricingSource = "provider_response";
      pricingVersion = undefined;
      details.provider_reported_cost_usd = providerCostUsd.toString();
    }
    if (providerUpstreamCostUsd !== undefined) {
      details.provider_upstream_cost_usd = providerUpstreamCostUsd.toString();
    }
  }
  if (provider === "xai") {
    const pricingLane = xaiPricingLane(
      rawResponse ?? { usage: rawUsage },
      inputTokens,
    );
    if (pricingLane !== undefined) {
      const dimensions = Array.isArray(details.attribution_dimensions)
        ? details.attribution_dimensions as Array<Record<string, unknown>>
        : [];
      details.attribution_dimensions = [
        ...dimensions,
        { key: "xai_pricing_lane", value: { type: "string", value: pricingLane } },
      ];
    }
  }
  if (provider === "groq") {
    const response = rawResponse !== null && typeof rawResponse === "object"
      ? rawResponse as Record<string, unknown>
      : { usage: rawUsage };
    const pricingLane = groqPricingLane({
      ...response,
      ...((response.service_tier === undefined || response.service_tier === null) &&
          serviceTier !== undefined
        ? { service_tier: serviceTier }
        : {}),
    });
    if (pricingLane !== undefined) {
      const dimensions = Array.isArray(details.attribution_dimensions)
        ? details.attribution_dimensions as Array<Record<string, unknown>>
        : [];
      details.attribution_dimensions = [
        ...dimensions,
        { key: "groq_pricing_lane", value: { type: "string", value: pricingLane } },
      ];
    }
  }
  if (provider === "mistral") {
    const pricingLane = mistralPricingLane(rawResponse ?? { usage: rawUsage });
    if (pricingLane !== undefined) {
      const dimensions = Array.isArray(details.attribution_dimensions)
        ? details.attribution_dimensions as Array<Record<string, unknown>>
        : [];
      details.attribution_dimensions = [
        ...dimensions,
        { key: "mistral_pricing_lane", value: { type: "string", value: pricingLane } },
      ];
    }
  }
  const usageLines = [
    ...(billableInputTokens > 0 ? [{ metric: "input_tokens", quantity: String(billableInputTokens), unit: "Tokens" }] : []),
    ...(cachedTokens > 0 ? [{ metric: "cache_read_input_tokens", quantity: String(cachedTokens), unit: "Tokens" }] : []),
    ...(cacheWriteTokens > 0 ? [{ metric: "cache_write_input_tokens", quantity: String(cacheWriteTokens), unit: "Tokens" }] : []),
    ...(billableOutputTokens > 0 ? [{ metric: "output_tokens", quantity: String(billableOutputTokens), unit: "Tokens" }] : []),
    ...(reasoningTokens > 0 ? [{ metric: "reasoning_output_tokens", quantity: String(reasoningTokens), unit: "Tokens" }] : []),
    ...(serverToolCallsRequested > 0 ? [{
      metric: "server_tool_calls_requested", quantity: String(serverToolCallsRequested), unit: "Calls",
    }] : []),
    ...(serverToolCallsExecuted > 0 ? [{
      metric: "server_tool_calls_executed", quantity: String(serverToolCallsExecuted), unit: "Calls",
    }] : []),
    ...(webSearchRequests > 0 ? [{
      metric: "web_search_requests", quantity: String(webSearchRequests), unit: "Requests",
    }] : []),
  ];
  if (details.openai_usage_error === undefined) {
    details.attribution_usage_lines = usageLines.length > 0
      ? usageLines
      : [{ metric: "request_count", quantity: "1", unit: "Requests" }];
  }
  if (operationError !== undefined) {
    details.attribution_error_type = operationError instanceof Error
      ? operationError.name.toLowerCase()
      : typeof operationError;
  }

  const event = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
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
    serviceName: service,
    isRetry: false,
    details,
  });
  applyEventCapability(event, capability);
  applyEventIdempotency(event, idempotencyKey);

  _buffer.addEvent(event);
  registerLlmCapture(task.taskId, inputTokens, outputTokens);

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
  responsesApi: boolean = false,
  route: RoutedIdentity = { provider: "openai" },
  requestedModel: string = "unknown",
  capability?: CapabilityIdentity,
  idempotencyKey?: CapturedIdempotencyKey,
  serviceTier?: unknown,
): AsyncIterable<any> {
  let model = routedModel(route, undefined, requestedModel);
  let usage: unknown;
  let providerRecordId: unknown;
  let terminalResponse: unknown;
  let groqServiceTier: unknown = route.provider === "groq" ? serviceTier : undefined;
  let groqToolExecutionSeen = false;
  let finalized = false;

  const finalize = (
    status: "succeeded" | "failed" | "cancelled",
    error?: unknown,
  ): void => {
    if (finalized) return;
    finalized = true;
    try {
      const pricingResponse = route.provider === "groq"
        ? {
            ...(terminalResponse !== null && typeof terminalResponse === "object"
              ? terminalResponse as Record<string, unknown>
              : {}),
            ...(groqServiceTier === undefined ? {} : { service_tier: groqServiceTier }),
            _dexcost_groq_tool_execution_seen: groqToolExecutionSeen,
          }
        : terminalResponse;
      recordUsageEvent(
        task, routedModel(route, model, requestedModel), usage,
        Math.round(performance.now() - startTime), providerRecordId, route,
        responsesApi, status, error, capability, idempotencyKey, pricingResponse,
        serviceTier,
      );
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
          const response = responsesApi && chunk?.type === "response.completed"
            ? chunk.response
            : chunk;
          terminalResponse = response;
          if (route.provider === "groq") {
            if (response?.service_tier !== undefined && response?.service_tier !== null) {
              groqServiceTier = response.service_tier;
            }
            if (groqToolExecutionBlocksStaticPricing(response)) groqToolExecutionSeen = true;
          }
          if (response?.model) model = response.model;
          if (response?.id) providerRecordId = response.id;
          if (response?.usage) usage = response.usage;
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
registerInstrument("openai", instrumentOpenai, uninstrumentOpenai, (ref: any) => {
  // Accept the OpenAI class, the module namespace, or Completions directly.
  const mod = ref?.default ?? ref;
  _providedModule = mod;
  const completions = mod?.Chat?.Completions;
  if (completions) _setCompletionsClass(completions);
  const responses = mod?.Responses ?? ref?.Responses;
  if (responses) _setResponsesClass(responses);
});
