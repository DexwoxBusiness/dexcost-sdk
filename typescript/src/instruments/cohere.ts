/**
 * Cohere auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `CohereClient.prototype.chat` to automatically
 * record cost events and aggregate token usage on the active task context.
 *
 * Token usage from response.meta.billedUnits (inputTokens, outputTokens).
 *
 * Supports V1 and generated V2 chat, streaming, Embed and Rerank. Instrument
 * before constructing CohereClientV2: it binds its resource methods at creation.
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
import { hasPaidProviderBilling } from "../core/provider-billing.js";
import { debugLog } from "../core/debug.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
const _chatPatches: Array<{ prototype: any; chat: any; stream: any }> = [];
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

function nativePrototypes(classes: any[]): any[] {
  return classes.flatMap((Client) => {
    if (!Client?.prototype) return [];
    if (["chat", "chatStream", "embed", "rerank"].some(name => typeof Client.prototype[name] === "function")) return [Client.prototype];
    // Official CohereClientV2 has bound instance fields, not prototype methods.
    // Constructing a keyless transport performs no request and reveals the
    // generated resource prototype. Never use ambient/user credentials here.
    try {
      const sample = new Client({ token: "dexcost-instrumentation-no-request" });
      const resource = sample?.clientV2;
      return resource ? [Object.getPrototypeOf(resource)] : [];
    } catch { return []; }
  }).filter((value, index, values) => value && values.indexOf(value) === index);
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

  let meteredPrototypes: any[];
  if (_clientClass) {
    meteredPrototypes = nativePrototypes(Array.isArray(_clientClass) ? _clientClass : [_clientClass]);
  } else {
    // cohere-ai is an optional peer dependency; the dynamic import
    // only succeeds at runtime if the user has installed it.
    const packageName = "cohere-ai";
    const cohereModule = await import(packageName);
    const mod = cohereModule.default ?? cohereModule;
    meteredPrototypes = nativePrototypes([mod.CohereClient, mod.CohereClientV2]);
  }

  _buffer = buffer;
  _pricing = pricing;
  for (const prototype of meteredPrototypes) patchChatPrototype(prototype);
  for (const prototype of meteredPrototypes) patchMeteredMethods(prototype);

  _patched = true;
}

function patchChatPrototype(ClientProto: any): void {
  const _originalChat = ClientProto.chat;
  const _originalChatStream = ClientProto.chatStream ?? null;
  if (typeof _originalChat !== "function" && !_originalChatStream) return;
  _chatPatches.push({ prototype: ClientProto, chat: _originalChat, stream: _originalChatStream });

  ClientProto.chat = function (
    this: any,
    body: any,
    options?: any,
  ): any {
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
    const paid = paidCohere(self, options);
    const complete = (response: any): any => {
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        const model: string = body?.model ?? response?.model ?? "command-r-plus";
        recordEvent(response, model, task, latencyMs, paid);
      } catch {
        // dexcost errors must never crash user code
      }
      if (autoCreated) {
        finalizeAutoTask(task, "success", _buffer);
      }
      return response;
    };
    const fail = (err: unknown): never => {
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType: "cohere.chat", provider: "cohere", service: "chat",
        operation: "cohere.chat", component: "llm", model: body?.model, eventType: "llm_call",
      }, err, startTime);
      if (autoCreated) {
        finalizeAutoTask(task, "failed", _buffer);
      }
      throw err;
    };
    let result: any;
    try {
      result = suppressNetworkEvent(() =>
        runWithProviderCapture("cohere", () =>
          runWithTask(task, () => _originalChat!.call(self, body, options))),
      );
    } catch (err) { return fail(err); }
    return observeCohereResult(result, complete, fail);
  };

  if (_originalChatStream) {
    ClientProto.chatStream = function (
      this: any,
      body: any,
      options?: any,
    ): any {
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
      const paid = paidCohere(self, options);
      const model: string = body?.model ?? "command-r-plus";
      const fail = (err: unknown): never => {
        if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
          taskType: "cohere.chat_stream", provider: "cohere", service: "chat",
          operation: "cohere.chat_stream", component: "llm", model, eventType: "llm_call",
        }, err, startTime);
        if (autoCreated) {
          finalizeAutoTask(task, "failed", _buffer);
        }
        throw err;
      };
      let result: any;
      try {
        result = suppressNetworkEvent(() =>
          runWithProviderCapture("cohere", () =>
            runWithTask(task, () => _originalChatStream!.call(self, body, options))),
        );
      } catch (err) { return fail(err); }
      return observeCohereResult(result,
        (rawStream) => wrapStream(rawStream, model, task, startTime, autoCreated, paid), fail);
    };
  }

}

/**
 * Remove the monkey-patches and restore the original methods.
 */
