import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Decimal } from "../core/models.js";
import type { PricingSource } from "../core/models.js";

// Sprint 3 Theme E / §4.2.3 — Node 18 compat: runtime JSON load.
const _thisDir = dirname(fileURLToPath(import.meta.url));
const costMapData = JSON.parse(
  readFileSync(join(_thisDir, "cost_map.json"), "utf-8"),
);

export interface CostResult {
  /** Exact decimal cost. Trailing-zero-stripped, never float64. */
  costUsd: Decimal;
  pricingSource: PricingSource;
  costConfidence: "computed" | "unknown";
  pricingVersion: string;
}

export interface MeteredCostLine {
  dimension: string;
  quantity: Decimal;
  rateField: string;
  rateUsd: Decimal;
  costUsd: Decimal;
}

export interface MeteredCostResult extends CostResult {
  resolvedModel?: string;
  lines: MeteredCostLine[];
  unpricedDimensions: string[];
}

interface ModelPricing {
  input_cost_per_token?: number;
  output_cost_per_token?: number;
  cache_read_input_token_cost?: number;
  cache_creation_input_token_cost?: number;
  cache_creation_input_token_cost_above_1hr?: number;
  litellm_provider?: string;
  mode?: string;
  [field: string]: unknown;
}

interface CustomPricing {
  inputPer1k: number;
  outputPer1k: number;
}

/**
 * Coerce a numeric rate/literal to Decimal via String() so a float64 rate
 * from the JSON cost map never poisons the product. Mirrors Python's
 * `Decimal(str(rate))`.
 */
function dec(v: string | number | Decimal): Decimal {
  return new Decimal(String(v));
}

