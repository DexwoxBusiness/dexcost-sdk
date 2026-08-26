import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import {
  mapProviderResult,
  providerJobMeasurementFields,
  ProviderOperationSession,
  wrapProviderStream,
  type OperationMeasurement,
  type ProviderOperationOptions,
} from "./provider-metering.js";
import { field, nonNegativeDecimal, nonNegativeInteger, tokenMeasurement } from "./provider-extract.js";
import { ProviderJobRevision, providerJobFromDict, type ProviderJobStatus } from "../core/provider-jobs.js";
import {
  canonicalLiteLlmModel,
  classifyLiteLlmProvider,
} from "./litellm-routing.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

export { classifyLiteLlmProvider } from "./litellm-routing.js";

const patches: Array<{ owner: any; name: string; original: (...args: any[]) => any }> = [];
let providedModule: any;
let patched = false;

type OperationKind = "tokens" | "embedding" | "image" | "transcription" | "speech" | "rerank" | "ocr" | "search" | "moderation";
interface OperationSpec {
  kind: OperationKind;
  component: "llm" | "image" | "speech_to_text" | "text_to_speech" | "rerank" | "ocr" | "search" | "moderation";
  service: string;
  eventType: "llm_call" | "external_cost";
}

const OPERATION_SPECS: Record<string, OperationSpec> = {
  completion: { kind: "tokens", component: "llm", service: "litellm", eventType: "llm_call" },
  acompletion: { kind: "tokens", component: "llm", service: "litellm", eventType: "llm_call" },
  chat: { kind: "tokens", component: "llm", service: "litellm", eventType: "llm_call" },
  generate: { kind: "tokens", component: "llm", service: "litellm", eventType: "llm_call" },
  text_completion: { kind: "tokens", component: "llm", service: "litellm", eventType: "llm_call" },
  atext_completion: { kind: "tokens", component: "llm", service: "litellm", eventType: "llm_call" },
  responses: { kind: "tokens", component: "llm", service: "litellm", eventType: "llm_call" },
  aresponses: { kind: "tokens", component: "llm", service: "litellm", eventType: "llm_call" },
  embed: { kind: "embedding", component: "llm", service: "embeddings", eventType: "external_cost" },
  embedding: { kind: "embedding", component: "llm", service: "embeddings", eventType: "external_cost" },
  aembedding: { kind: "embedding", component: "llm", service: "embeddings", eventType: "external_cost" },
  image_generation: { kind: "image", component: "image", service: "images", eventType: "external_cost" },
  aimage_generation: { kind: "image", component: "image", service: "images", eventType: "external_cost" },
  image_edit: { kind: "image", component: "image", service: "images", eventType: "external_cost" },
  aimage_edit: { kind: "image", component: "image", service: "images", eventType: "external_cost" },
  image_variation: { kind: "image", component: "image", service: "images", eventType: "external_cost" },
  aimage_variation: { kind: "image", component: "image", service: "images", eventType: "external_cost" },
  transcription: { kind: "transcription", component: "speech_to_text", service: "speech_to_text", eventType: "external_cost" },
  atranscription: { kind: "transcription", component: "speech_to_text", service: "speech_to_text", eventType: "external_cost" },
  speech: { kind: "speech", component: "text_to_speech", service: "text_to_speech", eventType: "external_cost" },
  aspeech: { kind: "speech", component: "text_to_speech", service: "text_to_speech", eventType: "external_cost" },
  rerank: { kind: "rerank", component: "rerank", service: "rerank", eventType: "external_cost" },
  arerank: { kind: "rerank", component: "rerank", service: "rerank", eventType: "external_cost" },
  ocr: { kind: "ocr", component: "ocr", service: "ocr", eventType: "external_cost" },
  aocr: { kind: "ocr", component: "ocr", service: "ocr", eventType: "external_cost" },
  search: { kind: "search", component: "search", service: "search", eventType: "external_cost" },
  asearch: { kind: "search", component: "search", service: "search", eventType: "external_cost" },
  moderation: { kind: "moderation", component: "moderation", service: "moderation", eventType: "external_cost" },
  amoderation: { kind: "moderation", component: "moderation", service: "moderation", eventType: "external_cost" },
};

