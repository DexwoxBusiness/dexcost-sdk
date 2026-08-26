import { ProviderJobRevision, providerJobFromDict, type ProviderJobStatus } from "../core/provider-jobs.js";
import type { Decimal } from "../core/models.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import {
  mapProviderResult,
  providerJobMeasurementFields,
  ProviderOperationSession,
  wrapProviderStream,
  type OperationMeasurement,
  type ProviderOperationStatus,
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
  "videoGeneration", "generations", "classifications", "beta",
] as const;
const BILLABLE_METHODS = [
  "send", "sendAsync", "create", "generate", "generateAsync", "createTranscription",
  "createTranscriptionMultipart", "createTranslation", "createSpeech", "rerank", "rerankAsync",
  "createVideos", "getGeneration",
] as const;

const METHOD_SERVICE: Record<string, string> = {
  callModel: "responses", send: "chat", create: "responses", generate: "embeddings",
  createTranscription: "stt", createTranslation: "stt", createSpeech: "tts",
  rerank: "rerank", createVideos: "video_generation",
};

function serviceFor(ownerName: string, method: string): string {
  const name = ownerName.toLowerCase();
  if (name.includes("videogeneration") || name.includes("video_generation")) return "video_generation";
  if (name.includes("generations")) return "generation_reconciliation";
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
    value?.requestBody ?? value?.request ?? value ?? {};
}

function responseBody(value: any): any {
  if (value === null || value === undefined || typeof value !== "object") return value;
  // Embeddings and images legitimately use a top-level `data` collection;
  // it is not an SDK transport envelope and its sibling usage/model fields
  // must remain visible to metering.  A bare `{ data: {...} }` result (for
  // example generations.getGeneration) is an envelope and is unwrapped.
  if (
    value.usage !== undefined || value.model !== undefined || value.id !== undefined ||
    value.status !== undefined || value.type !== undefined || Array.isArray(value.data)
  ) return value;
  return value.data ?? value.response ?? value.chatCompletion ?? value.result ?? value;
}

function recordId(response: any): string | undefined {
  const direct = response?.id ?? response?.generation_id ?? response?.generationId ??
    response?.job_id ?? response?.jobId;
  if (typeof direct === "string" && direct.length > 0) return direct.slice(0, 256);
  const headers = response?.headers;
  for (const name of ["x-generation-id", "X-Generation-Id"]) {
    const value = typeof headers?.get === "function" ? headers.get(name) : headers?.[name];
    if (typeof value === "string" && value.length > 0) return value.slice(0, 256);
  }
  return undefined;
}

function upstreamProvider(response: any): string | undefined {
  const direct = response?.provider ?? response?.provider_name ?? response?.providerName;
  if (typeof direct === "string" && direct.length > 0) return direct.slice(0, 256);
  const attempts = response?.openrouter_metadata?.attempts ?? response?.openrouterMetadata?.attempts;
  if (!Array.isArray(attempts)) return undefined;
  for (const attempt of [...attempts].reverse()) {
    const status = nonNegativeInteger(attempt?.status);
    if (status >= 200 && status < 300 && typeof attempt?.provider === "string") {
      return attempt.provider.slice(0, 256);
    }
  }
  return undefined;
}

function openRouterDimensions(response: any, usage: any): Array<readonly [string, string]> {
  const result: Array<readonly [string, string]> = [["gateway", "openrouter"]];
  const upstream = upstreamProvider(response);
  if (upstream) result.push(["upstream_provider", upstream]);
  const byok = typeof usage?.is_byok === "boolean"
    ? usage.is_byok
    : response?.openrouter_metadata?.is_byok ?? response?.openrouterMetadata?.isByok;
  if (typeof byok === "boolean") result.push(["is_byok", byok ? "true" : "false"]);
  const tier = response?.service_tier ?? response?.serviceTier;
  if (typeof tier === "string" && tier.length > 0) result.push(["service_tier", tier.slice(0, 256)]);
  return result;
}

function addLine(
  lines: NonNullable<OperationMeasurement["usageLines"]>,
  metric: string,
  quantity: unknown,
  unit: string,
): void {
  const value = nonNegativeDecimal(quantity);
  if (value?.gt(0)) lines.push({ metric, quantity: value, unit });
}

