import type { Task } from "../core/models.js";
import { runWithTask } from "../core/context.js";
import { ProviderJobRevision, providerJobFromDict, type ProviderJobStatus } from "../core/provider-jobs.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine } from "../pricing/engine.js";
import {
  mapProviderResult,
  providerJobMeasurementFields,
  ProviderOperationSession,
  wrapProviderStream,
  type OperationMeasurement,
  type ProviderUsageLine,
} from "./provider-metering.js";
import {
  canonicalXaiModel,
  groqPricingLane,
  groqToolExecutionBlocksStaticPricing,
  nonNegativeDecimal,
  nonNegativeInteger,
  prefixedModel,
  tokenMeasurement,
  xaiPricingLane,
} from "./provider-extract.js";
import {
  canonicalLiteLlmModel,
  classifyLiteLlmProvider,
  isConfiguredLiteLlmProxyUrl,
} from "./litellm-routing.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

interface Patch { owner: any; name: string; original: (...args: any[]) => any }
const patches: Patch[] = [];

type DirectKind = "tokens" | "embeddings" | "images" | "transcription" | "speech" | "moderation";
interface DirectSpec {
  service: string;
  operation: string;
  component: "llm" | "external";
  eventType: "llm_call" | "external_cost";
  kind: DirectKind;
}

function resourceBaseUrl(resource: any): string {
  return String(resource?._client?.baseURL ?? resource?._client?.base_url ?? "");
}

function isLiteLlmGateway(resource: any): boolean {
  return isConfiguredLiteLlmProxyUrl(resourceBaseUrl(resource));
}

function routedProvider(resource: any, requestedModel?: unknown): string {
  try {
    const raw = resourceBaseUrl(resource);
    if (isConfiguredLiteLlmProxyUrl(raw)) return classifyLiteLlmProvider(requestedModel);
    const hostname = new URL(raw).hostname.toLowerCase();
    if (hostname === "openrouter.ai" || hostname.endsWith(".openrouter.ai")) return "openrouter";
    if (hostname === "api.perplexity.ai" || hostname.endsWith(".perplexity.ai")) return "perplexity";
    if (hostname === "api.deepseek.com" || hostname.endsWith(".deepseek.com")) return "deepseek";
    if (hostname === "api.moonshot.ai") return "moonshot";
    if (hostname === "api.fireworks.ai" || hostname.endsWith(".api.fireworks.ai")) return "fireworks_ai";
    if (hostname === "api.x.ai" || hostname.endsWith(".api.x.ai")) return "xai";
    if (hostname === "api.groq.com" || hostname.endsWith(".api.groq.com")) return "groq";
    if (hostname === "api.mistral.ai") return "mistral";
    if (hostname.endsWith(".openai.azure.com") || hostname.endsWith(".services.ai.azure.com")) return "azure_openai";
  } catch { /* default client */ }
  return "openai";
}

function modelFor(provider: string, requested: unknown, response?: any, liteLlm = false): string {
  const selected = typeof response?.model === "string" ? response.model :
    typeof requested === "string" ? requested : "unknown";
  if (liteLlm) return canonicalLiteLlmModel(provider, response?.model, requested);
  if (provider === "xai") return canonicalXaiModel(selected);
  return ["openai", "deepseek", "moonshot", "fireworks_ai", "xai", "groq", "mistral"].includes(provider)
    ? selected
    : prefixedModel(provider, selected);
}

function gatewayDimensions(provider: string, liteLlm: boolean): Array<readonly [string, string]> {
  if (liteLlm) return [["gateway", "litellm"]];
  return provider === "openai" ? [] : [["gateway", provider]];
}

function fireworksTierDimension(provider: string, body: any): Array<readonly [string, string]> {
  if (provider !== "fireworks_ai") return [];
  const value = body?.service_tier ?? body?.extra_body?.service_tier;
  return [["service_tier", value === "priority" ? "priority" : "default"]];
}

function line(metric: string, quantity: unknown, unit: string): ProviderUsageLine[] {
  const value = nonNegativeDecimal(quantity);
  return value?.gt(0) ? [{ metric, quantity: value, unit }] : [];
}

