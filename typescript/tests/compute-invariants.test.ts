/**
 * Compute pricing property invariants — spec §10.3.
 *
 * Must hold across arbitrary task shapes (billing_model × region ×
 * architecture × duration × memory × vcpu). Parametrized.
 *
 * Ports python/tests/test_compute_invariants.py to vitest.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import Decimal from "decimal.js";
import {
  ComputePricingEngine,
  _resetWarningStateForTests,
} from "../src/pricing/compute-pricing.js";
import type { CloudEnv } from "../src/cloud-detect.js";

const ALL_BILLING_MODELS = [
  "lambda",
  "fargate",
  "cloud_run_request",
  "cloud_run_instance",
  "cloud_functions",
  "azure_functions",
  "vercel_fluid",
  "ec2",
  "gce",
  "azure_vm",
  "k8s_pod",
] as const;

function envFor(
  provider = "aws",
  region = "us-east-1",
  instanceType: string | null = "c7g.xlarge",
): CloudEnv {
  return { provider, region, source: "env", instanceType };
}

function baseDetails(billingModel: string): Record<string, any> {
  return {
    billing_model: billingModel,
    duration_ms: 1000,
    memory_bytes_limit: 512 * 1024 * 1024,
    vcpu_count: 1.0,
    vcpu_seconds_used: 0.5,
    invocation_count: 1,
    region: "us-east-1",
    architecture: "x86_64",
  };
}

let engine: ComputePricingEngine;

beforeEach(() => {
  _resetWarningStateForTests();
  engine = new ComputePricingEngine();
});

afterEach(() => {
  _resetWarningStateForTests();
});

describe("Invariant 1 — cost is never negative", () => {
  it.each(ALL_BILLING_MODELS)("%s — cost_usd >= 0", (billingModel) => {
    const cost = engine.resolveComputeCost(
      baseDetails(billingModel),
      envFor(),
      {},
      new Decimal(1),
    );
    expect(cost.costUsd.gte(0)).toBe(true);
  });
});

describe("Invariant 3 — linearity in duration", () => {
  it.each(["lambda", "azure_functions"])(
    "%s — doubling duration increases gb-second portion",
    (billingModel) => {
      const base = baseDetails(billingModel);
      const a = engine.resolveComputeCost({ ...base, duration_ms: 100 }, envFor(), {});
      const b = engine.resolveComputeCost({ ...base, duration_ms: 200 }, envFor(), {});
      expect(b.costUsd.gt(a.costUsd)).toBe(true);
    },
  );
});

describe("Invariant 4 — ARM cheaper than x86", () => {
  it("Lambda — arm < x86", () => {
    const base = baseDetails("lambda");
    const x86 = engine.resolveComputeCost(
      { ...base, architecture: "x86_64" },
      envFor(),
      {},
    );
    const arm = engine.resolveComputeCost(
      { ...base, architecture: "arm64" },
      envFor(),
      {},
    );
    expect(arm.costUsd.lt(x86.costUsd)).toBe(true);
  });

  it("Fargate — arm < x86", () => {
    const base = baseDetails("fargate");
    const x86 = engine.resolveComputeCost(
      { ...base, architecture: "x86_64" },
      envFor(),
      {},
      new Decimal(1),
    );
    const arm = engine.resolveComputeCost(
      { ...base, architecture: "arm64" },
      envFor(),
      {},
      new Decimal(1),
    );
    expect(arm.costUsd.lt(x86.costUsd)).toBe(true);
  });
});

describe("Invariant 5 — well-formed input never yields unknown confidence", () => {
  it.each(ALL_BILLING_MODELS)("%s — confidence ∈ {computed, estimated}", (billingModel) => {
    const cost = engine.resolveComputeCost(
      baseDetails(billingModel),
      envFor(),
      {},
      new Decimal(1),
    );
    expect(["computed", "estimated"]).toContain(cost.costConfidence);
  });
});

describe("Invariant 6 — pricing_source namespace", () => {
  it.each(ALL_BILLING_MODELS)("%s — starts with 'compute_catalog:'", (billingModel) => {
    const cost = engine.resolveComputeCost(
      baseDetails(billingModel),
      envFor(),
      {},
      new Decimal(1),
    );
    expect(cost.pricingSource.startsWith("compute_catalog:")).toBe(true);
  });
});

describe("Invariant 3 — linearity in memory (Fargate)", () => {
  it.each([1, 2, 4, 16, 64])("memory %i GiB", (memoryGib) => {
    const details = baseDetails("fargate");
    details.memory_bytes_limit = memoryGib * 1024 * 1024 * 1024;
    const cost = engine.resolveComputeCost(details, envFor(), {}, new Decimal(1));
    // vcpu_term + gib_term*memory_gib at us-east-1 x86_64
    const vcpuTerm = new Decimal("1").times("1").times("0.0000112444");
    const gibTerm = new Decimal(memoryGib).times("1").times("0.0000012347");
    expect(cost.costUsd.equals(vcpuTerm.plus(gibTerm))).toBe(true);
  });
});
