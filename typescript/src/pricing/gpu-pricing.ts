/**
 * GPU pricing engine — Phase 2 v2.
 *
 * Dispatches on `details.billing_model` and applies the per-billing-model
 * math from spec §6. Four discriminator values:
 *
 * - per_gpu_second_active     — Modal / RunPod / Replicate
 * - per_instance_hour         — AWS EC2 GPU / GCP GCE bundled / Azure VM GPU
 * - per_gpu_hour_reserved     — Lambda Labs / CoreWeave / GCP N1+accelerator
 * - per_vgpu_hour             — Azure NVadsA10 v5 fractional (Decision #10)
 *
 * Per Decision #7: NO per-runtime memory-unit conversion table. VRAM tier
 * is encoded in the SKU key (h100-80gb-sxm5 vs a100-40gb are separate
 * catalog entries).
 *
 * Fail-silent (convention §9): every code path returns a usable GpuCost.
 * Five-tier degradation ladder:
 *  Tier 1: per-region SKU exact
 *  Tier 2: per-runtime default
 *  Tier 3a: device-class fallback (Decision #4)
 *  Tier 3b: universal _meta default
 *  Tier 4: hardcoded constants
 *  Tier 5: try/except returns cost=0 + unknown
 *
 * Decision #1 measurement-side fallback: when details carries
 * `_cgroup_scope_fallback`, it is appended to pricing_source and confidence
 * drops to `estimated`.
 *
 * Mirrors python/src/dexcost/gpu_pricing.py.
 */

import { Decimal } from "decimal.js";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import type { CloudEnv } from "../cloud-detect.js";

// ─── Warn-once tracking ─────────────────────────────────────────────────────

const _warnedModes: Set<string> = new Set();

export function _resetWarningStateForTests(): void {
  _warnedModes.clear();
}

function _warnOnce(mode: string, message: string): void {
  if (_warnedModes.has(mode)) return;
  _warnedModes.add(mode);
  // eslint-disable-next-line no-console
  console.warn(message);
}

// ─── Constants ──────────────────────────────────────────────────────────────

const HOUR_S = new Decimal(3600);
const MS_PER_S = new Decimal(1000);

// Tier-4 hardcoded constants — MUST mirror _meta defaults in gpu_prices.json.
const HARDCODED: Record<string, Record<string, Decimal>> = {
  per_instance_hour: { hourly_usd: new Decimal("55.04") },
  per_gpu_second_active: { gpu_second_usd: new Decimal("0.000694") },
  per_gpu_hour_reserved: { gpu_hour_usd: new Decimal("3.99") },
  per_vgpu_hour: { vgpu_hour_usd: new Decimal("0.454") },
};

// Decision #4 device-class default rates — cold-start fallback for unknown SKUs.
const DEVICE_CLASS_DEFAULTS: Record<string, Record<string, Decimal>> = {
  hopper: {
    per_instance_hour: new Decimal("98.32"),
    per_gpu_second_active: new Decimal("0.001097"),
    per_gpu_hour_reserved: new Decimal("3.99"),
    per_vgpu_hour: new Decimal("3.99"),
  },
  ampere: {
    per_instance_hour: new Decimal("32.77"),
    per_gpu_second_active: new Decimal("0.000833"),
    per_gpu_hour_reserved: new Decimal("2.20"),
    per_vgpu_hour: new Decimal("2.20"),
  },
  ada_lovelace: {
    per_instance_hour: new Decimal("12.00"),
    per_gpu_second_active: new Decimal("0.000400"),
    per_gpu_hour_reserved: new Decimal("1.50"),
    per_vgpu_hour: new Decimal("1.50"),
  },
  blackwell: {
    per_instance_hour: new Decimal("180.00"),
    per_gpu_second_active: new Decimal("0.002500"),
    per_gpu_hour_reserved: new Decimal("6.50"),
    per_vgpu_hour: new Decimal("6.50"),
  },
};

// productName substring → device-class (most specific first).
const DEVICE_CLASS_PATTERNS: ReadonlyArray<readonly [string, readonly string[]]> = [
  ["blackwell", ["b100", "b200", "gb200", "b300", "blackwell"]],
  ["hopper", ["h100", "h200", "hopper"]],
  ["ada_lovelace", ["l4", "l40", "ada lovelace", "rtx 4090", "rtx 5090"]],
  ["ampere", ["a100", "a40", "a10", "ampere", "rtx 3090", "rtx a6000"]],
];