type JobKind = "response" | "video" | "batch" | "fine_tuning";
interface JobSpec { kind: JobKind; phase: "submit" | "reconcile"; recordIdName?: string }
const JOB_SPECS: Record<string, JobSpec> = {
  video_generation: { kind: "video", phase: "submit" },
  avideo_generation: { kind: "video", phase: "submit" },
  video_edit: { kind: "video", phase: "submit" },
  avideo_edit: { kind: "video", phase: "submit" },
  video_remix: { kind: "video", phase: "submit" },
  avideo_remix: { kind: "video", phase: "submit" },
  video_extension: { kind: "video", phase: "submit" },
  avideo_extension: { kind: "video", phase: "submit" },
  video_status: { kind: "video", phase: "reconcile", recordIdName: "video_id" },
  avideo_status: { kind: "video", phase: "reconcile", recordIdName: "video_id" },
  create_batch: { kind: "batch", phase: "submit" },
  acreate_batch: { kind: "batch", phase: "submit" },
  retrieve_batch: { kind: "batch", phase: "reconcile", recordIdName: "batch_id" },
  aretrieve_batch: { kind: "batch", phase: "reconcile", recordIdName: "batch_id" },
  cancel_batch: { kind: "batch", phase: "reconcile", recordIdName: "batch_id" },
  acancel_batch: { kind: "batch", phase: "reconcile", recordIdName: "batch_id" },
  create_fine_tuning_job: { kind: "fine_tuning", phase: "submit" },
  acreate_fine_tuning_job: { kind: "fine_tuning", phase: "submit" },
  retrieve_fine_tuning_job: { kind: "fine_tuning", phase: "reconcile", recordIdName: "fine_tuning_job_id" },
  aretrieve_fine_tuning_job: { kind: "fine_tuning", phase: "reconcile", recordIdName: "fine_tuning_job_id" },
  cancel_fine_tuning_job: { kind: "fine_tuning", phase: "reconcile", recordIdName: "fine_tuning_job_id" },
  acancel_fine_tuning_job: { kind: "fine_tuning", phase: "reconcile", recordIdName: "fine_tuning_job_id" },
  get_responses: { kind: "response", phase: "reconcile", recordIdName: "response_id" },
  aget_responses: { kind: "response", phase: "reconcile", recordIdName: "response_id" },
  cancel_responses: { kind: "response", phase: "reconcile", recordIdName: "response_id" },
  acancel_responses: { kind: "response", phase: "reconcile", recordIdName: "response_id" },
};

function jobService(kind: JobKind): string {
  return `litellm.${kind === "response" ? "responses" : kind === "video" ? "videos" : kind === "batch" ? "batches" : "fine_tuning"}`;
}

function jobStatus(resource: any, submission: boolean): ProviderJobStatus {
  const value = String(resource?.status ?? "").trim().toLowerCase().replace(/-/g, "_");
  if (["completed", "succeeded", "success"].includes(value)) return "succeeded";
  if (["failed", "error", "expired"].includes(value)) return "failed";
  if (["cancelled", "canceled"].includes(value)) return "cancelled";
  if (value === "incomplete") return "unknown";
  return submission ? "submitted" : "running";
}

function jobRecordId(spec: JobSpec, args: any[], resource: any): string | undefined {
  const body = args[0] ?? {};
  const value = resource?.id ?? (spec.recordIdName === undefined ? undefined
    : typeof body === "string" ? body : body?.[spec.recordIdName] ?? args.find((item) => typeof item === "string"));
  return typeof value === "string" && value.length > 0 ? value.slice(0, 256) : undefined;
}

function requestedJobModel(spec: JobSpec, body: any): unknown {
  if (typeof body?.model === "string") return body.model;
  if (spec.kind === "batch") return typeof body?.endpoint === "string" ? `batch:${body.endpoint}` : "batch:unknown";
  return `${spec.kind.replace("_", "-")}:unknown`;
}