const METERED_RATE_FIELDS: Record<string, ReadonlyArray<readonly [string, Decimal]>> = {
  input_tokens: [["input_cost_per_token", new Decimal(1)]],
  output_tokens: [["output_cost_per_token", new Decimal(1)]],
  cache_read_input_tokens: [["cache_read_input_token_cost", new Decimal(1)]],
  cache_write_input_tokens: [["cache_creation_input_token_cost", new Decimal(1)]],
  reasoning_output_tokens: [
    ["output_cost_per_reasoning_token", new Decimal(1)],
    ["output_cost_per_token", new Decimal(1)],
  ],
  input_image_tokens: [
    ["input_cost_per_image_token", new Decimal(1)],
    ["input_cost_per_token", new Decimal(1)],
  ],
  cache_read_input_image_tokens: [
    ["cache_read_input_image_token_cost", new Decimal(1)],
    ["cache_read_input_token_cost", new Decimal(1)],
  ],
  output_image_tokens: [["output_cost_per_image_token", new Decimal(1)]],
  input_audio_tokens: [["input_cost_per_audio_token", new Decimal(1)]],
  cache_read_input_audio_tokens: [["cache_read_input_audio_token_cost", new Decimal(1)]],
  cache_write_input_audio_tokens: [["cache_creation_input_audio_token_cost", new Decimal(1)]],
  output_audio_tokens: [["output_cost_per_audio_token", new Decimal(1)]],
  input_video_tokens: [
    ["input_cost_per_video_token", new Decimal(1)],
    ["input_cost_per_token", new Decimal(1)],
  ],
  cache_read_input_video_tokens: [
    ["cache_read_input_video_token_cost", new Decimal(1)],
    ["cache_read_input_token_cost", new Decimal(1)],
  ],
  output_video_tokens: [
    ["output_cost_per_video_token", new Decimal(1)],
    ["output_cost_per_token", new Decimal(1)],
  ],
  tool_input_tokens: [["input_cost_per_token", new Decimal(1)]],
  tool_input_image_tokens: [
    ["input_cost_per_image_token", new Decimal(1)],
    ["input_cost_per_token", new Decimal(1)],
  ],
  tool_input_audio_tokens: [["input_cost_per_audio_token", new Decimal(1)]],
  tool_input_video_tokens: [
    ["input_cost_per_video_token", new Decimal(1)],
    ["input_cost_per_token", new Decimal(1)],
  ],
  characters: [["input_cost_per_character", new Decimal(1)]],
  output_characters: [["output_cost_per_character", new Decimal(1)]],
  input_audio_seconds: [
    ["input_cost_per_audio_per_second", new Decimal(1)],
    ["input_cost_per_second", new Decimal(1)],
  ],
  output_audio_seconds: [["output_cost_per_second", new Decimal(1)]],
  input_video_seconds: [["input_cost_per_video_per_second", new Decimal(1)]],
  output_video_seconds: [
    ["output_cost_per_video_per_second", new Decimal(1)],
    ["output_cost_per_second", new Decimal(1)],
  ],
  image_count: [["input_cost_per_image", new Decimal(1)]],
  output_image_count: [["output_cost_per_image", new Decimal(1)]],
  output_image_count_premium: [["output_cost_per_image_premium_image", new Decimal(1)]],
  output_image_count_above_512: [
    ["output_cost_per_image_above_512_and_512_pixels", new Decimal(1)],
    ["output_cost_per_image", new Decimal(1)],
  ],
  output_image_count_above_512_premium: [
    ["output_cost_per_image_above_512_and_512_pixels_and_premium_image", new Decimal(1)],
    ["output_cost_per_image_premium_image", new Decimal(1)],
  ],
  output_image_count_above_1024: [
    ["output_cost_per_image_above_1024_and_1024_pixels", new Decimal(1)],
    ["output_cost_per_image_above_512_and_512_pixels", new Decimal(1)],
    ["output_cost_per_image", new Decimal(1)],
  ],
  output_image_count_above_1024_premium: [
    ["output_cost_per_image_above_1024_and_1024_pixels_and_premium_image", new Decimal(1)],
    ["output_cost_per_image_above_512_and_512_pixels_and_premium_image", new Decimal(1)],
    ["output_cost_per_image_premium_image", new Decimal(1)],
  ],
  input_pixels: [["input_cost_per_pixel", new Decimal(1)]],
  output_pixels: [["output_cost_per_pixel", new Decimal(1)]],
  request_count: [["input_cost_per_request", new Decimal(1)]],
  query_count: [["input_cost_per_query", new Decimal(1)]],
  web_search_calls: [
    ["web_search_cost_per_call", new Decimal(1)],
    ["input_cost_per_query", new Decimal(1)],
  ],
  session_count: [["code_interpreter_cost_per_session", new Decimal(1)]],
  file_search_calls: [["file_search_cost_per_1k_calls", new Decimal(1000)]],
  batch_input_tokens: [["input_cost_per_token_batches", new Decimal(1)]],
  batch_output_tokens: [["output_cost_per_token_batches", new Decimal(1)]],
  batch_reasoning_output_tokens: [["output_cost_per_token_batches", new Decimal(1)]],
  anthropic_batch_input_tokens: [["input_cost_per_token", new Decimal(2)]],
  anthropic_batch_output_tokens: [["output_cost_per_token", new Decimal(2)]],
  anthropic_batch_cache_read_input_tokens: [["cache_read_input_token_cost", new Decimal(2)]],
  anthropic_batch_cache_write_input_tokens: [["cache_creation_input_token_cost", new Decimal(2)]],
  anthropic_batch_cache_write_input_tokens_1h: [["cache_creation_input_token_cost_above_1hr", new Decimal(2)]],
};

function meteredQuantity(dimension: string, value: unknown): Decimal {
  if (typeof value === "boolean" || (typeof value === "number" && !Number.isSafeInteger(value))) {
    throw new TypeError(`metered usage ${JSON.stringify(dimension)} must be an integer, bigint, Decimal, or decimal string`);
  }
  let quantity: Decimal;
  try {
    quantity = value instanceof Decimal ? value : new Decimal(String(value));
  } catch {
    throw new TypeError(`metered usage ${JSON.stringify(dimension)} is not a plain decimal`);
  }
  if (!quantity.isFinite() || quantity.lt(0)) {
    throw new RangeError(`metered usage ${JSON.stringify(dimension)} must be finite and non-negative`);
  }
  return quantity;
}

/** Anthropic reports input, cache-read, and cache-write as disjoint buckets. */
function usesDisjointCacheBuckets(model: string, provider: string = ""): boolean {
  provider = provider.toLowerCase();
  const normalizedModel = model.toLowerCase();
  return provider === "anthropic"
    || provider === "vertex_ai-anthropic_models"
    || normalizedModel.includes("claude")
    || normalizedModel.includes("anthropic.");
}

