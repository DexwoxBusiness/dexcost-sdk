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

type ContractCell = {
  status: "implemented" | "intentionally_excluded" | "required";
};

describe("frozen Python-to-TypeScript public contract", () => {
  it("has no generated TypeScript API drift", () => {
    expect(() => execFileSync(
      process.execPath,
      [join(typescriptRoot, "scripts", "freeze-contract.mjs")],
      { cwd: typescriptRoot, stdio: "pipe" },
    )).not.toThrow();
  }, 30_000);

  it("keeps volatile package versions outside the semantic contract", () => {
    const pythonPublic = JSON.parse(readFileSync(
      join(freezeRoot, "public-api.json"), "utf-8",
    )) as Record<string, unknown>;
    const typescriptPublic = JSON.parse(readFileSync(
      join(freezeRoot, "typescript-public-api.json"), "utf-8",
    )) as Record<string, unknown>;
    expect(pythonPublic).not.toHaveProperty("package_version");
    expect(typescriptPublic).not.toHaveProperty("package_version");
  });

  it("classifies every Python root export with no unresolved names", () => {
    const parity = JSON.parse(readFileSync(
      join(freezeRoot, "typescript-api-map.json"), "utf-8",
    )) as {
      python_export_count: number;
      equivalent_count: number;
      language_specific_count: number;
      unresolved_count: number;
      mappings: Array<{
        python_name: string;
        classification: string;
        typescript_exports: string[];
        notes: string | null;
      }>;
    };
    expect(parity.python_export_count).toBe(parity.mappings.length);
    expect(parity.equivalent_count + parity.language_specific_count)
      .toBe(parity.python_export_count);
    expect(parity.unresolved_count).toBe(0);
    expect(parity.mappings.find((item) => item.python_name === "InfrastructureRateEntry")
      ?.classification).toBe("equivalent");
    expect(parity.mappings.find((item) => item.python_name === "AttributionOperationErrorV3")
      ?.classification).toBe("equivalent");
    expect(parity.mappings.find((item) => item.python_name === "instrument_gemini")
      ?.typescript_exports).toEqual(["instrumentGoogleGenAI"]);
    expect(parity.mappings.find((item) => item.python_name === "uninstrument_gemini")
      ?.typescript_exports).toEqual(["uninstrumentGoogleGenAI"]);
    expect(parity.mappings.find((item) => item.python_name === "ALL_SUPPORTED_INSTRUMENTS")
      ?.notes).toMatch(/additionally exposes/u);
  });

  it("has no unresolved TypeScript capability requirements", () => {
    const capabilities = JSON.parse(readFileSync(
      join(freezeRoot, "capability-matrix.json"), "utf-8",
    )) as {
      rows: Array<{
        id: string;
        cells: { dexcost_typescript: ContractCell };
      }>;
    };
    const providers = JSON.parse(readFileSync(
      join(freezeRoot, "provider-capabilities.json"), "utf-8",
    )) as {
      providers: Array<{
        id: string;
        coverage: Record<string, { dexcost_typescript: ContractCell }>;
      }>;
    };

    const unresolved = capabilities.rows
      .filter((row) => row.cells.dexcost_typescript.status === "required")
      .map((row) => row.id);
    for (const provider of providers.providers) {
      for (const [dimension, languages] of Object.entries(provider.coverage)) {
        if (languages.dexcost_typescript.status === "required") {
          unresolved.push(`${provider.id}.${dimension}`);
        }
      }
    }

    expect(unresolved, `unresolved TypeScript contract requirements:\n${unresolved.join("\n")}`)
      .toEqual([]);
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