function imageMeasurement(response: any, body: any, provider: string, liteLlm = false): OperationMeasurement {
  const usage = response?.usage ?? {};
  const input = nonNegativeInteger(usage.input_tokens);
  const hasInputDetails = usage.input_tokens_details !== undefined && usage.input_tokens_details !== null;
  const textInput = hasInputDetails ? nonNegativeInteger(usage.input_tokens_details?.text_tokens) : 0;
  const imageInput = hasInputDetails ? nonNegativeInteger(usage.input_tokens_details?.image_tokens) : 0;
  const output = nonNegativeInteger(usage.output_tokens);
  const hasOutputDetails = usage.output_tokens_details !== undefined && usage.output_tokens_details !== null;
  const textOutput = hasOutputDetails ? nonNegativeInteger(usage.output_tokens_details?.text_tokens) : 0;
  const imageOutput = hasOutputDetails ? nonNegativeInteger(usage.output_tokens_details?.image_tokens) : output;
  const count = Array.isArray(response?.data) ? response.data.length :
    response?.type === "image_generation.completed" ? 1 : nonNegativeInteger(body?.n) || 1;
  const usageLines = [
    ...line("input_tokens", hasInputDetails ? textInput : input, "Tokens"),
    ...line("input_image_tokens", imageInput, "Tokens"),
    ...(hasInputDetails && input > textInput + imageInput
      ? line("unallocated_input_tokens", input - textInput - imageInput, "Tokens") : []),
    ...line("output_tokens", textOutput, "Tokens"),
    ...line("output_image_tokens", imageOutput, "Tokens"),
    ...(hasOutputDetails && output > textOutput + imageOutput
      ? line("unallocated_output_tokens", output - textOutput - imageOutput, "Tokens") : []),
    ...line("image_count", count, "Images"),
  ];
  const pricingUsage = Object.fromEntries(
    usageLines.filter((item) => item.metric !== "image_count").map((item) => [item.metric, item.quantity]),
  );
  const model = modelFor(provider, body?.model ?? "gpt-image-1", response, liteLlm);
  if (response?.usage === undefined && count > 0) pricingUsage.image_count = count;
  return {
    usageLines, pricingUsage,
    providerRecordId: response?.id,
    responseModel: model,
    modelCandidates: typeof body?.quality === "string" && typeof body?.size === "string"
      ? [`${body.quality}/${body.size.replace("x", "-x-")}/${model}`, `${body.size.replace("x", "-x-")}/${model}`]
      : [],
    inputTokens: input, outputTokens: output,
    billingDimensions: [
      ...gatewayDimensions(provider, liteLlm),
      ...(typeof body?.quality === "string" ? [["quality", body.quality] as const] : []),
      ...(typeof body?.size === "string" ? [["size", body.size] as const] : []),
    ],
  };
}

function transcriptionMeasurement(response: any, body: any, provider: string, liteLlm = false): OperationMeasurement {
  const usage = response?.usage ?? {};
  if (usage.type === "duration" || usage.seconds !== undefined || response?.duration !== undefined) {
    const seconds = usage.seconds ?? response.duration;
    return {
      usageLines: line("audio_seconds", seconds, "Seconds"),
      pricingUsage: { input_audio_seconds: seconds },
      responseModel: modelFor(provider, body?.model, response, liteLlm),
      billingDimensions: gatewayDimensions(provider, liteLlm),
    };
  }
  const input = nonNegativeInteger(usage.input_tokens);
  const output = nonNegativeInteger(usage.output_tokens);
  const audio = nonNegativeInteger(
    usage.input_token_details?.audio_tokens ?? usage.input_tokens_details?.audio_tokens,
  );
  const text = nonNegativeInteger(
    usage.input_token_details?.text_tokens ?? usage.input_tokens_details?.text_tokens,
  );
  return {
    usageLines: [
      ...line("input_tokens", text || (audio === 0 ? input : 0), "Tokens"),
      ...line("input_audio_tokens", audio, "Tokens"),
      ...line("output_tokens", output, "Tokens"),
    ],
    pricingUsage: Object.fromEntries([
      ["input_tokens", text || (audio === 0 ? input : 0)],
      ["input_audio_tokens", audio], ["output_tokens", output],
    ].filter(([, quantity]) => Number(quantity) > 0)),
    providerRecordId: response?.id, responseModel: modelFor(provider, body?.model, response, liteLlm),
    inputTokens: input, outputTokens: output,
    billingDimensions: gatewayDimensions(provider, liteLlm),
  };
}