export class PricingEngine {
  private _modelMap: Record<string, ModelPricing>;
  private _customPricing: Map<string, CustomPricing> = new Map();
  private _pricingVersion: string;
  private _refreshInterval: ReturnType<typeof setInterval> | null = null;

  constructor() {
    try {
      this._modelMap = costMapData as Record<string, ModelPricing>;
      this._pricingVersion = createHash("sha256")
        .update(JSON.stringify(costMapData))
        .digest("hex")
        .slice(0, 12);
    } catch {
      this._modelMap = {};
      this._pricingVersion = "unknown";
    }
  }

  get pricingVersion(): string {
    return this._pricingVersion;
  }

  /** Return the catalog-declared operation mode for provider routing. */
  modelMode(model: string, modelCandidates: readonly string[] = []): string | undefined {
    for (const candidate of [...modelCandidates, model]) {
      const resolved = this._resolveModelEntry(candidate);
      const mode = resolved?.[1].mode;
      if (typeof mode === "string" && mode.length > 0) return mode;
    }
    return undefined;
  }

  /**
   * Atomically replace the model catalog after a release has been fully
   * validated. The provider hot path only reads this in-memory snapshot.
   */
  replaceCatalog(catalog: Record<string, unknown>, pricingVersion?: string): void {
    const next: Record<string, ModelPricing> = {};
    for (const [model, value] of Object.entries(catalog)) {
      if (model === "_meta" || model === "sample_spec") continue;
      if (value === null || typeof value !== "object" || Array.isArray(value)) {
        throw new TypeError(`pricing entry ${model} must be an object`);
      }
      next[model] = value as ModelPricing;
    }
    if (Object.keys(next).length === 0) {
      throw new TypeError("pricing catalog contains no token-priced models");
    }
    const version = pricingVersion?.trim() || createHash("sha256")
      .update(JSON.stringify(catalog))
      .digest("hex")
      .slice(0, 12);
    this._modelMap = next;
    this._pricingVersion = version;
  }

