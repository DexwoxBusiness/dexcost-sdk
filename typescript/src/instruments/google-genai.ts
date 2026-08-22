import { ProviderJobRevision, providerJobFromDict, type ProviderJobStatus } from "../core/provider-jobs.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import {
  mapProviderResult,
  providerJobMeasurementFields,
  ProviderOperationSession,
  wrapProviderStream,
  type OperationMeasurement,
  type ProviderUsageLine,
} from "./provider-metering.js";
import { nonNegativeDecimal, nonNegativeInteger, prefixedModel } from "./provider-extract.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

type MethodPatch = { kind: "method"; owner: any; name: string; original: (...args: any[]) => any };
type DescriptorPatch = { kind: "descriptor"; owner: any; name: string; original: PropertyDescriptor };
type Patch = MethodPatch | DescriptorPatch;
const patches: Patch[] = [];
let providedModule: any;
let patched = false;

type DirectKind = "content" | "embedding" | "image" | "interaction";
interface DirectSpec { kind: DirectKind; eventType: "llm_call" | "external_cost"; component: "llm" | "external" }

const DIRECT: Record<string, DirectSpec> = {
  generateContent: { kind: "content", eventType: "llm_call", component: "llm" },
  generateContentStream: { kind: "content", eventType: "llm_call", component: "llm" },
  embedContent: { kind: "embedding", eventType: "external_cost", component: "external" },
  generateImages: { kind: "image", eventType: "external_cost", component: "external" },
  upscaleImage: { kind: "image", eventType: "external_cost", component: "external" },
  editImage: { kind: "image", eventType: "external_cost", component: "external" },
  recontextImage: { kind: "image", eventType: "external_cost", component: "external" },
  segmentImage: { kind: "image", eventType: "external_cost", component: "external" },
};

function service(vertex: boolean): string { return vertex ? "vertex_ai" : "gemini"; }
function modelName(body: any, response?: any, vertex = false): string {
  const selected = response?.modelVersion ?? response?.model ?? body?.model ?? body?.baseModel ?? body?.agent ?? "unknown";
  return prefixedModel(vertex ? "vertex_ai" : "google", selected);
}

function positiveLine(metric: string, quantity: unknown, unit: string): ProviderUsageLine[] {
  const value = nonNegativeDecimal(quantity);
  return value?.gt(0) ? [{ metric, quantity: value, unit }] : [];
}

function contentMeasurement(response: any, body: any, vertex: boolean): OperationMeasurement {
  const usage = response?.usageMetadata ?? response?.usage_metadata ?? response?.usage ?? {};
  const prompt = nonNegativeInteger(
    usage.promptTokenCount ?? usage.prompt_token_count ?? usage.totalInputTokens ?? usage.total_input_tokens,
  );
  const cached = nonNegativeInteger(
    usage.cachedContentTokenCount ?? usage.cached_content_token_count ??
      usage.totalCachedTokens ?? usage.total_cached_tokens,
  );
  const candidates = nonNegativeInteger(
    usage.candidatesTokenCount ?? usage.candidates_token_count ??
      usage.totalOutputTokens ?? usage.total_output_tokens,
  );
  const reasoning = nonNegativeInteger(
    usage.thoughtsTokenCount ?? usage.thoughts_token_count ??
      usage.totalThoughtTokens ?? usage.total_thought_tokens,
  );
  const tool = nonNegativeInteger(
    usage.toolUsePromptTokenCount ?? usage.tool_use_prompt_token_count ??
      usage.totalToolUseTokens ?? usage.total_tool_use_tokens,
  );
  const ordinary = cached <= prompt ? prompt - cached : prompt;
  const lines: ProviderUsageLine[] = [
    ...positiveLine("input_tokens", ordinary, "Tokens"),
    ...positiveLine("cache_read_input_tokens", cached <= prompt ? cached : 0, "Tokens"),
    ...positiveLine("tool_input_tokens", tool, "Tokens"),
    ...positiveLine("output_tokens", candidates, "Tokens"),
    ...positiveLine("reasoning_output_tokens", reasoning, "Tokens"),
  ];
  return {
    usageLines: lines,
    providerRecordId: response?.responseId ?? response?.response_id ?? response?.id,
    responseModel: modelName(body, response, vertex),
    inputTokens: prompt + tool,
    outputTokens: candidates + reasoning,
    cachedTokens: cached <= prompt ? cached : 0,
    reasoningTokens: reasoning,
    billingDimensions: [["api", service(vertex)]],
  };
}

