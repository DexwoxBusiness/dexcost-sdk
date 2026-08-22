import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import {
  mapProviderResult,
  ProviderOperationSession,
  wrapProviderStream,
  type OperationMeasurement,
} from "./provider-metering.js";
import { nonNegativeDecimal, nonNegativeInteger, prefixedModel, tokenMeasurement } from "./provider-extract.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

type MethodPatch = { kind: "method"; owner: any; name: string; original: (...args: any[]) => any };
type DescriptorPatch = { kind: "descriptor"; owner: any; name: string; original: PropertyDescriptor };
type Patch = MethodPatch | DescriptorPatch;
const patches: Patch[] = [];
let providedModule: any;
let patched = false;

const BILLABLE_RESOURCE_GETTERS = [
  "chat", "responses", "embeddings", "images", "stt", "tts", "rerank",
  "videoGeneration", "classifications",
] as const;
const BILLABLE_METHODS = [
  "send", "sendAsync", "create", "generate", "generateAsync", "createTranscription",
  "createTranslation", "createSpeech", "rerank", "rerankAsync", "createVideos",
] as const;

const METHOD_SERVICE: Record<string, string> = {
  callModel: "responses", send: "chat", create: "responses", generate: "embeddings",
  createTranscription: "stt", createTranslation: "stt", createSpeech: "tts",
  rerank: "rerank", createVideos: "video_generation",
};

function serviceFor(ownerName: string, method: string): string {
  const name = ownerName.toLowerCase();
  for (const candidate of [
    "classifications", "responses", "embeddings", "images", "stt", "tts", "rerank", "video", "chat",
  ]) {
    if (name.includes(candidate)) return candidate === "video" ? "video_generation" : candidate;
  }
  return METHOD_SERVICE[method] ?? "inference";
}

function requestBody(value: any): any {
  return value?.chatRequest ?? value?.openResponsesRequest ?? value?.responsesRequest ??
    value?.embeddingRequest ?? value?.embeddingsRequest ?? value?.imageGenerationRequest ??
    value?.transcriptionRequest ?? value?.translationRequest ?? value?.speechRequest ??
    value?.rerankRequest ?? value?.videoGenerationRequest ?? value?.classificationRequest ??
    value?.request ?? value ?? {};
}

function responseBody(value: any): any {
  return value?.data ?? value?.response ?? value?.chatCompletion ?? value?.result ?? value;
}

function measurementFor(rawResponse: any, model: string, service: string): OperationMeasurement {
  const response = responseBody(rawResponse);
  const result = tokenMeasurement(response, model, "openrouter");
  result.responseModel = prefixedModel("openrouter", result.responseModel ?? model);
  const usage = response?.usage ?? {};
  const extra = [...(result.usageLines ?? [])];
  if (service === "images") {
    const count = Array.isArray(response?.data)
      ? response.data.length
      : Array.isArray(response?.images) ? response.images.length : nonNegativeInteger(usage.images);
    if (count > 0) extra.push({ metric: "image_count", quantity: count, unit: "Images" });
  }
  if (service === "stt" || service === "tts") {
    const seconds = nonNegativeDecimal(usage.audio_seconds ?? response?.duration);
    if (seconds?.gt(0)) extra.push({ metric: "audio_seconds", quantity: seconds, unit: "Seconds" });
  }
  if (service === "rerank" || service === "classifications") {
    const rows = response?.results ?? response?.data;
    const documents = Array.isArray(rows) ? rows.length : 0;
    if (documents > 0) extra.push({
      metric: service === "rerank" ? "document_count" : "classification_count",
      quantity: documents,
      unit: service === "rerank" ? "Documents" : "Classifications",
    });
  }
  result.usageLines = extra;
  return result;
}