  getCost(
    model: string,
    inputTokens: number,
    outputTokens: number,
    cachedTokens: number = 0,
    cacheCreationTokens: number = 0,
    cacheCreationTokens1h: number = 0,
  ): CostResult {
    const custom = this._customPricing.get(model);
    if (custom) {
      // Decimal math throughout — rates routed through String() so a float64
      // rate literal never poisons the product (matches Python Decimal(str)).
      const safeInput = Math.max(0, inputTokens);
      const safeOutput = Math.max(0, outputTokens);
      const safeCached = Math.max(0, cachedTokens);
      const safeCreation = Math.max(0, cacheCreationTokens);
      const safeCreation1h = Math.max(0, cacheCreationTokens1h);
      const hasUnpricedDisjointCache = usesDisjointCacheBuckets(model)
        && (safeCached > 0 || safeCreation > 0 || safeCreation1h > 0);
      const billableInput = safeInput
        + (hasUnpricedDisjointCache ? safeCached + safeCreation + safeCreation1h : 0);
      const cost = dec(custom.inputPer1k)
        .times(billableInput)
        .dividedBy(1000)
        .plus(dec(custom.outputPer1k).times(safeOutput).dividedBy(1000));
      return {
        costUsd: cost,
        costConfidence: hasUnpricedDisjointCache ? "unknown" : "computed",
        pricingSource: "custom",
        pricingVersion: this._pricingVersion,
      };
    }

    const info = this._resolveModel(model);
    if (!info) {
      return {
        costUsd: new Decimal(0),
        costConfidence: "unknown",
        pricingSource: "unknown",
        pricingVersion: this._pricingVersion,
      };
    }

    const inputRate = dec(info.input_cost_per_token ?? 0);
    const outputRate = dec(info.output_cost_per_token ?? 0);
    const hasCacheReadRate = info.cache_read_input_token_cost !== undefined;
    const hasCacheCreationRate = info.cache_creation_input_token_cost !== undefined;
    const hasCacheCreation1hRate = info.cache_creation_input_token_cost_above_1hr !== undefined;
    const cacheReadRate = dec(info.cache_read_input_token_cost ?? info.input_cost_per_token ?? 0);
    const cacheCreationRate = dec(
      info.cache_creation_input_token_cost ?? info.input_cost_per_token ?? 0,
    );
    const cacheCreation1hRate = dec(
      info.cache_creation_input_token_cost_above_1hr ?? info.input_cost_per_token ?? 0,
    );
    const safeInput = Math.max(0, inputTokens);
    const safeOutput = Math.max(0, outputTokens);
    const safeCached = Math.max(0, cachedTokens);
    const safeCreation = Math.max(0, cacheCreationTokens);
    const safeCreation1h = Math.max(0, cacheCreationTokens1h);

    let cost: Decimal;
    let costConfidence: "computed" | "unknown" = "computed";
    if (usesDisjointCacheBuckets(model, info.litellm_provider)) {
      // Anthropic usage exposes four independent input buckets. Subtracting
      // cache tokens from input_tokens drops most of the billable usage.
      cost = inputRate
        .times(safeInput)
        .plus(cacheReadRate.times(safeCached))
        .plus(cacheCreationRate.times(safeCreation))
        .plus(cacheCreation1hRate.times(safeCreation1h))
        .plus(outputRate.times(safeOutput));
      if (
        (safeCached > 0 && !hasCacheReadRate)
        || (safeCreation > 0 && !hasCacheCreationRate)
        || (safeCreation1h > 0 && !hasCacheCreation1hRate)
      ) {
        costConfidence = "unknown";
      }
    } else {
      // OpenAI-style usage includes cached tokens inside input_tokens. Only
      // subtract a cache bucket when the catalog supplies its dedicated rate.
      const effectiveCached = hasCacheReadRate
        ? Math.min(safeCached, safeInput)
        : 0;
      const remaining = safeInput - effectiveCached;
      const effectiveCreation = hasCacheCreationRate
        ? Math.min(safeCreation, remaining)
        : 0;
      const remainingAfterCreation = remaining - effectiveCreation;
      const effectiveCreation1h = hasCacheCreation1hRate
        ? Math.min(safeCreation1h, remainingAfterCreation)
        : 0;
      const nonCachedInput = remainingAfterCreation - effectiveCreation1h;
      cost = inputRate
        .times(nonCachedInput)
        .plus(cacheReadRate.times(effectiveCached))
        .plus(cacheCreationRate.times(effectiveCreation))
        .plus(cacheCreation1hRate.times(effectiveCreation1h))
        .plus(outputRate.times(safeOutput));
    }

    return {
      costUsd: cost,
      costConfidence,
      pricingSource: "litellm",
      pricingVersion: this._pricingVersion,
    };
  }

  /** Price exact provider meters without translating modalities into text tokens. */
  getMeteredCost(
    model: string,
    usage: Readonly<Record<string, string | number | bigint | Decimal>>,
    modelCandidates: readonly string[] = [],
  ): MeteredCostResult {
    if (typeof model !== "string" || model.length === 0) {
      throw new TypeError("model must be a non-empty string");
    }
    if (usage === null || typeof usage !== "object" || Array.isArray(usage)) {
      throw new TypeError("usage must be an object");
    }
    const quantities = new Map<string, Decimal>();
    for (const [dimension, raw] of Object.entries(usage)) {
      if (dimension.length === 0) throw new TypeError("metered usage dimensions must be non-empty strings");
      quantities.set(dimension, meteredQuantity(dimension, raw));
    }

    const ordered: string[] = [];
    for (const candidate of [...modelCandidates, model]) {
      if (typeof candidate !== "string" || candidate.length === 0) {
        throw new TypeError("model candidates must be non-empty strings");
      }
      if (!ordered.includes(candidate)) ordered.push(candidate);
    }
    let resolvedModel: string | undefined;
    let modelInfo: ModelPricing | undefined;
    for (const candidate of ordered) {
      const resolved = this._resolveModelEntry(candidate);
      if (resolved !== undefined) {
        [resolvedModel, modelInfo] = resolved;
        break;
      }
    }

    const positive = [...quantities.entries()].filter(([, quantity]) => quantity.gt(0));
    if (modelInfo === undefined || positive.length === 0) {
      return {
        costUsd: new Decimal(0),
        costConfidence: "unknown",
        pricingSource: modelInfo === undefined ? "unknown" : "litellm",
        pricingVersion: this._pricingVersion,
        resolvedModel,
        lines: [],
        unpricedDimensions: positive.map(([dimension]) => dimension).sort(),
      };
    }

    const lines: MeteredCostLine[] = [];
    const unpricedDimensions: string[] = [];
    for (const [dimension, quantity] of positive.sort(([left], [right]) => left.localeCompare(right))) {
      const alternatives = METERED_RATE_FIELDS[dimension] ?? [];
      const selected = alternatives.find(([field]) => Object.prototype.hasOwnProperty.call(modelInfo, field));
      if (selected === undefined) {
        unpricedDimensions.push(dimension);
        continue;
      }
      const [rateField, divisor] = selected;
      let rateUsd: Decimal;
      try {
        rateUsd = dec(modelInfo[rateField] as string | number | Decimal);
      } catch {
        unpricedDimensions.push(dimension);
        continue;
      }
      if (!rateUsd.isFinite() || rateUsd.lt(0)) {
        unpricedDimensions.push(dimension);
        continue;
      }
      lines.push({
        dimension,
        quantity,
        rateField,
        rateUsd,
        costUsd: quantity.times(rateUsd).dividedBy(divisor),
      });
    }
    return {
      costUsd: lines.reduce((total, item) => total.plus(item.costUsd), new Decimal(0)),
      costConfidence: unpricedDimensions.length === 0 ? "computed" : "unknown",
      pricingSource: "litellm",
      pricingVersion: this._pricingVersion,
      resolvedModel,
      lines,
      unpricedDimensions: unpricedDimensions.sort(),
    };
  }

