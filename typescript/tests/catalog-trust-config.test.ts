import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resolveCatalogTrustPolicy } from "../src/core/config.js";
import { CostTracker } from "../src/core/tracker.js";

const PUBLIC_KEY = "11qYAYdk9JNu81kOIyRUDn69brTa7WHqmX84xB6sSPA";
const ENVIRONMENT_NAMES = [
  "DEXCOST_CATALOG_TRUSTED_KEYS",
  "DEXCOST_CATALOG_REQUIRE_SIGNATURE",
] as const;

let directory: string;
let savedEnvironment: Record<string, string | undefined>;

beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "dexcost-catalog-trust-"));
  savedEnvironment = Object.fromEntries(
    ENVIRONMENT_NAMES.map((name) => [name, process.env[name]]),
  );
  for (const name of ENVIRONMENT_NAMES) delete process.env[name];
});

afterEach(() => {
  for (const name of ENVIRONMENT_NAMES) {
    const value = savedEnvironment[name];
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  rmSync(directory, { recursive: true, force: true });
});

describe("catalog trust configuration", () => {
  it("requires signatures from bundled production trust by default", () => {
    const policy = resolveCatalogTrustPolicy();
    expect(policy).toMatchObject({
      requireSignature: true,
      remoteRefreshEnabled: true,
    });
    expect(Object.keys(policy.trustedKeys).sort()).toEqual([
      "catalog-prod-2026-08-a",
      "catalog-prod-2026-08-b",
    ]);
  });

  it("requires signatures by default when environment keys are configured", () => {
    process.env.DEXCOST_CATALOG_TRUSTED_KEYS = JSON.stringify({
      "dexcost-prod-2026-01": PUBLIC_KEY,
    });
    expect(resolveCatalogTrustPolicy()).toEqual({
      trustedKeys: { "dexcost-prod-2026-01": PUBLIC_KEY },
      requireSignature: true,
      remoteRefreshEnabled: true,
    });
  });

  it("lets explicit options override both environment settings", () => {
    process.env.DEXCOST_CATALOG_TRUSTED_KEYS = "not-json";
    process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = "true";
    expect(resolveCatalogTrustPolicy({}, false)).toEqual({
      trustedKeys: {},
      requireSignature: false,
      remoteRefreshEnabled: true,
    });
  });

  it("allows only an explicit unsigned migration override", () => {
    process.env.DEXCOST_CATALOG_TRUSTED_KEYS = JSON.stringify({
      "dexcost-prod-2026-01": PUBLIC_KEY,
    });
    process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = "false";
    expect(resolveCatalogTrustPolicy().requireSignature).toBe(false);
    expect(resolveCatalogTrustPolicy().remoteRefreshEnabled).toBe(true);
  });

  it.each([
    ["not-json", undefined, /not valid JSON/u],
    ["{}", undefined, /1-8 public keys/u],
    ["[]", undefined, /1-8 public keys/u],
    [JSON.stringify({ "BAD KEY": PUBLIC_KEY }), undefined, /key ID is invalid/u],
    [JSON.stringify({ "dexcost-prod": "AAAA" }), undefined, /wrong byte length/u],
    [undefined, "1", /must be true or false/u],
  ] as const)("fails closed for malformed environment policy %#", (keys, requirement, message) => {
    if (keys !== undefined) process.env.DEXCOST_CATALOG_TRUSTED_KEYS = keys;
    if (requirement !== undefined) process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = requirement;
    expect(() => resolveCatalogTrustPolicy()).toThrow(message);
  });

  it("fails closed when signatures are required with explicit empty trust", () => {
    process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = "true";
    expect(() => resolveCatalogTrustPolicy({})).toThrow(/requires at least one/u);
  });

  it("fails before creating storage when the security policy is invalid", () => {
    process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = "true";
    const database = join(directory, "must-not-exist.db");
    expect(() => new CostTracker({
      dbPath: database,
      autoInstrument: [],
      trackHttp: false,
      catalogTrustedKeys: {},
    })).toThrow(/requires at least one/u);
    expect(existsSync(database)).toBe(false);
  });

  it("surfaces bundled verified trust and validates offline bundles", () => {
    const instance = new CostTracker({
      dbPath: join(directory, "disabled.db"), autoInstrument: [], trackHttp: false,
      storage: "local",
    });
    try {
      expect(instance.catalogStatus).toMatchObject({
        signatureVerification: "verified",
        trustedKeyIds: ["catalog-prod-2026-08-a", "catalog-prod-2026-08-b"],
        remoteRefreshEnabled: true,
      });
      expect(() => instance.importCatalogBundle(new Uint8Array()))
        .toThrow(/catalog bundle is not valid UTF-8 JSON/u);
    } finally {
      instance.close();
    }
  });
});