export function uninstrumentCohere(): void {
  if (!_patched) return;

  for (const patch of _chatPatches.splice(0)) {
    if (patch.chat) patch.prototype.chat = patch.chat;
    else delete patch.prototype.chat;
    if (patch.stream) patch.prototype.chatStream = patch.stream;
    else delete patch.prototype.chatStream;
  }
  for (const patch of _meteredPatches.splice(0)) {
    patch.prototype[patch.name] = patch.original;
  }

  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

const COHERE_METADATA_HEADERS = new Set([
  "x-fern-language", "x-fern-sdk-name", "x-fern-sdk-version",
  "x-fern-runtime", "x-fern-runtime-version", "user-agent", "x-client-name",
]);

/** Observe native settlement once, before caller recovery callbacks. Keep the
 * HttpResponsePromise surface and bound receiver for withRawResponse(). Raw
 * stream helpers are passed through; their stream consumption is not captured.
 */
function observeCohereResult(raw: any, complete: (value: any) => any, fail: (error: unknown) => never): any {
  if (raw == null || typeof raw.then !== "function") return complete(raw);
  const observed = raw.then(complete, fail);
  // An ignored SDK promise must not create an extra unhandled-rejection report.
  void observed.catch(() => undefined);
  return new Proxy(raw, {
    get(target, property) {
      if (property === "then" || property === "catch" || property === "finally") {
        return observed[property].bind(observed);
      }
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

function ordinaryCohereHeaders(headers: unknown): boolean {
  if (headers == null) return true;
  if (typeof headers !== "object" || Array.isArray(headers)) return false;
  const prototype = Object.getPrototypeOf(headers);
  if (prototype !== Object.prototype && prototype !== null) return false;
  // Cohere normalizes these metadata headers on every native client. Inspect
  // names only: never evaluate a header supplier or read an auth secret value.
  return Object.keys(headers).every(name => COHERE_METADATA_HEADERS.has(name.toLowerCase()));
}

function paidCohere(client: any, requestOptions: any): boolean {
  // Reject all per-call options: they can contain alternate auth or routing.
  const options = client?._options;
  if (requestOptions != null || !options || !ordinaryCohereHeaders(options.headers) ||
      options.fetcher != null || options.fetch != null) return false;
  const endpoint = options.baseUrl ?? options.environment ?? "https://api.cohere.com";
  return typeof endpoint === "string" && hasPaidProviderBilling(client, "cohere", endpoint);
}
function validCount(value: unknown): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function validChat(response: any, billed: any): boolean {
  const reason = response?.delta?.finishReason ?? response?.finishReason ?? response?.finish_reason;
  return ["COMPLETE", "STOP_SEQUENCE", "MAX_TOKENS", "TOOL_CALL"].includes(reason) &&
    validCount(billed?.inputTokens ?? billed?.input_tokens) && validCount(billed?.outputTokens ?? billed?.output_tokens);
}
function billingDimensions(paid: boolean): Array<readonly [string, string]> {
  return paid ? [["provider_billing_lane", "caller_paid_standard"]] : [];
}
function providerId(response: any): string | undefined {
  const value = response?.id ?? response?.generationId ?? response?.generation_id ?? response?.responseId;
  return typeof value === "string" && value.trim() ? value.trim().slice(0, 256) : undefined;
}

function nonNegativeNumber(value: unknown): number | undefined {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) && result >= 0 ? result : undefined;
}

function cohereBilledUnits(response: any): any {
  return response?.usage?.billedUnits ?? response?.usage?.billed_units ??
    response?.meta?.billedUnits ?? response?.meta?.billed_units;
}

function meteredMeasurement(kind: "embed" | "rerank", response: any, model: string, paid = false): any {
  const billed = cohereBilledUnits(response);
  const inputTokens = nonNegativeNumber(billed?.inputTokens ?? billed?.input_tokens);
  const imageTokens = nonNegativeNumber(billed?.imageTokens ?? billed?.image_tokens);
  const outputTokens = nonNegativeNumber(billed?.outputTokens ?? billed?.output_tokens);
  const searchUnits = nonNegativeNumber(billed?.searchUnits ?? billed?.search_units);
  const classifications = nonNegativeNumber(billed?.classifications);
  const usageLines: Array<{ metric: string; quantity: number; unit: string }> = [];
  const pricingUsage: Record<string, number> = {};
  if (inputTokens !== undefined && inputTokens > 0) {
    usageLines.push({ metric: "input_tokens", quantity: inputTokens, unit: "Tokens" });
    if (kind !== "embed") pricingUsage.input_tokens = inputTokens;
  }
  if (kind === "embed" && imageTokens !== undefined && imageTokens > 0) {
    usageLines.push({ metric: "input_image_tokens", quantity: imageTokens, unit: "Tokens" });
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
    providerService: kind,
    billingDimensions: billingDimensions(paid && (kind === "rerank"
      ? validCount(billed?.searchUnits ?? billed?.search_units)
      : validCount(billed?.inputTokens ?? billed?.input_tokens) &&
        ((billed?.imageTokens ?? billed?.image_tokens) == null || validCount(billed?.imageTokens ?? billed?.image_tokens)))),
  };
}

function patchMeteredMethods(prototype: any): void {
  for (const name of ["embed", "rerank"] as const) {
    if (typeof prototype?.[name] !== "function") continue;
    const original = prototype[name] as Function;
    _meteredPatches.push({ prototype, name, original });
    prototype[name] = function (this: any, body: any, options?: any): any {
      if (currentProviderCaptureOwner() !== undefined) return original.call(this, body, options);
      const requested = typeof body?.model === "string" && body.model.length > 0 ? body.model : "unknown";
      const model = name === "embed" && requested.startsWith("cohere/")
        ? requested.slice("cohere/".length)
        : requested;
      const service = name === "embed" ? "embeddings" : "rerank";
      const paid = paidCohere(this, options);
      const session = new ProviderOperationSession(_pricing!, _buffer!, {
        taskType: `cohere.${name}`,
        provider: "cohere",
        service,
        operation: `cohere.${name}`,
        component: "external",
        model,
        eventType: "external_cost",
      });
      let result: any;
      try { result = session.invoke(() => original.call(this, body, options)); }
      catch (error) { session.fail(error); throw error; }
      return observeCohereResult(result, (response) => {
        session.finish(meteredMeasurement(name, response, model, paid));
        return response;
      }, (error) => {
        session.fail(error);
        throw error;
      });
    };
  }
}

function recordEvent(response: any, model: string, task: Task, latencyMs: number, paid = false): void {
  if (!_buffer || !_pricing) return;

  const billedUnits = cohereBilledUnits(response);
  const hasUsage = billedUnits != null;

  const inputTokens: number = nonNegativeNumber(billedUnits?.inputTokens ?? billedUnits?.input_tokens) ?? 0;
  const outputTokens: number = nonNegativeNumber(billedUnits?.outputTokens ?? billedUnits?.output_tokens) ?? 0;

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
    serviceName: "chat",
    details: {
      attribution_component: "llm", attribution_operation_name: "cohere.chat",
      attribution_operation_status: ["ERROR", "TIMEOUT"].includes(response?.finishReason ?? response?.finish_reason) ? "failed" : "succeeded",
      attribution_resource_type: "model", attribution_resource_id: model,
      provider_record_id: providerId(response),
      attribution_dimensions: billingDimensions(paid && validChat(response, billedUnits)).map(([key, value]) => ({key, value: {type: "string", value}})),
      attribution_usage_lines: [
        ...(inputTokens > 0 ? [{metric: "input_tokens", quantity: String(inputTokens), unit: "Tokens"}] : []),
        ...(outputTokens > 0 ? [{metric: "output_tokens", quantity: String(outputTokens), unit: "Tokens"}] : []),
      ],
    },
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
  paid: boolean = false,
): AsyncIterable<any> {
  let inputTokens = 0;
  let outputTokens = 0;
  let hasUsage = false;
  let finalized = false;
  let recordId: string | undefined;
  let terminal: any;
  let terminalBilled: any;

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
        isRetry: false, serviceName: "chat",
        details: {
          provider_record_id: recordId,
          attribution_dimensions: billingDimensions(paid && validChat(terminal, terminalBilled)).map(([key, value]) => ({key, value: {type: "string", value}})),
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
            finalize(["ERROR", "TIMEOUT"].includes(terminal?.delta?.finishReason ?? terminal?.finishReason) ? "failed" : "succeeded");
            return result;
          }

          const chunk = result.value;
          // Preserve partial diagnostic usage without granting terminal admission.
          const partial = cohereBilledUnits(chunk);
          if (partial != null) {
            hasUsage = true;
            inputTokens = nonNegativeNumber(partial.inputTokens ?? partial.input_tokens) ?? inputTokens;
            outputTokens = nonNegativeNumber(partial.outputTokens ?? partial.output_tokens) ?? outputTokens;
          }
          const type = chunk?.type ?? chunk?.eventType;
          if (type === "message-start") recordId = providerId(chunk);
          if (type === "message-end" || type === "stream-end") {
            terminal = chunk?.response ?? chunk;
            terminalBilled = type === "message-end" ? cohereBilledUnits(chunk?.delta) : cohereBilledUnits(terminal);
            recordId = providerId(terminal) ?? recordId;
            hasUsage = terminalBilled != null;
            inputTokens = nonNegativeNumber(terminalBilled?.inputTokens ?? terminalBilled?.input_tokens) ?? 0;
            outputTokens = nonNegativeNumber(terminalBilled?.outputTokens ?? terminalBilled?.output_tokens) ?? 0;
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
  const classes = [mod?.CohereClient, mod?.CohereClientV2].filter(Boolean);
  _setClientClass(classes.length ? [...new Set(classes)] : mod);
});
