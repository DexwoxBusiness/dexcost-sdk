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
import type { CostEvent, Task } from "../src/core/models.js";

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

describe("EventBuffer — in-memory store round-trips events and tasks", () => {
  afterEach(() => {
    EventBuffer._forceFallbackForTest = false;
    vi.restoreAllMocks();
  });

  function makeEvent(id: string, taskId: string): CostEvent {
    return {
      eventId: id,
      taskId,
      eventType: "llm_call",
      costUsd: 0.001,
      costConfidence: "computed",
      isRetry: false,
      details: {},
      occurredAt: new Date(),
      schemaVersion: "1",
    } as CostEvent;
  }

  function makeTask(id: string): Task {
    return {
      taskId: id,
      taskType: "test",
      status: "pending",
      startedAt: new Date(),
      metadata: {},
      llmCostUsd: 0,
      externalCostUsd: 0,
      computeCostUsd: 0,
      totalCostUsd: 0,
      totalInputTokens: 0,
      totalOutputTokens: 0,
      totalCachedTokens: 0,
      retryCount: 0,
      retryCostUsd: 0,
      failureCount: 0,
      networkBytesIn: 0,
      networkBytesOut: 0,
      networkCallCount: 0,
      networkByHost: { hosts: [] },
      networkCostUsd: 0,
      schemaVersion: "1",
    } as Task;
  }

  test("addEvent + getPendingEvents round-trip and pendingCount is accurate", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const buf = new EventBuffer();

    buf.addEvent(makeEvent("e1", "t1"));
    buf.addEvent(makeEvent("e2", "t1"));
    buf.addEvent(makeEvent("e3", "t2"));

    expect(buf.pendingCount).toBe(3);
    const pending = buf.getPendingEvents();
    expect(pending.map((e) => e.eventId).sort()).toEqual(["e1", "e2", "e3"]);
  });

  test("markSynced flips pending → synced (pendingCount drops, getAllEvents still includes them)", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const buf = new EventBuffer();

    buf.addEvent(makeEvent("e1", "t1"));
    buf.addEvent(makeEvent("e2", "t1"));
    buf.markSynced(["e1"]);

    expect(buf.pendingCount).toBe(1);
    expect(buf.getPendingEvents().map((e) => e.eventId)).toEqual(["e2"]);
    expect(buf.getAllEvents()).toHaveLength(2);
  });

  test("quarantine retains failed events outside the pending scan", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const buf = new EventBuffer();

    buf.addEvent(makeEvent("e1", "t1"));
    buf.addEvent(makeEvent("e2", "t1"));
    buf.markQuarantined(["e1"]);

    expect(buf.getPendingEvents().map((event) => event.eventId)).toEqual(["e2"]);
    expect(buf.getQuarantinedEvents().map((event) => event.eventId)).toEqual(["e1"]);
    expect(buf.getAllEvents()).toHaveLength(2);
  });

  test("queryEvents returns events filtered by taskId", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const buf = new EventBuffer();

    buf.addEvent(makeEvent("e1", "t1"));
    buf.addEvent(makeEvent("e2", "t1"));
    buf.addEvent(makeEvent("e3", "t2"));

    expect(buf.queryEvents("t1").map((e) => e.eventId).sort()).toEqual(["e1", "e2"]);
    expect(buf.queryEvents("t2").map((e) => e.eventId)).toEqual(["e3"]);
    expect(buf.queryEvents("nope")).toEqual([]);
  });

  test("upsertTask + getTask + markTasksSynced round-trip", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const buf = new EventBuffer();

    buf.upsertTask(makeTask("t1"));
    expect(buf.getTask("t1")?.taskId).toBe("t1");
    expect(buf.pendingTaskCount).toBe(1);

    // upsert idempotency — second call updates in place, doesn't add.
    buf.upsertTask({ ...makeTask("t1"), taskType: "renamed" });
    expect(buf.getAllTasks()).toHaveLength(1);
    expect(buf.getTask("t1")?.taskType).toBe("renamed");

    buf.markTasksSynced(["t1"]);
    expect(buf.pendingTaskCount).toBe(0);
    expect(buf.getPendingTasks()).toHaveLength(0);
    expect(buf.getAllTasks()).toHaveLength(1);
  });

  test("hard 10k cap evicts oldest events FIFO", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const buf = new EventBuffer();

    // Push 10005 events; only the last 10000 should be retained.
    for (let i = 0; i < 10_005; i++) {
      buf.addEvent(makeEvent(`e${i}`, "t1"));
    }
    expect(buf.getAllEvents()).toHaveLength(10_000);
    // The first 5 must be evicted; e5 through e10004 remain.
    const ids = new Set(buf.getAllEvents().map((e) => e.eventId));
    expect(ids.has("e0")).toBe(false);
    expect(ids.has("e4")).toBe(false);
    expect(ids.has("e5")).toBe(true);
    expect(ids.has("e10004")).toBe(true);
  });

  test("purgeOldPending removes only pending events older than maxAgeDays", () => {
    EventBuffer._forceFallbackForTest = true;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.useFakeTimers();
    const start = new Date("2026-01-01T00:00:00Z");
    vi.setSystemTime(start);

    const buf = new EventBuffer();
    buf.addEvent(makeEvent("old-pending", "t1"));
    buf.addEvent(makeEvent("old-synced", "t1"));
    buf.markSynced(["old-synced"]);

    // 10 days later, add a recent pending event.
    vi.setSystemTime(new Date(start.getTime() + 10 * 24 * 3600 * 1000));
    buf.addEvent(makeEvent("recent-pending", "t1"));

    const removed = buf.purgeOldPending(7);
    expect(removed).toBe(1); // only "old-pending" — older than 7 days AND pending
    const remaining = new Set(buf.getAllEvents().map((e) => e.eventId));
    expect(remaining.has("old-pending")).toBe(false);
    expect(remaining.has("old-synced")).toBe(true); // synced not affected
    expect(remaining.has("recent-pending")).toBe(true); // too recent

    vi.useRealTimers();
  });
});
