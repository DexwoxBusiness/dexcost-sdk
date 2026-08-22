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
  it("keeps unsigned bootstrap compatibility when no trust policy exists", () => {
    expect(resolveCatalogTrustPolicy()).toEqual({ trustedKeys: {}, requireSignature: false });
  });

  it("requires signatures by default when environment keys are configured", () => {
    process.env.DEXCOST_CATALOG_TRUSTED_KEYS = JSON.stringify({
      "dexcost-prod-2026-01": PUBLIC_KEY,
    });
    expect(resolveCatalogTrustPolicy()).toEqual({
      trustedKeys: { "dexcost-prod-2026-01": PUBLIC_KEY },
      requireSignature: true,
    });
  });

  it("lets explicit options override both environment settings", () => {
    process.env.DEXCOST_CATALOG_TRUSTED_KEYS = "not-json";
    process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = "true";
    expect(resolveCatalogTrustPolicy({}, false)).toEqual({
      trustedKeys: {},
      requireSignature: false,
    });
  });

  it("allows only an explicit unsigned migration override", () => {
    process.env.DEXCOST_CATALOG_TRUSTED_KEYS = JSON.stringify({
      "dexcost-prod-2026-01": PUBLIC_KEY,
    });
    process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = "false";
    expect(resolveCatalogTrustPolicy().requireSignature).toBe(false);
  });

  it.each([
    ["not-json", undefined, /not valid JSON/u],
    ["{}", undefined, /1-8 public keys/u],
    ["[]", undefined, /1-8 public keys/u],
    [JSON.stringify({ "BAD KEY": PUBLIC_KEY }), undefined, /key ID is invalid/u],
    [JSON.stringify({ "dexcost-prod": "AAAA" }), undefined, /wrong byte length/u],
    [undefined, "1", /must be true or false/u],
    [undefined, "true", /requires at least one/u],
  ] as const)("fails closed for malformed environment policy %#", (keys, requirement, message) => {
    if (keys !== undefined) process.env.DEXCOST_CATALOG_TRUSTED_KEYS = keys;
    if (requirement !== undefined) process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = requirement;
    expect(() => resolveCatalogTrustPolicy()).toThrow(message);
  });

  it("fails before creating storage when the security policy is invalid", () => {
    process.env.DEXCOST_CATALOG_REQUIRE_SIGNATURE = "true";
    const database = join(directory, "must-not-exist.db");
    expect(() => new CostTracker({
      dbPath: database,
      autoInstrument: [],
      trackHttp: false,
    })).toThrow(/requires at least one/u);
    expect(existsSync(database)).toBe(false);
  });
});
