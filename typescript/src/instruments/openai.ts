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
import type { PricingEngine, CostResult } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import { normalizeOpenAIUsage, OpenAIUsageError } from "./openai-usage.js";
import { applyEventCapability } from "../core/capabilities.js";
import { applyEventIdempotency } from "../core/idempotency.js";
import { nonNegativeDecimal, prefixedModel } from "./provider-extract.js";
import { mapProviderResult, recordProviderFailure } from "./provider-metering.js";
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

    const startTime = performance.now();
    const self = this;
    const routedProvider = providerForResource(self);
    const requestedModel = typeof body?.model === "string" ? body.model : "unknown";

    let result: any;
    try {
      result = suppressNetworkEvent(() =>
        runWithTask(task, () => original.call(self, body, options)),
      );
    } catch (err) {
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType, provider: routedProvider, service: responsesApi ? "responses" : "chat",
        operation: `${routedProvider}.${responsesApi ? "responses" : "chat"}.create`,
        component: "llm", model: routedModel(routedProvider, undefined, requestedModel), eventType: "llm_call",
      }, err, startTime);
      if (autoCreated) finalizeAutoTask(task, "failed", _buffer);
      throw err;
    }
    const complete = (response: any): any => {
      if (body?.stream) {
        return wrapStream(
          response, task, startTime, autoCreated, responsesApi, routedProvider, requestedModel,
        );
      }
      if (_modernInstalled && responsesApi && body?.background === true) {
        if (autoCreated) finalizeAutoTask(task, "success", _buffer);
        return response;
      }
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        recordEvent(response, task, latencyMs, routedProvider, requestedModel);
        if (responsesApi && routedProvider === "openai" && _pricing && _buffer) {
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
        taskType, provider: routedProvider, service: responsesApi ? "responses" : "chat",
        operation: `${routedProvider}.${responsesApi ? "responses" : "chat"}.create`,
        component: "llm", model: routedModel(routedProvider, undefined, requestedModel), eventType: "llm_call",
      }, err, startTime);
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

type RoutedProvider = "openai" | "openrouter" | "perplexity" | "azure_openai";

function providerForResource(resource: any): RoutedProvider {
  try {
    const raw = String(resource?._client?.baseURL ?? resource?._client?.base_url ?? "");
    const hostname = new URL(raw).hostname.toLowerCase();
    if (hostname === "openrouter.ai" || hostname.endsWith(".openrouter.ai")) return "openrouter";
    if (hostname === "api.perplexity.ai" || hostname.endsWith(".perplexity.ai")) return "perplexity";
    if (hostname.endsWith(".openai.azure.com") || hostname.endsWith(".services.ai.azure.com")) {
      return "azure_openai";
    }
  } catch { /* the default OpenAI client may expose a relative/opaque URL */ }
  return "openai";
}

function routedModel(provider: RoutedProvider, responseModel: unknown, requestedModel: string): string {
  if (provider === "openai") {
    return typeof responseModel === "string" && responseModel.length > 0 ? responseModel : requestedModel;
  }
  const selected = provider === "azure_openai"
    ? requestedModel
    : (typeof responseModel === "string" && responseModel.length > 0 ? responseModel : requestedModel);
  return prefixedModel(provider, selected);
}

function recordEvent(
  response: any,
  task: Task,
  latencyMs: number,
  provider: RoutedProvider,
  requestedModel: string,
): void {
  if (!_buffer || !_pricing) return;

  const model = routedModel(provider, response?.model, requestedModel);
  recordUsageEvent(task, model, response?.usage, latencyMs, response?.id, provider);
}

