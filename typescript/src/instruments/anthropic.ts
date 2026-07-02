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

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _original: Function | null = null;
let _messagesClass: any = null;
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;

/** Test helper: inject a mock Messages class so tests avoid importing @anthropic-ai/sdk. */
export function _setMessagesClass(cls: any): void {
  _messagesClass = cls;
}

/** Test helper: reset to real module resolution. */
export function _resetMessagesClass(): void {
  _messagesClass = null;
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
  }

  _original = MessagesProto.create;
  _buffer = buffer;
  _pricing = pricing;

  MessagesProto.create = async function (
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
      task = getAmbientSessionTask("anthropic.messages");
      if (!task) {
        task = createAutoTask("anthropic.messages");
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const startTime = performance.now();

    // Scope the SDK call inside runWithTask so the HTTP adapter's
    // _resolveHttpTask() finds this task via getCurrentTask() during
    // the underlying fetch — keeps llm_call and its network bytes
    // attributed to the same task.
    const self = this;

    if (body?.stream) {
      try {
        const rawStream = await suppressNetworkEvent(() =>
          runWithTask(task, () => _original!.call(self, body, options)),
        );
        return wrapStream(rawStream, task, startTime, autoCreated);
      } catch (err) {
        if (autoCreated) {
          finalizeAutoTask(task, "failed", _buffer);
        }
        throw err;
      }
    }

    try {
      const response = await suppressNetworkEvent(() =>
        runWithTask(task, () => _original!.call(self, body, options)),
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

  _patched = true;
}

/**
 * Remove the monkey-patch and restore the original `create` method.
 */
export function uninstrumentAnthropic(): void {
  if (!_patched || !_original) return;

  if (_messagesClass) {
    _messagesClass.prototype.create = _original;
  }

  _original = null;
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
  const usage = response?.usage;
  const hasUsage = usage != null;

  const inputTokens: number = usage?.input_tokens ?? 0;
  const outputTokens: number = usage?.output_tokens ?? 0;
  const cachedTokens: number = usage?.cache_read_input_tokens ?? 0;
  const cacheCreationTokens: number = usage?.cache_creation_input_tokens ?? 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
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

  const details: Record<string, unknown> = {};
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
    provider: "anthropic",
    model,
    inputTokens,
    outputTokens,
    cachedTokens,
    latencyMs,
    isRetry: false,
    details,
  });

  _buffer.addEvent(event);
  registerLlmCapture(task.taskId, event.inputTokens ?? 0, event.outputTokens ?? 0);

  task.llmCostUsd = task.llmCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  task.totalInputTokens += inputTokens;
  task.totalOutputTokens += outputTokens;
  task.totalCachedTokens += cachedTokens;
  _buffer.upsertTask(task);
}

function wrapStream(rawStream: any, task: Task, startTime: number, autoCreated: boolean = false): AsyncIterable<any> {
  let model = "unknown";
  let inputTokens = 0;
  let outputTokens = 0;
  let cachedTokens = 0;
  let cacheCreationTokens = 0;
  let hasUsage = false;
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
              if (hasUsage && _pricing && _buffer) {
                const costResult = _pricing.getCost(
                  model,
                  inputTokens,
                  outputTokens,
                  cachedTokens,
                  cacheCreationTokens,
                );

                const details: Record<string, unknown> = {};
                if (cacheCreationTokens > 0) {
                  details["cache_creation_input_tokens"] = cacheCreationTokens;
                }

                const event = createCostEvent({
                  eventId: randomUUID(),
                  taskId: task.taskId,
                  eventType: "llm_call",
                  costUsd: costResult.costUsd,
                  costConfidence: costResult.costConfidence,
                  pricingSource: costResult.pricingSource,
                  provider: "anthropic",
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
                task.llmCostUsd = task.llmCostUsd.plus(costResult.costUsd);
                task.totalCostUsd = task.totalCostUsd.plus(costResult.costUsd);
                task.totalInputTokens += inputTokens;
                task.totalOutputTokens += outputTokens;
                task.totalCachedTokens += cachedTokens;
                _buffer.upsertTask(task);
              } else if (_buffer) {
                const event = createCostEvent({
                  eventId: randomUUID(),
                  taskId: task.taskId,
                  eventType: "llm_call",
                  costUsd: 0,
                  costConfidence: "estimated",
                  pricingSource: "unknown",
                  provider: "anthropic",
                  model,
                  inputTokens: 0,
                  outputTokens: 0,
                  latencyMs,
                  isRetry: false,
                });
                _buffer.addEvent(event);
                registerLlmCapture(task.taskId, event.inputTokens ?? 0, event.outputTokens ?? 0);
                _buffer.upsertTask(task);
              }
            } catch {
              // dexcost errors must never crash user code
            }
            if (autoCreated) {
              finalizeAutoTask(task, "success", _buffer);
            }
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
registerInstrument("anthropic", instrumentAnthropic, uninstrumentAnthropic, (ref: any) => {
  // Accept the Anthropic class, the module namespace, or Messages directly.
  const mod = ref?.default ?? ref;
  _setMessagesClass(mod?.Messages ?? mod);
});
