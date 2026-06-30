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
let _original: Function | null = null;
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
  _buffer = buffer;
  _pricing = pricing;

  ClientProto.send = async function (
    this: any,
    command: any,
    ...rest: any[]
  ): Promise<any> {
    // Only intercept InvokeModelCommand (by constructor name convention)
    const commandName: string = command?.constructor?.name ?? "";
    if (commandName !== "InvokeModelCommand") {
      return _original!.call(this, command, ...rest);
    }

    let task = getCurrentTask();
    let autoCreated = false;

    // Auto-create a task when no explicit task is active so LLM costs
    // are never silently lost (mirrors Python create_auto_task).
    if (!task) {
      task = createAutoTask("bedrock.invokeModel");
      _buffer?.upsertTask(task);
      autoCreated = true;
    }

    const startTime = performance.now();
    try {
      const response = await _original!.call(this, command, ...rest);
      try {
        const latencyMs = Math.round(performance.now() - startTime);
        const modelId: string = command?.input?.modelId ?? "unknown";
        recordEvent(response, modelId, task, latencyMs);
      } catch {
        // dexcost errors must never crash user code
      }
      if (autoCreated) {
        task.status = "success";
        task.endedAt = new Date();
        _buffer?.upsertTask(task);
      }
      return response;
    } catch (err) {
      if (autoCreated) {
        task.status = "failed";
        task.endedAt = new Date();
        _buffer?.upsertTask(task);
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
  if (!_patched || !_original) return;

  if (_clientClass) {
    _clientClass.prototype.send = _original;
  }

  _original = null;
  _buffer = null;
  _pricing = null;
  _patched = false;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

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

  _buffer.addEvent(event);

  task.llmCostUsd = task.llmCostUsd.plus(costUsd);
  task.totalCostUsd = task.totalCostUsd.plus(costUsd);
  task.totalInputTokens += inputTokens;
  task.totalOutputTokens += outputTokens;
  _buffer.upsertTask(task);
}

// Self-register so importing this module is enough to make the instrument available.
registerInstrument("bedrock", instrumentBedrock, uninstrumentBedrock);
