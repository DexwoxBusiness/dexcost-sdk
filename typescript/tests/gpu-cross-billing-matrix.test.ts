/**
 * Cross-billing-model matrix — one canonical case per dispatch discriminator.
 * Mirrors python/tests/test_gpu_cross_billing_model_matrix.py.
 *
 * If a future refactor accidentally routes per_vgpu_hour through the
 * per_instance_hour math (or any mis-wire), at least one test fails with
 * a specific billing_model in the failure message.
 */

import { describe, it, expect, beforeEach } from "vitest";
import Decimal from "decimal.js";
import {
  GpuPricingEngine,
  _resetWarningStateForTests,
} from "../src/pricing/gpu-pricing.js";
import type { CloudEnv } from "../src/cloud-detect.js";

function cloud(
  provider: string | null,
  region: string | null,
  source: "env" | "imds" | "dmi" | "none",
  instanceType?: string | null,
): CloudEnv {
  return { provider, region, source, instanceType: instanceType ?? null };
}

describe("GPU cross-billing-model dispatch matrix", () => {
  let engine: GpuPricingEngine;
  beforeEach(() => {
    _resetWarningStateForTests();
    engine = new GpuPricingEngine();
  });

  it("per_gpu_second_active → Modal", () => {
    const details = {
      billing_model: "per_gpu_second_active",
      gpu_vendor: "nvidia",
      gpu_sku: "h100-80gb-sxm5",
      gpu_count: 1,
      region: null,
      duration_ms: 1000,
      gpu_seconds_used: 1.0,
      instance_type: null,
      vgpu_profile: null,
      mig_profile: null,
    };
    const cost = engine.resolveGpuCost(
      details,
      cloud("modal", null, "env"),
      new Decimal("1"),
    );
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("modal");
    expect(cost.pricingSource).toContain("per_gpu_second_active");
  });

  it("per_instance_hour → AWS ec2_gpu", () => {
    const details = {
      billing_model: "per_instance_hour",
      gpu_vendor: "nvidia",
      gpu_sku: "h100-80gb-sxm5",
      gpu_count: 8,
      region: "us-east-1",
      duration_ms: 60_000,
      gpu_seconds_used: 1.0,
      instance_type: "p5.48xlarge",
      vgpu_profile: null,
      mig_profile: null,
    };
    const cost = engine.resolveGpuCost(
      details,
      cloud("aws", "us-east-1", "imds", "p5.48xlarge"),
      new Decimal("60"),
    );
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("aws");
    expect(cost.pricingSource).toContain("ec2_gpu");
  });

  it("per_instance_hour → GCP gce_gpu_bundled", () => {
    const details = {
      billing_model: "per_instance_hour",
      gpu_vendor: "nvidia",
      gpu_sku: "h100-80gb-sxm5",
      gpu_count: 8,
      region: "us-central1",
      duration_ms: 60_000,
      gpu_seconds_used: 1.0,
      instance_type: "a3-highgpu-8g",
      vgpu_profile: null,
      mig_profile: null,
    };
    const cost = engine.resolveGpuCost(
      details,
      cloud("gcp", "us-central1", "imds", "a3-highgpu-8g"),
      new Decimal("60"),
    );
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("gcp");
    expect(cost.pricingSource).toContain("gce_gpu_bundled");
  });

  it("per_instance_hour → Azure vm_gpu", () => {
    const details = {
      billing_model: "per_instance_hour",
      gpu_vendor: "nvidia",
      gpu_sku: "h100-80gb-sxm5",
      gpu_count: 8,
      region: "eastus",
      duration_ms: 60_000,
      gpu_seconds_used: 1.0,
      instance_type: "Standard_ND96isr_H100_v5",
      vgpu_profile: null,
      mig_profile: null,
    };
    const cost = engine.resolveGpuCost(
      details,
      cloud("azure", "eastus", "imds", "Standard_ND96isr_H100_v5"),
      new Decimal("60"),
    );
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("azure");
    expect(cost.pricingSource).toContain("vm_gpu");
  });

  it("per_gpu_hour_reserved → Lambda Labs", () => {
    const details = {
      billing_model: "per_gpu_hour_reserved",
      gpu_vendor: "nvidia",
      gpu_sku: "h100-80gb-sxm5",
      gpu_count: 8,
      region: null,
      duration_ms: 60_000,
      gpu_seconds_used: 1.0,
      instance_type: null,
      vgpu_profile: null,
      mig_profile: null,
    };
    const cost = engine.resolveGpuCost(
      details,
      cloud("lambda_labs", null, "dmi"),
      new Decimal("60"),
    );
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("lambda_labs");
    expect(cost.pricingSource).toContain("per_gpu_hour_reserved");
  });

  it("per_gpu_hour_reserved → CoreWeave", () => {
    const details = {
      billing_model: "per_gpu_hour_reserved",
      gpu_vendor: "nvidia",
      gpu_sku: "h100-80gb-sxm5",
      gpu_count: 8,
      region: null,
      duration_ms: 60_000,
      gpu_seconds_used: 1.0,
      instance_type: null,
      vgpu_profile: null,
      mig_profile: null,
    };
    const cost = engine.resolveGpuCost(
      details,
      cloud("coreweave", null, "dmi"),
      new Decimal("60"),
    );
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("coreweave");
  });

  it("per_vgpu_hour → Azure vm_vgpu", () => {
    const details = {
      billing_model: "per_vgpu_hour",
      gpu_vendor: "nvidia",
      gpu_sku: "a10-vgpu-1of6",
      gpu_count: 1,
      region: "eastus",
      duration_ms: 60_000,
      gpu_seconds_used: 1.0,
      instance_type: "Standard_NV6ads_A10_v5",
      vgpu_profile: "1/6 A10",
      mig_profile: null,
    };
    const cost = engine.resolveGpuCost(
      details,
      cloud("azure", "eastus", "imds", "Standard_NV6ads_A10_v5"),
      new Decimal("60"),
    );
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("azure");
    expect(cost.pricingSource).toContain("vm_vgpu");
  });

  it("Decision #9 — GCP N1 + attached accelerator via gce_gpu_attached", () => {
    const details = {
      billing_model: "per_gpu_hour_reserved",
      gpu_vendor: "nvidia",
      gpu_sku: "h100-80gb-sxm5",
      gpu_count: 1,
      region: "us-central1",
      duration_ms: 60_000,
      gpu_seconds_used: 1.0,
      instance_type: "n1-standard-8",
      vgpu_profile: null,
      mig_profile: null,
    };
    const cost = engine.resolveGpuCost(
      details,
      cloud("gcp", "us-central1", "imds", "n1-standard-8"),
      new Decimal("60"),
    );
    expect(cost.costUsd.greaterThan(0)).toBe(true);
    expect(cost.pricingSource).toContain("gcp");
    expect(cost.pricingSource).toContain("gce_gpu_attached");
  });
});
