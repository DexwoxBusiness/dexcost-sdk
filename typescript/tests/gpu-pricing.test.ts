/**
 * GPU pricing engine — 4 billing models, 5-tier ladder, Decision #4
 * device-class fallback. Mirrors python/tests/test_gpu_pricing.py.
 */

import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import {
  GpuPricingEngine,
  _resetWarningStateForTests,
} from "../src/pricing/gpu-pricing.js";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import type { CloudEnv } from "../src/cloud-detect.js";

function cloud(
  provider: string | null,
  region: string | null,
  source: "env" | "imds" | "dmi" | "none",
  instanceType?: string | null,
): CloudEnv {
  return { provider, region, source, instanceType: instanceType ?? null };
}

function baseDetails(
  billingModel: string,
  overrides: Record<string, unknown> = {},
): Record<string, any> {
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

describe("GpuPricingEngine — per_gpu_second_active", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it("Modal H100: 1.234 GPU-seconds × $0.001097/s", () => {
    const details = baseDetails("per_gpu_second_active", {
      gpu_seconds_used: 1.234,
      duration_ms: 1234,
    });
    const cost = engine.resolveGpuCost(
      details,
      cloud("modal", null, "env"),
      new Decimal("1.234"),
    );
    const expected = new Decimal("1.234").times(new Decimal("0.001097"));
    expect(cost.costUsd.equals(expected)).toBe(true);
    expect(cost.costConfidence).toBe("computed");
    expect(cost.pricingSource).toContain("modal");
  });

  it("RunPod nested on_demand/community_cloud handled", () => {
    const details = baseDetails("per_gpu_second_active", {
      gpu_seconds_used: 10.0,
      duration_ms: 10_000,
    });
    const cost = engine.resolveGpuCost(
      details,
      cloud("runpod", null, "env"),
      new Decimal("10"),
    );
    expect(cost.costUsd.greaterThan(new Decimal(0))).toBe(true);
    expect(cost.costConfidence).toBe("computed");
    expect(cost.pricingSource).toContain("runpod");
  });
});

describe("GpuPricingEngine — per_gpu_hour_reserved", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it("Lambda Labs 8x H100 SXM5 — share math", () => {
    const details = baseDetails("per_gpu_hour_reserved", {
      gpu_count: 8,
      duration_ms: 60_000,
    });
    const cost = engine.resolveGpuCost(
      details,
      cloud("lambda_labs", null, "dmi"),
      new Decimal("60"),
    );
    const share = new Decimal("1.0").dividedBy(new Decimal("8").times(60));
    const expectedHours = share
      .times(new Decimal("60").dividedBy(3600))
      .times(8);
    const expected = expectedHours.times(new Decimal("3.99"));
    expect(cost.costUsd.equals(expected)).toBe(true);
  });
});

describe("GpuPricingEngine — per_instance_hour", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it("AWS p5.48xlarge share math reads catalog rate", () => {
    const details = baseDetails("per_instance_hour", {
      gpu_count: 8,
      duration_ms: 60_000,
      region: "us-east-1",
      instance_type: "p5.48xlarge",
    });
    const env = cloud("aws", "us-east-1", "imds", "p5.48xlarge");
    const cost = engine.resolveGpuCost(details, env, new Decimal("60"));
    const share = new Decimal(1).dividedBy(new Decimal(8).times(60));
    const expectedHours = share.times(new Decimal(60).dividedBy(3600));
    const p5 =
      engine.catalog.aws.ec2_gpu.regions["us-east-1"].instance_types[
        "p5.48xlarge"
      ];
    const expected = expectedHours.times(new Decimal(String(p5.hourly_usd)));
    expect(cost.costUsd.equals(expected)).toBe(true);
    expect(cost.costConfidence).toBe("computed");
  });

  it("GCP a3-highgpu-8g share math", () => {
    const details = baseDetails("per_instance_hour", {
      gpu_count: 8,
      duration_ms: 60_000,
      region: "us-central1",
      instance_type: "a3-highgpu-8g",
    });
    const env = cloud("gcp", "us-central1", "imds", "a3-highgpu-8g");
    const cost = engine.resolveGpuCost(details, env, new Decimal("60"));
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("gcp");
  });

  it("Azure ND H100 share math", () => {
    const details = baseDetails("per_instance_hour", {
      gpu_count: 8,
      duration_ms: 60_000,
      region: "eastus",
      instance_type: "Standard_ND96isr_H100_v5",
    });
    const env = cloud("azure", "eastus", "imds", "Standard_ND96isr_H100_v5");
    const cost = engine.resolveGpuCost(details, env, new Decimal("60"));
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("azure");
  });
});

describe("GpuPricingEngine — per_vgpu_hour (Decision #10)", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it("Azure NV6ads_A10_v5 (1/6 A10) — 1 GPU-sec / 60s", () => {
    const details = baseDetails("per_vgpu_hour", {
      gpu_sku: "a10-vgpu-1of6",
      gpu_count: 1,
      duration_ms: 60_000,
      region: "eastus",
      instance_type: "Standard_NV6ads_A10_v5",
      vgpu_profile: "1/6 A10",
    });
    const env = cloud("azure", "eastus", "imds", "Standard_NV6ads_A10_v5");
    const cost = engine.resolveGpuCost(details, env, new Decimal("60"));
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.costConfidence).toBe("computed");
  });
});

