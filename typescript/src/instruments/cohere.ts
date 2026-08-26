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
import { stampAmbientAttribution } from "../core/capabilities.js";
import { ProviderOperationSession, recordProviderFailure } from "./provider-metering.js";
import { currentProviderCaptureOwner, runWithProviderCapture } from "./provider-capture.js";
import { debugLog } from "../core/debug.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalChat: Function | null = null;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _originalChatStream: Function | null = null;
let _patchedPrototype: any = null;
let _clientClass: any = null;
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;
const _meteredPatches: Array<{ prototype: any; name: "embed" | "rerank"; original: Function }> = [];

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
  let meteredPrototypes: any[];
  if (_clientClass) {
    ClientProto = _clientClass.prototype;
    meteredPrototypes = [ClientProto];
  } else {
    // cohere-ai is an optional peer dependency; the dynamic import
    // only succeeds at runtime if the user has installed it.
    // @ts-expect-error -- cohere-ai types are not bundled with dexcost
    const cohereModule = await import("cohere-ai");
    const mod = cohereModule.default ?? cohereModule;
    ClientProto = mod.CohereClient.prototype;
    meteredPrototypes = [mod.CohereClient?.prototype, mod.CohereClientV2?.prototype]
      .filter((value, index, values) => value && values.indexOf(value) === index);
  }

  _originalChat = ClientProto.chat;
  _originalChatStream = ClientProto.chatStream ?? null;
  _patchedPrototype = ClientProto;
  _buffer = buffer;
  _pricing = pricing;

  ClientProto.chat = async function (
    this: any,
    body: any,
    options?: any,
  ): Promise<any> {
    if (currentProviderCaptureOwner() !== undefined) {
      return _originalChat!.call(this, body, options);
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
        runWithProviderCapture("cohere", () =>
          runWithTask(task, () => _originalChat!.call(self, body, options))),
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
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType: "cohere.chat", provider: "cohere", service: "chat",
        operation: "cohere.chat", component: "llm", model: body?.model, eventType: "llm_call",
      }, err, startTime);
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
      if (currentProviderCaptureOwner() !== undefined) {
        return _originalChatStream!.call(this, body, options);
      }
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
          runWithProviderCapture("cohere", () =>
            runWithTask(task, () => _originalChatStream!.call(self, body, options))),
        );
        return wrapStream(rawStream, model, task, startTime, autoCreated);
      } catch (err) {
        if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
          taskType: "cohere.chat_stream", provider: "cohere", service: "chat",
          operation: "cohere.chat_stream", component: "llm", model, eventType: "llm_call",
        }, err, startTime);
        if (autoCreated) {
          finalizeAutoTask(task, "failed", _buffer);
        }
        throw err;
      }
    };
  }

  for (const prototype of meteredPrototypes) patchMeteredMethods(prototype);

  _patched = true;
}

/**
 * Remove the monkey-patches and restore the original methods.
 */
export function uninstrumentCohere(): void {
  if (!_patched) return;

  if (_patchedPrototype) {
    if (_originalChat) _patchedPrototype.chat = _originalChat;
    else delete _patchedPrototype.chat;
    if (_originalChatStream) _patchedPrototype.chatStream = _originalChatStream;
    else delete _patchedPrototype.chatStream;
  }
  for (const patch of _meteredPatches.splice(0)) {
    patch.prototype[patch.name] = patch.original;
  }

  _originalChat = null;
  _originalChatStream = null;
  _patchedPrototype = null;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function nonNegativeNumber(value: unknown): number | undefined {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) && result >= 0 ? result : undefined;
}

function cohereBilledUnits(response: any): any {
  return response?.usage?.billedUnits ?? response?.usage?.billed_units ??
    response?.meta?.billedUnits ?? response?.meta?.billed_units ?? {};
}