function embeddingMeasurement(response: any, body: any, vertex: boolean): OperationMeasurement {
  const embeddings = Array.isArray(response?.embeddings) ? response.embeddings : [];
  let tokens = 0;
  for (const item of embeddings) {
    tokens += nonNegativeInteger(item?.statistics?.tokenCount ?? item?.statistics?.token_count);
  }
  const characters = nonNegativeInteger(
    response?.metadata?.billableCharacterCount ?? response?.metadata?.billable_character_count,
  );
  return {
    usageLines: [
      ...positiveLine("input_tokens", tokens, "Tokens"),
      ...positiveLine("characters", characters, "Characters"),
      ...positiveLine("embedding_count", embeddings.length, "Embeddings"),
    ],
    responseModel: modelName(body, response, vertex), inputTokens: tokens,
    billingDimensions: [["api", service(vertex)]],
  };
}

function imageMeasurement(response: any, body: any, vertex: boolean, method: string): OperationMeasurement {
  const images = method === "segmentImage" ? response?.generatedMasks : response?.generatedImages;
  const count = Array.isArray(images) ? images.length : 0;
  return {
    usageLines: positiveLine("output_image_count", count, "Images"),
    responseModel: modelName(body, response, vertex),
    billingDimensions: [["api", service(vertex)], ["method", method]],
  };
}

function interactionMeasurement(response: any, body: any, vertex: boolean): OperationMeasurement {
  return contentMeasurement(response, body, vertex);
}

function directMeasurement(
  kind: DirectKind,
  response: any,
  body: any,
  vertex: boolean,
  method: string,
): OperationMeasurement {
  if (kind === "embedding") return embeddingMeasurement(response, body, vertex);
  if (kind === "image") return imageMeasurement(response, body, vertex, method);
  if (kind === "interaction") return interactionMeasurement(response, body, vertex);
  return contentMeasurement(response, body, vertex);
}

function googleJobStatus(resource: any, submission = false): ProviderJobStatus {
  if (resource?.done === false) return submission ? "submitted" : "running";
  if (resource?.done === true) {
    if (resource?.error) return String(resource.error).toLowerCase().includes("cancel") ? "cancelled" : "failed";
    return "succeeded";
  }
  const raw = String(resource?.state ?? resource?.status ?? "").toUpperCase().split(".").at(-1) ?? "";
  if (["SUCCEEDED", "SUCCESS", "COMPLETED", "ACTIVE", "JOB_STATE_SUCCEEDED"].includes(raw)) return "succeeded";
  if (["FAILED", "ERROR", "EXPIRED", "JOB_STATE_FAILED", "JOB_STATE_EXPIRED"].includes(raw)) return "failed";
  if (["CANCELLED", "CANCELED", "JOB_STATE_CANCELLED"].includes(raw)) return "cancelled";
  if (["RUNNING", "IN_PROGRESS", "PENDING", "QUEUED", "JOB_STATE_RUNNING", "JOB_STATE_PENDING"].includes(raw)) {
    return submission ? "submitted" : "running";
  }
  return submission ? "submitted" : "unknown";
}

function jobId(resource: any, body?: any): string | undefined {
  const value = resource?.name ?? resource?.id ?? resource?.responseId ?? resource?.response_id ??
    body?.name ?? body?.id ?? body?.interactionId ?? body?.interaction_id;
  return typeof value === "string" && value.length > 0 ? value.slice(0, 256) : undefined;
}

