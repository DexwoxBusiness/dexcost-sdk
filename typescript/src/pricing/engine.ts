import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import type { PricingSource } from "../core/models.js";

// Sprint 3 Theme E / §4.2.3 — Node 18 compat: runtime JSON load.
const _thisDir = dirname(fileURLToPath(import.meta.url));
const costMapData = JSON.parse(
  readFileSync(join(_thisDir, "cost_map.json"), "utf-8"),
);

export interface CostResult {
  costUsd: number;
  pricingSource: PricingSource;
  costConfidence: "computed" | "unknown";
  pricingVersion: string;
}

interface ModelPricing {
  input_cost_per_token: number;
  output_cost_per_token: number;
  cache_read_input_token_cost?: number;
  cache_creation_input_token_cost?: number;
}

interface CustomPricing {
  inputPer1k: number;
  outputPer1k: number;
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

  getCost(
    model: string,
    inputTokens: number,
    outputTokens: number,
    cachedTokens: number = 0,
    cacheCreationTokens: number = 0
  ): CostResult {
    const custom = this._customPricing.get(model);
    if (custom) {
      const cost =
        (custom.inputPer1k * inputTokens) / 1000 +
        (custom.outputPer1k * outputTokens) / 1000;
      return {
        costUsd: cost,
        costConfidence: "computed",
        pricingSource: "custom",
        pricingVersion: this._pricingVersion,
      };
    }

    const info = this._resolveModel(model);
    if (!info) {
      return {
        costUsd: 0,
        costConfidence: "unknown",
        pricingSource: "unknown",
        pricingVersion: this._pricingVersion,
      };
    }

    const inputRate = info.input_cost_per_token;
    const outputRate = info.output_cost_per_token;
    const cacheReadRate = info.cache_read_input_token_cost ?? 0;
    const cacheCreationRate = info.cache_creation_input_token_cost ?? 0;

    const effectiveCached = Math.min(cachedTokens, inputTokens);
    const remaining = inputTokens - effectiveCached;
    const effectiveCreation = Math.min(cacheCreationTokens, remaining);
    const nonCachedInput = remaining - effectiveCreation;

    const cost =
      inputRate * nonCachedInput +
      cacheReadRate * effectiveCached +
      cacheCreationRate * effectiveCreation +
      outputRate * outputTokens;

    return {
      costUsd: cost,
      costConfidence: "computed",
      pricingSource: "litellm",
      pricingVersion: this._pricingVersion,
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
      const headers: Record<string, string> = { "User-Agent": "dexcost-typescript/0.1.0" };
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
      if (!serverData || typeof serverData !== "object" || Object.keys(serverData).length === 0) {
        return;
      }
      // Drop the schema sample if present (matches Python).
      delete (serverData as Record<string, unknown>).sample_spec;
      this._modelMap = serverData;
      this._pricingVersion =
        typeof rawData.pricing_version === "string" && rawData.pricing_version
          ? rawData.pricing_version
          : createHash("sha256")
              .update(JSON.stringify(serverData))
              .digest("hex")
              .slice(0, 12);
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
    if (model in this._modelMap) return this._modelMap[model];

    if (model.includes("/")) {
      const short = model.split("/").pop()!;
      if (short in this._modelMap) return this._modelMap[short];
    }

    const parts = model.split("-");
    for (let i = parts.length - 1; i > 0; i--) {
      const candidate = parts.slice(0, i).join("-");
      if (candidate in this._modelMap) return this._modelMap[candidate];
    }

    return undefined;
  }
}