function measurement(
  kind: DirectKind,
  response: any,
  body: any,
  provider: string,
  liteLlm = false,
  groqState?: { serviceTier?: unknown; toolExecutionSeen: boolean },
): OperationMeasurement {
  if (kind === "images") return imageMeasurement(response, body, provider, liteLlm);
  if (kind === "transcription") return transcriptionMeasurement(response, body, provider, liteLlm);
  if (kind === "speech") {
    const characters = typeof body?.input === "string" ? [...body.input].length : 0;
    return {
      usageLines: line("characters", characters, "Characters"),
      pricingUsage: characters > 0 ? { characters } : {},
      responseModel: modelFor(provider, body?.model, response, liteLlm),
      billingDimensions: gatewayDimensions(provider, liteLlm),
    };
  }
  if (kind === "moderation") {
    return {
      usageLines: [{ metric: "request_count", quantity: 1, unit: "Requests" }],
      pricingUsage: { request_count: 1 },
      providerRecordId: response?.id, responseModel: modelFor(provider, body?.model, response, liteLlm),
      billingDimensions: gatewayDimensions(provider, liteLlm),
    };
  }
  const result = tokenMeasurement(response, modelFor(provider, body?.model, response, liteLlm), provider);
  if (kind === "embeddings") {
    const count = Array.isArray(response?.data) ? response.data.length : 0;
    result.usageLines = [...(result.usageLines ?? []), ...line("embedding_count", count, "Embeddings")];
  }
  result.responseModel = modelFor(provider, body?.model, response, liteLlm);
  result.billingDimensions = [
    ...gatewayDimensions(provider, liteLlm),
    ...(kind === "tokens" ? fireworksTierDimension(provider, body) : []),
    ...(kind === "tokens" && provider === "xai"
      ? (() => {
          const lane = xaiPricingLane(response, result.inputTokens ?? 0);
          return lane === undefined ? [] : [["xai_pricing_lane", lane] as const];
        })()
      : []),
    ...(kind === "tokens" && provider === "groq"
      ? (() => {
          const responseRecord = response !== null && typeof response === "object" ? response : {};
          const requestTier = body?.service_tier ?? body?.extra_body?.service_tier;
          const capturedTier = groqState?.serviceTier ?? requestTier;
          const pricingResponse = {
            ...responseRecord,
            ...((responseRecord.service_tier === undefined || responseRecord.service_tier === null) &&
                capturedTier !== undefined
              ? { service_tier: capturedTier }
              : {}),
            ...(groqState === undefined
              ? {}
              : { _dexcost_groq_tool_execution_seen: groqState.toolExecutionSeen }),
          };
          const lane = groqPricingLane(pricingResponse);
          return lane === undefined ? [] : [["groq_pricing_lane", lane] as const];
        })()
      : []),
  ];
  return result;
}

function patchDirect(
  owner: any,
  name: string,
  spec: DirectSpec,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = args[0] ?? {};
    const liteLlm = isLiteLlmGateway(this);
    const provider = routedProvider(this, body?.model);
    const operation = liteLlm ? `litellm.${spec.service}.${name}` : spec.operation;
    const session = new ProviderOperationSession(pricing, buffer, {
      taskType: operation, provider, service: liteLlm ? "litellm" : spec.service, operation,
      component: spec.component, model: modelFor(provider, body?.model, undefined, liteLlm), eventType: spec.eventType,
    });
    let result: any;
    try { result = session.invoke(() => original.apply(this, args)); }
    catch (error) { session.fail(error); throw error; }
    const complete = (response: any): any => {
      if (body?.stream === true) {
        let terminal = response;
        const groqState = {
          serviceTier: (body?.service_tier ?? body?.extra_body?.service_tier) as unknown,
          toolExecutionSeen: false,
        };
        return wrapProviderStream(response, session, (chunk) => {
          const item = chunk as any;
          if (item?.usage !== undefined) terminal = chunk;
          if (provider === "groq") {
            if (item?.service_tier !== undefined && item?.service_tier !== null) {
              groqState.serviceTier = item.service_tier;
            }
            if (groqToolExecutionBlocksStaticPricing(item)) groqState.toolExecutionSeen = true;
          }
        }, () => measurement(spec.kind, terminal, body, provider, liteLlm, groqState));
      }
      session.finish(measurement(spec.kind, response, body, provider, liteLlm));
      return response;
    };
    return mapProviderResult(result, complete, (error) => { session.fail(error); throw error; });
  };
  patches.push({ owner, name, original });
}