function videoMeasurement(resource: any, body: any, vertex: boolean): OperationMeasurement | undefined {
  const response = resource?.response ?? resource?.result;
  const videos = Array.isArray(response?.generatedVideos) ? response.generatedVideos :
    Array.isArray(response?.generated_videos) ? response.generated_videos : [];
  const count = videos.filter((item: any) => item?.video !== undefined).length;
  if (count === 0) return undefined;
  const duration = nonNegativeDecimal(
    body?.config?.durationSeconds ?? body?.config?.duration_seconds ?? body?.durationSeconds,
  );
  return {
    usageLines: [
      ...positiveLine("output_video_count", count, "Videos"),
      ...(duration?.gt(0) ? positiveLine("output_video_seconds", duration.times(count), "Seconds") : []),
    ],
    providerRecordId: jobId(resource), responseModel: modelName(body, undefined, vertex),
    billingDimensions: [["api", service(vertex)]],
  };
}

function batchMeasurement(resource: any, body: any, vertex: boolean): OperationMeasurement | undefined {
  const lines: ProviderUsageLine[] = [];
  const stats = resource?.completionStats ?? resource?.completion_stats ?? {};
  lines.push(...positiveLine(
    "batch_successful_request_count", stats.successfulCount ?? stats.successful_count, "Requests",
  ));
  lines.push(...positiveLine("batch_failed_request_count", stats.failedCount ?? stats.failed_count, "Requests"));
  lines.push(...positiveLine(
    "batch_incomplete_request_count", stats.incompleteCount ?? stats.incomplete_count, "Requests",
  ));
  const responses = resource?.dest?.inlinedResponses ?? resource?.dest?.inlined_responses ?? [];
  let input = 0; let output = 0; let cached = 0;
  if (Array.isArray(responses)) {
    for (const item of responses) {
      if (item?.response === undefined) continue;
      const measured = contentMeasurement(item.response, body, vertex);
      input += measured.inputTokens ?? 0; output += measured.outputTokens ?? 0; cached += measured.cachedTokens ?? 0;
      for (const line of measured.usageLines ?? []) {
        lines.push({ metric: `batch_${line.metric}`, quantity: line.quantity, unit: line.unit });
      }
    }
  }
  if (lines.length === 0) return undefined;
  return {
    usageLines: lines, providerRecordId: jobId(resource), responseModel: modelName(body, resource, vertex),
    inputTokens: input || undefined, outputTokens: output || undefined, cachedTokens: cached || undefined,
    billingDimensions: [["api", service(vertex)]],
  };
}

function tuningMeasurement(resource: any, body: any, vertex: boolean): OperationMeasurement | undefined {
  const stats = resource?.tuningDataStats ?? resource?.tuning_data_stats ?? {};
  const selected = stats.supervisedTuningDataStats ?? stats.supervised_tuning_data_stats ??
    stats.preferenceOptimizationDataStats ?? stats.preference_optimization_data_stats ??
    stats.reinforcementTuningDataStats ?? stats.reinforcement_tuning_data_stats ??
    stats.distillationDataStats?.trainingDatasetStats ?? stats.distillation_data_stats?.training_dataset_stats ?? {};
  const metadata = resource?.tuningJobMetadata ?? resource?.tuning_job_metadata ?? {};
  const lines = [
    ...positiveLine("training_billable_tokens", selected.totalBillableTokenCount ?? selected.total_billable_token_count, "Tokens"),
    ...positiveLine("training_billable_characters", selected.totalBillableCharacterCount ?? selected.total_billable_character_count, "Characters"),
    ...positiveLine("training_example_count", selected.tuningDatasetExampleCount ?? selected.tuning_dataset_example_count, "Examples"),
    ...positiveLine("training_step_count", metadata.completedStepCount ?? metadata.completed_step_count ?? selected.tuningStepCount, "Steps"),
    ...positiveLine("training_epoch_count", metadata.completedEpochCount ?? metadata.completed_epoch_count, "Epochs"),
  ];
  if (lines.length === 0) return undefined;
  return {
    usageLines: lines, providerRecordId: jobId(resource), responseModel: modelName(body, resource, vertex),
    billingDimensions: [["api", service(vertex)]],
  };
}

