/**
 * Compute pricing engine — dispatches on `details.billing_model` and applies
 * the per-billing-model math from spec §6.
 *
 * The per-runtime memory-unit conversion table (Decision #7) is pinned at the
 * catalog-lookup boundary in §6.2 of the spec; the implementation enforces
 * it via two Decimal divisor constants (decimal GB vs binary GiB) selected
 * per billing model. Confusing them silently over-attributes Fargate memory
 * cost by ~4.86% — the pricing tests pin the divisor choice per model.
 *
 * Fail-silent contract (convention §9): every code path returns a usable
 * `ComputeCost` — the five-tier degradation ladder from convention §7
 * applies (per-region exact → per-runtime default → universal _meta default
 * → hardcoded constants → cost=0 with warning).
 *
 * Mirrors python/src/dexcost/compute_pricing.py.
 */

import Decimal from "decimal.js";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import type { CloudEnv } from "../cloud-detect.js";

// ─── Warn-once tracking (module-level, single-threaded JS → no lock) ─────────

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

// ─── Conversion constants (Decision #7 pinned table) ─────────────────────────

const GB_DECIMAL = new Decimal("1000000000"); // 10^9 bytes — Lambda / Azure Funcs / Vercel
const GIB_BINARY = new Decimal(1024 * 1024 * 1024); // 2^30 bytes — Fargate / Cloud Run
const HOUR_S = new Decimal(3600);
const MS_PER_S = new Decimal(1000);

// ─── Tier-4 hardcoded constants (must mirror _meta defaults) ─────────────────

type RateBlock = Record<string, Decimal>;

const HARDCODED: Record<string, RateBlock> = {
  lambda: {
    request_usd: new Decimal("0.0000002"),
    gb_second_usd: new Decimal("0.0000166667"),
  },
  fargate: {
    vcpu_second_usd: new Decimal("0.0000112444"),
    gib_second_usd: new Decimal("0.0000012347"),
  },
  cloud_run_request: {
    request_usd: new Decimal("0.0000004"),
    vcpu_second_usd: new Decimal("0.000024"),
    gib_second_usd: new Decimal("0.0000025"),
  },
  cloud_run_instance: {
    vcpu_second_usd: new Decimal("0.000024"),
    gib_second_usd: new Decimal("0.0000025"),
  },
  cloud_functions: {
    request_usd: new Decimal("0.0000004"),
    vcpu_second_usd: new Decimal("0.000024"),
    gib_second_usd: new Decimal("0.0000025"),
  },
  azure_functions: {
    execution_usd: new Decimal("0.0000002"),
    gb_second_usd: new Decimal("0.000016"),
  },
  vercel_fluid: {
    active_cpu_hour_usd: new Decimal("0.128"),
    memory_gb_hour_usd: new Decimal("0.0106"),
    invocation_usd: new Decimal("0.000000600"),
  },
  ec2: { vcpu_hour_usd: new Decimal("0.0464") },
  gce: { vcpu_hour_usd: new Decimal("0.0475") },
  azure_vm: { vcpu_hour_usd: new Decimal("0.046") },
  k8s_pod: { vcpu_hour_usd: new Decimal("0.0464") },
};

// ─── Public types ────────────────────────────────────────────────────────────

export interface ComputeCost {
  costUsd: Decimal;
  pricingSource: string;
  costConfidence: "exact" | "computed" | "estimated" | "unknown";
}

export interface ComputePricingEngineOptions {
  /** Override path to the catalog JSON; defaults to the bundled file. */
  catalogPath?: string;
  /** Already-parsed catalog object — overrides both `catalogPath` and bundled load. */
  catalog?: Record<string, any>;
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function toDecimal(v: unknown): Decimal {
  // Always go through String() so we never coerce through float — matches
  // the Python `Decimal(str(x))` pattern.
  return new Decimal(String(v));
}

function parseRateBlock(block: Record<string, unknown>, keys: string[]): RateBlock {
  const out: RateBlock = {};
  for (const k of keys) {
    out[k] = new Decimal(String(block[k]));
  }
  return out;
}

// ═════════════════════════════════════════════════════════════════════════════
// ComputePricingEngine
// ═════════════════════════════════════════════════════════════════════════════

export class ComputePricingEngine {
  private _catalog: Record<string, any> = {};
  private _catalogVersion: string = "unknown";

