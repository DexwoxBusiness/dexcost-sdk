/**
 * Suppression-flag tests (v1 §5.3).
 *
 * Mirrors python/tests/test_network_suppression.py and the Rust + Go
 * suppression-flag tests.
 */

import { describe, it, expect } from "vitest";
import {
  isNetworkEventSuppressed,
  suppressNetworkEvent,
} from "../src/core/context.js";

describe("suppressNetworkEvent — v1 §5.3 scope", () => {
  it("returns false outside any scope", () => {
    expect(isNetworkEventSuppressed()).toBe(false);
  });

  it("returns true inside the scope", async () => {
    let seen = false;
    suppressNetworkEvent(() => {
      seen = isNetworkEventSuppressed();
    });
    expect(seen).toBe(true);
  });

  it("resets to false after the scope exits", async () => {
    suppressNetworkEvent(() => {
      expect(isNetworkEventSuppressed()).toBe(true);
    });
    expect(isNetworkEventSuppressed()).toBe(false);
  });

  it("propagates through awaits inside the scope", async () => {
    await suppressNetworkEvent(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1));
      expect(isNetworkEventSuppressed()).toBe(true);
    });
  });

  it("nested suppression scopes remain suppressed (idempotent)", () => {
    suppressNetworkEvent(() => {
      expect(isNetworkEventSuppressed()).toBe(true);
      suppressNetworkEvent(() => {
        expect(isNetworkEventSuppressed()).toBe(true);
      });
      expect(isNetworkEventSuppressed()).toBe(true);
    });
  });

  it("async branches outside the scope are not suppressed", async () => {
    let insideScope = false;
    let outsideScope: boolean | null = null;
    await Promise.all([
      suppressNetworkEvent(async () => {
        await new Promise((r) => setTimeout(r, 1));
        insideScope = isNetworkEventSuppressed();
      }),
      (async () => {
        await new Promise((r) => setTimeout(r, 1));
        outsideScope = isNetworkEventSuppressed();
      })(),
    ]);
    expect(insideScope).toBe(true);
    expect(outsideScope).toBe(false);
  });
});
