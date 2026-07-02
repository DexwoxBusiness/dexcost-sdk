/**
 * Cohere auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `CohereClient.prototype.chat` to automatically
 * record cost events and aggregate token usage on the active task context.
 *
 * Token usage from response.meta.billedUnits (inputTokens, outputTokens).
 *
 * Supports both non-streaming and streaming responses (chatStream).
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
let _originalChat: Function | null = null;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalChatStream: Function | null = null;
let _clientClass: any = null;
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;

/** Test helper: inject a mock CohereClient class so tests avoid importing cohere-ai. */
export function _setClientClass(cls: any): void {
  _clientClass = cls;
}

/** Test helper: reset to real module resolution. */
export function _resetClientClass(): void {
  _clientClass = null;
}

/**
 * Patch `CohereClient.prototype.chat` and `CohereClient.prototype.chatStream`
 * to record cost events.
 *
 * If `cohere-ai` is not installed and no mock class is injected, the dynamic
 * import will throw and the function will reject.
 */
export async function instrumentCohere(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  let ClientProto: any;
  if (_clientClass) {
    ClientProto = _clientClass.prototype;
  } else {
    // cohere-ai is an optional peer dependency; the dynamic import
    // only succeeds at runtime if the user has installed it.
    // @ts-expect-error -- cohere-ai types are not bundled with dexcost
    const cohereModule = await import("cohere-ai");
    const mod = cohereModule.default ?? cohereModule;
    ClientProto = mod.CohereClient.prototype;
  }

  _originalChat = ClientProto.chat;
  _originalChatStream = ClientProto.chatStream ?? null;
  _buffer = buffer;
  _pricing = pricing;

  ClientProto.chat = async function (
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
      task = getAmbientSessionTask("cohere.chat");
      if (!task) {
        task = createAutoTask("cohere.chat");
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const startTime = performance.now();
    const self = this;
    try {
      const response = await suppressNetworkEvent(() =>
        runWithTask(task, () => _originalChat!.call(self, body, options)),
      );
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        const model: string = body?.model ?? response?.model ?? "command-r-plus";
        recordEvent(response, model, task, latencyMs);
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

  if (_originalChatStream) {
    ClientProto.chatStream = async function (
      this: any,
      body: any,
      options?: any,
    ): Promise<any> {
      let task = getCurrentTask();
      let autoCreated = false;

      if (!task) {
        // Join the ambient session (grouping with sibling HTTP/LLM calls
        // in the same context) when session tracking is active; the
        // session sweep owns its lifecycle. Otherwise fall back to a
        // per-call auto-task owned (and finalized) here.
        task = getAmbientSessionTask("cohere.chatStream");
        if (!task) {
          task = createAutoTask("cohere.chatStream");
          _buffer?.upsertTask(task);
          autoCreated = true;
        }
      }

      const startTime = performance.now();
      const self = this;
      const model: string = body?.model ?? "command-r-plus";
      try {
        const rawStream = await suppressNetworkEvent(() =>
          runWithTask(task, () => _originalChatStream!.call(self, body, options)),
        );
        return wrapStream(rawStream, model, task, startTime, autoCreated);
      } catch (err) {
        if (autoCreated) {
          finalizeAutoTask(task, "failed", _buffer);
        }
        throw err;
      }
    };
  }

  _patched = true;
}

/**
 * Remove the monkey-patches and restore the original methods.
 */
export function uninstrumentCohere(): void {
  if (!_patched || !_originalChat) return;

  if (_clientClass) {
    _clientClass.prototype.chat = _originalChat;
    if (_originalChatStream) {
      _clientClass.prototype.chatStream = _originalChatStream;
    }
  }

  _originalChat = null;
  _originalChatStream = null;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function recordEvent(response: any, model: string, task: Task, latencyMs: number): void {
  if (!_buffer || !_pricing) return;

  const billedUnits = response?.meta?.billedUnits;
  const hasUsage = billedUnits != null;

  const inputTokens: number = billedUnits?.inputTokens ?? 0;
  const outputTokens: number = billedUnits?.outputTokens ?? 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
    const result: CostResult = _pricing.getCost(model, inputTokens, outputTokens);
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
    provider: "cohere",
    model,
    inputTokens,
    outputTokens,
    latencyMs,
    isRetry: false,
  });

  _buffer.addEvent(event);
  registerLlmCapture(task.taskId, event.inputTokens ?? 0, event.outputTokens ?? 0);

  task.llmCostUsd = task.llmCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  task.totalInputTokens += inputTokens;
  task.totalOutputTokens += outputTokens;
  _buffer.upsertTask(task);
}

function wrapStream(
  rawStream: any,
  model: string,
  task: Task,
  startTime: number,
  autoCreated: boolean = false,
): AsyncIterable<any> {
  let inputTokens = 0;
  let outputTokens = 0;
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
                const costResult = _pricing.getCost(model, inputTokens, outputTokens);
                const event = createCostEvent({
                  eventId: randomUUID(),
                  taskId: task.taskId,
                  eventType: "llm_call",
                  costUsd: costResult.costUsd,
                  costConfidence: costResult.costConfidence,
                  pricingSource: costResult.pricingSource,
                  provider: "cohere",
                  model,
                  inputTokens,
                  outputTokens,
                  latencyMs,
                  isRetry: false,
                });
                _buffer.addEvent(event);
                registerLlmCapture(task.taskId, event.inputTokens ?? 0, event.outputTokens ?? 0);
                task.llmCostUsd = task.llmCostUsd.plus(costResult.costUsd);
                task.totalCostUsd = task.totalCostUsd.plus(costResult.costUsd);
                task.totalInputTokens += inputTokens;
                task.totalOutputTokens += outputTokens;
                _buffer.upsertTask(task);
              } else if (_buffer) {
                const event = createCostEvent({
                  eventId: randomUUID(),
                  taskId: task.taskId,
                  eventType: "llm_call",
                  costUsd: 0,
                  costConfidence: "estimated",
                  pricingSource: "unknown",
                  provider: "cohere",
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
          // Cohere streaming events: look for usage in stream-end or meta events
          if (chunk?.eventType === "stream-end" && chunk?.response?.meta?.billedUnits) {
            hasUsage = true;
            inputTokens = chunk.response.meta.billedUnits.inputTokens ?? inputTokens;
            outputTokens = chunk.response.meta.billedUnits.outputTokens ?? outputTokens;
          }
          // Also check for meta.billedUnits directly on chunks
          if (chunk?.meta?.billedUnits) {
            hasUsage = true;
            inputTokens = chunk.meta.billedUnits.inputTokens ?? inputTokens;
            outputTokens = chunk.meta.billedUnits.outputTokens ?? outputTokens;
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
registerInstrument("cohere", instrumentCohere, uninstrumentCohere, (ref: any) => {
  const mod = ref?.default ?? ref;
  _setClientClass(mod?.CohereClient ?? mod);
});