function jobMeasurement(spec: JobSpec, resource: any, model: string, provider: string): OperationMeasurement | undefined {
  if (spec.kind === "response") {
    const result = resolvedResponse(resource, model, provider, OPERATION_SPECS.responses!, {}).measurement;
    return (result.usageLines?.length ?? 0) > 0 ? result : undefined;
  }
  const usageLines: NonNullable<OperationMeasurement["usageLines"]> = [];
  const pricingUsage: Record<string, string | number | bigint | import("../core/models.js").Decimal> = {};
  let inputTokens: number | undefined;
  let outputTokens: number | undefined;
  let cachedTokens: number | undefined;
  if (spec.kind === "video") {
    const seconds = nonNegativeDecimal(resource?.seconds ?? resource?.usage?.duration_seconds);
    if (seconds?.gt(0)) {
      usageLines.push({ metric: "output_video_count", quantity: 1, unit: "Videos" });
      usageLines.push({ metric: "output_video_seconds", quantity: seconds, unit: "Seconds" });
      pricingUsage.output_video_count = 1;
      pricingUsage.output_video_seconds = seconds;
    }
  } else if (spec.kind === "batch") {
    const usage = tokenMeasurement(resource, model, provider);
    inputTokens = usage.inputTokens;
    outputTokens = usage.outputTokens;
    cachedTokens = usage.cachedTokens;
    usageLines.push(...line("batch_input_tokens", Math.max(0, (inputTokens ?? 0) - (cachedTokens ?? 0)), "Tokens"));
    usageLines.push(...line("batch_cache_read_input_tokens", cachedTokens, "Tokens"));
    usageLines.push(...line("batch_output_tokens", outputTokens, "Tokens"));
    for (const [metric, key] of [
      ["batch_request_count", "total"],
      ["batch_successful_request_count", "completed"],
      ["batch_failed_request_count", "failed"],
    ] as const) usageLines.push(...line(metric, resource?.request_counts?.[key], "Requests"));
  } else {
    usageLines.push(...line("training_billable_tokens", resource?.trained_tokens, "Tokens"));
  }
  if (usageLines.length === 0) return undefined;
  const measurement: OperationMeasurement = {
    usageLines,
    pricingUsage,
    providerRecordId: typeof resource?.id === "string" ? resource.id : undefined,
    responseModel: model,
    inputTokens,
    outputTokens,
    cachedTokens,
    billingDimensions: [["gateway", "litellm"]],
  };
  const gatewayCost = nonNegativeDecimal(field(resource, "_hidden_params", "response_cost"));
  if (gatewayCost?.gt(0)) measurement.gatewayCalculatedCostUsd = gatewayCost;
  return measurement;
}

function finishJob(
  name: string,
  spec: JobSpec,
  args: any[],
  resource: any,
  session: ProviderOperationSession | undefined,
  pricing: PricingEngine,
  buffer: EventBuffer,
): any {
  const body = args[0] ?? {};
  const requestedModel = requestedJobModel(spec, body);
  const provider = classifyLiteLlmProvider(
    field(resource, "_hidden_params", "custom_llm_provider"), resource?.provider,
    body?.custom_llm_provider, requestedModel,
  );
  const model = canonicalLiteLlmModel(provider, resource?.model, requestedModel);
  const recordId = jobRecordId(spec, args, resource);
  if (recordId === undefined) { session?.fail(new Error(`LiteLLM ${name} response omitted durable job id`)); return resource; }
  let status = jobStatus(resource, spec.phase === "submit");
  const measurement = status === "submitted" || status === "running"
    ? undefined : jobMeasurement(spec, resource, model, provider);
  if (status === "succeeded" && measurement === undefined) status = "unknown";
  const service = jobService(spec.kind);
  if (session) {
    const now = new Date();
    buffer.insertProviderJobRevision(new ProviderJobRevision({
      taskId: session.task.taskId, provider, service, providerRecordId: recordId,
      operation: `litellm.${name}`, component: spec.kind === "response" || spec.kind === "batch" ? "llm" : spec.kind,
      eventType: spec.kind === "response" || spec.kind === "batch" ? "llm_call" : "external_cost",
      resourceType: "model", resourceId: model, status, submittedAt: now, observedAt: now,
      ownsTask: session.autoCreated,
      billingDimensions: [
        ["gateway", "litellm"],
        ...(typeof body?.endpoint === "string" ? [["batch_endpoint", body.endpoint] as const] : []),
        ...(typeof body?.completion_window === "string" ? [["batch_completion_window", body.completion_window] as const] : []),
      ],
      ...providerJobMeasurementFields(pricing, model, measurement), capability: session.capability,
    }));
    session.releaseForProviderJob();
    return resource;
  }
  const stored = buffer.getProviderJob(provider, service, recordId);
  if (stored === undefined) return resource;
  const previous = providerJobFromDict(stored);
  buffer.insertProviderJobRevision(new ProviderJobRevision({
    eventId: previous.eventId, revision: previous.revision + 1, taskId: previous.taskId,
    provider: previous.provider, service: previous.service, providerRecordId: previous.providerRecordId,
    operation: previous.operation, component: previous.component, eventType: previous.eventType,
    resourceType: previous.resourceType, resourceId: previous.resourceId, status,
    submittedAt: previous.submittedAt,
    observedAt: new Date(Math.max(Date.now(), previous.observedAt.getTime())),
    ownsTask: previous.ownsTask, billingDimensions: previous.billingDimensions,
    ...providerJobMeasurementFields(pricing, previous.resourceId, measurement), capability: previous.capability,
  }));
  return resource;
}

