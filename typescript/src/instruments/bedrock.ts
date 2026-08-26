/**
 * AWS Bedrock auto-instrumentation for dexcost TypeScript SDK.
 *
 * Monkey-patches `BedrockRuntimeClient.prototype.send` to capture
 * InvokeModel and InvokeModelWithResponseStream commands, automatically
 * recording cost events and aggregating token usage on the active task context.
 *
 * Token usage is parsed from the response body JSON and varies by model
 * family (Anthropic, Amazon Titan, Meta Llama, Cohere, Mistral, AI21).
 */

import { createHash, randomUUID } from "node:crypto";
import { createCostEvent, Decimal } from "../core/models.js";
import type { Task, CostConfidence, PricingSource } from "../core/models.js";
import { getCurrentTask, suppressNetworkEvent } from "../core/context.js";
import { createAutoTask, finalizeAutoTask } from "../core/auto-task.js";
import { registerLlmCapture } from "../core/llm-dedup.js";
import { getAmbientSessionTask } from "../core/session.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine, CostResult } from "../pricing/engine.js";
import { registerInstrument } from "./index.js";
import { stampAmbientAttribution } from "../core/capabilities.js";
import {
  ProviderOperationSession,
  providerJobMeasurementFields,
  recordProviderFailure,
} from "./provider-metering.js";
import type { OperationMeasurement } from "./provider-metering.js";
import { ProviderJobRevision, providerJobFromDict } from "../core/provider-jobs.js";
import { currentProviderCaptureOwner, runWithProviderCapture } from "./provider-capture.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

let _patched = false;
// eslint-disable-next-line @typescript-eslint/no-unsafe-function-type
let _original: Function | null = null;
let _patchedPrototype: any = null;
let _clientClass: any = null;
let _buffer: EventBuffer | null = null;
let _pricing: PricingEngine | null = null;

/** Test helper: inject a mock BedrockRuntimeClient class so tests avoid importing @aws-sdk/client-bedrock-runtime. */
export function _setClientClass(cls: any): void {
  _clientClass = cls;
}

/** Test helper: reset to real module resolution. */
export function _resetClientClass(): void {
  _clientClass = null;
}

/**
 * Patch `BedrockRuntimeClient.prototype.send` to record cost events for
 * InvokeModelCommand calls.
 *
 * If `@aws-sdk/client-bedrock-runtime` is not installed and no mock class is
 * injected, the dynamic import will throw and the function will reject.
 */