function recordUsageEvent(
  task: Task,
  model: string,
  rawUsage: unknown,
  latencyMs: number,
  providerRecordId?: unknown,
  provider: RoutedProvider = "openai",
): void {
  if (!_buffer || !_pricing) return;

  let inputTokens = 0;
  let outputTokens = 0;
  let cachedTokens = 0;
  let cacheWriteTokens = 0;
  let reasoningTokens = 0;
  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";
  let pricingVersion: string | undefined;
  const details: Record<string, unknown> = {};

  if (typeof providerRecordId === "string" && providerRecordId.length > 0) {
    details.provider_record_id = providerRecordId;
  }
  if (provider !== "openai") {
    details.attribution_dimensions = [{ key: "gateway", value: { type: "string", value: provider } }];
  }
  if (provider === "azure_openai") {
    details.azure_deployment = model.replace(/^azure\//, "");
  }

  if (rawUsage !== undefined && rawUsage !== null) {
    try {
      const usage = normalizeOpenAIUsage(rawUsage);
      inputTokens = usage.totalInputTokens;
      outputTokens = usage.totalOutputTokens;
      cachedTokens = usage.cacheReadInputTokens;
      cacheWriteTokens = usage.cacheWriteInputTokens;
      reasoningTokens = usage.reasoningOutputTokens;
      if (cacheWriteTokens > 0) details.cache_write_input_tokens = cacheWriteTokens;
      if (reasoningTokens > 0) details.reasoning_output_tokens = reasoningTokens;

      const result: CostResult = _pricing.getCost(
        model,
        inputTokens,
        outputTokens,
        cachedTokens,
        cacheWriteTokens,
      );
      costUsd = result.costUsd;
      costConfidence = result.costConfidence;
      pricingSource = result.pricingSource;
      pricingVersion = result.pricingVersion;
      const exact = provider === "openrouter"
        ? nonNegativeDecimal((rawUsage as any)?.cost)
        : provider === "perplexity"
          ? nonNegativeDecimal((rawUsage as any)?.cost?.total_cost)
          : undefined;
      if (exact !== undefined) {
        costUsd = exact;
        costConfidence = "exact";
        pricingSource = "provider_response";
        pricingVersion = undefined;
        details.provider_reported_cost_usd = exact.toString();
      }
      if (provider === "openrouter") {
        const upstream = nonNegativeDecimal(
          (rawUsage as any)?.cost_details?.upstream_inference_cost ??
          (rawUsage as any)?.cost_details?.upstream_cost,
        );
        if (upstream !== undefined) details.provider_upstream_cost_usd = upstream.toString();
      }
    } catch (error) {
      if (!(error instanceof OpenAIUsageError)) throw error;
      details.openai_usage_error = error.message;
      costConfidence = "unknown";
    }
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
    isRetry: false,
    details,
  });
  applyEventCapability(event);
  applyEventIdempotency(event);

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
  provider: RoutedProvider = "openai",
  requestedModel: string = "unknown",
): AsyncIterable<any> {
  let model = routedModel(provider, undefined, requestedModel);
  let usage: unknown;
  let providerRecordId: unknown;
  let finalized = false;

  return {
    [Symbol.asyncIterator]() {
      const iter = rawStream[Symbol.asyncIterator]();
      const finalizeTask = (status: "success" | "failed") => {
        if (finalized) return;
        finalized = true;
        if (autoCreated) {
          finalizeAutoTask(task, status, _buffer);
        }
      };
      return {
        async next(): Promise<IteratorResult<any>> {
          let result: IteratorResult<any>;
          try {
            result = await iter.next();
          } catch (err) {
            if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
              taskType: `${provider}.${responsesApi ? "responses" : "chat"}`,
              provider, service: responsesApi ? "responses" : "chat",
              operation: `${provider}.${responsesApi ? "responses" : "chat"}.create`,
              component: "llm", model, eventType: "llm_call",
            }, err, startTime);
            finalizeTask("failed");
            throw err;
          }
          if (result.done) {
            if (finalized) return result;
            finalized = true;
            try {
              const latencyMs = Math.round(performance.now() - startTime);
              recordUsageEvent(task, routedModel(provider, model, requestedModel), usage, latencyMs, providerRecordId, provider);
            } catch {
              // dexcost errors must never crash user code
            }
            if (autoCreated) {
              finalizeAutoTask(task, "success", _buffer);
            }
            return result;
          }

          const chunk = result.value;
          const response = responsesApi && chunk?.type === "response.completed"
            ? chunk.response
            : chunk;
          if (response?.model) model = response.model;
          if (response?.id) providerRecordId = response.id;
          if (response?.usage) usage = response.usage;
          return result;
        },
        async return(value?: any): Promise<IteratorResult<any>> {
          finalizeTask("success");
          return iter.return ? await iter.return(value) : { done: true as const, value };
        },
        async throw(error?: any): Promise<IteratorResult<any>> {
          finalizeTask("failed");
          if (iter.throw) return await iter.throw(error);
          throw error;
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