function meteredMeasurement(kind: "embed" | "rerank", response: any, model: string): any {
  const billed = cohereBilledUnits(response);
  const inputTokens = nonNegativeNumber(billed?.inputTokens ?? billed?.input_tokens);
  const outputTokens = nonNegativeNumber(billed?.outputTokens ?? billed?.output_tokens);
  const searchUnits = nonNegativeNumber(billed?.searchUnits ?? billed?.search_units);
  const classifications = nonNegativeNumber(billed?.classifications);
  const usageLines: Array<{ metric: string; quantity: number; unit: string }> = [];
  const pricingUsage: Record<string, number> = {};
  if (inputTokens !== undefined && inputTokens > 0) {
    usageLines.push({ metric: "input_tokens", quantity: inputTokens, unit: "Tokens" });
    pricingUsage.input_tokens = inputTokens;
  }
  if (outputTokens !== undefined && outputTokens > 0) {
    usageLines.push({ metric: "output_tokens", quantity: outputTokens, unit: "Tokens" });
    pricingUsage.output_tokens = outputTokens;
  }
  if (searchUnits !== undefined && searchUnits > 0) {
    usageLines.push({ metric: "search_units", quantity: searchUnits, unit: "SearchUnits" });
    pricingUsage.query_count = searchUnits;
  }
  if (classifications !== undefined && classifications > 0) {
    usageLines.push({ metric: "classifications", quantity: classifications, unit: "Classifications" });
  }
  // A conforming rerank response normally reports searchUnits.  Retain a
  // privacy-safe query meter if a compatible deployment omits usage.
  if (kind === "rerank" && usageLines.length === 0) {
    usageLines.push({ metric: "query_count", quantity: 1, unit: "Queries" });
    pricingUsage.query_count = 1;
  }
  return {
    usageLines,
    pricingUsage,
    providerRecordId: typeof response?.id === "string" ? response.id : undefined,
    responseModel: model,
    inputTokens,
    outputTokens,
  };
}

function patchMeteredMethods(prototype: any): void {
  for (const name of ["embed", "rerank"] as const) {
    if (typeof prototype?.[name] !== "function") continue;
    const original = prototype[name] as Function;
    _meteredPatches.push({ prototype, name, original });
    prototype[name] = async function (this: any, body: any, options?: any): Promise<any> {
      if (currentProviderCaptureOwner() !== undefined) return original.call(this, body, options);
      const requested = typeof body?.model === "string" && body.model.length > 0 ? body.model : "unknown";
      const model = name === "embed" && !requested.startsWith("cohere/")
        ? `cohere/${requested}`
        : requested;
      const service = name === "embed" ? "embeddings" : "rerank";
      const session = new ProviderOperationSession(_pricing!, _buffer!, {
        taskType: `cohere.${name}`,
        provider: "cohere",
        service,
        operation: `cohere.${name}`,
        component: "llm",
        model,
        eventType: "external_cost",
      });
      try {
        const response = await session.invoke(() => original.call(this, body, options));
        session.finish(meteredMeasurement(name, response, model));
        return response;
      } catch (error) {
        session.fail(error);
        throw error;
      }
    };
  }
}

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
  stampAmbientAttribution(event);

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

  const finalize = (status: "succeeded" | "failed" | "cancelled", error?: unknown): void => {
    if (finalized) return;
    finalized = true;
    try {
      const costResult = hasUsage && _pricing
        ? _pricing.getCost(model, inputTokens, outputTokens)
        : { costUsd: new Decimal(0), costConfidence: "estimated" as const, pricingSource: "unknown" as const };
      const usageLines = [
        ...(inputTokens > 0 ? [{ metric: "input_tokens", quantity: String(inputTokens), unit: "Tokens" }] : []),
        ...(outputTokens > 0 ? [{ metric: "output_tokens", quantity: String(outputTokens), unit: "Tokens" }] : []),
      ];
      const event = createCostEvent({
        eventId: randomUUID(), taskId: task.taskId, eventType: "llm_call",
        costUsd: costResult.costUsd, costConfidence: costResult.costConfidence,
        pricingSource: costResult.pricingSource, provider: "cohere", model,
        inputTokens, outputTokens, latencyMs: Math.round(performance.now() - startTime),
        isRetry: false,
        details: {
          attribution_component: "llm",
          attribution_operation_name: "cohere.chat_stream",
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
        _buffer?.upsertTask(task);
      }
    } catch (error) {
      // dexcost errors must never crash the provider stream
      debugLog("cohere", `failed to finalize stream attribution: ${String(error)}`);
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
registerInstrument("cohere", instrumentCohere, uninstrumentCohere, (ref: any) => {
  const mod = ref?.default ?? ref;
  _setClientClass(mod?.CohereClient ?? mod);
});