function providerJobStatus(resource: any, submission = false): ProviderJobStatus {
  const value = String(resource?.status ?? "").toLowerCase();
  if (["completed", "succeeded"].includes(value)) return "succeeded";
  if (["failed", "expired", "error"].includes(value)) return "failed";
  if (["cancelled", "canceled"].includes(value)) return "cancelled";
  if (["in_progress", "running", "finalizing", "cancelling", "paused"].includes(value)) return "running";
  if (["validating", "queued", "pending"].includes(value)) return submission ? "submitted" : "running";
  return submission ? "submitted" : "unknown";
}

function jobMeasurement(
  kind: "responses" | "batches" | "fine_tuning" | "videos",
  resource: any,
  provider = "openai",
  liteLlm = false,
): OperationMeasurement | undefined {
  if (kind === "responses") {
    const result = tokenMeasurement(
      resource,
      modelFor(provider, resource?.model ?? "unknown", resource, liteLlm),
      provider,
    );
    result.billingDimensions = gatewayDimensions(provider, liteLlm);
    return result;
  }
  if (kind === "batches") {
    const usage = resource?.usage ?? {};
    const input = nonNegativeInteger(usage.input_tokens);
    const cached = nonNegativeInteger(usage.input_tokens_details?.cached_tokens);
    const output = nonNegativeInteger(usage.output_tokens);
    const reasoning = nonNegativeInteger(usage.output_tokens_details?.reasoning_tokens);
    const counts = resource?.request_counts ?? {};
    const lines = [
      ...line("batch_input_tokens", Math.max(0, input - cached), "Tokens"),
      ...line("batch_cache_read_input_tokens", cached, "Tokens"),
      ...line("batch_output_tokens", Math.max(0, output - reasoning), "Tokens"),
      ...line("batch_reasoning_output_tokens", reasoning, "Tokens"),
      ...line("batch_request_count", counts.total, "Requests"),
      ...line("batch_successful_request_count", counts.completed, "Requests"),
      ...line("batch_failed_request_count", counts.failed, "Requests"),
    ];
    return lines.length === 0 ? undefined : {
      usageLines: lines, providerRecordId: resource?.id,
      responseModel: modelFor(provider, resource?.model ?? "batch", resource, liteLlm),
      inputTokens: input, outputTokens: output, cachedTokens: cached,
      billingDimensions: gatewayDimensions(provider, liteLlm),
    };
  }
  if (kind === "fine_tuning") {
    const lines = [
      ...line("training_tokens", resource?.trained_tokens, "Tokens"),
      ...line("training_epoch_count", resource?.hyperparameters?.n_epochs, "Epochs"),
    ];
    return lines.length === 0 ? undefined : {
      usageLines: lines, providerRecordId: resource?.id,
      responseModel: modelFor(provider, resource?.model ?? resource?.fine_tuned_model ?? "fine_tuning", resource, liteLlm),
      billingDimensions: gatewayDimensions(provider, liteLlm),
    };
  }
  const count = resource?.id ? 1 : 0;
  const seconds = nonNegativeDecimal(resource?.seconds);
  const lines = [
    ...line("output_video_count", count, "Videos"),
    ...(seconds?.gt(0) ? line("output_video_seconds", seconds.times(count), "Seconds") : []),
  ];
  return lines.length === 0 ? undefined : {
    usageLines: lines, providerRecordId: resource?.id,
    responseModel: modelFor(provider, resource?.model ?? "video", resource, liteLlm),
    billingDimensions: gatewayDimensions(provider, liteLlm),
  };
}

function jobId(resource: any, args: any[]): string | undefined {
  const value = resource?.id ?? (typeof args[0] === "string" ? args[0] : args[0]?.id);
  return typeof value === "string" && value.length > 0 ? value.slice(0, 256) : undefined;
}

function liteLlmJobService(kind: "responses" | "batches" | "fine_tuning" | "videos"): string {
  return `litellm.${kind}`;
}

function liteLlmJobProvider(resource: any, requestedModel: unknown, fallback: string): string {
  return classifyLiteLlmProvider(
    resource?._hidden_params?.custom_llm_provider,
    resource?.provider,
    resource?.model,
    requestedModel,
    fallback,
  );
}

