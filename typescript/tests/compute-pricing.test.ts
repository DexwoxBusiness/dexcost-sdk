/**
 * Compute pricing — per-billing-model math, degradation ladder, no-float-drift.
 *
 * Ports python/tests/test_compute_pricing.py (16 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import Decimal from "decimal.js";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  ComputePricingEngine,
  _resetWarningStateForTests,
} from "../src/pricing/compute-pricing.js";
import type { CloudEnv } from "../src/cloud-detect.js";

beforeEach(() => {
  _resetWarningStateForTests();
});

afterEach(() => {
  _resetWarningStateForTests();
});

function makeEnv(
  provider: string | null = "aws",
  region: string | null = "us-east-1",
  instanceType: string | null = null,
): CloudEnv {
  return { provider, region, source: "env", instanceType };
}

function makeEngine(): ComputePricingEngine {
  return new ComputePricingEngine();
}

// ─── Lambda ──────────────────────────────────────────────────────────────────

describe("Lambda", () => {
  test("x86 canonical case", () => {
    const engine = makeEngine();
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
    const cost = engine.resolveComputeCost(details, makeEnv(), {});
    const gbSeconds = new Decimal(1024 * 1024 * 1024)
      .dividedBy(new Decimal("1000000000"))
      .times(new Decimal("0.1"));
    const expected = new Decimal("0.0000002").plus(gbSeconds.times(new Decimal("0.0000166667")));
    expect(cost.costUsd.equals(expected)).toBe(true);
    expect(cost.costConfidence).toBe("computed");
    expect(cost.pricingSource).toBe("compute_catalog:aws:lambda:us-east-1:x86_64");
  });

  test("ARM is cheaper", () => {
    const engine = makeEngine();
    const base = {
      billing_model: "lambda",
      duration_ms: 100,
      memory_bytes_limit: 1024 * 1024 * 1024,
      vcpu_count: 1.0,
      vcpu_seconds_used: 0,
      invocation_count: 1,
      region: "us-east-1",
    };
    const x86 = engine.resolveComputeCost({ ...base, architecture: "x86_64" }, makeEnv(), {});
    const arm = engine.resolveComputeCost({ ...base, architecture: "arm64" }, makeEnv(), {});
    expect(arm.costUsd.lt(x86.costUsd)).toBe(true);
  });
});

// ─── Fargate (the binary GiB bug-prevention test) ─────────────────────────────

describe("Fargate", () => {
  test("uses BINARY GiB divisor (Decision #7 — pins ~4.86% over-attribution bug)", () => {
    const engine = makeEngine();
    const details = {
      billing_model: "fargate",
      duration_ms: 60_000,
      memory_bytes_limit: 1024 * 1024 * 1024, // exactly 1 GiB
      vcpu_count: 0.5,
      vcpu_seconds_used: 30,
      invocation_count: 0,
      region: "us-east-1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(details, makeEnv(), {}, new Decimal("60"));
    const vcpuTerm = new Decimal("0.5").times("60").times("0.0000112444");
    const gibTerm = new Decimal("1").times("60").times("0.0000012347");
    expect(cost.costUsd.equals(vcpuTerm.plus(gibTerm))).toBe(true);
  });
});

// ─── Cloud Run ───────────────────────────────────────────────────────────────

describe("Cloud Run", () => {
  test("default is estimated", () => {
    const engine = makeEngine();
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
    const cost = engine.resolveComputeCost(details, makeEnv("gcp", "us-central1"), {});
    expect(cost.costConfidence).toBe("estimated");
    expect(cost.pricingSource).toBe("compute_catalog:cloud_run:request_based_default");
  });

  test("instance override is computed", () => {
    const engine = makeEngine();
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
      makeEnv("gcp", "us-central1"),
      { cloud_run: "instance" },
      new Decimal("60"),
    );
    expect(cost.costConfidence).toBe("computed");
    expect(cost.pricingSource.endsWith("instance_override")).toBe(true);
  });
});

// ─── Azure Functions ─────────────────────────────────────────────────────────

describe("Azure Functions", () => {
  test("canonical case", () => {
    const engine = makeEngine();
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
    const cost = engine.resolveComputeCost(details, makeEnv("azure", "eastus"), {});
    const gbSeconds = new Decimal(512 * 1000 * 1000)
      .dividedBy("1000000000")
      .times("0.2");
    const expected = new Decimal("0.0000002").plus(gbSeconds.times("0.000016"));
    expect(cost.costUsd.equals(expected)).toBe(true);
  });
});

// ─── Vercel ──────────────────────────────────────────────────────────────────

describe("Vercel Fluid", () => {
  test("active CPU approximates wall duration", () => {
    const engine = makeEngine();
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
    const cost = engine.resolveComputeCost(details, makeEnv(null, null), {});
    expect(cost.costUsd.gt(0)).toBe(true);
    expect(cost.costConfidence).toBe("computed");
  });
});

// ─── EC2 instance share ──────────────────────────────────────────────────────

describe("EC2 instance share", () => {
  test("share-factor math", () => {
    const engine = makeEngine();
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
      makeEnv("aws", "us-east-1", "c7g.xlarge"),
      {},
      new Decimal("60"),
    );
    const expectedShare = new Decimal("1").dividedBy(new Decimal("4").times("60"));
    const expectedHours = expectedShare.times(new Decimal("60").dividedBy("3600"));
    const expected = expectedHours.times(new Decimal("0.1450"));
    expect(cost.costUsd.equals(expected)).toBe(true);
  });
});

// ─── K8s pod default (no node-aware) ─────────────────────────────────────────

describe("K8s pod (limits × duration × hourly default)", () => {
  test("math", () => {
    const engine = makeEngine();
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
    const cost = engine.resolveComputeCost(details, makeEnv(null, null), {}, new Decimal("60"));
    const expected = new Decimal("0.5")
      .times(new Decimal("60").dividedBy("3600"))
      .times("0.0464");
    expect(cost.costUsd.equals(expected)).toBe(true);
    expect(cost.costConfidence).toBe("computed");
  });
});

// ─── Degradation ladder ──────────────────────────────────────────────────────

describe("Degradation ladder", () => {
  test("Tier-2: unknown region falls to runtime default", () => {
    const engine = makeEngine();
    const details = {
      billing_model: "lambda",
      duration_ms: 100,
      memory_bytes_limit: 128 * 1000 * 1000,
      vcpu_count: 1.0,
      vcpu_seconds_used: 0,
      invocation_count: 1,
      region: null,
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(details, makeEnv(null, null), {});
    expect(cost.costConfidence).toBe("estimated");
    expect(cost.pricingSource).toBe("compute_catalog:aws:lambda:default:x86_64");
  });

  test("Tier-4: missing catalog uses HARDCODED", () => {
    const tmp = mkdtempSync(join(tmpdir(), "dexcost-cp-"));
    try {
      const bogus = join(tmp, "no.json");
      const eng = new ComputePricingEngine({ catalogPath: bogus });
      const details = {
        billing_model: "lambda",
        duration_ms: 100,
        memory_bytes_limit: 128 * 1000 * 1000,
        vcpu_count: 1.0,
        vcpu_seconds_used: 0,
        invocation_count: 1,
        region: "us-east-1",
        architecture: "x86_64",
      };
      const cost = eng.resolveComputeCost(details, makeEnv(), {});
      expect(cost.costUsd.gt(0)).toBe(true);
      expect(cost.pricingSource.startsWith("compute_catalog:hardcoded")).toBe(true);
      expect(cost.costConfidence).toBe("estimated");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  test("Tier-5: computation failure returns zero", () => {
    const engine = makeEngine();
    const bad = { billing_model: "lambda", duration_ms: "not-a-number" };
    const cost = engine.resolveComputeCost(bad as any, makeEnv(), {});
    expect(cost.costUsd.equals(0)).toBe(true);
  });

  test("Unknown billing_model returns zero", () => {
    const engine = makeEngine();
    const bad = {
      billing_model: "totally_made_up",
      duration_ms: 100,
      memory_bytes_limit: 0,
      vcpu_count: 1.0,
      vcpu_seconds_used: 0,
      invocation_count: 0,
      region: "us-east-1",
      architecture: "x86_64",
    };
    const cost = engine.resolveComputeCost(bad, makeEnv(), {});
    expect(cost.costUsd.equals(0)).toBe(true);
  });
});

// ─── No-float-drift ──────────────────────────────────────────────────────────

describe("No-float-drift (Decision #7 pinned divisors)", () => {
  test("decimals stay Decimal, never coerce through float", () => {
    // Fargate / Cloud Run — binary GiB.
    expect(
      new Decimal(2 * 1024 * 1024 * 1024).dividedBy(1024 * 1024 * 1024).equals(2),
    ).toBe(true);
    // Lambda / Azure Functions / Vercel — decimal GB.
    expect(
      new Decimal(2 * 1000 * 1000 * 1000).dividedBy("1000000000").equals(2),
    ).toBe(true);
    // Multiplication step against hand-computed expected:
    expect(
      new Decimal("0.0000166667").times("1024").equals(new Decimal("0.0170667008")),
    ).toBe(true);
  });
});

// ─── Warning state ───────────────────────────────────────────────────────────

describe("Warn-once + catalog version", () => {
  test("warn-once per failure mode", () => {
    const tmp = mkdtempSync(join(tmpdir(), "dexcost-cp-"));
    try {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const bogus = join(tmp, "missing.json");
      // Two engines, same missing path → one log only (warn-once).
      new ComputePricingEngine({ catalogPath: bogus });
      new ComputePricingEngine({ catalogPath: bogus });
      const msgs = warn.mock.calls.filter((args) =>
        args.some(
          (a) => typeof a === "string" && a.toLowerCase().includes("compute catalog"),
        ),
      );
      expect(msgs.length).toBe(1);
      warn.mockRestore();
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  test("catalog_version exposed", () => {
    const engine = makeEngine();
    expect(engine.catalogVersion.startsWith("1.")).toBe(true);
  });
});
