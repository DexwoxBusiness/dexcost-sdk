/**
 * Fix 2 — tasks table sync_status + purge of stale pending events.
 *
 * Before the fix the `tasks` table had no `sync_status` column, so every
 * task was re-POSTed on every push, and there was no way to evict pending
 * events that could never be synced. These tests verify:
 *   - a synced task is not re-pushed on the next cycle,
 *   - an upserted (changed) task becomes pending again,
 *   - purgeOldPending deletes stale pending events but keeps fresh ones.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";
import { createTask, createCostEvent } from "../src/core/models.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-task-sync-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("Fix 2 — task sync_status", () => {
  it("marks pushed tasks synced and excludes them from the next push", async () => {
    const buffer = new EventBuffer(join(tmpDir, "t.db"));
    const taskId = randomUUID();
    buffer.upsertTask(createTask({ taskId, taskType: "resolve" }));
    buffer.addEvent(
      createCostEvent({ eventId: randomUUID(), taskId, eventType: "external_cost" }),
    );

    const taskPayloads: unknown[][] = [];
    globalThis.fetch = vi.fn().mockImplementation(async (_url, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { tasks: unknown[] };
      taskPayloads.push(body.tasks);
      return new Response("{}", { status: 202 });
    });

    const pusher = new EventPusher(buffer, { apiKey: "dx_live_x" });

    // First push: task is pending, must be sent.
    await pusher.flush();
    expect(buffer.pendingTaskCount).toBe(0);
    expect(taskPayloads[0]).toHaveLength(1);

    // Add a fresh event so there is something to push again, but DON'T
    // touch the task — it should now be excluded as already-synced.
    buffer.addEvent(
      createCostEvent({ eventId: randomUUID(), taskId, eventType: "external_cost" }),
    );
    await pusher.flush();

    // Second push carried no tasks — the synced task was not re-sent.
    expect(taskPayloads[1]).toHaveLength(0);

    pusher.stop();
    buffer.close();
  });

  it("re-marks a task pending when it is upserted after being synced", () => {
    const buffer = new EventBuffer(join(tmpDir, "u.db"));
    const taskId = randomUUID();
    const task = createTask({ taskId, taskType: "resolve" });

    buffer.upsertTask(task);
    expect(buffer.pendingTaskCount).toBe(1);

    buffer.markTasksSynced([taskId]);
    expect(buffer.pendingTaskCount).toBe(0);
    expect(buffer.getPendingTasks()).toHaveLength(0);

    // Upserting the task again (e.g. a cost rolled up) makes it pending.
    buffer.upsertTask({ ...task, totalCostUsd: 1.5 });
    expect(buffer.pendingTaskCount).toBe(1);
    expect(buffer.getPendingTasks()[0].taskId).toBe(taskId);

    buffer.close();
  });

  it("purgeOldPending deletes stale pending events but keeps fresh ones", () => {
    const buffer = new EventBuffer(join(tmpDir, "p.db"));
    const taskId = randomUUID();

    // Stale: occurred 10 days ago (older than the 7-day default).
    const stale = createCostEvent({
      eventId: randomUUID(),
      taskId,
      eventType: "external_cost",
      occurredAt: new Date(Date.now() - 10 * 86_400_000),
    });
    // Fresh: occurred just now.
    const fresh = createCostEvent({
      eventId: randomUUID(),
      taskId,
      eventType: "external_cost",
      occurredAt: new Date(),
    });
    buffer.addEvent(stale);
    buffer.addEvent(fresh);
    expect(buffer.pendingCount).toBe(2);

    const deleted = buffer.purgeOldPending(7);

    expect(deleted).toBe(1);
    expect(buffer.pendingCount).toBe(1);
    const remaining = buffer.getAllEvents();
    expect(remaining).toHaveLength(1);
    expect(remaining[0].eventId).toBe(fresh.eventId);

    buffer.close();
  });

  it("purgeOldPending does not delete synced events", () => {
    const buffer = new EventBuffer(join(tmpDir, "s.db"));
    const taskId = randomUUID();
    const old = createCostEvent({
      eventId: randomUUID(),
      taskId,
      eventType: "external_cost",
      occurredAt: new Date(Date.now() - 10 * 86_400_000),
    });
    buffer.addEvent(old);
    buffer.markSynced([old.eventId]);

    // Only 'pending' events are eligible — a synced event survives.
    const deleted = buffer.purgeOldPending(7);
    expect(deleted).toBe(0);
    expect(buffer.getAllEvents()).toHaveLength(1);

    buffer.close();
  });
});