function patchJobMethod(owner: any, name: string, spec: JobSpec, pricing: PricingEngine, buffer: EventBuffer): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((patch) => patch.owner === owner && patch.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    const requestedModel = requestedJobModel(spec, body);
    const provider = classifyLiteLlmProvider(body?.custom_llm_provider, body?.provider, requestedModel);
    const model = canonicalLiteLlmModel(provider, undefined, requestedModel);
    const session = spec.phase === "submit" ? new ProviderOperationSession(pricing, buffer, {
      taskType: `litellm.${name}`, provider, service: jobService(spec.kind), operation: `litellm.${name}`,
      component: spec.kind === "response" || spec.kind === "batch" ? "llm" : spec.kind,
      model, eventType: spec.kind === "response" || spec.kind === "batch" ? "llm_call" : "external_cost",
    }) : undefined;
    let result: any;
    try { result = session ? session.invoke(() => original.apply(this, args)) : original.apply(this, args); }
    catch (error) { session?.fail(error); throw error; }
    return mapProviderResult(result,
      (resource) => finishJob(name, spec, args, resource, session, pricing, buffer),
      (error) => { session?.fail(error); throw error; });
  };
  patches.push({ owner, name, original });
}

function resolvedResponse(
  response: any,
  requestedModel: unknown,
  fallbackProvider: string,
  spec: OperationSpec = OPERATION_SPECS.completion!,
  body: any = {},
): { provider: string; model: string; measurement: OperationMeasurement } {
  const provider = classifyLiteLlmProvider(
    field(response, "_hidden_params", "custom_llm_provider"),
    response?.provider,
    fallbackProvider,
  );
  const model = canonicalLiteLlmModel(provider, response?.model, requestedModel);
  const measurement = operationMeasurement(spec, response, body, model, provider);
  measurement.responseModel = model;
  measurement.billingDimensions = [["gateway", "litellm"]];

  // LiteLLM exposes OpenRouter's authoritative response cost in this header
  // when the normalized usage object does not contain `cost` itself.
  if (provider === "openrouter" && measurement.providerCostUsd === undefined) {
    const headerCost = nonNegativeDecimal(
      field(response, "_hidden_params", "additional_headers", "llm_provider-x-litellm-response-cost"),
    );
    if (headerCost !== undefined) measurement.providerCostUsd = headerCost;
  }
  const gatewayCost = nonNegativeDecimal(field(response, "_hidden_params", "response_cost"));
  if (measurement.providerCostUsd === undefined && gatewayCost !== undefined) {
    measurement.gatewayCalculatedCostUsd = gatewayCost;
  }
  return { provider, model, measurement };
}

function line(metric: string, quantity: unknown, unit: string): Array<{ metric: string; quantity: number; unit: string }> {
  const value = Number(quantity);
  return Number.isFinite(value) && value > 0 ? [{ metric, quantity: value, unit }] : [];
}

