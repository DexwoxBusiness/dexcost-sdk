import { Decimal } from "../core/models.js";
import type { OperationMeasurement, ProviderUsageLine } from "./provider-metering.js";
import { normalizeOpenAIUsage, OpenAIUsageError } from "./openai-usage.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

export function field(value: any, ...path: string[]): any {
  let current = value;
  for (const key of path) {
    if (current === null || current === undefined) return undefined;
    current = current[key];
  }
  return current;
}

export function nonNegativeDecimal(value: unknown): Decimal | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  try {
    const result = new Decimal(String(value));
    return result.isFinite() && !result.isNegative() ? result : undefined;
  } catch {
    return undefined;
  }
}

export function nonNegativeInteger(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value ?? 0);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
}

export function tokenMeasurement(
  response: any,
  requestedModel?: string,
  provider?: string,
): OperationMeasurement {
  const usage = response?.usage ?? response?.meta?.usage ?? {};
  const inputTotal = nonNegativeInteger(
    usage.input_tokens ?? usage.prompt_tokens ?? usage.inputTokens ?? usage.billedUnits?.inputTokens,
  );
  const outputTotal = nonNegativeInteger(
    usage.output_tokens ?? usage.completion_tokens ?? usage.outputTokens ?? usage.billedUnits?.outputTokens,
  );
  let input = inputTotal;
  let output = outputTotal;
  let cached = nonNegativeInteger(
    usage.input_tokens_details?.cached_tokens ?? usage.prompt_tokens_details?.cached_tokens ??
      usage.cache_read_input_tokens ?? usage.cached_tokens,
  );
  let cacheWrite = nonNegativeInteger(usage.cache_creation_input_tokens ?? usage.cache_write_input_tokens);
  let reasoning = nonNegativeInteger(
    usage.output_tokens_details?.reasoning_tokens ?? usage.completion_tokens_details?.reasoning_tokens ??
      usage.reasoning_tokens,
  );
  try {
    const normalized = normalizeOpenAIUsage(usage);
    input = normalized.inputTokens;
    output = normalized.outputTokens;
    cached = normalized.cacheReadInputTokens;
    cacheWrite = normalized.cacheWriteInputTokens;
    reasoning = normalized.reasoningOutputTokens;
  } catch (error) {
    if (!(error instanceof OpenAIUsageError)) throw error;
    // Non-OpenAI providers sometimes expose only a pair of ordinary totals.
    // If the detailed buckets are inconsistent, retain those totals as
    // unclassified usage instead of guessing a discounted split.
    if (cached + cacheWrite > inputTotal) cached = cacheWrite = 0;
    else input = inputTotal - cached - cacheWrite;
    if (reasoning > outputTotal) reasoning = 0;
    else output = outputTotal - reasoning;
  }
  const lines: ProviderUsageLine[] = [];
  if (input > 0) lines.push({ metric: "input_tokens", quantity: input, unit: "Tokens" });
  if (output > 0) lines.push({ metric: "output_tokens", quantity: output, unit: "Tokens" });
  if (cached > 0) lines.push({ metric: "cache_read_input_tokens", quantity: cached, unit: "Tokens" });
  if (cacheWrite > 0) lines.push({ metric: "cache_write_input_tokens", quantity: cacheWrite, unit: "Tokens" });
  if (reasoning > 0) lines.push({ metric: "reasoning_output_tokens", quantity: reasoning, unit: "Tokens" });
  let providerCost: Decimal | undefined;
  let upstreamCost: Decimal | undefined;
  if (provider === "openrouter") {
    providerCost = nonNegativeDecimal(usage.cost ?? response?.cost);
    upstreamCost = nonNegativeDecimal(
      usage.cost_details?.upstream_inference_cost ?? usage.cost_details?.upstream_cost,
    );
  } else if (provider === "perplexity") {
    providerCost = nonNegativeDecimal(usage.cost?.total_cost);
  }
  return {
    usageLines: lines,
    pricingUsage: Object.fromEntries(lines.map((item) => [item.metric, item.quantity])),
    providerRecordId: typeof response?.id === "string" ? response.id : undefined,
    providerCostUsd: providerCost,
    providerUpstreamCostUsd: upstreamCost,
    responseModel: typeof response?.model === "string" ? response.model : requestedModel,
    inputTokens: inputTotal,
    outputTokens: outputTotal,
    cachedTokens: cached,
    cacheWriteTokens: cacheWrite,
    reasoningTokens: reasoning,
    billingDimensions: provider === undefined ? [] : [["gateway", provider]],
  };
}

export function prefixedModel(provider: string, model: unknown): string {
  const raw = typeof model === "string" && model.length > 0 ? model : "unknown";
  const prefix: Record<string, string> = {
    azure_openai: "azure", azure_ai: "azure_ai", google: "google", vertex_ai: "google",
    bedrock: "bedrock", cohere: "cohere", huggingface: "huggingface", together_ai: "together",
    ollama: "ollama", mistral: "mistral", groq: "groq", openrouter: "openrouter",
    perplexity: "perplexity", fal: "fal", anthropic: "anthropic", openai: "openai",
  };
  const canonical = prefix[provider] ?? provider;
  return raw.startsWith(`${canonical}/`) ? raw : `${canonical}/${raw}`;
}
