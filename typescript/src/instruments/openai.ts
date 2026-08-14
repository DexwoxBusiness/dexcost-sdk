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

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
let _instrumenting: Promise<void> | null = null;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
const _patches: Array<{ prototype: any; original: Function }> = [];
let _completionsClass: any = null;
let _responsesClass: any = null;
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
  if (_completionsClass) {
    CompletionsProto = _completionsClass.prototype;
  } else {
    // openai is an optional peer dependency; the dynamic import only
    // succeeds at runtime if the user has installed it.
    // @ts-ignore -- openai is an optional peer dependency
    const openai = await import("openai");
    const OpenAI = openai.default ?? openai;
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

  patchCreate(CompletionsProto, "openai.chat", false);
  if (ResponsesProto) patchCreate(ResponsesProto, "openai.responses", true);
  _patched = true;
}

function patchCreate(prototype: any, taskType: string, responsesApi: boolean): void {
  const original = prototype.create as Function;
  _patches.push({ prototype, original });
  prototype.create = async function (
    this: any,
    body: any,
    options?: any,
  ): Promise<any> {
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

    if (body?.stream) {
      try {
        const rawStream = await suppressNetworkEvent(() =>
          runWithTask(task, () => original.call(self, body, options)),
        );
        return wrapStream(rawStream, task, startTime, autoCreated, responsesApi);
      } catch (err) {
        if (autoCreated) {
          finalizeAutoTask(task, "failed", _buffer);
        }
        throw err;
      }
    }

    try {
      const response = await suppressNetworkEvent(() =>
        runWithTask(task, () => original.call(self, body, options)),
      );
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        recordEvent(response, task, latencyMs);
      } catch {
        // dexcost errors must never crash user code
      }
      if (autoCreated) {
        finalizeAutoTask(task, "success", _buffer);
      }
      return response;
    } catch (err) {
      if (autoCreated) {
        finalizeAutoTask(task, "failed", _buffer);
      }
      throw err;
    }
  };
}

/**
 * Remove the monkey-patch and restore the original `create` method.
 */
export function uninstrumentOpenai(): void {
  if (!_patched) return;
  for (const patch of _patches) patch.prototype.create = patch.original;
  _patches.length = 0;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function recordEvent(response: any, task: Task, latencyMs: number): void {
  if (!_buffer || !_pricing) return;

  const model: string = response?.model ?? "unknown";
  recordUsageEvent(task, model, response?.usage, latencyMs, response?.id);
}

function recordUsageEvent(
  task: Task,
  model: string,
  rawUsage: unknown,
  latencyMs: number,
  providerRecordId?: unknown,
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
    provider: "openai",
    model,
    inputTokens,
    outputTokens,
    cachedTokens,
    latencyMs,
    isRetry: false,
    details,
  });

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
): AsyncIterable<any> {
  let model = "unknown";
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
            finalizeTask("failed");
            throw err;
          }
          if (result.done) {
            if (finalized) return result;
            finalized = true;
            try {
              const latencyMs = Math.round(performance.now() - startTime);
              recordUsageEvent(task, model, usage, latencyMs, providerRecordId);
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
  _setCompletionsClass(mod?.Chat?.Completions ?? mod);
  const responses = mod?.Responses ?? ref?.Responses;
  if (responses) _setResponsesClass(responses);
});