function measurementFor(
  rawResponse: any,
  model: string,
  service: string,
  body: any = {},
): OperationMeasurement {
  const response = responseBody(rawResponse);
  const result = tokenMeasurement(response, model, "openrouter");
  result.responseModel = prefixedModel("openrouter", result.responseModel ?? model);
  const usage = response?.usage ?? {};
  const extra = [...(result.usageLines ?? [])];
  const pricingUsage: Record<string, string | number | bigint | Decimal> = {
    ...(result.pricingUsage ?? {}),
  };
  result.providerRecordId = recordId(response) ?? result.providerRecordId;
  result.modelCandidates = [result.responseModel];
  result.billingDimensions = openRouterDimensions(response, usage);

  if (service === "embeddings") {
    const rows = response?.data;
    if (Array.isArray(rows)) addLine(extra, "embedding_count", rows.length, "Embeddings");
  }
  if (service === "images") {
    const count = Array.isArray(response?.data)
      ? response.data.length
      : Array.isArray(response?.images) ? response.images.length : nonNegativeInteger(usage.images);
    if (count > 0) {
      extra.push({ metric: "output_image_count", quantity: count, unit: "Images" });
      pricingUsage.output_image_count = count;
    }
  }
  if (service === "stt") {
    const seconds = nonNegativeDecimal(usage.seconds ?? usage.audio_seconds ?? response?.duration);
    if (seconds?.gt(0)) {
      extra.push({ metric: "audio_seconds", quantity: seconds, unit: "Seconds" });
      pricingUsage.input_audio_seconds = seconds;
    }
  }
  if (service === "tts") {
    const input = body?.input;
    if (typeof input === "string" && input.length > 0) {
      extra.push({ metric: "characters", quantity: input.length, unit: "Characters" });
      pricingUsage.characters = input.length;
    }
  }
  if (service === "rerank" || service === "classifications") {
    const rows = response?.results ?? response?.data;
    const count = Array.isArray(rows) ? rows.length : 0;
    addLine(extra, service === "rerank" ? "result_count" : "classification_count", count,
      service === "rerank" ? "Results" : "Classifications");
    const totalTokens = nonNegativeInteger(usage.total_tokens ?? usage.totalTokens);
    const searchUnits = nonNegativeInteger(usage.search_units ?? usage.searchUnits);
    addLine(extra, "total_tokens", totalTokens, "Tokens");
    addLine(extra, "search_units", searchUnits, "Units");
    if (totalTokens > 0) {
      pricingUsage.input_tokens = totalTokens;
      result.inputTokens = totalTokens;
    }
  }
  result.usageLines = extra;
  result.pricingUsage = pricingUsage;
  return result;
}

function operationStatus(response: any, service: string, terminal = false): ProviderOperationStatus {
  const type = String(response?.type ?? "").toLowerCase();
  const status = String(response?.status ?? "").toLowerCase();
  if (["error", "response.failed"].includes(type) || response?.error) return "failed";
  if (["failed", "error", "expired"].includes(status)) return "failed";
  if (["cancelled", "canceled"].includes(status)) return "cancelled";
  if (["response.incomplete"].includes(type)) return "unknown";
  if (["response.completed", "image_generation.completed"].includes(type)) return "succeeded";
  if (["completed", "succeeded"].includes(status)) return "succeeded";
  if (!terminal || (service === "chat" && response?.usage)) return "succeeded";
  return "unknown";
}

function providerJobStatus(response: any, submission = false): ProviderJobStatus {
  const status = String(response?.status ?? response?.data?.status ?? "").toLowerCase();
  if (["pending", "queued"].includes(status)) return "submitted";
  if (["in_progress", "running", "processing"].includes(status)) return "running";
  if (["completed", "succeeded"].includes(status)) return "succeeded";
  if (["failed", "error", "expired"].includes(status)) return "failed";
  if (["cancelled", "canceled"].includes(status)) return "cancelled";
  return submission ? "submitted" : "running";
}

function requestDimensions(body: any): Array<readonly [string, string]> {
  const result: Array<readonly [string, string]> = [];
  for (const key of ["duration", "resolution", "aspect_ratio", "aspectRatio"]) {
    const value = body?.[key];
    if (["string", "number", "bigint"].includes(typeof value)) {
      result.push([key === "aspectRatio" ? "aspect_ratio" : key, String(value).slice(0, 256)]);
    }
  }
  return result;
}

function videoMeasurement(response: any, model: string): OperationMeasurement {
  const value = responseBody(response);
  const usage = value?.usage ?? {};
  return {
    usageLines: [{ metric: "request_count", quantity: 1, unit: "Requests" }],
    pricingUsage: {},
    providerRecordId: recordId(value),
    providerCostUsd: nonNegativeDecimal(usage.cost ?? value?.cost),
    responseModel: model,
    modelCandidates: [model],
    billingDimensions: openRouterDimensions(value, usage),
  };
}