function insertJob(
  pricing: PricingEngine,
  buffer: EventBuffer,
  session: ProviderOperationSession,
  resource: any,
  options: {
    service: string; operation: string; resourceId: string; eventType: "llm_call" | "external_cost";
    measurement?: OperationMeasurement;
  },
): void {
  const id = jobId(resource);
  if (id === undefined) { session.fail(new Error("Google provider job omitted its id")); return; }
  let status = googleJobStatus(resource, true);
  if (status === "succeeded" && options.measurement === undefined) status = "unknown";
  const snapshot = status === "succeeded" ? options.measurement : undefined;
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    taskId: session.task.taskId, provider: "google", service: options.service,
    providerRecordId: id, operation: options.operation, component: "external",
    eventType: options.eventType, resourceType: "model", resourceId: options.resourceId,
    status, ownsTask: session.autoCreated, billingDimensions: [["api", options.service]],
    ...providerJobMeasurementFields(pricing, options.resourceId, snapshot),
  }));
  session.releaseForProviderJob();
}

function reconcileJob(
  pricing: PricingEngine,
  buffer: EventBuffer,
  serviceName: string,
  resource: any,
  measurement: OperationMeasurement | undefined,
  cancelled = false,
): void {
  const id = jobId(resource);
  if (id === undefined) return;
  const raw = buffer.getProviderJob("google", serviceName, id);
  if (raw === undefined) return;
  const previous = providerJobFromDict(raw);
  let status: ProviderJobStatus = cancelled ? "cancelled" : googleJobStatus(resource);
  if (status === "succeeded" && measurement === undefined) status = "unknown";
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    eventId: previous.eventId, revision: previous.revision + 1,
    taskId: previous.taskId, provider: previous.provider, service: previous.service,
    providerRecordId: id, operation: previous.operation, component: previous.component,
    eventType: previous.eventType, resourceType: previous.resourceType, resourceId: previous.resourceId,
    status, submittedAt: previous.submittedAt, ownsTask: previous.ownsTask,
    billingDimensions: previous.billingDimensions,
    ...providerJobMeasurementFields(pricing, previous.resourceId, measurement),
  }));
}

function patchDirect(
  owner: any,
  ownerName: string,
  name: string,
  spec: DirectSpec,
  vertex: boolean,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.kind === "method" && item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    const operation = `google.genai.${ownerName}.${name}`.toLowerCase();
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: operation, provider: "google", service: service(vertex), operation,
      component: spec.component, model: modelName(body, undefined, vertex), eventType: spec.eventType,
    });
    let result: any;
    try { result = session.invoke(() => original.apply(this, args)); }
    catch (error) { session.fail(error); throw error; }
    const complete = (response: any): any => {
      if (name.endsWith("Stream") || body?.stream === true) {
        let terminal = response;
        return wrapProviderStream(response, session, (chunk) => {
          terminal = (chunk as any)?.response ?? chunk;
        }, () => directMeasurement(spec.kind, terminal, body, vertex, name));
      }
      session.finish(directMeasurement(spec.kind, response, body, vertex, name));
      return response;
    };
    return mapProviderResult(result, complete, (error) => { session.fail(error); throw error; });
  };
  patches.push({ kind: "method", owner, name, original });
}

