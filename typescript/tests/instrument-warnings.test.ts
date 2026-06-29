/**
 * Regression tests for instrumentation log-noise control.
 *
 * Issue: the SDK auto-instruments every supported provider by default, so
 * uninstalled providers (most apps only use one or two) produced a wall of
 * "Failed to instrument <provider>" warnings on startup. Those warnings now
 * fire only when the user *explicitly* requested the provider via
 * `autoInstrument`; default auto-instrumentation stays silent for the common
 * not-installed case but still surfaces real patching failures.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  registerInstrument,
  instrumentProvider,
} from "../src/instruments/index.js";
import type { PricingEngine } from "../src/pricing/engine.js";
import type { EventBuffer } from "../src/transport/buffer.js";

const pricing = {} as PricingEngine;
const buffer = {} as EventBuffer;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("instrumentProvider warning noise", () => {
  it("stays silent for a not-installed provider during default auto-instrumentation", async () => {
    registerInstrument(
      "test-missing",
      async () => {
        throw new Error("Cannot find package 'test-missing' imported from x");
      },
      () => {},
    );
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const ok = await instrumentProvider("test-missing", pricing, buffer, /* explicit */ false);

    expect(ok).toBe(false);
    expect(warn).not.toHaveBeenCalled();
  });

  it("warns for a not-installed provider the user explicitly requested", async () => {
    registerInstrument(
      "test-missing-explicit",
      async () => {
        throw new Error("Cannot find package 'test-missing-explicit' imported from x");
      },
      () => {},
    );
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const ok = await instrumentProvider("test-missing-explicit", pricing, buffer, /* explicit */ true);

    expect(ok).toBe(false);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toContain("not installed");
  });

  it("warns for a real patch failure even during default auto-instrumentation", async () => {
    registerInstrument(
      "test-broken",
      async () => {
        // Package IS present but patching threw — a genuine problem.
        throw new TypeError("Cannot read properties of undefined (reading 'prototype')");
      },
      () => {},
    );
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const ok = await instrumentProvider("test-broken", pricing, buffer, /* explicit */ false);

    expect(ok).toBe(false);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toContain("Failed to instrument test-broken");
  });

  it("warns for an unknown provider name only when explicit", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    expect(await instrumentProvider("nope-not-real", pricing, buffer, false)).toBe(false);
    expect(warn).not.toHaveBeenCalled();

    expect(await instrumentProvider("nope-not-real", pricing, buffer, true)).toBe(false);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toContain("unknown provider");
  });
});
