/**
 * GPU pricing property invariants — cost spec §10.3.
 * Mirrors python/tests/test_gpu_invariants.py.
 */

import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import {
  GpuPricingEngine,
  _resetWarningStateForTests,
} from "../src/pricing/gpu-pricing.js";
import type { CloudEnv } from "../src/cloud-detect.js";

function base(billingModel: string, overrides: Record<string, any> = {}): Record<string, any> {
  return {
    billing_model: billingModel,
    gpu_vendor: "nvidia",
    gpu_sku: "h100-80gb-sxm5",
    gpu_count: 1,
    region: null,
    duration_ms: 1000,
    gpu_seconds_used: 1.0,
    instance_type: null,
    vgpu_profile: null,
    mig_profile: null,
    ...overrides,
  };
}

function cloud(
  provider: string | null,
  region: string | null,
  source: "env" | "imds" | "dmi" | "none",
  instanceType?: string | null,
): CloudEnv {
  return { provider, region, source, instanceType: instanceType ?? null };
}

const ALL_BILLING_MODELS = [
  "per_gpu_second_active",
  "per_instance_hour",
  "per_gpu_hour_reserved",
  "per_vgpu_hour",
];

function configureFor(billingModel: string): {
  details: Record<string, any>;
  env: CloudEnv;
} {
  const details = base(billingModel);
  if (billingModel === "per_instance_hour") {
    details.instance_type = "p5.48xlarge";
    details.region = "us-east-1";
    return {
      details,
      env: cloud("aws", "us-east-1", "imds", "p5.48xlarge"),
    };
  }
  if (billingModel === "per_vgpu_hour") {
    details.instance_type = "Standard_NV6ads_A10_v5";
    details.region = "eastus";
    details.gpu_sku = "a10-vgpu-1of6";
    return {
      details,
      env: cloud("azure", "eastus", "imds", "Standard_NV6ads_A10_v5"),
    };
  }
  if (billingModel === "per_gpu_hour_reserved") {
    return { details, env: cloud("lambda_labs", null, "dmi") };
  }
  return { details, env: cloud("modal", null, "env") };
}

describe("GPU pricing property invariants", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it.each(ALL_BILLING_MODELS)(
    "Invariant 1 — costUsd >= 0 always (%s)",
    (billingModel) => {
      const { details, env } = configureFor(billingModel);
      const cost = engine.resolveGpuCost(details, env, new Decimal("1"));
      expect(cost.costUsd.greaterThanOrEqualTo(0)).toBe(true);
    },
  );

  it.each([1, 2, 5, 10])(
    "Invariant 3 — linearity in gpu_seconds_used (scale %i)",
    (scale) => {
      const env = cloud("modal", null, "env");
      const base1 = base("per_gpu_second_active", {
        gpu_seconds_used: 1.0,
        duration_ms: 1000,
      });
      const baseN = base("per_gpu_second_active", {
        gpu_seconds_used: scale,
        duration_ms: scale * 1000,
      });
      const cost1 = engine.resolveGpuCost(base1, env, new Decimal(1));
      const costN = engine.resolveGpuCost(baseN, env, new Decimal(scale));
      expect(costN.costUsd.equals(cost1.costUsd.times(scale))).toBe(true);
    },
  );

  it("Invariant 4 — H100 more expensive than A100 on Modal", () => {
    const env = cloud("modal", null, "env");
    const h100 = base("per_gpu_second_active", { gpu_sku: "h100-80gb-sxm5" });
    const a100 = base("per_gpu_second_active", { gpu_sku: "a100-80gb-sxm4" });
    const h = engine.resolveGpuCost(h100, env, new Decimal(1));
    const a = engine.resolveGpuCost(a100, env, new Decimal(1));
    expect(h.costUsd.greaterThan(a.costUsd)).toBe(true);
  });

  it("Invariant 5 — Modal per-second × 3600 within 0.5×–3× Lambda per-hour", () => {
    const modal = base("per_gpu_second_active", {
      gpu_sku: "h100-80gb-sxm5",
      gpu_seconds_used: 3600.0,
      duration_ms: 3_600_000,
    });
    const lambda = base("per_gpu_hour_reserved", {
      gpu_sku: "h100-80gb-sxm5",
      gpu_count: 1,
      gpu_seconds_used: 3600.0,
      duration_ms: 3_600_000,
    });
    const modalCost = engine.resolveGpuCost(
      modal,
      cloud("modal", null, "env"),
      new Decimal(3600),
    );
    const lambdaCost = engine.resolveGpuCost(
      lambda,
      cloud("lambda_labs", null, "dmi"),
      new Decimal(3600),
    );
    expect(modalCost.costUsd.greaterThan(0)).toBe(true);
    expect(lambdaCost.costUsd.greaterThan(0)).toBe(true);
    const ratio = modalCost.costUsd.dividedBy(lambdaCost.costUsd);
    expect(ratio.greaterThan(new Decimal("0.5"))).toBe(true);
    expect(ratio.lessThan(new Decimal("3.0"))).toBe(true);
  });

  it.each(ALL_BILLING_MODELS)(
    "Invariant 6 — confidence in {computed, estimated} (%s)",
    (billingModel) => {
      const { details, env } = configureFor(billingModel);
      const cost = engine.resolveGpuCost(details, env, new Decimal("1"));
      expect(["computed", "estimated"]).toContain(cost.costConfidence);
    },
  );

  it.each(ALL_BILLING_MODELS)(
    "Invariant 7 — pricingSource starts with gpu_catalog: (%s)",
    (billingModel) => {
      const { details, env } = configureFor(billingModel);
      const cost = engine.resolveGpuCost(details, env, new Decimal("1"));
      expect(cost.pricingSource.startsWith("gpu_catalog:")).toBe(true);
    },
  );
});