function patchJob(
  owner: any,
  name: string,
  kind: "responses" | "batches" | "fine_tuning" | "videos",
  action: "create" | "retrieve" | "cancel" | "pause" | "resume",
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!owner || typeof owner[name] !== "function") return;
  if (patches.some((item) => item.owner === owner && item.name === name)) return;
  const original = owner[name] as (...args: any[]) => any;
  owner[name] = function (this: any, ...args: any[]): any {
    const body = action === "create" ? args[0] ?? {} : {};
    if (kind === "responses" && action === "create" && body.background !== true) {
      return original.apply(this, args);
    }
    const liteLlm = isLiteLlmGateway(this);
    const provider = routedProvider(this, body?.model);
    if (provider !== "openai" && !liteLlm) return original.apply(this, args);
    const service = liteLlm ? liteLlmJobService(kind) : kind;
    const operation = liteLlm ? `litellm.${kind}.${action}` : `openai.${kind}.${action}`;
    const requestedModel = typeof body.model === "string" ? body.model :
      kind === "batches" ? "batch" : kind === "fine_tuning" ? body.model ?? "fine_tuning" : "unknown";
    const model = modelFor(provider, requestedModel, undefined, liteLlm);
    const component = kind === "responses" || kind === "batches" ? "llm" : "external";
    const eventType = kind === "fine_tuning" || kind === "videos" ? "external_cost" : "llm_call";
    const session = action === "create" ? new ProviderOperationSession(pricing, buffer, {
      taskType: operation, provider, service, operation,
      component, model, eventType,
    }) : undefined;
    let result: any;
    try { result = session ? session.invoke(() => original.apply(this, args)) : original.apply(this, args); }
    catch (error) { session?.fail(error); throw error; }
    const complete = (resource: any): any => {
      const id = jobId(resource, args);
      if (id === undefined) { session?.fail(new Error("OpenAI provider job omitted its id")); return resource; }
      const resolvedProvider = liteLlm ? liteLlmJobProvider(resource, body?.model, provider) : provider;
      const resolvedModel = modelFor(resolvedProvider, requestedModel, resource, liteLlm);
      const meter = jobMeasurement(kind, resource, resolvedProvider, liteLlm);
      if (action === "create" && session) {
        let status = providerJobStatus(resource, true);
        if (status === "succeeded" && meter === undefined) status = "unknown";
        const snapshot = status === "succeeded" ? meter : undefined;
        const now = new Date();
        buffer.insertProviderJobRevision(new ProviderJobRevision({
          taskId: session.task.taskId, provider: resolvedProvider, service, providerRecordId: id,
          operation, component, eventType,
          resourceType: "model", resourceId: resolvedModel, status,
          submittedAt: now, observedAt: now, ownsTask: session.autoCreated,
          billingDimensions: [
            ...gatewayDimensions(resolvedProvider, liteLlm),
            ...(typeof body.endpoint === "string" ? [["batch_endpoint", body.endpoint] as const] : []),
            ...(typeof body.completion_window === "string" ? [["batch_completion_window", body.completion_window] as const] : []),
          ],
          ...providerJobMeasurementFields(pricing, resolvedModel, snapshot),
          capability: session.capability,
        }));
        session.releaseForProviderJob();
      } else {
        const raw = buffer.getProviderJob(resolvedProvider, service, id);
        if (raw === undefined) return resource;
        const previous = providerJobFromDict(raw);
        let status = action === "cancel" ? "cancelled" as const : providerJobStatus(resource);
        if (status === "succeeded" && meter === undefined) status = "unknown";
        buffer.insertProviderJobRevision(new ProviderJobRevision({
          eventId: previous.eventId, revision: previous.revision + 1,
          taskId: previous.taskId, provider: previous.provider, service: previous.service,
          providerRecordId: id, operation: previous.operation, component: previous.component,
          eventType: previous.eventType, resourceType: previous.resourceType, resourceId: previous.resourceId,
          status, submittedAt: previous.submittedAt,
          observedAt: new Date(Math.max(Date.now(), previous.observedAt.getTime())),
          ownsTask: previous.ownsTask,
          billingDimensions: previous.billingDimensions,
          ...providerJobMeasurementFields(pricing, previous.resourceId, meter),
          capability: previous.capability,
        }));
      }
      return resource;
    };
    return mapProviderResult(result, complete, (error) => { session?.fail(error); throw error; });
  };
  patches.push({ owner, name, original });
}

function prototype(value: any): any { return value?.prototype; }

