import { describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  parseCatalogManifest,
  verifyCatalogManifestSignature,
} from "../src/pricing/catalog-releases.js";

const here = dirname(fileURLToPath(import.meta.url));
const typescriptRoot = resolve(here, "..");
const freezeRoot = resolve(typescriptRoot, "..", "contracts", "python-vnext", "v1");

describe("frozen Python-to-TypeScript public contract", () => {
  it("has no generated TypeScript API drift", () => {
    expect(() => execFileSync(
      process.execPath,
      [join(typescriptRoot, "scripts", "freeze-contract.mjs")],
      { cwd: typescriptRoot, stdio: "pipe" },
    )).not.toThrow();
  });

  it("classifies every Python root export with no unresolved names", () => {
    const parity = JSON.parse(readFileSync(
      join(freezeRoot, "typescript-api-map.json"), "utf-8",
    )) as {
      python_export_count: number;
      equivalent_count: number;
      language_specific_count: number;
      unresolved_count: number;
      mappings: Array<{ python_name: string; classification: string }>;
    };
    expect(parity.python_export_count).toBe(parity.mappings.length);
    expect(parity.equivalent_count + parity.language_specific_count)
      .toBe(parity.python_export_count);
    expect(parity.unresolved_count).toBe(0);
    expect(parity.mappings.find((item) => item.python_name === "InfrastructureRateEntry")
      ?.classification).toBe("equivalent");
    expect(parity.mappings.find((item) => item.python_name === "AttributionOperationErrorV3")
      ?.classification).toBe("equivalent");
  });

  it("verifies the shared signed catalog golden with the frozen test key", () => {
    const raw = readFileSync(join(freezeRoot, "golden", "catalog-release.v1.json"));
    const manifest = parseCatalogManifest(raw);
    expect(verifyCatalogManifestSignature(manifest, {
      trustedKeys: {
        "dexcost-test-rfc8032-1": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
      },
      requireSignature: true,
    })).toBe("dexcost-test-rfc8032-1");
  });

  it("ships the canonical production trust document byte-for-byte", () => {
    const canonical = readFileSync(join(freezeRoot, "catalog-production-trust.json"));
    expect(readFileSync(join(
      typescriptRoot,
      "src",
      "core",
      "catalog-production-trust.json",
    ))).toEqual(canonical);
    expect(readFileSync(join(
      typescriptRoot,
      "..",
      "python",
      "src",
      "dexcost",
      "data",
      "catalog_production_trust.json",
    ))).toEqual(canonical);
  });
});