function detectDeviceClass(productNameLower: string | null | undefined): string | null {
  if (!productNameLower) return null;
  for (const [cls, patterns] of DEVICE_CLASS_PATTERNS) {
    for (const p of patterns) {
      if (productNameLower.includes(p)) return cls;
    }
  }
  return null;
}

// ─── Public types ──────────────────────────────────────────────────────────

export interface GpuCost {
  costUsd: Decimal;
  pricingSource: string;
  costConfidence: "computed" | "estimated" | "unknown";
}

export interface GpuPricingEngineOptions {
  catalogPath?: string;
  catalog?: Record<string, any>;
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function toDecimal(v: unknown): Decimal {
  // Always go through String() to avoid JS-float coercion (matches Python's
  // `Decimal(str(x))` pattern). Throws InvalidOperation on bad input → caught
  // by the Tier-5 try/except wrapper.
  return new Decimal(String(v));
}

// ═════════════════════════════════════════════════════════════════════════════
// GpuPricingEngine
// ═════════════════════════════════════════════════════════════════════════════

export class GpuPricingEngine {
  private _catalog: Record<string, any> = {};
  private _catalogVersion = "unknown";

  constructor(opts?: GpuPricingEngineOptions) {
    if (opts?.catalog !== undefined) {
      this._catalog = opts.catalog;
      const meta = (this._catalog as any)._meta;
      if (meta && typeof meta === "object") {
        this._catalogVersion = String(meta.version ?? "unknown");
      }
      return;
    }
    this._load(opts?.catalogPath);
  }

  // ------------------------------------------------------------------
  // Catalog loading
  // ------------------------------------------------------------------

  private _load(catalogPath?: string): void {
    let raw: string;
    try {
      if (catalogPath !== undefined) {
        raw = readFileSync(catalogPath, "utf-8");
      } else {
        try {
          const req = createRequire(import.meta.url);
          const obj = req("../data/gpu_prices.json") as Record<string, any>;
          this._catalog = obj;
          const meta = obj._meta;
          if (meta && typeof meta === "object") {
            this._catalogVersion = String(meta.version ?? "unknown");
          }
          return;
        } catch (err) {
          _warnOnce(
            "gpu_catalog_missing",
            `gpu catalog file not found; falling back to hardcoded ` +
              `per-billing-model defaults (${String(err)})`,
          );
          return;
        }
      }
    } catch (err) {
      const code = (err as NodeJS.ErrnoException)?.code;
      _warnOnce(
        code === "ENOENT" ? "gpu_catalog_missing" : "gpu_catalog_unreadable",
        code === "ENOENT"
          ? `gpu catalog file not found; falling back to hardcoded per-billing-model defaults`
          : `gpu catalog unreadable (${String(err)}); falling back to hardcoded`,
      );
      return;
    }
    try {
      this._catalog = JSON.parse(raw);
    } catch (err) {
      _warnOnce(
        "gpu_catalog_malformed",
        `gpu catalog malformed JSON (${String(err)}); falling back to hardcoded`,
      );
      this._catalog = {};
      return;
    }
    const meta = (this._catalog as any)._meta;
    if (meta && typeof meta === "object") {
      this._catalogVersion = String(meta.version ?? "unknown");
    }
  }

  get catalogVersion(): string {
    return this._catalogVersion;
  }

  /** Read-only catalog accessor — used by tests to verify rate sources. */
  get catalog(): Record<string, any> {
    return this._catalog;
  }

  // ------------------------------------------------------------------
  // Public entry point — Tier-5 wrapper + Decision #1 suffix
  // ------------------------------------------------------------------

  resolveGpuCost(
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    windowS?: Decimal,
  ): GpuCost {
    const billingModel = (details && details.billing_model) || "unknown";
    let cost: GpuCost;
    try {
      cost = this._dispatch(billingModel, details, cloudEnv, windowS);
    } catch (exc) {
      _warnOnce(
        `gpu_pricing_failure:${billingModel}`,
        `gpu pricing failed for billing_model=${billingModel}: ${String(exc)}; ` +
          `emitting cost_usd=0`,
      );
      return {
        costUsd: new Decimal(0),
        pricingSource: `gpu_catalog:error:${billingModel}`,
        costConfidence: "unknown",
      };
    }

    // Decision #1 measurement-side fallback suffix.
    const scopeFb = (details || {})._cgroup_scope_fallback;
    if (scopeFb) {
      return {
        costUsd: cost.costUsd,
        pricingSource: `${cost.pricingSource}:${String(scopeFb)}`,
        costConfidence: "estimated",
      };
    }
    return cost;
  }

