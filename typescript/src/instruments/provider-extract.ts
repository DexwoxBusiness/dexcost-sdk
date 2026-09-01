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

const XAI_MODEL_ALIASES: Readonly<Record<string, string>> = {
  "grok-4.3-latest": "grok-4.3",
  "grok-code-fast-1": "grok-build-0.1",
  "grok-code-fast": "grok-build-0.1",
  "grok-code-fast-1-0825": "grok-build-0.1",
  "grok-4.5-latest": "grok-4.5",
  "grok-build-latest": "grok-4.5",
  "grok-4.20-reasoning-latest": "grok-4.20-0309-reasoning",
  "grok-4.20": "grok-4.20-0309-reasoning",
  "grok-4.20-reasoning": "grok-4.20-0309-reasoning",
  "grok-4.20-0309": "grok-4.20-0309-reasoning",
  "grok-4.20-beta-0309-reasoning": "grok-4.20-0309-reasoning",
  "grok-4.20-beta": "grok-4.20-0309-reasoning",
  "grok-4.20-beta-0309": "grok-4.20-0309-reasoning",
  "grok-4.20-beta-latest": "grok-4.20-0309-reasoning",
  "grok-4.20-beta-latest-reasoning": "grok-4.20-0309-reasoning",
  "grok-4.20-beta-reasoning": "grok-4.20-0309-reasoning",
  "grok-4.20-experimental-beta-0304-reasoning": "grok-4.20-0309-reasoning",
  "grok-4.20-experimental-beta-0304": "grok-4.20-0309-reasoning",
  "grok-4.20-experimental-beta-reasoning-latest": "grok-4.20-0309-reasoning",
  "grok-4.20-experimental-beta-latest": "grok-4.20-0309-reasoning",
  "grok-4.20-reasoning-gv2": "grok-4.20-0309-reasoning",
  "grok-4.20-non-reasoning": "grok-4.20-0309-non-reasoning",
  "grok-4.20-non-reasoning-latest": "grok-4.20-0309-non-reasoning",
  "grok-4.20-beta-non-reasoning": "grok-4.20-0309-non-reasoning",
  "grok-4.20-beta-latest-non-reasoning": "grok-4.20-0309-non-reasoning",
  "grok-4.20-experimental-beta-0304-non-reasoning": "grok-4.20-0309-non-reasoning",
  "grok-4.20-experimental-beta-non-reasoning-latest": "grok-4.20-0309-non-reasoning",
  "grok-4.20-beta-0309-non-reasoning": "grok-4.20-0309-non-reasoning",
  "grok-4.20-non-reasoning-gv2": "grok-4.20-0309-non-reasoning",
  "grok-4.20-multi-agent": "grok-4.20-multi-agent-0309",
  "grok-4.20-multi-agent-latest": "grok-4.20-multi-agent-0309",
  "grok-4.20-multi-agent-beta-latest": "grok-4.20-multi-agent-0309",
  "grok-4.20-multi-agent-experimental-beta-0304": "grok-4.20-multi-agent-0309",
  "grok-4.20-multi-agent-experimental-beta-latest": "grok-4.20-multi-agent-0309",
  "grok-4.20-multi-agent-beta-0309": "grok-4.20-multi-agent-0309",
  "grok-4-1-fast-reasoning": "grok-4.3",
  "grok-4-1-fast-non-reasoning": "grok-4.3",
  "grok-4-fast-reasoning": "grok-4.3",
  "grok-4-fast-non-reasoning": "grok-4.3",
  "grok-4-0709": "grok-4.3",
  "grok-3": "grok-4.3",
};

export function canonicalXaiModel(model: string): string {
  return XAI_MODEL_ALIASES[model] ?? model;
}

export function xaiPricingLane(
  response: any,
  totalInputTokens: number,
): "default_short" | "default_long" | "priority_short" | "priority_long" | undefined {
  const usage = response?.usage ?? response?.meta?.usage;
  if (usage === undefined || usage === null) return undefined;
  const rawToolCount = usage.num_server_side_tools_used;
  if (rawToolCount !== undefined && rawToolCount !== null) {
    if (typeof rawToolCount !== "number" || !Number.isSafeInteger(rawToolCount) ||
      rawToolCount < 0 || rawToolCount > 0) return undefined;
  }
  const rawTier = response?.service_tier;
  const tier = rawTier === undefined || rawTier === null || rawTier === "default"
    ? "default"
    : rawTier === "priority" ? "priority" : undefined;
  if (tier === undefined) return undefined;
  return `${tier}_${totalInputTokens >= 200_000 ? "long" : "short"}`;
}

export function groqToolExecutionBlocksStaticPricing(response: any): boolean {
  if (response?._dexcost_groq_tool_execution_seen === true) return true;
  const candidates: unknown[] = [response?.executed_tools];
  if (Array.isArray(response?.choices)) {
    for (const choice of response.choices) {
      candidates.push(choice?.message?.executed_tools, choice?.delta?.executed_tools);
    }
  }
  for (const executed of candidates) {
    if (executed === undefined || executed === null) continue;
    if (!Array.isArray(executed) || executed.length > 0) return true;
  }
  return false;
}

export function groqPricingLane(response: any): "public_sync" | undefined {
  if (response === undefined || response === null || groqToolExecutionBlocksStaticPricing(response)) {
    return undefined;
  }
  const tier = response?.service_tier;
  return tier === undefined || tier === null || tier === "default" ||
    tier === "on_demand" || tier === "flex"
    ? "public_sync"
    : undefined;
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
      usage.cache_read_input_tokens ?? usage.cached_tokens ?? usage.prompt_cache_hit_tokens,
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
  const serverTools = usage.server_tool_use ?? usage.server_tool_use_details ?? {};
  for (const [metric, key, unit] of [
    ["server_tool_calls_requested", "tool_calls_requested", "Calls"],
    ["server_tool_calls_executed", "tool_calls_executed", "Calls"],
    ["web_search_requests", "web_search_requests", "Requests"],
  ] as const) {
    const quantity = nonNegativeInteger(serverTools[key]);
    if (quantity > 0) lines.push({ metric, quantity, unit });
  }
  let providerCost: Decimal | undefined;
  let upstreamCost: Decimal | undefined;
  if (provider === "openrouter") {
    providerCost = nonNegativeDecimal(
      usage.cost ?? response?.cost ??
        field(response, "_hidden_params", "additional_headers", "llm_provider-x-litellm-response-cost"),
    );
    upstreamCost = nonNegativeDecimal(
      usage.cost_details?.upstream_inference_cost ?? usage.cost_details?.upstream_cost,
    );
  } else if (provider === "perplexity") {
    providerCost = nonNegativeDecimal(usage.cost?.total_cost);
  } else if (provider === "xai") {
    const ticks = nonNegativeDecimal(usage.cost_in_usd_ticks);
    if (ticks?.isInteger()) providerCost = ticks.div("10000000000");
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