function operationMeasurement(
  spec: OperationSpec,
  response: any,
  body: any,
  model: string,
  provider: string,
): OperationMeasurement {
  if (spec.kind === "tokens") return tokenMeasurement(response, model, provider);
  if (spec.kind === "embedding") {
    const result = tokenMeasurement(response, model, provider);
    const count = Array.isArray(response?.data) ? response.data.length
      : Array.isArray(response?.embeddings) ? response.embeddings.length : 0;
    result.usageLines = [...(result.usageLines ?? []), ...line("embedding_count", count, "Embeddings")];
    return result;
  }
  if (spec.kind === "image") {
    const count = Array.isArray(response?.data) ? response.data.length
      : Array.isArray(response?.images) ? response.images.length
        : nonNegativeInteger(body?.n) || 1;
    return {
      usageLines: line("image_count", count, "Images"),
      pricingUsage: { image_count: count },
      providerRecordId: typeof response?.id === "string" ? response.id : undefined,
      responseModel: model,
      billingDimensions: [
        ["gateway", "litellm"],
        ...(typeof body?.size === "string" ? [["size", body.size] as const] : []),
        ...(typeof body?.quality === "string" ? [["quality", body.quality] as const] : []),
      ],
    };
  }
  if (spec.kind === "transcription") {
    const seconds = nonNegativeDecimal(
      response?.usage?.seconds ?? field(response, "_hidden_params", "audio_transcription_duration") ?? response?.duration,
    );
    if (seconds?.gt(0)) return {
      usageLines: [{ metric: "audio_seconds", quantity: seconds, unit: "Seconds" }],
      pricingUsage: { input_audio_seconds: seconds },
      providerRecordId: typeof response?.id === "string" ? response.id : undefined,
      responseModel: model,
    };
    return tokenMeasurement(response, model, provider);
  }
  if (spec.kind === "speech") {
    const characters = typeof body?.input === "string" ? [...body.input].length : 0;
    return {
      usageLines: line("characters", characters, "Characters"),
      pricingUsage: characters > 0 ? { characters } : {},
      providerRecordId: typeof response?.id === "string" ? response.id : undefined,
      responseModel: model,
    };
  }
  if (spec.kind === "rerank") {
    const billed = response?.meta?.billed_units ?? response?.meta?.billedUnits ?? {};
    const searchUnits = Number(billed.search_units ?? billed.searchUnits ?? 0);
    const totalTokens = nonNegativeInteger(billed.total_tokens ?? billed.totalTokens);
    return {
      usageLines: [
        ...line("search_units", searchUnits, "SearchUnits"),
        ...line("input_tokens", totalTokens, "Tokens"),
        ...(searchUnits > 0 || (totalTokens ?? 0) > 0 ? [] : line("query_count", 1, "Queries")),
      ],
      pricingUsage: {
        ...(searchUnits > 0 ? { query_count: searchUnits } : { query_count: 1 }),
        ...((totalTokens ?? 0) > 0 ? { input_tokens: totalTokens! } : {}),
      },
      providerRecordId: typeof response?.id === "string" ? response.id : undefined,
      responseModel: model,
      inputTokens: totalTokens,
    };
  }
  if (spec.kind === "ocr") {
    const pages = nonNegativeInteger(response?.usage_info?.pages_processed ?? response?.usageInfo?.pagesProcessed);
    const bytes = nonNegativeInteger(response?.usage_info?.doc_size_bytes ?? response?.usageInfo?.docSizeBytes);
    return {
      usageLines: [...line("page_count", pages, "Pages"), ...line("document_bytes", bytes, "Bytes")],
      pricingUsage: (pages ?? 0) > 0 ? { page_count: pages! } : {},
      providerRecordId: typeof response?.id === "string" ? response.id : undefined,
      responseModel: model,
    };
  }
  if (spec.kind === "search") {
    const count = Array.isArray(body?.query) ? body.query.length : 1;
    return {
      usageLines: line("query_count", count, "Queries"),
      pricingUsage: { query_count: count },
      providerRecordId: typeof response?.id === "string" ? response.id : undefined,
      responseModel: model,
    };
  }
  return {
    usageLines: [{ metric: "request_count", quantity: 1, unit: "Requests" }],
    pricingUsage: { request_count: 1 },
    providerRecordId: typeof response?.id === "string" ? response.id : undefined,
    responseModel: model,
  };
}

