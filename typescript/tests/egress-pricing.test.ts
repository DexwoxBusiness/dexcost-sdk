/**
 * Egress pricing resolver — every tier of the §7.1 ladder.
 *
 * Ported from python/tests/test_egress_pricing.py (12 cases). The TS port
 * preserves rates as strings; the float-drift test asserts string equality
 * of known catalog rates (the equivalent precision guarantee in number-land).
 */

import { beforeEach, afterEach, describe, expect, test, vi } from "vitest";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  EgressPricingEngine,
  _resetEgressWarningStateForTests,
} from "../src/pricing/egress-pricing.js";
import egressCatalog from "../src/data/egress_prices.json" with { type: "json" };

beforeEach(() => {
  _resetEgressWarningStateForTests();
});

afterEach(() => {
  _resetEgressWarningStateForTests();
});

function freshEngine(): EgressPricingEngine {
  // Default constructor uses the bundled catalog.
  return new EgressPricingEngine();
}

describe("EgressPricingEngine — §7.1 ladder", () => {
  test("tier 1: region match is computed", () => {
    const e = freshEngine();
    const r = e.resolveRate("aws", "us-east-1");
    expect(r.ratePerGb).toBe("0.09");
    expect(r.pricingSource).toBe("egress_catalog:aws:us-east-1");
    expect(r.costConfidence).toBe("computed");
  });

  test("tier 2: provider known, region missing is estimated", () => {
    const e = freshEngine();
    const r = e.resolveRate("aws", "moon-base-1");
    expect(r.ratePerGb).toBe("0.09");
    expect(r.pricingSource).toBe("egress_catalog:aws:default");
    expect(r.costConfidence).toBe("estimated");
  });

  test("tier 3: unknown provider falls to meta default", () => {
    const e = freshEngine();
    const r = e.resolveRate(null, null);
    expect(r.ratePerGb).toBe("0.09");
    expect(r.pricingSource).toBe("egress_catalog:default");
    expect(r.costConfidence).toBe("estimated");
  });

  test("internal traffic is free and exact", () => {
    const e = freshEngine();
    const r = e.rateForInternal();
    expect(r.ratePerGb).toBe("0");
    expect(r.pricingSource).toBe("egress_catalog:internal");
    expect(r.costConfidence).toBe("exact");
  });

  test("tier 4: missing catalog falls to hardcoded", () => {
    const tmp = mkdtempSync(join(tmpdir(), "dexcost-egress-"));
    const bogus = join(tmp, "no.json");
    const eng = EgressPricingEngine.fromPath(bogus);
    const r = eng.resolveRate("aws", "us-east-1");
    expect(r.ratePerGb).toBe("0.09");
    expect(r.costConfidence).toBe("estimated");
  });

  test("tier 4: malformed catalog falls to hardcoded", () => {
    const tmp = mkdtempSync(join(tmpdir(), "dexcost-egress-"));
    const bad = join(tmp, "bad.json");
    writeFileSync(bad, "{not json");
    const eng = EgressPricingEngine.fromPath(bad);
    const r = eng.resolveRate("aws", "us-east-1");
    expect(r.ratePerGb).toBe("0.09");
    expect(r.costConfidence).toBe("estimated");
  });

  test("tier 4: meta default missing falls to hardcoded", () => {
    const tmp = mkdtempSync(join(tmpdir(), "dexcost-egress-"));
    const bad = join(tmp, "no_meta_default.json");
    writeFileSync(
      bad,
      JSON.stringify({ _meta: { version: "x", currency: "USD" } }),
    );
    const eng = EgressPricingEngine.fromPath(bad);
    const r = eng.resolveRate(null, null);
    expect(r.ratePerGb).toBe("0.09");
    expect(r.costConfidence).toBe("estimated");
  });

  test("warn-once per failure mode", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const tmp = mkdtempSync(join(tmpdir(), "dexcost-egress-"));
      const bogus = join(tmp, "missing.json");
      EgressPricingEngine.fromPath(bogus);
      EgressPricingEngine.fromPath(bogus);
      // The second call should NOT emit a new warning for the same mode.
      const catalogCalls = warn.mock.calls.filter((args) =>
        String(args[0]).toLowerCase().includes("catalog"),
      );
      expect(catalogCalls.length).toBe(1);
    } finally {
      warn.mockRestore();
    }
  });

  test("warn distinct modes independently", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const tmp = mkdtempSync(join(tmpdir(), "dexcost-egress-"));
      const missing = join(tmp, "missing.json");
      const malformed = join(tmp, "bad.json");
      writeFileSync(malformed, "{");
      EgressPricingEngine.fromPath(missing);
      EgressPricingEngine.fromPath(malformed);
      const msgs = warn.mock.calls.map((args) =>
        String(args[0]).toLowerCase(),
      );
      expect(msgs.some((m) => m.includes("not found"))).toBe(true);
      expect(msgs.some((m) => m.includes("malformed"))).toBe(true);
    } finally {
      warn.mockRestore();
    }
  });

  test("no float drift in stored catalog data (string preservation)", () => {
    // The Python suite asserts Decimal exactness; in TS we preserve the
    // string form, which is the equivalent guarantee for stored data.
    // These known catalog values must round-trip unchanged.
    const cat = egressCatalog as unknown as {
      aws: { regions: Record<string, string> };
      azure: { regions: Record<string, string> };
    };
    expect(cat.aws.regions["ap-south-1"]).toBe("0.1093");
    expect(cat.azure.regions["westus"]).toBe("0.087");

    // Also verify the resolver returns the exact catalog string.
    const e = freshEngine();
    expect(e.resolveRate("aws", "ap-south-1").ratePerGb).toBe("0.1093");
    expect(e.resolveRate("azure", "westus").ratePerGb).toBe("0.087");
  });

  test("catalog version comes from _meta.version", () => {
    const e = freshEngine();
    expect(e.catalogVersion).toBe("1.0.0");
  });

  test("EgressRate is shallowly immutable in practice", () => {
    // TS doesn't have Python's frozen-dataclass; we assert that re-resolving
    // returns a new object whose fields match the contract, not aliasing.
    const e = freshEngine();
    const r1 = e.resolveRate("aws", "us-east-1");
    const r2 = e.resolveRate("aws", "us-east-1");
    expect(r1).toEqual(r2);
    expect(r1).not.toBe(r2); // Distinct object refs.
  });
});

describe("EgressPricingEngine — passes through an in-memory catalog", () => {
  test("ctor accepts a parsed object", () => {
    const eng = new EgressPricingEngine({
      _meta: { version: "test-1", default_rate_usd_per_gb: "0.05" },
      myprov: { default_usd_per_gb: "0.07", regions: { "r-1": "0.11" } },
    });
    expect(eng.catalogVersion).toBe("test-1");
    const t1 = eng.resolveRate("myprov", "r-1");
    expect(t1.ratePerGb).toBe("0.11");
    expect(t1.pricingSource).toBe("egress_catalog:myprov:r-1");
    expect(t1.costConfidence).toBe("computed");

    const t2 = eng.resolveRate("myprov", null);
    expect(t2.ratePerGb).toBe("0.07");
    expect(t2.costConfidence).toBe("estimated");

    const t3 = eng.resolveRate("otherprov", "any");
    expect(t3.ratePerGb).toBe("0.05");
    expect(t3.pricingSource).toBe("egress_catalog:default");
  });
});

// Reference: this line exists to ensure mkdirSync stays imported in case
// future tests need it (no-op).
void mkdirSync;
