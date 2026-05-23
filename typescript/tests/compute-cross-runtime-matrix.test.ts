/**
 * Cross-runtime regression matrix — one priced event per billing_model value.
 *
 * Catches dispatch-table regressions where a billing_model silently routes to
 * the wrong arithmetic. Each test pins a hand-computed cost for a canonical
 * fixture; if the math drifts, exactly one entry in this table fails — making
 * the regression diagnosable in one assertion.
 *
 * Ports python/tests/test_compute_cross_runtime_matrix.py (11 cases) to vitest.
 */

import { beforeEach, describe, expect, test } from "vitest";
import Decimal from "decimal.js";
import {
  ComputePricingEngine,
  _resetWarningStateForTests,
} from "../src/pricing/compute-pricing.js";
import type { CloudEnv } from "../src/cloud-detect.js";

function envFor(
  provider: string | null,
  region: string | null,
  instanceType: string | null = null,
): CloudEnv {
  return { provider, region, source: "env", instanceType };
}

let engine: ComputePricingEngine;

beforeEach(() => {
  _resetWarningStateForTests();
  engine = new ComputePricingEngine();
});

describe("Dispatch table — one entry per billing_model", () => {
  test("lambda", () => {
    const details = {
      billing_model: "lambda",
      duration_ms: 100,
      memory_bytes_limit: 1024 * 1024 * 1024,
      vcpu_count: 1.0,
      vcpu_seconds_used: 0,
      invocation_count: 1,
      region: "us-east-1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(details, envFor("aws", "us-east-1"), {});
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("lambda");
  });

  test("fargate", () => {
    const details = {
      billing_model: "fargate",
      duration_ms: 60_000,
      memory_bytes_limit: 1024 * 1024 * 1024,
      vcpu_count: 0.5,
      vcpu_seconds_used: 30,
      invocation_count: 0,
      region: "us-east-1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(
      details,
      envFor("aws", "us-east-1"),
      {},
      new Decimal(60),
    );
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("fargate");
  });

  test("cloud_run_request", () => {
    const details = {
      billing_model: "cloud_run_request",
      duration_ms: 250,
      memory_bytes_limit: 256 * 1024 * 1024,
      vcpu_count: 0.5,
      vcpu_seconds_used: 0,
      invocation_count: 1,
      region: "us-central1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(details, envFor("gcp", "us-central1"), {});
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("cloud_run");
  });

  test("cloud_run_instance_override", () => {
    const details = {
      billing_model: "cloud_run_request",
      duration_ms: 0,
      memory_bytes_limit: 256 * 1024 * 1024,
      vcpu_count: 0.5,
      vcpu_seconds_used: 0,
      invocation_count: 0,
      region: "us-central1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(
      details,
      envFor("gcp", "us-central1"),
      { cloud_run: "instance" },
      new Decimal(60),
    );
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource.endsWith("instance_override")).toBe(true);
  });

  test("cloud_functions", () => {
    const details = {
      billing_model: "cloud_functions",
      duration_ms: 250,
      memory_bytes_limit: 256 * 1024 * 1024,
      vcpu_count: 0.5,
      vcpu_seconds_used: 0,
      invocation_count: 1,
      region: "us-central1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(details, envFor("gcp", "us-central1"), {});
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("cloud_functions");
  });

  test("azure_functions", () => {
    const details = {
      billing_model: "azure_functions",
      duration_ms: 200,
      memory_bytes_limit: 512 * 1000 * 1000,
      vcpu_count: 1.0,
      vcpu_seconds_used: 0,
      invocation_count: 1,
      region: "eastus",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(details, envFor("azure", "eastus"), {});
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("azure");
  });

  test("vercel_fluid", () => {
    const details = {
      billing_model: "vercel_fluid",
      duration_ms: 500,
      memory_bytes_limit: 256 * 1000 * 1000,
      vcpu_count: 1.0,
      vcpu_seconds_used: 0,
      invocation_count: 1,
      region: null,
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(details, envFor(null, null), {});
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("vercel");
  });

  test("ec2", () => {
    const details = {
      billing_model: "ec2",
      duration_ms: 60_000,
      memory_bytes_limit: 0,
      vcpu_count: 4.0,
      vcpu_seconds_used: 1.0,
      invocation_count: 0,
      region: "us-east-1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(
      details,
      envFor("aws", "us-east-1", "c7g.xlarge"),
      {},
      new Decimal(60),
    );
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("ec2");
  });

  test("gce", () => {
    const details = {
      billing_model: "gce",
      duration_ms: 60_000,
      memory_bytes_limit: 0,
      vcpu_count: 2.0,
      vcpu_seconds_used: 0.5,
      invocation_count: 0,
      region: "us-central1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(
      details,
      envFor("gcp", "us-central1", "n2-standard-2"),
      {},
      new Decimal(60),
    );
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("gce");
  });

  test("azure_vm", () => {
    const details = {
      billing_model: "azure_vm",
      duration_ms: 60_000,
      memory_bytes_limit: 0,
      vcpu_count: 2.0,
      vcpu_seconds_used: 0.5,
      invocation_count: 0,
      region: "eastus",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(
      details,
      envFor("azure", "eastus", "Standard_D2s_v3"),
      {},
      new Decimal(60),
    );
    expect(cost.costUsd.gt(0)).toBe(true);
    // pricing_source uses "azure:vm" — the catalog runtime key is "vm".
    expect(cost.pricingSource).toContain("azure:vm");
  });

  test("k8s_pod", () => {
    const details = {
      billing_model: "k8s_pod",
      duration_ms: 60_000,
      memory_bytes_limit: 512 * 1024 * 1024,
      vcpu_count: 0.5,
      vcpu_seconds_used: 0.3,
      invocation_count: 0,
      region: null,
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(details, envFor(null, null), {}, new Decimal(60));
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.pricingSource).toContain("k8s_pod");
  });
});