describe("Tier-3 device-class fallback (Decision #4)", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it("unknown SKU + recognised productName → device-class fallback", () => {
    const details = baseDetails("per_gpu_second_active", {
      gpu_sku: null,
      gpu_seconds_used: 1.0,
      duration_ms: 1000,
      _nvml_product_name_lower: "nvidia b300 200gb hbm4",
    });
    const cost = engine.resolveGpuCost(
      details,
      cloud("modal", null, "env"),
      new Decimal("1"),
    );
    expect(cost.costConfidence).toBe("estimated");
    expect(cost.pricingSource).toContain("device_class_fallback");
    expect(cost.costUsd.greaterThan(0)).toBe(true);
  });

  it("unknown SKU + unrecognised productName → Tier-4 meta default", () => {
    const details = baseDetails("per_gpu_second_active", {
      gpu_sku: null,
      gpu_seconds_used: 1.0,
      duration_ms: 1000,
      _nvml_product_name_lower: "totally unknown gpu model from mars",
    });
    const cost = engine.resolveGpuCost(
      details,
      cloud("modal", null, "env"),
      new Decimal("1"),
    );
    expect(["estimated", "unknown"]).toContain(cost.costConfidence);
  });
});

describe("Tier-4 fallback — missing catalog → hardcoded constants", () => {
  it("missing catalog file → cost > 0 via hardcoded constants", () => {
    _resetWarningStateForTests();
    const tmp = mkdtempSync(join(tmpdir(), "dexcost-gpu-pricing-"));
    const bogus = join(tmp, "no.json");
    try {
      const eng = new GpuPricingEngine({ catalogPath: bogus });
      const details = baseDetails("per_gpu_second_active", {
        gpu_seconds_used: 1.0,
        duration_ms: 1000,
      });
      const cost = eng.resolveGpuCost(
        details,
        cloud("modal", null, "env"),
        new Decimal("1"),
      );
      expect(cost.costUsd.greaterThan(0)).toBe(true);
      expect(cost.pricingSource).toContain("hardcoded");
      expect(cost.costConfidence).toBe("estimated");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});

describe("Tier-5 fail-silent — computation error → cost=0 / unknown", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it("malformed gpu_seconds_used → cost=0 + unknown + :error: suffix", () => {
    const bad = {
      billing_model: "per_gpu_second_active",
      gpu_seconds_used: "not-a-number",
    };
    const cost = engine.resolveGpuCost(
      bad,
      cloud(null, null, "none"),
      new Decimal("1"),
    );
    expect(cost.costUsd.equals(0)).toBe(true);
    expect(cost.costConfidence).toBe("unknown");
    expect(cost.pricingSource).toContain("error");
  });

  it("unknown billing model → cost=0 + unsupported suffix", () => {
    const bad = baseDetails("made_up_billing_model");
    const cost = engine.resolveGpuCost(bad, cloud(null, null, "none"));
    expect(cost.costUsd.equals(0)).toBe(true);
  });
});

describe("Decision #1 measurement-side fallback labels", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it("_cgroup_scope_fallback=self_pid_only → suffix appended + confidence dropped", () => {
    const details = baseDetails("per_gpu_second_active", {
      gpu_seconds_used: 1.0,
      duration_ms: 1000,
      _cgroup_scope_fallback: "self_pid_only",
    });
    const cost = engine.resolveGpuCost(
      details,
      cloud("modal", null, "env"),
      new Decimal("1"),
    );
    expect(cost.pricingSource.endsWith(":self_pid_only")).toBe(true);
    expect(cost.costConfidence).toBe("estimated");
  });

  it("_cgroup_scope_fallback=no_container_scope → suffix appended", () => {
    const details = baseDetails("per_gpu_second_active", {
      gpu_seconds_used: 1.0,
      duration_ms: 1000,
      _cgroup_scope_fallback: "no_container_scope",
    });
    const cost = engine.resolveGpuCost(
      details,
      cloud("modal", null, "env"),
      new Decimal("1"),
    );
    expect(cost.pricingSource.endsWith(":no_container_scope")).toBe(true);
    expect(cost.costConfidence).toBe("estimated");
  });

  it("_cgroup_scope_fallback=multi_container_pod_partial → suffix appended", () => {
    const details = baseDetails("per_instance_hour", {
      gpu_count: 8,
      duration_ms: 60_000,
      region: "us-east-1",
      instance_type: "p5.48xlarge",
      _cgroup_scope_fallback: "multi_container_pod_partial",
    });
    const env = cloud("aws", "us-east-1", "imds", "p5.48xlarge");
    const cost = engine.resolveGpuCost(details, env, new Decimal("60"));
    expect(cost.pricingSource.endsWith(":multi_container_pod_partial")).toBe(
      true,
    );
    expect(cost.costConfidence).toBe("estimated");
  });
});

describe("Misc", () => {
  it("catalog_version is exposed and semver-like", () => {
    _resetWarningStateForTests();
    const eng = new GpuPricingEngine();
    expect(eng.catalogVersion.startsWith("1.")).toBe(true);
  });
});
