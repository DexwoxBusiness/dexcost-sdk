/**
 * A2 regression — Sprint 1 Theme A / plan §2.1.
 *
 * `DEXCOST_ENDPOINT` env var must be rejected if it doesn't start with
 * `https://`. An attacker who controls the env (misconfigured CI
 * runner, hostile container) could otherwise silently exfiltrate cost
 * telemetry to an HTTP collector — we refuse and fall back to the
 * production default with a warning.
 */

import { afterEach, describe, expect, test, vi } from "vitest";

import { resolveEndpoint, DEFAULT_ENDPOINT } from "../src/core/tracker.js";

const ORIGINAL_ENV = process.env.DEXCOST_ENDPOINT;

describe("DEXCOST_ENDPOINT allow-list", () => {
  afterEach(() => {
    if (ORIGINAL_ENV === undefined) {
      delete process.env.DEXCOST_ENDPOINT;
    } else {
      process.env.DEXCOST_ENDPOINT = ORIGINAL_ENV;
    }
    vi.restoreAllMocks();
  });

  test("accepts https:// values", () => {
    process.env.DEXCOST_ENDPOINT = "https://custom.example.com";
    expect(resolveEndpoint()).toBe("https://custom.example.com");
  });

  test("rejects http:// and falls back to default with warning", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    process.env.DEXCOST_ENDPOINT = "http://attacker.example/";
    expect(resolveEndpoint()).toBe(DEFAULT_ENDPOINT);
    expect(warnSpy).toHaveBeenCalled();
    expect(warnSpy.mock.calls[0]?.[0]).toContain("DEXCOST_ENDPOINT");
  });

  test("rejects arbitrary non-https schemes", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    process.env.DEXCOST_ENDPOINT = "javascript:alert(1)";
    expect(resolveEndpoint()).toBe(DEFAULT_ENDPOINT);
  });

  test("returns default when env var unset", () => {
    delete process.env.DEXCOST_ENDPOINT;
    expect(resolveEndpoint()).toBe(DEFAULT_ENDPOINT);
  });
});