  // ------------------------------------------------------------------
  // Dispatch
  // ------------------------------------------------------------------

  private _dispatch(
    billingModel: string,
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    windowS: Decimal | undefined,
  ): GpuCost {
    if (billingModel === "per_gpu_second_active") {
      return this._perGpuSecond(details, cloudEnv);
    }
    if (billingModel === "per_instance_hour") {
      return this._perInstanceHour(details, cloudEnv, windowS);
    }
    if (billingModel === "per_gpu_hour_reserved") {
      return this._perGpuHour(details, cloudEnv, windowS);
    }
    if (billingModel === "per_vgpu_hour") {
      return this._perVgpuHour(details, cloudEnv, windowS);
    }
    _warnOnce(
      `gpu_unsupported_billing_model:${billingModel}`,
      `gpu pricing has no math for billing_model=${billingModel}`,
    );
    return {
      costUsd: new Decimal(0),
      pricingSource: `gpu_catalog:unsupported:${billingModel}`,
      costConfidence: "unknown",
    };
  }

  // ─── per_gpu_second_active ───────────────────────────────────────────

  private _perGpuSecond(
    details: Record<string, any>,
    cloudEnv: CloudEnv,
  ): GpuCost {
    const provider = cloudEnv.provider;
    const gpuSku = details.gpu_sku as string | null | undefined;
    const { rate, source, confidence } = this._resolvePerGpuSecondRate(
      provider,
      gpuSku,
      details,
    );
    const gpuSeconds = toDecimal(details.gpu_seconds_used);
    return {
      costUsd: gpuSeconds.times(rate),
      pricingSource: source,
      costConfidence: confidence,
    };
  }

  private _resolvePerGpuSecondRate(
    provider: string | null | undefined,
    gpuSku: string | null | undefined,
    details: Record<string, any>,
  ): { rate: Decimal; source: string; confidence: GpuCost["costConfidence"] } {
    if (provider && gpuSku) {
      const block = this._catalog[provider]?.per_gpu_second_active;
      if (block && typeof block === "object") {
        const def = (block.default || {}) as Record<string, any>;
        // Direct (Modal / Replicate) → entry.gpu_sku === gpuSku
        for (const [key, entry] of Object.entries(def)) {
          if (entry && typeof entry === "object" && (entry as any).gpu_sku === gpuSku) {
            try {
              return {
                rate: new Decimal(String((entry as any).gpu_second_usd)),
                source: `gpu_catalog:${provider}:per_gpu_second_active:${key}`,
                confidence: "computed",
              };
            } catch {
              // fall through
            }
          }
          // Nested (RunPod on_demand / community_cloud)
          if (entry && typeof entry === "object") {
            for (const [skuKey, skuEntry] of Object.entries(entry)) {
              if (
                skuEntry &&
                typeof skuEntry === "object" &&
                (skuEntry as any).gpu_sku === gpuSku
              ) {
                try {
                  return {
                    rate: new Decimal(String((skuEntry as any).gpu_second_usd)),
                    source: `gpu_catalog:${provider}:per_gpu_second_active:${key}:${skuKey}`,
                    confidence: "computed",
                  };
                } catch {
                  // fall through
                }
              }
            }
          }
        }
      }
    }
    return this._deviceClassOrMetaFallback(
      details,
      "per_gpu_second_active",
      "gpu_second_usd",
    );
  }

  // ─── per_instance_hour ───────────────────────────────────────────────