function wrapModelResult(raw: any, session: ProviderOperationSession, model: string): any {
  if (raw === null || (typeof raw !== "object" && typeof raw !== "function")) {
    session.finish(measurementFor(raw, model, "responses"));
    return raw;
  }
  const finishFromCachedResponse = async (value: unknown): Promise<unknown> => {
    try {
      if (typeof raw.getResponse === "function") {
        const response = await raw.getResponse();
        session.finish(measurementFor(response, model, "responses"));
      } else {
        session.finish(measurementFor(value, model, "responses"));
      }
      return value;
    } catch (error) {
      session.fail(error);
      throw error;
    }
  };
  const streamMethods = new Set([
    "getFullResponsesStream", "getTextStream", "getItemsStream", "getNewMessagesStream",
    "getToolCallsStream", "getToolEventsStream",
  ]);
  return new Proxy(raw, {
    get(target, property, receiver): unknown {
      const value = Reflect.get(target, property, receiver);
      if (property === "getResponse" && typeof value === "function") {
        return (...args: unknown[]) => mapProviderResult(
          value.apply(target, args),
          (response) => { session.finish(measurementFor(response, model, "responses")); return response; },
          (error) => { session.fail(error); throw error; },
        );
      }
      if (property === "getText" && typeof value === "function") {
        return (...args: unknown[]) => mapProviderResult(
          value.apply(target, args), finishFromCachedResponse,
          (error) => { session.fail(error); throw error; },
        );
      }
      if (typeof property === "string" && streamMethods.has(property) && typeof value === "function") {
        return (...args: unknown[]) => {
          const stream = value.apply(target, args);
          if (!stream || typeof stream[Symbol.asyncIterator] !== "function") return stream;
          return {
            async *[Symbol.asyncIterator](): AsyncGenerator<unknown> {
              try {
                for await (const item of stream as AsyncIterable<unknown>) yield item;
                await finishFromCachedResponse(undefined);
              } catch (error) {
                session.fail(error);
                throw error;
              }
            },
          };
        };
      }
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

function patchMethod(
  owner: any,
  ownerName: string,
  name: string,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.kind === "method" && item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = requestBody(args[0]);
    const service = serviceFor(ownerName, name);
    const model = prefixedModel("openrouter", body.model ?? "unknown");
    const llm = ["chat", "responses", "embeddings"].includes(service);
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: `openrouter.${service}.${name}`, provider: "openrouter", service,
      operation: `openrouter.${service}.${name}`,
      component: llm ? "llm" : "external", model,
      eventType: llm ? "llm_call" : "external_cost",
    });
    let result: any;
    try { result = session.invoke(() => original.apply(this, args)); }
    catch (error) { session.fail(error); throw error; }
    if (name === "callModel") return wrapModelResult(result, session, model);
    const complete = (response: any): any => {
      if (body.stream === true) {
        let final = response;
        return wrapProviderStream(
          response, session,
          (chunk) => { final = (chunk as any)?.response ?? chunk; },
          () => measurementFor(final, model, service),
        );
      }
      session.finish(measurementFor(response, model, service));
      return response;
    };
    return mapProviderResult(result, complete, (error) => { session.fail(error); throw error; });
  };
  patches.push({ kind: "method", owner, name, original });
}

function patchResourceGetter(
  owner: any,
  ownerName: string,
  name: string,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || patches.some((item) => item.kind === "descriptor" && item.owner === owner && item.name === name)) return;
  const descriptor = Object.getOwnPropertyDescriptor(owner, name);
  if (descriptor?.get === undefined || descriptor.configurable === false) return;
  const originalGet = descriptor.get;
  Object.defineProperty(owner, name, {
    ...descriptor,
    get(this: any): any {
      const resource = originalGet.call(this);
      discover(resource, pricing, buffer, `${ownerName}.${name}`);
      return resource;
    },
  });
  patches.push({ kind: "descriptor", owner, name, original: descriptor });
}

function discover(root: any, pricing: PricingEngine, buffer: EventBuffer, rootName = "openrouter"): void {
  const seen = new Set<any>();
  const visit = (value: any, name: string, depth: number): void => {
    if (!value || (typeof value !== "object" && typeof value !== "function") || seen.has(value) || depth > 4) return;
    seen.add(value);
    const owner = typeof value === "function" ? value.prototype : value;
    if (name.toLowerCase().includes("openrouter")) {
      try { patchMethod(owner, name, "callModel", pricing, buffer); } catch { /* immutable export */ }
      for (const key of BILLABLE_RESOURCE_GETTERS) {
        try { patchResourceGetter(owner, name, key, pricing, buffer); } catch { /* immutable export */ }
      }
    }
    for (const method of BILLABLE_METHODS) {
      try { patchMethod(owner, name, method, pricing, buffer); } catch { /* immutable export */ }
    }
    for (const key of Object.keys(value).slice(0, 100)) {
      if (["constructor", "length", "name", "prototype"].includes(key)) continue;
      try { visit(value[key], `${name}.${key}`, depth + 1); } catch { /* getter side effect */ }
    }
  };
  visit(root, rootName, 0);
  if (root?.default !== root) visit(root?.default, "OpenRouter", 0);
}

export async function instrumentOpenRouter(pricing: PricingEngine, buffer: EventBuffer): Promise<void> {
  if (patched) return;
  let mod = providedModule;
  if (!mod) {
    // @ts-expect-error optional official SDK
    mod = await import("@openrouter/sdk");
  }
  discover(mod, pricing, buffer);
  if (patches.length === 0) {
    throw new Error("@openrouter/sdk exposes no supported metered surface; pass a client via instrumentModules.openrouter");
  }
  patched = true;
}

export function uninstrumentOpenRouter(): void {
  for (const item of patches.splice(0).reverse()) {
    if (item.kind === "method") item.owner[item.name] = item.original;
    else Object.defineProperty(item.owner, item.name, item.original);
  }
  patched = false;
}
export function provideOpenRouterModule(ref: unknown): void { providedModule = ref; }
registerInstrument("openrouter", instrumentOpenRouter, uninstrumentOpenRouter, provideOpenRouterModule);
