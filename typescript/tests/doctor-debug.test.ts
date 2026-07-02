/**
 * Tests for `dexcost doctor` (pipeline verification) and debug mode
 * (capture-decision logging).
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { runDoctor } from "../src/cli/doctor.js";
import {
  setDebugMode,
  isDebugMode,
  debugLog,
  _resetDebugModeForTests,
} from "../src/core/debug.js";

afterEach(() => {
  _resetDebugModeForTests();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("dexcost doctor", () => {
  it("produces a structured report and never throws", async () => {
    const report = await runDoctor({ offline: true });

    expect(report.checks.length).toBeGreaterThanOrEqual(8);
    for (const check of report.checks) {
      expect(["ok", "warn", "fail", "skip"]).toContain(check.status);
      expect(check.detail.length).toBeGreaterThan(0);
    }
  }, 30_000);

  it("reports the load-bearing checks healthy in this environment", async () => {
    const report = await runDoctor({ offline: true });
    const byId = new Map(report.checks.map((c) => [c.id, c]));

    expect(byId.get("runtime")!.status).toBe("ok");
    expect(byId.get("als")!.status).toBe("ok");
    expect(byId.get("sqlite")!.status).toBe("ok"); // installed in this repo
    expect(byId.get("fetch")!.status).toBe("ok");
    expect(byId.get("buffer")!.status).toBe("ok");
    expect(byId.get("endpoint")!.status).toBe("skip"); // --offline
    // No provider package is installed here — every provider check is a
    // skip, and the dry-run degrades to a warn, not a fail.
    expect(byId.get("patch")!.status).toBe("warn");
    expect(report.healthy).toBe(true);
  }, 30_000);

  it("restores globalThis.fetch after the fetch-patch check", async () => {
    const before = globalThis.fetch;
    await runDoctor({ offline: true });
    expect(globalThis.fetch).toBe(before);
    expect((globalThis.fetch as any)[Symbol.for("dexcost.patched")]).toBeUndefined();
  }, 30_000);

  it("flags a malformed API key as a failure", async () => {
    const report = await runDoctor({ offline: true, apiKey: "not-a-real-key" });
    const apikey = report.checks.find((c) => c.id === "apikey")!;
    expect(apikey.status).toBe("fail");
    expect(report.healthy).toBe(false);
  }, 30_000);
});

describe("debug mode", () => {
  it("is off by default and debugLog is silent", () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    debugLog("test", "should not appear");
    expect(err).not.toHaveBeenCalled();
  });

  it("setDebugMode(true) enables scoped stderr logging", () => {
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    setDebugMode(true);
    debugLog("http", "llm_call captured");
    expect(err).toHaveBeenCalledWith("[dexcost:http] llm_call captured");
  });

  it("DEXCOST_DEBUG env var enables it; explicit setDebugMode wins over env", () => {
    vi.stubEnv("DEXCOST_DEBUG", "1");
    expect(isDebugMode()).toBe(true);
    setDebugMode(false);
    expect(isDebugMode()).toBe(false);
  });
});
