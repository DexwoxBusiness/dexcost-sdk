/**
 * B8 regression — Sprint 1 Theme B / plan §2.2.3.
 *
 * The SDK must not crash when better-sqlite3 cannot be loaded. The
 * audited-failure modes are Vercel Edge Runtime, Cloudflare Workers,
 * and Bun configurations where the native binding fails to compile or
 * isn't permitted. Pre-fix: top-level `import Database from
 * "better-sqlite3"` throws at module load, taking down the customer app.
 * Post-fix: EventBuffer falls back to a graceful no-op store (events
 * dropped with a warning log) so `init()` returns normally.
 *
 * vi.doMock cannot intercept createRequire-loaded CJS modules, so we
 * use the EventBuffer._forceFallbackForTest seam — set + constructor
 * takes the same no-binding path that runs when the require throws in
 * production.
 */

import { afterEach, describe, expect, test, vi } from "vitest";

import { EventBuffer } from "../src/transport/buffer.js";

describe("EventBuffer — better-sqlite3 fallback", () => {
  afterEach(() => {
    EventBuffer._forceFallbackForTest = false;
    vi.restoreAllMocks();
  });

  test("falls back to no-op store when better-sqlite3 is unavailable", () => {
    EventBuffer._forceFallbackForTest = true;
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    // Constructor must not throw.
    const buf = new EventBuffer();
    expect(warnSpy).toHaveBeenCalled();

    // Public read methods must return empty / zero, not throw.
    expect(buf.getAllEvents()).toEqual([]);
    expect(buf.getAllTasks()).toEqual([]);
    expect(buf.getPendingEvents()).toEqual([]);
    expect(buf.getPendingTasks()).toEqual([]);
    expect(buf.pendingCount).toBe(0);
    expect(buf.pendingTaskCount).toBe(0);
    expect(buf.getTask("nonexistent")).toBeUndefined();
    expect(buf.queryEvents("nonexistent")).toEqual([]);

    // Public write methods must silently no-op, not throw.
    expect(() => buf.markSynced(["e1"])).not.toThrow();
    expect(() => buf.markTasksSynced(["t1"])).not.toThrow();
    expect(() => buf.purgeSynced(48)).not.toThrow();
    expect(() => buf.purgeOldPending(7)).not.toThrow();
    expect(() => buf.close()).not.toThrow();
  });

  test("normal-mode construction still works (regression guard)", () => {
    // Sanity: with the seam off, the existing SQLite path is intact.
    EventBuffer._forceFallbackForTest = false;
    const buf = new EventBuffer(":memory:");
    expect(buf.pendingCount).toBe(0);
    buf.close();
  });
});
