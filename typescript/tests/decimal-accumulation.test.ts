/**
 * B3 regression — Sprint 2 Theme E / plan §3.3.1.
 *
 * Pre-fix the SDK accumulated `costUsd` deltas via JavaScript's `+=`
 * on `number`. After 10 000 iterations of a tiny per-event cost
 * (1.23e-8), the running total drifts by ~2e-16 — small in absolute
 * terms but enough to fail the cross-SDK fixture invariant.
 *
 * Post-fix every accumulation site goes through a Decimal-based
 * helper so adding `0.0000000123` ten thousand times yields exactly
 * `0.000123`. The customer-facing field type stays `number` (storing
 * `string` on the wire would be a breaking API change deferred to a
 * major version per plan §0.7).
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { init, close } from "../src/index.js";
import { EventBuffer } from "../src/transport/buffer.js";

describe("decimal accumulation (B3)", () => {
  let tmpDir: string;

  afterEach(() => {
    try {
      close();
    } catch {
      // already closed
    }
    if (tmpDir) {
      rmSync(tmpDir, { recursive: true, force: true });
    }
    EventBuffer._forceFallbackForTest = false;
    vi.restoreAllMocks();
  });

  test("10 000 × 1.23e-8 accumulates to exactly 0.000123 in task.totalCostUsd", async () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});

    const tracker = init({ apiKey: "dx_test_x" });
    const per = 1.23e-8;
    const iters = 10_000;
    const want = 0.000123;

    await tracker.track({ taskType: "decimal-accumulation" }, async (tt) => {
      for (let i = 0; i < iters; i++) {
        tt.recordCost("x", per);
      }
    });

    const tasks = tracker.buffer.getAllTasks();
    expect(tasks).toHaveLength(1);
    const task = tasks[0]!;

    // The native-float baseline: 10 000 × 1.23e-8 += per drifts to
    // 0.00012299999999999998 (~2e-16 error). Decimal accumulation
    // yields exactly 0.000123 — the task field is now an exact Decimal, so
    // assert BOTH the exact string and the numeric round-trip.
    expect(task.totalCostUsd.toString()).toBe("0.000123");
    expect(task.totalCostUsd.toNumber()).toBe(want);
  });

  test("native float baseline drifts (proves the test isn't trivially green)", () => {
    // Sanity: assert that the pre-fix path would FAIL the above
    // exact-equality. If this assertion ever flips, JS engines have
    // changed floating-point behaviour and the B3 fix is no longer
    // load-bearing.
    let total = 0;
    for (let i = 0; i < 10_000; i++) total += 1.23e-8;
    expect(total).not.toBe(0.000123);
  });
});