export async function instrumentBedrock(
  pricing: PricingEngine,
  buffer: EventBuffer,
): Promise<void> {
  if (_patched) return;

  let ClientProto: any;
  if (_clientClass) {
    ClientProto = _clientClass.prototype;
  } else {
    // @aws-sdk/client-bedrock-runtime is an optional peer dependency; the
    // dynamic import only succeeds at runtime if the user has installed it.
    // @ts-expect-error -- aws-sdk types are not bundled with dexcost
    const bedrockModule = await import("@aws-sdk/client-bedrock-runtime");
    const mod = bedrockModule.default ?? bedrockModule;
    ClientProto = mod.BedrockRuntimeClient.prototype;
  }

  _original = ClientProto.send;
  _patchedPrototype = ClientProto;
  _buffer = buffer;
  _pricing = pricing;

  ClientProto.send = async function (
    this: any,
    command: any,
    ...rest: any[]
  ): Promise<any> {
    const commandName: string = command?.constructor?.name ?? "";
    if (!["InvokeModelCommand", "StartAsyncInvokeCommand", "GetAsyncInvokeCommand"].includes(commandName)) {
      return _original!.call(this, command, ...rest);
    }
    if (currentProviderCaptureOwner() !== undefined) {
      return _original!.call(this, command, ...rest);
    }

    if (commandName === "StartAsyncInvokeCommand") {
      return handleStartAsyncInvoke(this, command, rest);
    }
    if (commandName === "GetAsyncInvokeCommand") {
      return handleGetAsyncInvoke(this, command, rest);
    }

    const requestedModel = canonicalBedrockModel(command?.input?.modelId);
    const mode = _pricing?.modelMode(requestedModel);
    if (["embedding", "image_generation", "image_edit", "rerank"].includes(mode ?? "")) {
      return handleMeteredInvoke(this, command, rest, requestedModel, mode!);
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
      task = getAmbientSessionTask("bedrock.invokeModel");
      if (!task) {
        task = createAutoTask("bedrock.invokeModel");
        _buffer?.upsertTask(task);
        autoCreated = true;
      }
    }

    const startTime = performance.now();
    try {
      const response = await runWithProviderCapture(
        "bedrock",
        () => _original!.call(this, command, ...rest),
      );
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        const modelId: string = command?.input?.modelId ?? "unknown";
        recordEvent(response, modelId, task, latencyMs);
      } catch {
        // dexcost errors must never crash user code
      }
      if (autoCreated) {
        finalizeAutoTask(task, "success", _buffer);
      }
      return response;
    } catch (err) {
      if (_pricing && _buffer) recordProviderFailure(_pricing, _buffer, task, {
        taskType: "bedrock.invoke_model", provider: "bedrock", service: "bedrock_runtime",
        operation: "bedrock.invoke_model", component: "llm",
        model: command?.input?.modelId ?? "unknown", eventType: "llm_call",
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
 * Remove the monkey-patch and restore the original `send` method.
 */
export function uninstrumentBedrock(): void {
  if (!_patched) return;

  if (_patchedPrototype) {
    if (_original) _patchedPrototype.send = _original;
    else delete _patchedPrototype.send;
  }

  _original = null;
  _patchedPrototype = null;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function canonicalBedrockModel(value: unknown): string {
  const candidate = typeof value === "string" ? value.trim() || "unknown" : "unknown";
  if (!candidate.startsWith("arn:")) return candidate;
  const parts = candidate.split(":", 6);
  if (parts.length !== 6) return "bedrock-resource";
  const resource = parts[5] ?? "";
  const slash = resource.indexOf("/");
  const colon = resource.indexOf(":");
  const separator = slash >= 0 ? slash : colon;
  const resourceType = (separator >= 0 ? resource.slice(0, separator) : resource).trim();
  const resourceId = separator >= 0 ? resource.slice(separator + 1) : "";
  if (["foundation-model", "inference-profile"].includes(resourceType)) {
    return resourceId || `bedrock-${resourceType}`;
  }
  const normalized = resourceType.toLowerCase().replace(/_/g, "-");
  return /^[a-z0-9.-]+$/.test(normalized)
    ? `bedrock-${normalized}`.slice(0, 128)
    : "bedrock-resource";
}

function parsedResponseBody(response: any): any {
  try {
    const raw = response?.body;
    if (raw instanceof Uint8Array) return JSON.parse(new TextDecoder().decode(raw));
    if (typeof raw === "string") return JSON.parse(raw);
    if (raw && typeof raw === "object") return raw;
  } catch {
    // A provider response is always returned even when its telemetry body is new/malformed.
  }
  return {};
}

function parsedRequestBody(command: any): any {
  try {
    const raw = command?.input?.body;
    if (raw instanceof Uint8Array) return JSON.parse(new TextDecoder().decode(raw));
    if (typeof raw === "string") return JSON.parse(raw);
    if (raw && typeof raw === "object") return raw;
  } catch {
    // Request content is inspected transiently for non-sensitive dimensions only.
  }
  return {};
}

function nonNegativeInteger(value: unknown): number | undefined {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : undefined;
}

function responseRequestId(response: any): string | undefined {
  const value = response?.$metadata?.requestId ?? response?.$metadata?.request_id;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function meteredInvokeMeasurement(
  mode: string,
  response: any,
  command: any,
  model: string,
): OperationMeasurement {
  const body = parsedResponseBody(response);
  const providerRecordId = responseRequestId(response);
  if (mode === "embedding") {
    const usage = body?.usage ?? {};
    const inputTokens = [
      usage?.inputTokens, usage?.input_tokens, usage?.totalTokens, usage?.total_tokens,
      body?.inputTextTokenCount, body?.inputTokenCount, body?.input_tokens,
    ].map(nonNegativeInteger).find((value) => value !== undefined);
    return {
      usageLines: inputTokens !== undefined && inputTokens > 0
        ? [{ metric: "input_tokens", quantity: inputTokens, unit: "Tokens" }]
        : [],
      pricingUsage: inputTokens === undefined ? {} : { input_tokens: inputTokens },
      providerRecordId,
      responseModel: model,
      inputTokens,
    };
  }
  if (mode === "rerank") {
    return {
      usageLines: [{ metric: "query_count", quantity: 1, unit: "Queries" }],
      pricingUsage: { query_count: 1 },
      providerRecordId,
      responseModel: model,
    };
  }

  const imageValues = Array.isArray(body?.images)
    ? body.images
    : Array.isArray(body?.artifacts) ? body.artifacts : [];
  const count = imageValues.length;
  const request = parsedRequestBody(command);
  const config = request?.imageGenerationConfig && typeof request.imageGenerationConfig === "object"
    ? request.imageGenerationConfig
    : request;
  const width = nonNegativeInteger(config?.width);
  const height = nonNegativeInteger(config?.height);
  const steps = nonNegativeInteger(config?.steps);
  const quality = typeof config?.quality === "string" ? config.quality.toLowerCase().slice(0, 256) : undefined;
  const billingDimensions: Array<readonly [string, string]> = [];
  if (width !== undefined && width > 0) billingDimensions.push(["image_width", String(width)]);
  if (height !== undefined && height > 0) billingDimensions.push(["image_height", String(height)]);
  if (steps !== undefined && steps > 0) billingDimensions.push(["image_steps", String(steps)]);
  if (quality) billingDimensions.push(["image_quality", quality]);
  const above1024 = (width ?? 0) > 1024 || (height ?? 0) > 1024;
  const premium = quality === "premium";
  const pricingMetric = above1024 && premium
    ? "output_image_count_above_1024_premium"
    : above1024 ? "output_image_count_above_1024"
      : premium ? "output_image_count_premium" : "output_image_count";
  const modelCandidates = width && height && steps
    ? [`${width}-x-${height}/${steps}-steps/bedrock/${model}`, `${width}-x-${height}/${steps}-steps/${model}`]
    : [];
  return {
    usageLines: count > 0 ? [{ metric: "output_image_count", quantity: count, unit: "Images" }] : [],
    pricingUsage: count > 0 ? { [pricingMetric]: count } : {},
    providerRecordId,
    responseModel: model,
    modelCandidates,
    billingDimensions,
  };
}

async function handleMeteredInvoke(
  receiver: any,
  command: any,
  rest: any[],
  model: string,
  mode: string,
): Promise<any> {
  const session = new ProviderOperationSession(_pricing!, _buffer!, {
    taskType: "bedrock.invoke_model",
    provider: "aws_bedrock",
    service: "bedrock_runtime",
    operation: "bedrock.invoke_model",
    component: "external",
    model,
    eventType: "external_cost",
  });
  try {
    const response = await session.invoke(() => _original!.call(receiver, command, ...rest));
    session.finish(meteredInvokeMeasurement(mode, response, command, model));
    return response;
  } catch (error) {
    session.fail(error);
    throw error;
  }
}

function asyncInvokeIdentity(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length === 0) return undefined;
  return createHash("sha256").update(value, "utf8").digest("hex");
}

async function handleStartAsyncInvoke(receiver: any, command: any, rest: any[]): Promise<any> {
  const model = canonicalBedrockModel(command?.input?.modelId);
  const session = new ProviderOperationSession(_pricing!, _buffer!, {
    taskType: "bedrock.async_invoke.start",
    provider: "aws_bedrock",
    service: "bedrock_async_invoke",
    operation: "bedrock.async_invoke.start",
    component: "external",
    model,
    eventType: "external_cost",
  });
  try {
    const response = await session.invoke(() => _original!.call(receiver, command, ...rest));
    const recordId = asyncInvokeIdentity(response?.invocationArn);
    if (recordId === undefined) {
      session.fail(new Error("Bedrock async invoke response omitted invocationArn"));
      return response;
    }
    const now = new Date();
    const revision = new ProviderJobRevision({
      taskId: session.task.taskId,
      provider: "aws_bedrock",
      service: "bedrock_async_invoke",
      providerRecordId: recordId,
      operation: "bedrock.async_invoke.start",
      component: "external",
      eventType: "external_cost",
      resourceType: "model",
      resourceId: model,
      status: "submitted",
      submittedAt: now,
      observedAt: now,
      ownsTask: session.autoCreated,
      billingDimensions: [["output_destination", "s3"]],
    });
    session.releaseForProviderJob();
    try { _buffer!.insertProviderJobRevision(revision); } catch { /* telemetry-only */ }
    return response;
  } catch (error) {
    session.fail(error);
    throw error;
  }
}

async function handleGetAsyncInvoke(receiver: any, command: any, rest: any[]): Promise<any> {
  const startedAt = performance.now();
  const response = await suppressNetworkEvent(() => runWithProviderCapture(
    "aws_bedrock",
    () => _original!.call(receiver, command, ...rest),
  ));
  const recordId = asyncInvokeIdentity(command?.input?.invocationArn);
  if (recordId === undefined || !_buffer || !_pricing) return response;
  try {
    const stored = _buffer.getProviderJob("aws_bedrock", "bedrock_async_invoke", recordId);
    if (stored === undefined) return response;
    const previous = providerJobFromDict(stored);
    const rawStatus = response?.status;
    const status = rawStatus === "InProgress" ? "running"
      : rawStatus === "Completed" ? "succeeded"
        : rawStatus === "Failed" ? "failed" : "unknown";
    const measurement: OperationMeasurement | undefined = status === "succeeded"
      ? {
        usageLines: [{ metric: "request_count", quantity: 1, unit: "Requests" }],
        pricingUsage: {},
        responseModel: previous.resourceId,
      }
      : undefined;
    const observedAt = new Date(Math.max(Date.now(), previous.observedAt.getTime()));
    _buffer.insertProviderJobRevision(new ProviderJobRevision({
      eventId: previous.eventId,
      revision: previous.revision + 1,
      taskId: previous.taskId,
      provider: previous.provider,
      service: previous.service,
      providerRecordId: previous.providerRecordId,
      operation: previous.operation,
      component: previous.component,
      eventType: previous.eventType,
      resourceType: previous.resourceType,
      resourceId: previous.resourceId,
      status,
      submittedAt: previous.submittedAt,
      observedAt,
      ownsTask: previous.ownsTask,
      billingDimensions: previous.billingDimensions,
      ...providerJobMeasurementFields(_pricing, previous.resourceId, measurement),
      latencyMs: Math.max(0, Math.round(performance.now() - startedAt)),
      errorType: status === "failed" ? "bedrock_async_invoke_failed" : undefined,
      capability: previous.capability,
    }));
  } catch {
    // Reconciliation is telemetry-only and must never replace a valid AWS response.
  }
  return response;
}

/**
 * Parse token usage from a Bedrock InvokeModel response body.
 *
 * Different model families embed token usage in different JSON structures:
 * - Anthropic Claude: usage.input_tokens / usage.output_tokens
 * - Amazon Titan: inputTextTokenCount / results[0].tokenCount
 * - Meta Llama: prompt_token_count / generation_token_count
 * - Cohere: meta.billed_units.input_tokens / meta.billed_units.output_tokens
 * - Mistral: usage.prompt_tokens / usage.completion_tokens
 * - AI21: usage.prompt_tokens / usage.completion_tokens (Jamba)
 */
function parseUsage(body: any, modelId: string): { inputTokens: number; outputTokens: number } {
  let inputTokens = 0;
  let outputTokens = 0;

  if (!body) return { inputTokens, outputTokens };

  const lowerModel = modelId.toLowerCase();

  if (lowerModel.includes("anthropic") || lowerModel.includes("claude")) {
    // Anthropic Claude on Bedrock
    inputTokens = body?.usage?.input_tokens ?? 0;
    outputTokens = body?.usage?.output_tokens ?? 0;
  } else if (lowerModel.includes("titan")) {
    // Amazon Titan
    inputTokens = body?.inputTextTokenCount ?? 0;
    outputTokens = body?.results?.[0]?.tokenCount ?? 0;
  } else if (lowerModel.includes("llama") || lowerModel.includes("meta")) {
    // Meta Llama
    inputTokens = body?.prompt_token_count ?? 0;
    outputTokens = body?.generation_token_count ?? 0;
  } else if (lowerModel.includes("cohere")) {
    // Cohere on Bedrock
    inputTokens = body?.meta?.billed_units?.input_tokens ?? 0;
    outputTokens = body?.meta?.billed_units?.output_tokens ?? 0;
  } else if (lowerModel.includes("mistral")) {
    // Mistral on Bedrock
    inputTokens = body?.usage?.prompt_tokens ?? 0;
    outputTokens = body?.usage?.completion_tokens ?? 0;
  } else if (lowerModel.includes("ai21") || lowerModel.includes("jamba")) {
    // AI21 Jamba
    inputTokens = body?.usage?.prompt_tokens ?? 0;
    outputTokens = body?.usage?.completion_tokens ?? 0;
  } else {
    // Fallback: try common field names
    inputTokens =
      body?.usage?.input_tokens ??
      body?.usage?.prompt_tokens ??
      body?.inputTextTokenCount ??
      0;
    outputTokens =
      body?.usage?.output_tokens ??
      body?.usage?.completion_tokens ??
      body?.results?.[0]?.tokenCount ??
      0;
  }

  return { inputTokens, outputTokens };
}

function recordEvent(response: any, modelId: string, task: Task, latencyMs: number): void {
  if (!_buffer || !_pricing) return;

  let parsedBody: any = null;
  try {
    const rawBody = response?.body;
    if (rawBody instanceof Uint8Array) {
      parsedBody = JSON.parse(new TextDecoder().decode(rawBody));
    } else if (typeof rawBody === "string") {
      parsedBody = JSON.parse(rawBody);
    } else if (rawBody && typeof rawBody === "object") {
      parsedBody = rawBody;
    }
  } catch {
    // body parse failure — record event with zero tokens
  }

  const { inputTokens, outputTokens } = parseUsage(parsedBody, modelId);
  const hasUsage = inputTokens > 0 || outputTokens > 0;

  let costUsd: Decimal = new Decimal(0);
  let costConfidence: CostConfidence = "estimated";
  let pricingSource: PricingSource = "unknown";

  if (hasUsage) {
    const result: CostResult = _pricing.getCost(modelId, inputTokens, outputTokens);
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
    provider: "aws-bedrock",
    model: modelId,
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

// Self-register so importing this module is enough to make the instrument available.
registerInstrument("bedrock", instrumentBedrock, uninstrumentBedrock, (ref: any) => {
  const mod = ref?.default ?? ref;
  _setClientClass(mod?.BedrockRuntimeClient ?? mod);
});