  private _perInstanceHour(
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    windowS: Decimal | undefined,
  ): GpuCost {
    let window = windowS;
    if (window === undefined || window.lessThanOrEqualTo(0)) {
      window = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    }
    const provider = cloudEnv.provider;
    const region = details.region as string | null | undefined;
    const instanceType =
      (details.instance_type as string | null | undefined) ||
      cloudEnv.instanceType ||
      null;
    const { rate: hourlyRate, source, confidence } = this._resolvePerInstanceRate(
      provider,
      region,
      instanceType,
      details,
    );
    const gpuCount = toDecimal(details.gpu_count);
    const gpuSeconds = toDecimal(details.gpu_seconds_used);
    if (gpuCount.lessThanOrEqualTo(0) || window.lessThanOrEqualTo(0)) {
      return { costUsd: new Decimal(0), pricingSource: source, costConfidence: confidence };
    }
    const shareFactor = gpuSeconds.dividedBy(gpuCount.times(window));
    const taskInstanceHours = shareFactor.times(window.dividedBy(HOUR_S));
    return {
      costUsd: taskInstanceHours.times(hourlyRate),
      pricingSource: source,
      costConfidence: confidence,
    };
  }

  private _resolvePerInstanceRate(
    provider: string | null | undefined,
    region: string | null | undefined,
    instanceType: string | null | undefined,
    details: Record<string, any>,
  ): { rate: Decimal; source: string; confidence: GpuCost["costConfidence"] } {
    const blockKeys: Record<string, string> = {
      aws: "ec2_gpu",
      gcp: "gce_gpu_bundled",
      azure: "vm_gpu",
    };
    const blockKey = provider ? blockKeys[provider] : undefined;
    if (provider && blockKey && instanceType && region) {
      const entry =
        this._catalog?.[provider]?.[blockKey]?.regions?.[region]?.instance_types?.[
          instanceType
        ];
      if (entry) {
        try {
          return {
            rate: new Decimal(String(entry.hourly_usd)),
            source: `gpu_catalog:${provider}:${blockKey}:${region}:${instanceType}`,
            confidence: "computed",
          };
        } catch {
          // fall through
        }
      }
    }
    return this._deviceClassOrMetaFallback(
      details,
      "per_instance_hour",
      "hourly_usd",
    );
  }

  // ─── per_gpu_hour_reserved ───────────────────────────────────────────

  private _perGpuHour(
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    windowS: Decimal | undefined,
  ): GpuCost {
    let window = windowS;
    if (window === undefined || window.lessThanOrEqualTo(0)) {
      window = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    }
    const provider = cloudEnv.provider;
    const gpuSku = details.gpu_sku as string | null | undefined;
    const { rate: gpuHourUsd, source, confidence } = this._resolvePerGpuHourRate(
      provider,
      gpuSku,
      details,
    );
    const gpuCount = toDecimal(details.gpu_count);
    const gpuSeconds = toDecimal(details.gpu_seconds_used);
    if (gpuCount.lessThanOrEqualTo(0) || window.lessThanOrEqualTo(0)) {
      return { costUsd: new Decimal(0), pricingSource: source, costConfidence: confidence };
    }
    const shareFactor = gpuSeconds.dividedBy(gpuCount.times(window));
    const taskGpuHours = shareFactor.times(window.dividedBy(HOUR_S)).times(gpuCount);
    return {
      costUsd: taskGpuHours.times(gpuHourUsd),
      pricingSource: source,
      costConfidence: confidence,
    };
  }

  private _resolvePerGpuHourRate(
    provider: string | null | undefined,
    gpuSku: string | null | undefined,
    details: Record<string, any>,
  ): { rate: Decimal; source: string; confidence: GpuCost["costConfidence"] } {
    if (provider && gpuSku) {
      const block = this._catalog[provider]?.per_gpu_hour_reserved;
      if (block && typeof block === "object") {
        const def = (block.default || {}) as Record<string, any>;
        for (const [key, entry] of Object.entries(def)) {
          if (entry && typeof entry === "object" && (entry as any).gpu_sku === gpuSku) {
            try {
              return {
                rate: new Decimal(String((entry as any).gpu_hour_usd)),
                source: `gpu_catalog:${provider}:per_gpu_hour_reserved:${key}`,
                confidence: "computed",
              };
            } catch {
              // fall through
            }
          }
        }
      }
    }
    // GCP N1+accelerator path (Decision #9) — separate block.
    if (provider === "gcp" && gpuSku) {
      const region = details.region as string | null | undefined;
      if (region) {
        const accelerators =
          this._catalog.gcp?.gce_gpu_attached?.regions?.[region]?.accelerator_types || {};
        for (const [accKey, entry] of Object.entries(accelerators)) {
          if (entry && typeof entry === "object" && (entry as any).gpu_sku === gpuSku) {
            try {
              return {
                rate: new Decimal(String((entry as any).gpu_hour_usd)),
                source: `gpu_catalog:gcp:gce_gpu_attached:${region}:${accKey}`,
                confidence: "computed",
              };
            } catch {
              // fall through
            }
          }
        }
      }
    }
    return this._deviceClassOrMetaFallback(
      details,
      "per_gpu_hour_reserved",
      "gpu_hour_usd",
    );
  }