function patchJobCreate(
  owner: any,
  ownerName: string,
  name: string,
  kind: "video" | "batch" | "tuning" | "interaction",
  vertex: boolean,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.kind === "method" && item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    if (kind === "interaction" && body.background !== true) {
      return patchlessDirectCall(this, original, args, body, ownerName, name, vertex, pricing, buffer);
    }
    const operation = kind === "video" ? "google.genai.models.generate_videos" :
      kind === "batch" ? "google.genai.batches.create" :
        kind === "tuning" ? "google.genai.tunings.tune" : "google.genai.interactions.create";
    const resourceId = modelName(body, undefined, vertex);
    const serviceName = service(vertex);
    const eventType = kind === "tuning" ? "external_cost" : "llm_call";
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: operation, provider: "google", service: serviceName, operation,
      component: "external", model: resourceId, eventType,
    });
    let result: any;
    try { result = session.invoke(() => original.apply(this, args)); }
    catch (error) { session.fail(error); throw error; }
    const complete = (resource: any): any => {
      const meter = kind === "video" ? videoMeasurement(resource, body, vertex) :
        kind === "batch" ? batchMeasurement(resource, body, vertex) :
          kind === "tuning" ? tuningMeasurement(resource, body, vertex) :
            interactionMeasurement(resource, body, vertex);
      insertJob(pricing, buffer, session, resource, {
        service: serviceName, operation, resourceId, eventType, measurement: meter,
      });
      return resource;
    };
    return mapProviderResult(result, complete, (error) => { session.fail(error); throw error; });
  };
  patches.push({ kind: "method", owner, name, original });
}

function patchlessDirectCall(
  self: any,
  original: (...args: any[]) => any,
  args: any[],
  body: any,
  ownerName: string,
  name: string,
  vertex: boolean,
  pricing: PricingEngine,
  buffer: EventBuffer,
): any {
  const operation = `google.genai.${ownerName}.${name}`.toLowerCase();
  const session = new ProviderOperationSession(pricing, buffer, {
    taskType: operation, provider: "google", service: service(vertex), operation,
    component: "external", model: modelName(body, undefined, vertex), eventType: "llm_call",
  });
  let result: any;
  try { result = session.invoke(() => original.apply(self, args)); }
  catch (error) { session.fail(error); throw error; }
  const complete = (response: any): any => {
    if (body.stream === true) {
      let terminal = response;
      return wrapProviderStream(response, session, (chunk) => { terminal = (chunk as any)?.interaction ?? chunk; },
        () => interactionMeasurement(terminal, body, vertex));
    }
    session.finish(interactionMeasurement(response, body, vertex));
    return response;
  };
  return mapProviderResult(result, complete, (error) => { session.fail(error); throw error; });
}

function patchJobReconcile(
  owner: any,
  name: string,
  kind: "video" | "batch" | "tuning" | "interaction",
  vertex: boolean,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.kind === "method" && item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    const complete = (resource: any): any => {
      const vertexService = service(vertex);
      const meter = kind === "video" ? videoMeasurement(resource, body, vertex) :
        kind === "batch" ? batchMeasurement(resource, body, vertex) :
          kind === "tuning" ? tuningMeasurement(resource, body, vertex) :
            interactionMeasurement(resource, body, vertex);
      reconcileJob(pricing, buffer, vertexService, resource, meter, name === "cancel");
      return resource;
    };
    let result: any;
    try { result = original.apply(this, args); }
    catch (error) { throw error; }
    return mapProviderResult(result, complete, (error) => { throw error; });
  };
  patches.push({ kind: "method", owner, name, original });
}

function patchLazyGetter(
  owner: any,
  name: string,
  vertex: boolean,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  const descriptor = Object.getOwnPropertyDescriptor(owner, name);
  if (descriptor?.get === undefined || descriptor.configurable === false) return;
  if (patches.some((item) => item.kind === "descriptor" && item.owner === owner && item.name === name)) return;
  const originalGet = descriptor.get;
  Object.defineProperty(owner, name, {
    ...descriptor,
    get(this: any): any {
      const resource = originalGet.call(this);
      discover(resource, pricing, buffer, name, vertex);
      return resource;
    },
  });
  patches.push({ kind: "descriptor", owner, name, original: descriptor });
}

