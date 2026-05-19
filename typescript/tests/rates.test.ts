import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RateRegistry } from "../src/pricing/rates.js";

describe("RateRegistry", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "dexcost-rates-test-"));
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it("registers and retrieves a rate", () => {
    const registry = new RateRegistry();
    registry.register("maps.googleapis.com", "request", 0.005);
    const entry = registry.get("maps.googleapis.com");
    expect(entry).toBeDefined();
    expect(entry!.service).toBe("maps.googleapis.com");
    expect(entry!.per).toBe("request");
    expect(entry!.costUsd).toBe(0.005);
  });

  it("returns undefined for unregistered service", () => {
    const registry = new RateRegistry();
    expect(registry.get("nonexistent.service")).toBeUndefined();
  });

  it("overwrites existing rate on re-register", () => {
    const registry = new RateRegistry();
    registry.register("ocr-api.com", "page", 0.01);
    registry.register("ocr-api.com", "page", 0.02);
    const entry = registry.get("ocr-api.com");
    expect(entry!.costUsd).toBe(0.02);
  });

  it("computes deterministic pricing version (same rates in different order = same hash)", () => {
    const registry1 = new RateRegistry();
    registry1.register("service-a", "request", 0.001);
    registry1.register("service-b", "call", 0.002);

    const registry2 = new RateRegistry();
    registry2.register("service-b", "call", 0.002);
    registry2.register("service-a", "request", 0.001);

    expect(registry1.pricingVersion).toBe(registry2.pricingVersion);
    expect(registry1.pricingVersion).toHaveLength(12);
  });

  it("invalidates version on new registration", () => {
    const registry = new RateRegistry();
    registry.register("service-a", "request", 0.001);
    const v1 = registry.pricingVersion;
    registry.register("service-b", "call", 0.002);
    const v2 = registry.pricingVersion;
    expect(v1).not.toBe(v2);
  });

  it("returns all rates via rates getter", () => {
    const registry = new RateRegistry();
    registry.register("maps.googleapis.com", "request", 0.005);
    registry.register("ocr-api.com", "page", 0.01);
    const all = registry.rates;
    expect(Object.keys(all)).toHaveLength(2);
    expect(all["maps.googleapis.com"].costUsd).toBe(0.005);
    expect(all["ocr-api.com"].costUsd).toBe(0.01);
    // Verify it's a copy — mutating the returned object does not affect the registry
    delete (all as Record<string, unknown>)["ocr-api.com"];
    expect(Object.keys(registry.rates)).toHaveLength(2);
  });

  it("loads rates from YAML file", () => {
    const yamlContent = `rates:
  maps.googleapis.com:
    per: request
    cost_usd: "0.005"
  ocr-api.com:
    per: page
    cost_usd: "0.01"
`;
    const filePath = join(tmpDir, "rates.yaml");
    const { writeFileSync } = require("node:fs");
    writeFileSync(filePath, yamlContent, "utf-8");

    const registry = new RateRegistry();
    registry.load(filePath);

    const mapsEntry = registry.get("maps.googleapis.com");
    expect(mapsEntry).toBeDefined();
    expect(mapsEntry!.per).toBe("request");
    expect(mapsEntry!.costUsd).toBe(0.005);

    const ocrEntry = registry.get("ocr-api.com");
    expect(ocrEntry).toBeDefined();
    expect(ocrEntry!.per).toBe("page");
    expect(ocrEntry!.costUsd).toBe(0.01);
  });

  it("exports rates to YAML file (verify by reload)", () => {
    const registry = new RateRegistry();
    registry.register("maps.googleapis.com", "request", 0.005);
    registry.register("ocr-api.com", "page", 0.01);

    const filePath = join(tmpDir, "exported.yaml");
    registry.export(filePath);

    const reloaded = new RateRegistry();
    reloaded.load(filePath);

    expect(reloaded.get("maps.googleapis.com")!.costUsd).toBe(0.005);
    expect(reloaded.get("maps.googleapis.com")!.per).toBe("request");
    expect(reloaded.get("ocr-api.com")!.costUsd).toBe(0.01);
    expect(reloaded.get("ocr-api.com")!.per).toBe("page");

    // Verify original and reloaded versions match
    expect(registry.pricingVersion).toBe(reloaded.pricingVersion);
  });

  it("throws on invalid YAML structure", () => {
    const yamlContent = `not_rates:
  something: else
`;
    const filePath = join(tmpDir, "bad.yaml");
    const { writeFileSync } = require("node:fs");
    writeFileSync(filePath, yamlContent, "utf-8");

    const registry = new RateRegistry();
    // A YAML without a 'rates' key that maps to a dict with cost_usd entries should throw
    // The registry will load zero entries from missing 'rates' key — test a genuinely bad structure:
    const badYamlContent = `rates: "this should be a mapping not a string"`;
    const badFilePath = join(tmpDir, "bad2.yaml");
    writeFileSync(badFilePath, badYamlContent, "utf-8");
    expect(() => registry.load(badFilePath)).toThrow();

    // Also test missing cost_usd
    const missingCostYaml = `rates:
  my-service:
    per: request
`;
    const missingCostPath = join(tmpDir, "missing-cost.yaml");
    writeFileSync(missingCostPath, missingCostYaml, "utf-8");
    const registry2 = new RateRegistry();
    expect(() => registry2.load(missingCostPath)).toThrow();
  });
});
