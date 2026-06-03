/**
 * Vercel AI SDK auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `generateText` and `streamText` from the `ai` package to
 * automatically record cost events and aggregate token usage on the active
 * task context.
 *
 * The `ai` package is an optional peer dependency; the instrument gracefully
 * no-ops if the package is not installed.
 */

import { randomUUID } from "node:crypto";
import { createCostEvent, Decimal } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import { getCurrentTask } from "../core/context.js";
import { createAutoTask } from "../core/auto-task.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine, CostResult } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalGenerateText: Function | null = null;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalStreamText: Function | null = null;
let _aiModule: any = null;
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;

/** Test helper: inject a mock ai module so tests avoid importing ai. */
export function _setAiModule(mod: any): void {
  _aiModule = mod;
}

/** Test helper: reset to real module resolution. */
export function _resetAiModule(): void {
  _aiModule = null;
}

/**
 * Extract the model identifier from Vercel AI SDK options.
 *
 * The `ai` package accepts model as either a string or an object with
 * a `modelId` property (e.g., `openai("gpt-4o")` returns an object).
 */
function extractModel(opts: any): string {
  if (typeof opts?.model === "string") return opts.model;
  if (opts?.model?.modelId) return String(opts.model.modelId);
  return "unknown";
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function recordEvent(
  model: string,
  inputTokens: number,
  outputTokens: number,
  task: Task,
  latencyMs: number,
): void {
  if (!_buffer || !_pricing) return;

  const hasUsage = inputTokens > 0 || outputTokens > 0;

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
    provider: "vercel-ai",
    model,
    inputTokens,
    outputTokens,
    latencyMs,
    isRetry: false,
  });

  _buffer.addEvent(event);

  task.llmCostUsd = task.llmCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  task.totalInputTokens += inputTokens;
  task.totalOutputTokens += outputTokens;
  _buffer.upsertTask(task);
}

/**
 * Patch `generateText` and `streamText` from the `ai` module to record cost events.
 *
 * If `ai` is not installed and no mock module is injected, the dynamic
 * import will throw and the function will reject.
 */
export async function instrumentVercelAi(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  if (!_aiModule) {
    // ai is an optional peer dependency; the dynamic import only
    // succeeds at runtime if the user has installed it.
    // @ts-ignore -- ai is an optional peer dependency
    _aiModule = await import("ai");
  }

  _originalGenerateText = _aiModule.generateText;
  _originalStreamText = _aiModule.streamText;
  _buffer = buffer;
  _pricing = pricing;

  _aiModule.generateText = async function patchedGenerateText(
    this: any,
    opts: any,
  ): Promise<any> {
    let task = getCurrentTask();

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      task = createAutoTask("vercel-ai.generateText");
      _buffer?.upsertTask(task);
    }

    const startTime = performance.now();
    const result = await _originalGenerateText!.call(this, opts);
    const latencyMs = Math.round(performance.now() - startTime);

    const model = extractModel(opts);
    const inputTokens: number = result?.usage?.promptTokens ?? 0;
    const outputTokens: number = result?.usage?.completionTokens ?? 0;

    recordEvent(model, inputTokens, outputTokens, task, latencyMs);
    return result;
  };

  _aiModule.streamText = function patchedStreamText(
    this: any,
    opts: any,
  ): any {
    let task = getCurrentTask();

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      task = createAutoTask("vercel-ai.streamText");
      _buffer?.upsertTask(task);
    }

    const startTime = performance.now();
    const streamResult = _originalStreamText!.call(this, opts);

    // Wrap the stream to capture usage after iteration completes.
    // The Vercel AI SDK streamText returns an object with an async iterator
    // for text chunks and a `usage` promise that resolves when done.
    if (streamResult && typeof streamResult[Symbol.asyncIterator] === "function") {
      return wrapStream(streamResult, opts, task, startTime);
    }

    return streamResult;
  };

  _patched = true;
}

/**
 * Remove the monkey-patches and restore the original functions.
 */
export function uninstrumentVercelAi(): void {
  if (!_patched || !_aiModule) return;

  if (_originalGenerateText) _aiModule.generateText = _originalGenerateText;
  if (_originalStreamText) _aiModule.streamText = _originalStreamText;

  _originalGenerateText = null;
  _originalStreamText = null;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

function wrapStream(
  rawStream: any,
  opts: any,
  task: Task,
  startTime: number,
): AsyncIterable<any> & Record<string, any> {
  let recorded = false;

  // Preserve all properties of the original stream result
  const wrapped: any = Object.create(rawStream);

  wrapped[Symbol.asyncIterator] = function () {
    const iter = rawStream[Symbol.asyncIterator]();
    return {
      async next(): Promise<IteratorResult<any>> {
        const result = await iter.next();
        if (result.done && !recorded) {
          recorded = true;
          const latencyMs = Math.round(performance.now() - startTime);

          // After stream completes, try to read usage from the stream result.
          // Vercel AI SDK exposes usage as a property or promise on the result.
          let inputTokens = 0;
          let outputTokens = 0;

          try {
            const usage = rawStream.usage ?? (await rawStream.usage);
            if (usage) {
              inputTokens = usage.promptTokens ?? 0;
              outputTokens = usage.completionTokens ?? 0;
            }
          } catch {
            // usage not available
          }

          const model = extractModel(opts);
          recordEvent(model, inputTokens, outputTokens, task, latencyMs);
        }
        return result;
      },
    };
  };

  return wrapped;
}

// Self-register so importing this module is enough to make the instrument available.
registerInstrument("vercel-ai", instrumentVercelAi, uninstrumentVercelAi);
