/**
 * Endpoint resolution — explicit-config-only (security hardening).
 *
 * The endpoint is sourced ONLY from explicit in-code config (`resolveEndpoint`
 * arg), never from the process environment. An attacker who controls the env
 * (misconfigured CI runner, hostile container) can no longer set
 * `DEXCOST_ENDPOINT=http://attacker/` to exfiltrate cost telemetry or the
 * Bearer API key, because the SDK never reads that var.
 *
 * The explicit value is developer-supplied/trusted: it is honoured if it starts
 * with http:// or https:// (http:// is allowed for local/e2e since it is not
 * env-controllable); otherwise we fall back to the default with a warning.
 */

import { afterEach, describe, expect, test, vi } from "vitest";

import { resolveEndpoint, DEFAULT_ENDPOINT } from "../src/core/tracker.js";

const ORIGINAL_ENV = process.env.DEXCOST_ENDPOINT;

describe("endpoint resolution (explicit config only)", () => {
  afterEach(() => {
    if (ORIGINAL_ENV === undefined) {
      delete process.env.DEXCOST_ENDPOINT;
    } else {
      process.env.DEXCOST_ENDPOINT = ORIGINAL_ENV;
    }
    vi.restoreAllMocks();
  });

  test("returns the default when no explicit value is given", () => {
    expect(resolveEndpoint()).toBe(DEFAULT_ENDPOINT);
    expect(resolveEndpoint(DEFAULT_ENDPOINT)).toBe("https://api.dexcost.io");
  });

  test("treats an empty explicit value as unset", () => {
    expect(resolveEndpoint("")).toBe(DEFAULT_ENDPOINT);
  });

  test("honours an explicit https:// value", () => {
    expect(resolveEndpoint("https://custom.example")).toBe("https://custom.example");
  });

  test("honours an explicit http:// value (trusted, not env-controllable)", () => {
    // http:// is intentionally accepted for the explicit option (e.g.
    // http://localhost for e2e) — safe because it cannot come from the env.
    expect(resolveEndpoint("http://localhost:3000")).toBe("http://localhost:3000");
  });

  test("falls back to the default for a non-http(s) value with a warning", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(resolveEndpoint("javascript:alert(1)")).toBe(DEFAULT_ENDPOINT);
    expect(warnSpy).toHaveBeenCalled();
    expect(warnSpy.mock.calls[0]?.[0]).toContain("endpoint");
  });

  test("IGNORES the DEXCOST_ENDPOINT env var entirely", () => {
    // Even a malicious env value must have zero effect on resolution.
    process.env.DEXCOST_ENDPOINT = "http://evil.example";
    expect(resolveEndpoint()).toBe(DEFAULT_ENDPOINT);
    // And it never gets honoured implicitly via the explicit path either.
    expect(resolveEndpoint(undefined)).toBe(DEFAULT_ENDPOINT);
  });
});