function patchMethod(
  owner: any,
  name: string,
  spec: OperationSpec,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  const original = owner[name] as (...args: any[]) => any;
  if (patches.some((patch) => patch.owner === owner && patch.name === name)) return;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    if ((name === "responses" || name === "aresponses") && body?.background === true) {
      const jobSpec: JobSpec = { kind: "response", phase: "submit" };
      const requestedModel = body?.model;
      const provider = classifyLiteLlmProvider(body?.custom_llm_provider, body?.provider, requestedModel);
      const model = canonicalLiteLlmModel(provider, undefined, requestedModel);
      const jobSession = new ProviderOperationSession(pricing, buffer, {
        taskType: `litellm.${name}`, provider, service: jobService("response"),
        operation: `litellm.${name}`, component: "llm", model, eventType: "llm_call",
      });
      let jobResult: any;
      try { jobResult = jobSession.invoke(() => original.apply(this, args)); }
      catch (error) { jobSession.fail(error); throw error; }
      return mapProviderResult(jobResult,
        (resource) => finishJob(name, jobSpec, args, resource, jobSession, pricing, buffer),
        (error) => { jobSession.fail(error); throw error; });
    }
    const requestedModel = body?.model ?? args.find((item) => typeof item === "string");
    const provider = classifyLiteLlmProvider(
      body?.custom_llm_provider,
      body?.provider,
      requestedModel,
    );
    const model = canonicalLiteLlmModel(provider, undefined, requestedModel);
    const operation: ProviderOperationOptions = {
      taskType: `litellm.${name}`, provider, service: spec.service,
      operation: `litellm.${name}`, component: spec.component, model, eventType: spec.eventType,
    };
    const session = new ProviderOperationSession(pricing, buffer, operation);
    let result: any;
    try {
      result = session.invoke(() => original.apply(this, args));
    } catch (error) {
      session.fail(error);
      throw error;
    }
    const complete = (response: any): any => {
      if (body?.stream) {
        let terminal: any;
        return wrapProviderStream(
          response,
          session,
          (chunk) => {
            terminal = chunk;
            const resolved = resolvedResponse(chunk, requestedModel, operation.provider, spec, body);
            operation.provider = resolved.provider;
            operation.model = resolved.model;
          },
          () => resolvedResponse(terminal, requestedModel, operation.provider, spec, body).measurement,
        );
      }
      const resolved = resolvedResponse(response, requestedModel, provider, spec, body);
      operation.provider = resolved.provider;
      operation.model = resolved.model;
      session.finish(resolved.measurement);
      return response;
    };
    return mapProviderResult(result, complete, (error: unknown) => {
      session.fail(error);
      throw error;
    });
  };
  patches.push({ owner, name, original });
}

export async function instrumentLiteLlm(pricing: PricingEngine, buffer: EventBuffer): Promise<void> {
  if (patched) return;
  let mod = providedModule;
  if (!mod) {
    // @ts-expect-error optional provider package
    mod = await import("litellm");
  }
  const roots = [mod, mod?.default, mod?.LiteLLM?.prototype, mod?.Client?.prototype];
  for (const owner of roots) {
    for (const [name, spec] of Object.entries(OPERATION_SPECS)) {
      try { patchMethod(owner, name, spec, pricing, buffer); } catch { /* immutable module namespace */ }
    }
    for (const [name, spec] of Object.entries(JOB_SPECS)) {
      try { patchJobMethod(owner, name, spec, pricing, buffer); } catch { /* immutable module namespace */ }
    }
  }
  if (patches.length === 0) throw new Error("litellm package exposes no supported completion surface");
  patched = true;
}

export function uninstrumentLiteLlm(): void {
  for (const patch of patches.splice(0)) patch.owner[patch.name] = patch.original;
  patched = false;
}

export function provideLiteLlmModule(ref: unknown): void { providedModule = ref; }

registerInstrument("litellm", instrumentLiteLlm, uninstrumentLiteLlm, provideLiteLlmModule);