  setCustomPricing(model: string, inputPer1k: number, outputPer1k: number): void {
    this._customPricing.set(model, { inputPer1k, outputPer1k });
  }

  private _apiKey: string | undefined;

  setApiKey(key: string | undefined): void {
    this._apiKey = key;
  }

  async refreshFromServer(endpoint: string): Promise<void> {
    try {
      const headers: Record<string, string> = { "User-Agent": "dexcost-sdk" };
      if (this._apiKey) {
        headers["Authorization"] = `Bearer ${this._apiKey}`;
      }
      const response = await fetch(`${endpoint}/v1/api/pricing-data/latest`, {
        headers,
      });
      if (!response.ok) return;
      const text = await response.text();
      let payload: { data?: { data?: Record<string, ModelPricing>; pricing_version?: string } };
      try {
        payload = JSON.parse(text) as typeof payload;
      } catch {
        return; // Malformed JSON — keep using bundled pricing
      }
      // Control Layer contract: pricing models are nested under
      // payload.data.data, with payload.data.pricing_version alongside.
      const rawData = payload.data;
      if (!rawData || typeof rawData !== "object") return;
      const serverData = rawData.data;
      if (
        !serverData
        || typeof serverData !== "object"
        || Array.isArray(serverData)
        || Object.keys(serverData).length === 0
      ) {
        return;
      }
      this.replaceCatalog(serverData, rawData.pricing_version);
    } catch {
      // Network error — keep using bundled pricing
    }
  }

  startBackgroundRefresh(endpoint: string, intervalMs: number = 86_400_000): void {
    void this.refreshFromServer(endpoint);
    const interval = setInterval(
      () => void this.refreshFromServer(endpoint),
      intervalMs
    );
    interval.unref();
    this._refreshInterval = interval;
  }

  stopBackgroundRefresh(): void {
    if (this._refreshInterval !== null) {
      clearInterval(this._refreshInterval);
      this._refreshInterval = null;
    }
  }

  private _resolveModel(model: string): ModelPricing | undefined {
    return this._resolveModelEntry(model)?.[1];
  }

  private _resolveModelEntry(model: string): readonly [string, ModelPricing] | undefined {
    if (model in this._modelMap) return [model, this._modelMap[model]!];

    if (model.includes("/")) {
      const short = model.split("/").pop()!;
      if (short in this._modelMap) return [short, this._modelMap[short]!];
    }

    const parts = model.split("-");
    for (let i = parts.length - 1; i > 0; i--) {
      const candidate = parts.slice(0, i).join("-");
      if (candidate in this._modelMap) return [candidate, this._modelMap[candidate]!];
    }

    return undefined;
  }
}
