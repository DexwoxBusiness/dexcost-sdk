/**
 * Google Gemini auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `GenerativeModel.prototype.generateContent` to automatically
 * record cost events and aggregate token usage on the active task context.
 *
 * Token usage from response.usageMetadata (promptTokenCount,
 * candidatesTokenCount, cachedContentTokenCount).
 *
 * Supports both non-streaming and streaming responses (generateContentStream).
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
let _originalGenerateContent: Function | null = null;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalGenerateContentStream: Function | null = null;
let _generativeModelClass: any = null;
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;

/** Test helper: inject a mock GenerativeModel class so tests avoid importing @google/generative-ai. */
export function _setGenerativeModelClass(cls: any): void {
  _generativeModelClass = cls;
}

/** Test helper: reset to real module resolution. */
export function _resetGenerativeModelClass(): void {
  _generativeModelClass = null;
}

/**
 * Patch `GenerativeModel.prototype.generateContent` and
 * `GenerativeModel.prototype.generateContentStream` to record cost events.
 *
 * If `@google/generative-ai` is not installed and no mock class is injected,
 * the dynamic import will throw and the function will reject.
 */
export async function instrumentGemini(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  let GenerativeModelProto: any;
  if (_generativeModelClass) {
    GenerativeModelProto = _generativeModelClass.prototype;
  } else {
    // @google/generative-ai is an optional peer dependency; the dynamic import
    // only succeeds at runtime if the user has installed it.
    // @ts-expect-error -- google generative-ai types are not bundled with dexcost
    const geminiModule = await import("@google/generative-ai");
    const mod = geminiModule.default ?? geminiModule;
    GenerativeModelProto = mod.GenerativeModel.prototype;
  }

  _originalGenerateContent = GenerativeModelProto.generateContent;
  _originalGenerateContentStream = GenerativeModelProto.generateContentStream;
  _buffer = buffer;
  _pricing = pricing;

  GenerativeModelProto.generateContent = async function (
    this: any,
    ...args: any[]
  ): Promise<any> {
    let task = getCurrentTask();
    let autoCreated = false;

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      task = createAutoTask("gemini.generateContent");
      _buffer?.upsertTask(task);
      autoCreated = true;
    }

    const startTime = performance.now();
    const self = this;
    try {
      const response = await suppressNetworkEvent(() =>
        runWithTask(task, () => _originalGenerateContent!.apply(self, args)),
      );
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        const model: string = self.model ?? self._modelParams?.model ?? "unknown";
        recordEvent(response?.response ?? response, model, task, latencyMs);
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

  GenerativeModelProto.generateContentStream = async function (
    this: any,
    ...args: any[]
  ): Promise<any> {
    let task = getCurrentTask();
    let autoCreated = false;

    if (!task) {
      task = createAutoTask("gemini.generateContentStream");
      _buffer?.upsertTask(task);
      autoCreated = true;
    }

    const startTime = performance.now();
    const self = this;
    const model: string = self.model ?? self._modelParams?.model ?? "unknown";
    try {
      const streamResult = await suppressNetworkEvent(() =>
        runWithTask(task, () => _originalGenerateContentStream!.apply(self, args)),
      );
      return wrapStream(streamResult, model, task, startTime, autoCreated);
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
 * Remove the monkey-patches and restore the original methods.
 */
export function uninstrumentGemini(): void {
  if (!_patched || !_originalGenerateContent) return;

  if (_generativeModelClass) {
    _generativeModelClass.prototype.generateContent = _originalGenerateContent;
    if (_originalGenerateContentStream) {
      _generativeModelClass.prototype.generateContentStream = _originalGenerateContentStream;
    }
  }

  _originalGenerateContent = null;
  _originalGenerateContentStream = null;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function recordEvent(response: any, model: string, task: Task, latencyMs: number): void {
  if (!_buffer || !_pricing) return;

  const usage = response?.usageMetadata;
  const hasUsage = usage != null;

  const inputTokens: number = usage?.promptTokenCount ?? 0;
  const outputTokens: number = usage?.candidatesTokenCount ?? 0;
  const cachedTokens: number = usage?.cachedContentTokenCount ?? 0;

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
    provider: "google",
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

function wrapStream(
  rawStream: any,
  model: string,
  task: Task,
  startTime: number,
  autoCreated: boolean = false,
): any {
  // Gemini streaming returns an object with a `stream` async iterable
  // and a `response` promise. We wrap the stream to capture usage at the end.
  const stream = rawStream?.stream;
  if (!stream || typeof stream[Symbol.asyncIterator] !== "function") {
    return rawStream;
  }

  let inputTokens = 0;
  let outputTokens = 0;
  let cachedTokens = 0;
  let hasUsage = false;
  let finalized = false;

  const wrappedStream = {
    [Symbol.asyncIterator]() {
      const iter = stream[Symbol.asyncIterator]();
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
            if (autoCreated && _buffer) {
              task.status = "failed";
              task.endedAt = new Date();
              _buffer.upsertTask(task);
            }
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
                  provider: "google",
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
                  provider: "google",
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
          if (chunk?.usageMetadata) {
            hasUsage = true;
            inputTokens = chunk.usageMetadata.promptTokenCount ?? inputTokens;
            outputTokens = chunk.usageMetadata.candidatesTokenCount ?? outputTokens;
            cachedTokens = chunk.usageMetadata.cachedContentTokenCount ?? cachedTokens;
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

  // Return an object that preserves the original shape: { stream, response }
  return {
    stream: wrappedStream,
    response: rawStream.response,
  };
}
// Self-register so importing this module is enough to make the instrument available.
registerInstrument("gemini", instrumentGemini, uninstrumentGemini);
