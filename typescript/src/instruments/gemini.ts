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
import { createAutoTask, finalizeAutoTask } from "../core/auto-task.js";
import { registerLlmCapture } from "../core/llm-dedup.js";
import { getAmbientSessionTask } from "../core/session.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine, CostResult } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import { stampAmbientAttribution } from "../core/capabilities.js";
import { recordProviderFailure } from "./provider-metering.js";
import { currentProviderCaptureOwner, runWithProviderCapture } from "./provider-capture.js";
import { debugLog } from "../core/debug.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalGenerateContent: Function | null = null;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalGenerateContentStream: Function | null = null;
let _patchedPrototype: any = null;
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
  _patchedPrototype = GenerativeModelProto;
  _buffer = buffer;
  _pricing = pricing;

  GenerativeModelProto.generateContent = async function (
    this: any,
    ...args: any[]
  ): Promise<any> {
    if (currentProviderCaptureOwner() !== undefined) {
      return _originalGenerateContent!.apply(this, args);
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
      task = getAmbientSessionTask("gemini.generateContent");
      if (!task) {
        task = createAutoTask("gemini.generateContent");
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const startTime = performance.now();
    const self = this;
    try {
      const response = await suppressNetworkEvent(() =>
        runWithProviderCapture("google", () =>
          runWithTask(task, () => _originalGenerateContent!.apply(self, args))),
      );
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        const model: string = self.model ?? self._modelParams?.model ?? "unknown";
        recordEvent(response?.response ?? response, model, task, latencyMs);
      } catch {
        // dexcost errors must never crash user code
      }
      if (autoCreated) {
        finalizeAutoTask(task, "success", _buffer);
      }
      return response;
    } catch (err) {
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType: "gemini.generate_content", provider: "google", service: "gemini",
        operation: "google.generate_content", component: "llm",
        model: self.model ?? self._modelParams?.model, eventType: "llm_call",
      }, err, startTime);
      if (autoCreated) {
        finalizeAutoTask(task, "failed", _buffer);
      }
      throw err;
    }
  };

  GenerativeModelProto.generateContentStream = async function (
    this: any,
    ...args: any[]
  ): Promise<any> {
    if (currentProviderCaptureOwner() !== undefined) {
      return _originalGenerateContentStream!.apply(this, args);
    }
    let task = getCurrentTask();
    let autoCreated = false;

    if (!task) {
      // Join the ambient session (grouping with sibling HTTP/LLM calls
      // in the same context) when session tracking is active; the
      // session sweep owns its lifecycle. Otherwise fall back to a
      // per-call auto-task owned (and finalized) here.
      task = getAmbientSessionTask("gemini.generateContentStream");
      if (!task) {
        task = createAutoTask("gemini.generateContentStream");
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const startTime = performance.now();
    const self = this;
    const model: string = self.model ?? self._modelParams?.model ?? "unknown";
    try {
      const streamResult = await suppressNetworkEvent(() =>
        runWithProviderCapture("google", () =>
          runWithTask(task, () => _originalGenerateContentStream!.apply(self, args))),
      );
      return wrapStream(streamResult, model, task, startTime, autoCreated);
    } catch (err) {
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType: "gemini.generate_content_stream", provider: "google", service: "gemini",
        operation: "google.generate_content_stream", component: "llm", model, eventType: "llm_call",
      }, err, startTime);
      if (autoCreated) {
        finalizeAutoTask(task, "failed", _buffer);
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
  if (!_patched) return;

  if (_patchedPrototype) {
    if (_originalGenerateContent) _patchedPrototype.generateContent = _originalGenerateContent;
    else delete _patchedPrototype.generateContent;
    if (_originalGenerateContentStream) {
      _patchedPrototype.generateContentStream = _originalGenerateContentStream;
    } else {
      delete _patchedPrototype.generateContentStream;
    }
  }

  _originalGenerateContent = null;
  _originalGenerateContentStream = null;
  _patchedPrototype = null;
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

  const finalize = (status: "succeeded" | "failed" | "cancelled", error?: unknown): void => {
    if (finalized) return;
    finalized = true;
    try {
      const costResult = hasUsage && _pricing
        ? _pricing.getCost(model, inputTokens, outputTokens, cachedTokens)
        : { costUsd: new Decimal(0), costConfidence: "estimated" as const, pricingSource: "unknown" as const };
      const usageLines = [
        ...(inputTokens > 0 ? [{ metric: "input_tokens", quantity: String(inputTokens), unit: "Tokens" }] : []),
        ...(outputTokens > 0 ? [{ metric: "output_tokens", quantity: String(outputTokens), unit: "Tokens" }] : []),
        ...(cachedTokens > 0 ? [{ metric: "cache_read_input_tokens", quantity: String(cachedTokens), unit: "Tokens" }] : []),
      ];
      const event = createCostEvent({
        eventId: randomUUID(), taskId: task.taskId, eventType: "llm_call",
        costUsd: costResult.costUsd, costConfidence: costResult.costConfidence,
        pricingSource: costResult.pricingSource, provider: "google", model,
        inputTokens, outputTokens, cachedTokens,
        latencyMs: Math.round(performance.now() - startTime), isRetry: false,
        details: {
          attribution_component: "llm",
          attribution_operation_name: "google.generate_content_stream",
          attribution_operation_status: status,
          attribution_resource_type: "model",
          attribution_resource_id: model,
          attribution_usage_lines: usageLines.length > 0
            ? usageLines
            : [{ metric: "request_count", quantity: "1", unit: "Requests" }],
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
    } catch (error) {
      // dexcost errors must never crash the provider stream
      debugLog("gemini", `failed to finalize stream attribution: ${String(error)}`);
    }
    if (autoCreated) finalizeAutoTask(task, status === "succeeded" ? "success" : "failed", _buffer);
  };

  const wrappedStream = {
    [Symbol.asyncIterator]() {
      const iter = stream[Symbol.asyncIterator]();
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
          if (chunk?.usageMetadata) {
            hasUsage = true;
            inputTokens = chunk.usageMetadata.promptTokenCount ?? inputTokens;
            outputTokens = chunk.usageMetadata.candidatesTokenCount ?? outputTokens;
            cachedTokens = chunk.usageMetadata.cachedContentTokenCount ?? cachedTokens;
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

  // Return an object that preserves the original shape: { stream, response }
  return {
    stream: wrappedStream,
    response: rawStream.response,
  };
}

// Self-register so importing this module is enough to make the instrument available.
registerInstrument("gemini", instrumentGemini, uninstrumentGemini, (ref: any) => {
  const mod = ref?.default ?? ref;
  _setGenerativeModelClass(mod?.GenerativeModel ?? mod);
});