function discover(
  root: any,
  pricing: PricingEngine,
  buffer: EventBuffer,
  rootName = "googleGenAI",
  inheritedVertex = false,
): void {
  const seen = new Set<any>();
  const visit = (value: any, name: string, depth: number, vertex: boolean): void => {
    if (!value || (typeof value !== "object" && typeof value !== "function") || seen.has(value) || depth > 5) return;
    seen.add(value);
    const nextVertex = typeof value?.vertexai === "boolean" ? value.vertexai : vertex;
    const owner = typeof value === "function" ? value.prototype : value;
    for (const [method, spec] of Object.entries(DIRECT)) {
      try { patchDirect(owner, name, method, spec, nextVertex, pricing, buffer); } catch { /* immutable */ }
    }
    const lower = name.toLowerCase();
    if (lower.includes("models")) {
      try { patchJobCreate(owner, name, "generateVideos", "video", nextVertex, pricing, buffer); } catch { /* immutable */ }
    }
    if (lower.includes("batches")) {
      for (const method of ["create", "createEmbeddings"]) {
        try { patchJobCreate(owner, name, method, "batch", nextVertex, pricing, buffer); } catch { /* immutable */ }
      }
      for (const method of ["get", "cancel"]) {
        try { patchJobReconcile(owner, method, "batch", nextVertex, pricing, buffer); } catch { /* immutable */ }
      }
    }
    if (lower.includes("tunings")) {
      try { patchJobCreate(owner, name, "tune", "tuning", nextVertex, pricing, buffer); } catch { /* immutable */ }
      for (const method of ["get", "cancel"]) {
        try { patchJobReconcile(owner, method, "tuning", nextVertex, pricing, buffer); } catch { /* immutable */ }
      }
    }
    if (lower.includes("operations")) {
      for (const method of ["get", "getVideosOperation"]) {
        try { patchJobReconcile(owner, method, "video", nextVertex, pricing, buffer); } catch { /* immutable */ }
      }
    }
    if (lower.includes("interactions")) {
      try { patchJobCreate(owner, name, "create", "interaction", nextVertex, pricing, buffer); } catch { /* immutable */ }
      for (const method of ["get", "cancel"]) {
        try { patchJobReconcile(owner, method, "interaction", nextVertex, pricing, buffer); } catch { /* immutable */ }
      }
    }
    if (lower.includes("googlegenai")) {
      try { patchLazyGetter(owner, "interactions", nextVertex, pricing, buffer); } catch { /* immutable */ }
    }
    for (const key of Object.keys(value).slice(0, 100)) {
      if (["constructor", "length", "name", "prototype"].includes(key)) continue;
      try { visit(value[key], `${name}.${key}`, depth + 1, nextVertex); } catch { /* getter side effect */ }
    }
  };
  visit(root, rootName, 0, inheritedVertex);
  if (root?.default !== root) visit(root?.default, "GoogleGenAI", 0, inheritedVertex);
}

export async function instrumentGoogleGenAI(pricing: PricingEngine, buffer: EventBuffer): Promise<void> {
  if (patched) return;
  let mod = providedModule;
  if (!mod) {
    // @ts-expect-error optional current official SDK
    mod = await import("@google/genai");
  }
  discover(mod, pricing, buffer);
  if (!patches.some((item) => item.kind === "method")) {
    uninstrumentGoogleGenAI();
    throw new Error(
      "@google/genai defines billable methods as instance fields; pass the GoogleGenAI client via " +
      "instrumentModules['@google/genai'] so DexCost can instrument the exact client instance",
    );
  }
  patched = true;
}

export function uninstrumentGoogleGenAI(): void {
  for (const item of patches.splice(0).reverse()) {
    if (item.kind === "method") item.owner[item.name] = item.original;
    else Object.defineProperty(item.owner, item.name, item.original);
  }
  patched = false;
}
export function provideGoogleGenAIModule(ref: unknown): void { providedModule = ref; }
registerInstrument("google-genai", instrumentGoogleGenAI, uninstrumentGoogleGenAI, provideGoogleGenAIModule);
