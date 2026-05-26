/**
 * §4.2.1 regression — Sprint 3 Theme E.
 *
 * Pre-fix `trackHttp()` checked an internal `_patched` flag but did
 * not consult a marker on `globalThis.fetch` itself. Two scenarios
 * caused silent breakage:
 *
 * 1. A second dexcost SDK copy in the same process (Yarn PnP poor
 *    dedup, web bundling quirks) saw `_patched=false` in its own
 *    closure and wrapped an already-wrapped fetch — infinite
 *    recursion on every call.
 * 2. Sentry / OTEL wrap fetch independently; the dexcost wrapper
 *    forgot it had wrapped through them and `untrackHttp()` could
 *    leave the third-party wrapper dangling on top of itself.
 *
 * The fix tags our wrapped fetch with `Symbol.for("dexcost.patched") =
 * true` (cross-realm) and the second `trackHttp()` detects + skips.
 */

import { afterEach, describe, expect, test, vi } from "vitest";

import { trackHttp, untrackHttp } from "../src/adapters/http.js";

const DEXCOST_PATCHED = Symbol.for("dexcost.patched");

describe("fetch double-patching detection (§4.2.1)", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    untrackHttp();
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  test("second trackHttp() call is a no-op", () => {
    trackHttp();
    const firstWrap = globalThis.fetch;
    expect((firstWrap as unknown as Record<symbol, unknown>)[DEXCOST_PATCHED]).toBe(
      true,
    );

    trackHttp(); // Should detect and skip.
    expect(globalThis.fetch).toBe(firstWrap);
  });

  test("trackHttp() over a pre-tagged fetch logs warning and skips", () => {
    untrackHttp(); // start clean
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    // Simulate a duplicate SDK install: pre-tag fetch.
    const pretagged = async (input: RequestInfo | URL, init?: RequestInit) =>
      originalFetch(input as never, init);
    Object.defineProperty(pretagged, DEXCOST_PATCHED, {
      value: true,
      enumerable: false,
      configurable: false,
      writable: false,
    });
    globalThis.fetch = pretagged as typeof globalThis.fetch;

    trackHttp();

    expect(warnSpy).toHaveBeenCalled();
    expect(warnSpy.mock.calls[0]?.[0]).toContain("already wrapped");
    // Did not overwrite.
    expect(globalThis.fetch).toBe(pretagged);
  });
});
