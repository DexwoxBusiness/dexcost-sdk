/**
 * OpenAI auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `OpenAI.Chat.Completions.prototype.create` to automatically
 * record cost events and aggregate token usage on the active task context.
 *
 * Supports both non-streaming and streaming responses.
 */

import { randomUUID } from "node:crypto";
import { createCostEvent, Decimal } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import { getCurrentTask, runWithTask, suppressNetworkEvent } from "../core/context.js";
import { createAutoTask } from "../core/auto-task.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine, CostResult } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _original: Function | null = null;
let _completionsClass: any = null;
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

/**
 * Patch `OpenAI.Chat.Completions.prototype.create` to record cost events.
 *
 * If `openai` is not installed and no mock class is injected, the dynamic
 * import will throw and the function will reject.
 */
export async function instrumentOpenai(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  let CompletionsProto: any;
  if (_completionsClass) {
    CompletionsProto = _completionsClass.prototype;
  } else {
    // openai is an optional peer dependency; the dynamic import only
    // succeeds at runtime if the user has installed it.
    // @ts-ignore -- openai is an optional peer dependency
    const openai = await import("openai");
    const OpenAI = openai.default ?? openai;
    CompletionsProto = OpenAI.Chat.Completions.prototype;
  }

  _original = CompletionsProto.create;
  _buffer = buffer;
  _pricing = pricing;

  CompletionsProto.create = async function (
    this: any,
    body: any,
    options?: any,
  ): Promise<any> {
    let task = getCurrentTask();
    let autoCreated = false;

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      task = createAutoTask("openai.chat");
      _buffer?.upsertTask(task);
      autoCreated = true;
    }

    const startTime = performance.now();
    const self = this;

    if (body?.stream) {
      try {
        const rawStream = await suppressNetworkEvent(() =>
          runWithTask(task, () => _original!.call(self, body, options)),
        );
        return wrapStream(rawStream, task, startTime, autoCreated);
      } catch (err) {
        if (autoCreated) {
          task.status = "failed";
          task.endedAt = new Date();
          _buffer?.upsertTask(task);
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
        task.status = "success";
        task.endedAt = new Date();
        _buffer?.upsertTask(task);
      }
      return response;
    } catch (err) {
      if (autoCreated) {
        task.status = "failed";
        task.endedAt = new Date();
        _buffer?.upsertTask(task);
      }
      throw err;
    }
  };

  _patched = true;
}

/**
 * Remove the monkey-patch and restore the original `create` method.
 */
export function uninstrumentOpenai(): void {
  if (!_patched || !_original) return;

  if (_completionsClass) {
    _completionsClass.prototype.create = _original;
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

  const inputTokens: number = usage?.prompt_tokens ?? 0;
  const outputTokens: number = usage?.completion_tokens ?? 0;
  const cachedTokens: number = usage?.prompt_tokens_details?.cached_tokens ?? 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
    const result: CostResult = _pricing.getCost(model, inputTokens, outputTokens, cachedTokens);
    costUsd = result.costUsd;
    costConfidence = result.costConfidence;
    pricingSource = result.pricingSource;
  }

  const event = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
    eventType: "llm_call",
    costUsd,
    costConfidence,
    pricingSource,
    provider: "openai",
    model,
    inputTokens,
    outputTokens,
    cachedTokens,
    latencyMs,
    isRetry: false,
  });

  _buffer.addEvent(event);

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
  let hasUsage = false;
  let finalized = false;

  return {
    [Symbol.asyncIterator]() {
      const iter = rawStream[Symbol.asyncIterator]();
      const finalizeTask = (status: "success" | "failed") => {
        if (finalized) return;
        finalized = true;
        if (autoCreated && _buffer) {
          task.status = status;
          task.endedAt = new Date();
          _buffer.upsertTask(task);
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
                const costResult = _pricing.getCost(model, inputTokens, outputTokens, cachedTokens);
                const event = createCostEvent({
                  eventId: randomUUID(),
                  taskId: task.taskId,
                  eventType: "llm_call",
                  costUsd: costResult.costUsd,
                  costConfidence: costResult.costConfidence,
                  pricingSource: costResult.pricingSource,
                  provider: "openai",
                  model,
                  inputTokens,
                  outputTokens,
                  cachedTokens,
                  latencyMs,
                  isRetry: false,
                });
                _buffer.addEvent(event);
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
                  provider: "openai",
                  model,
                  inputTokens: 0,
                  outputTokens: 0,
                  latencyMs,
                  isRetry: false,
                });
                _buffer.addEvent(event);
                _buffer.upsertTask(task);
              }
            } catch {
              // dexcost errors must never crash user code
            }
            if (autoCreated && _buffer) {
              task.status = "success";
              task.endedAt = new Date();
              _buffer.upsertTask(task);
            }
            return result;
          }

          const chunk = result.value;
          if (chunk?.model) model = chunk.model;
          if (chunk?.usage) {
            hasUsage = true;
            inputTokens = chunk.usage.prompt_tokens ?? inputTokens;
            outputTokens = chunk.usage.completion_tokens ?? outputTokens;
            cachedTokens = chunk.usage.prompt_tokens_details?.cached_tokens ?? cachedTokens;
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
registerInstrument("openai", instrumentOpenai, uninstrumentOpenai);