function submitVideoJob(
  pricing: PricingEngine,
  buffer: EventBuffer,
  session: ProviderOperationSession,
  response: any,
  model: string,
  body: any,
): void {
  const value = responseBody(response);
  const id = recordId(value);
  if (!id) {
    session.fail(new Error("OpenRouter video response omitted its job id"));
    return;
  }
  const status = providerJobStatus(value, true);
  const meter = status === "succeeded" ? videoMeasurement(value, model) : undefined;
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    taskId: session.task.taskId,
    provider: "openrouter",
    service: "video_generation",
    providerRecordId: id,
    operation: "openrouter.video_generation.generate",
    component: "external",
    eventType: "external_cost",
    resourceType: "model",
    resourceId: model,
    status,
    ownsTask: session.autoCreated,
    billingDimensions: requestDimensions(body),
    ...providerJobMeasurementFields(pricing, model, meter),
  }));
  session.releaseForProviderJob();
}

function reconcileVideoJob(
  pricing: PricingEngine,
  buffer: EventBuffer,
  response: any,
  id: string,
): void {
  const raw = buffer.getProviderJob("openrouter", "video_generation", id);
  if (!raw) return;
  const previous = providerJobFromDict(raw);
  const value = responseBody(response);
  const status = providerJobStatus(value);
  const meter = status === "succeeded" ? videoMeasurement(value, previous.resourceId) : undefined;
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    eventId: previous.eventId,
    revision: previous.revision + 1,
    taskId: previous.taskId,
    provider: previous.provider,
    service: previous.service,
    providerRecordId: id,
    operation: previous.operation,
    component: previous.component,
    eventType: previous.eventType,
    resourceType: previous.resourceType,
    resourceId: previous.resourceId,
    status,
    submittedAt: previous.submittedAt,
    ownsTask: previous.ownsTask,
    billingDimensions: previous.billingDimensions,
    ...providerJobMeasurementFields(pricing, previous.resourceId, meter),
  }));
}

function generationMeasurement(response: any): OperationMeasurement {
  const data = response?.data ?? response;
  const inputTotal = nonNegativeInteger(data?.native_tokens_prompt ?? data?.tokens_prompt);
  const outputTotal = nonNegativeInteger(data?.native_tokens_completion ?? data?.tokens_completion);
  let cached = nonNegativeInteger(data?.native_tokens_cached);
  let reasoning = nonNegativeInteger(data?.native_tokens_reasoning);
  let input = inputTotal;
  let output = outputTotal;
  if (cached <= inputTotal) input -= cached;
  else cached = 0;
  if (reasoning <= outputTotal) output -= reasoning;
  else reasoning = 0;
  const lines: NonNullable<OperationMeasurement["usageLines"]> = [];
  addLine(lines, "input_tokens", input, "Tokens");
  addLine(lines, "output_tokens", output, "Tokens");
  addLine(lines, "cache_read_input_tokens", cached, "Tokens");
  addLine(lines, "reasoning_output_tokens", reasoning, "Tokens");
  addLine(lines, "input_media_count", data?.num_media_prompt, "Media");
  addLine(lines, "output_media_count", data?.num_media_completion, "Media");
  addLine(lines, "web_search_result_count", data?.num_search_results, "Results");
  addLine(lines, "web_fetch_count", data?.num_fetches, "Requests");
  const dimensions: Array<readonly [string, string]> = [];
  const provider = data?.provider_name ?? data?.providerName;
  if (typeof provider === "string" && provider.length > 0) {
    dimensions.push(["upstream_provider", provider.slice(0, 256)]);
  }
  for (const [key, value] of [
    ["data_region", data?.data_region ?? data?.dataRegion],
    ["service_tier", data?.service_tier ?? data?.serviceTier],
    ["web_search_engine", data?.web_search_engine ?? data?.webSearchEngine],
  ] as const) {
    if (typeof value === "string" && value.length > 0) dimensions.push([key, value.slice(0, 256)]);
  }
  if (typeof data?.is_byok === "boolean") dimensions.push(["is_byok", data.is_byok ? "true" : "false"]);
  return {
    usageLines: lines,
    pricingUsage: Object.fromEntries(lines
      .filter((line) => [
        "input_tokens", "output_tokens", "cache_read_input_tokens", "reasoning_output_tokens",
      ].includes(line.metric))
      .map((line) => [line.metric, line.quantity])),
    providerRecordId: recordId(data),
    providerCostUsd: nonNegativeDecimal(data?.total_cost ?? data?.totalCost),
    providerUpstreamCostUsd: nonNegativeDecimal(
      data?.upstream_inference_cost ?? data?.upstreamInferenceCost,
    ),
    responseModel: prefixedModel("openrouter", data?.model ?? "unknown"),
    modelCandidates: [prefixedModel("openrouter", data?.model ?? "unknown")],
    billingDimensions: dimensions,
    inputTokens: inputTotal,
    outputTokens: outputTotal,
    cachedTokens: cached,
    reasoningTokens: reasoning,
  };
}