  // ─── per_vgpu_hour (Azure NVadsA10 v5) — Decision #10 ─────────────────

  private _perVgpuHour(
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    windowS: Decimal | undefined,
  ): GpuCost {
    let window = windowS;
    if (window === undefined || window.lessThanOrEqualTo(0)) {
      window = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    }
    const provider = cloudEnv.provider;
    const region = details.region as string | null | undefined;
    const instanceType =
      (details.instance_type as string | null | undefined) ||
      cloudEnv.instanceType ||
      null;
    const { rate: vgpuHourUsd, source, confidence } = this._resolvePerVgpuRate(
      provider,
      region,
      instanceType,
      details,
    );
    const gpuSeconds = toDecimal(details.gpu_seconds_used);
    if (window.lessThanOrEqualTo(0)) {
      return { costUsd: new Decimal(0), pricingSource: source, costConfidence: confidence };
    }
    const shareFactor = gpuSeconds.dividedBy(window);
    const taskVgpuHours = shareFactor.times(window.dividedBy(HOUR_S));
    return {
      costUsd: taskVgpuHours.times(vgpuHourUsd),
      pricingSource: source,
      costConfidence: confidence,
    };
  }

  private _resolvePerVgpuRate(
    provider: string | null | undefined,
    region: string | null | undefined,
    instanceType: string | null | undefined,
    details: Record<string, any>,
  ): { rate: Decimal; source: string; confidence: GpuCost["costConfidence"] } {
    if (provider === "azure" && instanceType && region) {
      const entry =
        this._catalog.azure?.vm_vgpu?.regions?.[region]?.instance_types?.[instanceType];
      if (entry) {
        try {
          return {
            rate: new Decimal(String(entry.vgpu_hour_usd)),
            source: `gpu_catalog:azure:vm_vgpu:${region}:${instanceType}`,
            confidence: "computed",
          };
        } catch {
          // fall through
        }
      }
    }
    return this._deviceClassOrMetaFallback(
      details,
      "per_vgpu_hour",
      "vgpu_hour_usd",
    );
  }

  // ─── Tier-3 → Tier-3b → Tier-4 fallback ladder ────────────────────────

  private _deviceClassOrMetaFallback(
    details: Record<string, any>,
    billingModel: string,
    rateKey: string,
  ): { rate: Decimal; source: string; confidence: GpuCost["costConfidence"] } {
    // Tier-3a: device-class fallback via productName substring.
    const productName = details._nvml_product_name_lower as
      | string
      | null
      | undefined;
    const deviceClass = detectDeviceClass(productName);
    if (
      deviceClass &&
      ["per_instance_hour", "per_gpu_second_active", "per_gpu_hour_reserved", "per_vgpu_hour"].includes(
        billingModel,
      )
    ) {
      const rate = DEVICE_CLASS_DEFAULTS[deviceClass][billingModel];
      _warnOnce(
        `gpu_sku_unknown:${productName}`,
        `GPU SKU not in catalog (productName=${String(productName)}); ` +
          `falling back to device_class=${deviceClass} default rate ` +
          `(~30% accuracy band)`,
      );
      return {
        rate,
        source: `gpu_catalog:device_class_fallback:${deviceClass}:${billingModel}`,
        confidence: "estimated",
      };
    }

    // Tier-3b: universal _meta default.
    const meta = (this._catalog._meta || {}) as Record<string, any>;
    const metaKey = `default_${billingModel}_usd`;
    if (metaKey in meta) {
      try {
        return {
          rate: new Decimal(String(meta[metaKey])),
          source: `gpu_catalog:default:${billingModel}`,
          confidence: "estimated",
        };
      } catch {
        // fall through to hardcoded
      }
    }

    // Tier-4: hardcoded constants.
    const hc = HARDCODED[billingModel];
    return {
      rate: hc[rateKey],
      source: `gpu_catalog:hardcoded:${billingModel}`,
      confidence: "estimated",
    };
  }
}