export function installOpenAIModern(root: any, pricing: PricingEngine, buffer: EventBuffer): boolean {
  const before = patches.length;
  const OpenAI = root?.default ?? root;
  const direct: Array<[any, string[], DirectSpec]> = [
    [prototype(OpenAI?.Completions), ["create"], { service: "completions", operation: "openai.completions.create", component: "llm", eventType: "llm_call", kind: "tokens" }],
    [prototype(OpenAI?.Embeddings), ["create"], { service: "embeddings", operation: "openai.embeddings.create", component: "external", eventType: "external_cost", kind: "embeddings" }],
    [prototype(OpenAI?.Images), ["generate", "edit", "createVariation"], { service: "images", operation: "openai.images.create", component: "external", eventType: "external_cost", kind: "images" }],
    [prototype(OpenAI?.Audio?.Transcriptions), ["create"], { service: "speech_to_text", operation: "openai.audio.transcriptions.create", component: "external", eventType: "external_cost", kind: "transcription" }],
    [prototype(OpenAI?.Audio?.Translations), ["create"], { service: "speech_to_text", operation: "openai.audio.translations.create", component: "external", eventType: "external_cost", kind: "transcription" }],
    [prototype(OpenAI?.Audio?.Speech), ["create"], { service: "text_to_speech", operation: "openai.audio.speech.create", component: "external", eventType: "external_cost", kind: "speech" }],
    [prototype(OpenAI?.Moderations), ["create"], { service: "moderations", operation: "openai.moderations.create", component: "external", eventType: "external_cost", kind: "moderation" }],
  ];
  for (const [owner, methods, base] of direct) {
    for (const method of methods) patchDirect(owner, method, {
      ...base, operation: methods.length === 1 ? base.operation : `openai.images.${method}`,
    }, pricing, buffer);
  }
  const responses = prototype(OpenAI?.Responses);
  patchJob(responses, "create", "responses", "create", pricing, buffer);
  patchJob(responses, "retrieve", "responses", "retrieve", pricing, buffer);
  patchJob(responses, "cancel", "responses", "cancel", pricing, buffer);
  const batches = prototype(OpenAI?.Batches);
  patchJob(batches, "create", "batches", "create", pricing, buffer);
  patchJob(batches, "retrieve", "batches", "retrieve", pricing, buffer);
  patchJob(batches, "cancel", "batches", "cancel", pricing, buffer);
  const fineTuning = prototype(OpenAI?.FineTuning?.Jobs);
  patchJob(fineTuning, "create", "fine_tuning", "create", pricing, buffer);
  patchJob(fineTuning, "retrieve", "fine_tuning", "retrieve", pricing, buffer);
  patchJob(fineTuning, "cancel", "fine_tuning", "cancel", pricing, buffer);
  patchJob(fineTuning, "pause", "fine_tuning", "pause", pricing, buffer);
  patchJob(fineTuning, "resume", "fine_tuning", "resume", pricing, buffer);
  const videos = prototype(OpenAI?.Videos);
  patchJob(videos, "create", "videos", "create", pricing, buffer);
  patchJob(videos, "retrieve", "videos", "retrieve", pricing, buffer);
  patchJob(videos, "cancel", "videos", "cancel", pricing, buffer);
  return patches.length > before;
}

export function uninstallOpenAIModern(): void {
  for (const item of patches.splice(0).reverse()) item.owner[item.name] = item.original;
}

const TOOL_METERS: Record<string, readonly [string, string, string]> = {
  web_search_call: ["web_search", "web_search_calls", "Calls"],
  file_search_call: ["file_search", "file_search_calls", "Calls"],
  computer_call: ["computer", "computer_tool_calls", "Calls"],
  code_interpreter_call: ["container", "container_reference_count", "Containers"],
  image_generation_call: ["image_generation", "output_image_count", "Images"],
  mcp_call: ["mcp", "mcp_tool_calls", "Calls"],
};

export function recordOpenAIResponseTools(
  response: any,
  task: Task,
  pricing: PricingEngine,
  buffer: EventBuffer,
): void {
  if (!Array.isArray(response?.output)) return;
  const counts = new Map<string, number>();
  for (const item of response.output) {
    const type = typeof item?.type === "string" ? item.type : "";
    if (TOOL_METERS[type] !== undefined) counts.set(type, (counts.get(type) ?? 0) + 1);
  }
  runWithTask(task, () => {
    for (const [type, count] of counts) {
      const [suffix, metric, unit] = TOOL_METERS[type]!;
      const session = new ProviderOperationSession(pricing, buffer, {
        taskType: `openai.responses.${suffix}`, provider: "openai", service: "responses",
        operation: `openai.responses.${suffix}`, component: "external",
        model: response?.model ?? "unknown", eventType: "external_cost",
      });
      session.finish({
        usageLines: [{ metric, quantity: count, unit }], providerRecordId: response?.id,
        responseModel: response?.model,
      });
    }
  });
}