  constructor(opts?: ComputePricingEngineOptions) {
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

  // -------------------------------------------------------------------------
  // Catalog loading
  // -------------------------------------------------------------------------

  private _load(catalogPath?: string): void {
    let raw: string;
    try {
      if (catalogPath !== undefined) {
        raw = readFileSync(catalogPath, "utf-8");
      } else {
        // Use createRequire for the bundled JSON path — mirrors the egress
        // pricing engine pattern. Resolves relative to this module.
        try {
          const req = createRequire(import.meta.url);
          const obj = req("../data/compute_prices.json") as Record<string, any>;
          this._catalog = obj;
          const meta = obj._meta;
          if (meta && typeof meta === "object") {
            this._catalogVersion = String(meta.version ?? "unknown");
          }
          return;
        } catch (err) {
          _warnOnce(
            "catalog_missing",
            `compute catalog file not found; falling back to hardcoded ` +
              `per-billing-model defaults (${String(err)})`,
          );
          return;
        }
      }
    } catch (err) {
      const msg = (err as NodeJS.ErrnoException)?.code === "ENOENT"
        ? `compute catalog file not found; falling back to hardcoded per-billing-model defaults`
        : `compute catalog unreadable (${String(err)}); falling back to hardcoded per-billing-model defaults`;
      _warnOnce(
        (err as NodeJS.ErrnoException)?.code === "ENOENT"
          ? "catalog_missing"
          : "catalog_unreadable",
        msg,
      );
      return;
    }
    try {
      this._catalog = JSON.parse(raw);
    } catch (err) {
      _warnOnce(
        "catalog_malformed",
        `compute catalog malformed JSON (${String(err)}); falling back to ` +
          `hardcoded per-billing-model defaults`,
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

  // -------------------------------------------------------------------------
  // Public entry point — Tier 5 wrapper
  // -------------------------------------------------------------------------

  resolveComputeCost(
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    overrides: Record<string, string> | null | undefined,
    windowS?: Decimal,
  ): ComputeCost {
    const billingModel = (details && details.billing_model) || "unknown";
    const ov = overrides ?? {};
    try {
      return this._dispatch(billingModel, details, cloudEnv, ov, windowS);
    } catch (exc) {
      _warnOnce(
        `compute_failure:${billingModel}`,
        `compute pricing failed for billing_model=${billingModel}: ${String(exc)}; ` +
          `emitting cost_usd=0`,
      );
      return {
        costUsd: new Decimal(0),
        pricingSource: `compute_catalog:error:${billingModel}`,
        costConfidence: "unknown",
      };
    }
  }

  // -------------------------------------------------------------------------
  // Dispatch
  // -------------------------------------------------------------------------

  private _dispatch(
    billingModel: string,
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    overrides: Record<string, string>,
    windowS: Decimal | undefined,
  ): ComputeCost {
    // Cloud Run override — flip the math BEFORE catalog lookup.
    if (billingModel === "cloud_run_request" && overrides.cloud_run === "instance") {
      return this._cloudRunInstanceOverride(details, windowS);
    }

    if (billingModel === "lambda") return this._lambda(details);
    if (billingModel === "fargate") return this._fargate(details, windowS);
    if (billingModel === "cloud_run_request") return this._cloudRunRequest(details);
    if (billingModel === "cloud_run_instance") return this._cloudRunInstanceOverride(details, windowS);
    if (billingModel === "cloud_functions") return this._cloudFunctions(details);
    if (billingModel === "azure_functions") return this._azureFunctions(details);
    if (billingModel === "vercel_fluid") return this._vercel(details);
    if (billingModel === "ec2" || billingModel === "gce" || billingModel === "azure_vm") {
      return this._iaasShare(billingModel, details, cloudEnv, windowS);
    }
    if (billingModel === "k8s_pod") return this._k8sPodLimits(details, windowS);

    _warnOnce(
      `unsupported_billing_model:${billingModel}`,
      `compute pricing has no math for billing_model=${billingModel}; emitting cost_usd=0`,
    );
    return {
      costUsd: new Decimal(0),
      pricingSource: `compute_catalog:unsupported:${billingModel}`,
      costConfidence: "unknown",
    };
  }

  // ─── Lambda ────────────────────────────────────────────────────────────────

  private _lambda(details: Record<string, any>): ComputeCost {
    const region = details.region as string | undefined | null;
    const architecture = (details.architecture as string | undefined) || "x86_64";
    const { rate, source, confidence } = this._resolveLambdaRate(region, architecture);
    const durationS = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    const memoryGb = toDecimal(details.memory_bytes_limit).dividedBy(GB_DECIMAL);
    const gbSeconds = memoryGb.times(durationS);
    const invocations = toDecimal(details.invocation_count);
    const cost = invocations
      .times(rate.request_usd)
      .plus(gbSeconds.times(rate.gb_second_usd));
    return { costUsd: cost, pricingSource: source, costConfidence: confidence };
  }

  private _resolveLambdaRate(
    region: string | undefined | null,
    architecture: string,
  ): { rate: RateBlock; source: string; confidence: ComputeCost["costConfidence"] } {
    const block = this._catalog?.aws?.lambda;
    if (block && typeof block === "object") {
      const regions = block.regions || {};
      if (region && region in regions) {
        const archBlock = regions[region][architecture];
        if (archBlock) {
          return {
            rate: parseRateBlock(archBlock, ["request_usd", "gb_second_usd"]),
            source: `compute_catalog:aws:lambda:${region}:${architecture}`,
            confidence: "computed",
          };
        }
      }
      const def = block.default?.[architecture];
      if (def) {
        return {
          rate: parseRateBlock(def, ["request_usd", "gb_second_usd"]),
          source: `compute_catalog:aws:lambda:default:${architecture}`,
          confidence: "estimated",
        };
      }
    }
    const meta = this._catalog?._meta;
    try {
      return {
        rate: {
          request_usd: new Decimal(String(meta.default_lambda_request_usd)),
          gb_second_usd: new Decimal(String(meta.default_lambda_gb_second_usd)),
        },
        source: "compute_catalog:default:lambda",
        confidence: "estimated",
      };
    } catch {
      return {
        rate: HARDCODED.lambda,
        source: "compute_catalog:hardcoded:lambda",
        confidence: "estimated",
      };
    }
  }

  // ─── Fargate ───────────────────────────────────────────────────────────────

  private _fargate(details: Record<string, any>, windowS: Decimal | undefined): ComputeCost {
    let w = windowS;
    if (!w || w.lte(0)) {
      w = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    }
    const region = details.region as string | undefined | null;
    const architecture = (details.architecture as string | undefined) || "x86_64";
    const { rate, source, confidence } = this._resolveFargateRate(region, architecture);
    const memoryGib = toDecimal(details.memory_bytes_limit).dividedBy(GIB_BINARY);
    const vcpuCount = toDecimal(details.vcpu_count);
    const cost = vcpuCount
      .times(w)
      .times(rate.vcpu_second_usd)
      .plus(memoryGib.times(w).times(rate.gib_second_usd));
    return { costUsd: cost, pricingSource: source, costConfidence: confidence };
  }

  private _resolveFargateRate(
    region: string | undefined | null,
    architecture: string,
  ): { rate: RateBlock; source: string; confidence: ComputeCost["costConfidence"] } {
    const block = this._catalog?.aws?.fargate;
    if (block && typeof block === "object") {
      const regions = block.regions || {};
      if (region && region in regions) {
        const archBlock = regions[region][architecture];
        if (archBlock) {
          return {
            rate: parseRateBlock(archBlock, ["vcpu_second_usd", "gib_second_usd"]),
            source: `compute_catalog:aws:fargate:${region}:${architecture}`,
            confidence: "computed",
          };
        }
      }
      const def = block.default?.[architecture];
      if (def) {
        return {
          rate: parseRateBlock(def, ["vcpu_second_usd", "gib_second_usd"]),
          source: `compute_catalog:aws:fargate:default:${architecture}`,
          confidence: "estimated",
        };
      }
    }
    const meta = this._catalog?._meta;
    try {
      return {
        rate: {
          vcpu_second_usd: new Decimal(String(meta.default_fargate_vcpu_second_usd)),
          gib_second_usd: new Decimal(String(meta.default_fargate_gib_second_usd)),
        },
        source: "compute_catalog:default:fargate",
        confidence: "estimated",
      };
    } catch {
      return {
        rate: HARDCODED.fargate,
        source: "compute_catalog:hardcoded:fargate",
        confidence: "estimated",
      };
    }
  }

  // ─── Cloud Run (request-based, default) ────────────────────────────────────

  private _cloudRunRequest(details: Record<string, any>): ComputeCost {
    const region = details.region as string | undefined | null;
    const { rate } = this._resolveCloudRunRate(region);
    // Decision #1: Cloud Run defaults to request-based with estimated
    // confidence — the container cannot discover the actual billing mode.
    const source = "compute_catalog:cloud_run:request_based_default";
    const confidence: ComputeCost["costConfidence"] = "estimated";
    const durationS = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    const memoryGib = toDecimal(details.memory_bytes_limit).dividedBy(GIB_BINARY);
    const vcpuCount = toDecimal(details.vcpu_count);
    const invocations = toDecimal(details.invocation_count);
    const cost = invocations
      .times(rate.request_usd)
      .plus(vcpuCount.times(durationS).times(rate.vcpu_second_usd))
      .plus(memoryGib.times(durationS).times(rate.gib_second_usd));
    return { costUsd: cost, pricingSource: source, costConfidence: confidence };
  }

  private _cloudRunInstanceOverride(
    details: Record<string, any>,
    windowS: Decimal | undefined,
  ): ComputeCost {
    let w = windowS;
    if (!w || w.lte(0)) {
      w = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    }
    const region = details.region as string | undefined | null;
    const { rate } = this._resolveCloudRunRate(region);
    const memoryGib = toDecimal(details.memory_bytes_limit).dividedBy(GIB_BINARY);
    const vcpuCount = toDecimal(details.vcpu_count);
    const cost = vcpuCount
      .times(w)
      .times(rate.vcpu_second_usd)
      .plus(memoryGib.times(w).times(rate.gib_second_usd));
    return {
      costUsd: cost,
      pricingSource: "compute_catalog:cloud_run:instance_override",
      costConfidence: "computed",
    };
  }

  private _resolveCloudRunRate(
    region: string | undefined | null,
  ): { rate: RateBlock; source: string; confidence: ComputeCost["costConfidence"] } {
    const block = this._catalog?.gcp?.cloud_run;
    if (block && typeof block === "object") {
      const regions = block.regions || {};
      if (region && region in regions) {
        return {
          rate: parseRateBlock(regions[region], [
            "request_usd",
            "vcpu_second_usd",
            "gib_second_usd",
          ]),
          source: `compute_catalog:gcp:cloud_run:${region}`,
          confidence: "computed",
        };
      }
      const def = block.default;
      if (def) {
        return {
          rate: parseRateBlock(def, ["request_usd", "vcpu_second_usd", "gib_second_usd"]),
          source: "compute_catalog:gcp:cloud_run:default",
          confidence: "estimated",
        };
      }
    }
    const meta = this._catalog?._meta;
    try {
      return {
        rate: {
          request_usd: new Decimal(String(meta.default_cloud_run_request_usd)),
          vcpu_second_usd: new Decimal(String(meta.default_cloud_run_vcpu_second_usd)),
          gib_second_usd: new Decimal(String(meta.default_cloud_run_gib_second_usd)),
        },
        source: "compute_catalog:default:cloud_run",
        confidence: "estimated",
      };
    } catch {
      return {
        rate: HARDCODED.cloud_run_request,
        source: "compute_catalog:hardcoded:cloud_run",
        confidence: "estimated",
      };
    }
  }

  // ─── Cloud Functions Gen2 (Cloud Run pricing under the hood) ───────────────

  private _cloudFunctions(details: Record<string, any>): ComputeCost {
    const region = details.region as string | undefined | null;
    const { rate, source: cloudRunSource, confidence } = this._resolveCloudRunRate(region);
    const source = cloudRunSource.replace("cloud_run", "cloud_functions");
    const durationS = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    const memoryGib = toDecimal(details.memory_bytes_limit).dividedBy(GIB_BINARY);
    const vcpuCount = toDecimal(details.vcpu_count);
    const invocations = toDecimal(details.invocation_count);
    const cost = invocations
      .times(rate.request_usd)
      .plus(vcpuCount.times(durationS).times(rate.vcpu_second_usd))
      .plus(memoryGib.times(durationS).times(rate.gib_second_usd));
    return { costUsd: cost, pricingSource: source, costConfidence: confidence };
  }

  // ─── Azure Functions Consumption ───────────────────────────────────────────

  private _azureFunctions(details: Record<string, any>): ComputeCost {
    const region = details.region as string | undefined | null;
    const { rate, source, confidence } = this._resolveAzureFunctionsRate(region);
    const durationS = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    const memoryGb = toDecimal(details.memory_bytes_limit).dividedBy(GB_DECIMAL);
    const invocations = toDecimal(details.invocation_count);
    const cost = invocations
      .times(rate.execution_usd)
      .plus(memoryGb.times(durationS).times(rate.gb_second_usd));
    return { costUsd: cost, pricingSource: source, costConfidence: confidence };
  }

  private _resolveAzureFunctionsRate(
    region: string | undefined | null,
  ): { rate: RateBlock; source: string; confidence: ComputeCost["costConfidence"] } {
    const block = this._catalog?.azure?.functions_consumption;
    if (block && typeof block === "object") {
      const regions = block.regions || {};
      if (region && region in regions) {
        return {
          rate: parseRateBlock(regions[region], ["execution_usd", "gb_second_usd"]),
          source: `compute_catalog:azure:functions_consumption:${region}`,
          confidence: "computed",
        };
      }
      const def = block.default;
      if (def) {
        return {
          rate: parseRateBlock(def, ["execution_usd", "gb_second_usd"]),
          source: "compute_catalog:azure:functions_consumption:default",
          confidence: "estimated",
        };
      }
    }
    const meta = this._catalog?._meta;
    try {
      return {
        rate: {
          execution_usd: new Decimal(String(meta.default_azure_functions_execution_usd)),
          gb_second_usd: new Decimal(String(meta.default_azure_functions_gb_second_usd)),
        },
        source: "compute_catalog:default:azure_functions",
        confidence: "estimated",
      };
    } catch {
      return {
        rate: HARDCODED.azure_functions,
        source: "compute_catalog:hardcoded:azure_functions",
        confidence: "estimated",
      };
    }
  }

  // ─── Vercel Fluid ──────────────────────────────────────────────────────────

  private _vercel(details: Record<string, any>): ComputeCost {
    const { rate, source, confidence } = this._resolveVercelRate();
    const durationS = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    const memoryGb = toDecimal(details.memory_bytes_limit).dividedBy(GB_DECIMAL);
    const invocations = toDecimal(details.invocation_count);
    const activeCpuHours = durationS.dividedBy(HOUR_S);
    const memoryGbHours = memoryGb.times(durationS.dividedBy(HOUR_S));
    const cost = invocations
      .times(rate.invocation_usd)
      .plus(activeCpuHours.times(rate.active_cpu_hour_usd))
      .plus(memoryGbHours.times(rate.memory_gb_hour_usd));
    return { costUsd: cost, pricingSource: source, costConfidence: confidence };
  }

  private _resolveVercelRate(): {
    rate: RateBlock;
    source: string;
    confidence: ComputeCost["costConfidence"];
  } {
    const block = this._catalog?.vercel?.fluid;
    if (block && typeof block === "object") {
      const def = block.default;
      if (def) {
        return {
          rate: parseRateBlock(def, [
            "active_cpu_hour_usd",
            "memory_gb_hour_usd",
            "invocation_usd",
          ]),
          source: "compute_catalog:vercel:fluid",
          confidence: "computed",
        };
      }
    }
    const meta = this._catalog?._meta;
    try {
      return {
        rate: {
          active_cpu_hour_usd: new Decimal(String(meta.default_vercel_cpu_hour_usd)),
          memory_gb_hour_usd: new Decimal(String(meta.default_vercel_memory_gb_hour_usd)),
          invocation_usd: new Decimal("0.000000600"),
        },
        source: "compute_catalog:default:vercel",
        confidence: "estimated",
      };
    } catch {
      return {
        rate: HARDCODED.vercel_fluid,
        source: "compute_catalog:hardcoded:vercel",
        confidence: "estimated",
      };
    }
  }

  // ─── EC2 / GCE / Azure VM share ────────────────────────────────────────────

  private _iaasShare(
    billingModel: "ec2" | "gce" | "azure_vm",
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    windowS: Decimal | undefined,
  ): ComputeCost {
    let w = windowS;
    if (!w || w.lte(0)) {
      w = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    }
    const instanceType = cloudEnv.instanceType ?? null;
    const region = details.region as string | undefined | null;
    const { rate: instanceHourly, source, confidence } = this._resolveIaasRate(
      billingModel,
      region,
      instanceType,
    );
    const vcpuCount = toDecimal(details.vcpu_count);
    const vcpuSeconds = toDecimal(details.vcpu_seconds_used);
    if (vcpuCount.lte(0) || w.lte(0)) {
      return { costUsd: new Decimal(0), pricingSource: source, costConfidence: confidence };
    }
    const shareFactor = vcpuSeconds.dividedBy(vcpuCount.times(w));
    const taskInstanceHours = shareFactor.times(w.dividedBy(HOUR_S));
    const cost = taskInstanceHours.times(instanceHourly);
    return { costUsd: cost, pricingSource: source, costConfidence: confidence };
  }

  private _resolveIaasRate(
    billingModel: "ec2" | "gce" | "azure_vm",
    region: string | undefined | null,
    instanceType: string | null,
  ): { rate: Decimal; source: string; confidence: ComputeCost["costConfidence"] } {
    const mapping: Record<string, [string, string]> = {
      ec2: ["aws", "ec2"],
      gce: ["gcp", "gce"],
      azure_vm: ["azure", "vm"],
    };
    const [providerKey, runtimeKey] = mapping[billingModel]!;
    const block = this._catalog?.[providerKey]?.[runtimeKey];
    if (block && typeof block === "object") {
      const regions = block.regions || {};
      if (region && region in regions && instanceType) {
        const instances = regions[region].instance_types || {};
        const sku = instances[instanceType];
        if (sku) {
          try {
            return {
              rate: new Decimal(String(sku.hourly_usd)),
              source: `compute_catalog:${providerKey}:${runtimeKey}:${region}:${instanceType}`,
              confidence: "computed",
            };
          } catch {
            // fall through
          }
        }
      }
      try {
        return {
          rate: new Decimal(String(block.default_vcpu_hour_usd)),
          source: `compute_catalog:${providerKey}:${runtimeKey}:default`,
          confidence: "estimated",
        };
      } catch {
        // fall through
      }
    }
    const meta = this._catalog?._meta;
    const metaKey = "default_ec2_vcpu_hour_usd";
    try {
      return {
        rate: new Decimal(String(meta[metaKey])),
        source: `compute_catalog:default:${billingModel}`,
        confidence: "estimated",
      };
    } catch {
      return {
        rate: HARDCODED[billingModel].vcpu_hour_usd,
        source: `compute_catalog:hardcoded:${billingModel}`,
        confidence: "estimated",
      };
    }
  }

  // ─── K8s pod (default — limits × duration × hourly) ────────────────────────

  private _k8sPodLimits(details: Record<string, any>, windowS: Decimal | undefined): ComputeCost {
    let w = windowS;
    if (!w || w.lte(0)) {
      w = toDecimal(details.duration_ms).dividedBy(MS_PER_S);
    }
    const { rate, source, confidence } = this._resolveK8sPodRate();
    const vcpuCount = toDecimal(details.vcpu_count);
    const cost = vcpuCount.times(w.dividedBy(HOUR_S)).times(rate);
    return { costUsd: cost, pricingSource: source, costConfidence: confidence };
  }

  private _resolveK8sPodRate(): {
    rate: Decimal;
    source: string;
    confidence: ComputeCost["costConfidence"];
  } {
    const meta = this._catalog?._meta;
    try {
      return {
        rate: new Decimal(String(meta.default_k8s_pod_vcpu_hour_usd)),
        source: "compute_catalog:k8s_pod:limits",
        confidence: "computed",
      };
    } catch {
      return {
        rate: HARDCODED.k8s_pod.vcpu_hour_usd,
        source: "compute_catalog:hardcoded:k8s_pod",
        confidence: "estimated",
      };
    }
  }
}