function reconcileGeneration(buffer: EventBuffer, response: any): void {
  const data = response?.data ?? response;
  const id = recordId(data);
  if (!id) return;
  const event = buffer.getAllEvents().find((candidate) =>
    candidate.provider === "openrouter" && candidate.details.provider_record_id === id,
  );
  if (!event) return;
  const meter = generationMeasurement(data);
  const previousCost = event.costUsd;
  const previousInput = event.inputTokens ?? 0;
  const previousOutput = event.outputTokens ?? 0;
  const previousCached = event.cachedTokens ?? 0;
  event.model = meter.responseModel ?? event.model;
  event.inputTokens = meter.inputTokens;
  event.outputTokens = meter.outputTokens;
  event.cachedTokens = meter.cachedTokens;
  const providerCost = nonNegativeDecimal(meter.providerCostUsd);
  if (providerCost !== undefined) {
    event.costUsd = providerCost;
    event.costConfidence = "exact";
    event.pricingSource = "provider_response";
    event.pricingVersion = undefined;
    event.details.provider_reported_cost_usd = providerCost.toString();
  }
  const upstreamCost = nonNegativeDecimal(meter.providerUpstreamCostUsd);
  if (upstreamCost !== undefined) {
    event.details.provider_upstream_cost_usd = upstreamCost.toString();
  }
  event.details.attribution_usage_lines = (meter.usageLines ?? [])
    .filter((line) => nonNegativeDecimal(line.quantity)?.gt(0))
    .map((line) => ({ metric: line.metric, quantity: String(line.quantity), unit: line.unit }));
  if (meter.billingDimensions?.length) {
    event.details.attribution_dimensions = meter.billingDimensions.map(([key, value]) => ({
      key, value: { type: "string", value },
    }));
  }
  buffer.updateEvent(event);
  const task = buffer.getTask(event.taskId);
  if (!task) return;
  const costDelta = event.costUsd.minus(previousCost);
  if (event.eventType === "llm_call") {
    task.llmCostUsd = task.llmCostUsd.plus(costDelta);
    task.totalInputTokens += (event.inputTokens ?? 0) - previousInput;
    task.totalOutputTokens += (event.outputTokens ?? 0) - previousOutput;
    task.totalCachedTokens += (event.cachedTokens ?? 0) - previousCached;
  } else {
    task.externalCostUsd = task.externalCostUsd.plus(costDelta);
  }
  task.totalCostUsd = task.totalCostUsd.plus(costDelta);
  buffer.upsertTask(task);
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
    const llm = ["chat", "responses", "embeddings", "images", "rerank", "classifications"].includes(service);
    const operationService = service === "stt" ? "speech_to_text"
      : service === "tts" ? "text_to_speech"
        : service === "images" ? "image_generation"
          : service;
    const component = service === "stt" ? "speech_to_text"
      : service === "tts" ? "text_to_speech"
        : llm ? "llm" : "external";
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: `openrouter.${operationService}.${name}`, provider: "openrouter", service: operationService,
      operation: `openrouter.${operationService}.${name}`,
      component, model,
      eventType: llm ? "llm_call" : "external_cost",
    });
    let result: any;
    try { result = session.invoke(() => original.apply(this, args)); }
    catch (error) { session.fail(error); throw error; }
    if (name === "callModel") return wrapModelResult(result, session, model);
    const complete = (response: any): any => {
      if (name === "getGeneration" && service === "video_generation") {
        const id = recordId(body) ?? recordId(response) ?? (typeof args[0] === "string" ? args[0] : undefined);
        if (id) reconcileVideoJob(pricing, buffer, response, id);
        session.finalizeWithoutEvent();
        return response;
      }
      if (name === "getGeneration" && service === "generation_reconciliation") {
        reconcileGeneration(buffer, response);
        session.finalizeWithoutEvent();
        return response;
      }
      if (name === "generate" && service === "video_generation") {
        submitVideoJob(pricing, buffer, session, response, model, body);
        return response;
      }
      if (body.stream === true) {
        let final = response;
        return wrapProviderStream(
          response, session,
          (chunk) => {
            const candidate = (chunk as any)?.response ?? chunk;
            if ((candidate as any)?.usage !== undefined || operationStatus(candidate, service, true) !== "unknown") {
              final = candidate;
            }
          },
          () => measurementFor(final, model, service, body),
          () => operationStatus(final, service, true),
        );
      }
      session.finish(measurementFor(response, model, service, body), operationStatus(response, service));
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
    mod = await import("@openrouter/sdk");
  }
  discover(mod, pricing, buffer);
  if (patches.length === 0) {
    throw new Error(
      "OpenRouter exposes no supported metered surface; pass the @openrouter/sdk module/class " +
      "or an exact @openrouter/agent OpenRouter instance via instrumentModules.openrouter",
    );
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
