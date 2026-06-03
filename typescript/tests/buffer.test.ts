/**
 * Tests for the SQLite-backed EventBuffer.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { createCostEvent, createTask, Decimal } from "../src/core/models.js";
import { randomUUID } from "node:crypto";

let tmpDir: string;
let dbPath: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-buffer-"));
  dbPath = join(tmpDir, "test.db");
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("EventBuffer (SQLite)", () => {
  it("stores and retrieves events", () => {
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    const event1 = createCostEvent({
      eventId: randomUUID(),
      taskId,
      eventType: "llm_call",
      costUsd: 0.05,
    });
    const event2 = createCostEvent({
      eventId: randomUUID(),
      taskId,
      eventType: "external_cost",
      costUsd: 0.01,
    });

    buffer.addEvent(event1);
    buffer.addEvent(event2);

    const pending = buffer.getPendingEvents(100);
    expect(pending).toHaveLength(2);
    expect(pending[0].eventId).toBe(event1.eventId);
    expect(pending[1].eventId).toBe(event2.eventId);

    buffer.close();
  });

  it("marks events as synced and removes from pending", () => {
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    const event1 = createCostEvent({
      eventId: randomUUID(),
      taskId,
      costUsd: 0.01,
    });
    const event2 = createCostEvent({
      eventId: randomUUID(),
      taskId,
      costUsd: 0.02,
    });
    const event3 = createCostEvent({
      eventId: randomUUID(),
      taskId,
      costUsd: 0.03,
    });

    buffer.addEvent(event1);
    buffer.addEvent(event2);
    buffer.addEvent(event3);

    buffer.markSynced([event1.eventId, event2.eventId]);

    const pending = buffer.getPendingEvents(100);
    expect(pending).toHaveLength(1);
    expect(pending[0].eventId).toBe(event3.eventId);

    buffer.close();
  });

  it("respects limit in getPendingEvents", () => {
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    for (let i = 0; i < 10; i++) {
      buffer.addEvent(
        createCostEvent({
          eventId: randomUUID(),
          taskId,
          costUsd: i * 0.01,
        })
      );
    }

    const pending = buffer.getPendingEvents(3);
    expect(pending).toHaveLength(3);

    buffer.close();
  });

  it("upserts and retrieves tasks, including update", () => {
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    const task = createTask({
      taskId,
      taskType: "test-task",
      customerId: "customer-1",
    });

    buffer.upsertTask(task);

    const retrieved = buffer.getTask(taskId);
    expect(retrieved).toBeDefined();
    expect(retrieved!.taskType).toBe("test-task");
    expect(retrieved!.customerId).toBe("customer-1");

    // Update the task
    const updated = { ...task, taskType: "updated-task", totalCostUsd: new Decimal("1.23") };
    buffer.upsertTask(updated);

    const retrieved2 = buffer.getTask(taskId);
    expect(retrieved2!.taskType).toBe("updated-task");
    expect(retrieved2!.totalCostUsd.toString()).toBe("1.23");

    buffer.close();
  });

  it("returns correct pending count", () => {
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    expect(buffer.pendingCount).toBe(0);

    const e1 = createCostEvent({ eventId: randomUUID(), taskId, costUsd: 0.01 });
    const e2 = createCostEvent({ eventId: randomUUID(), taskId, costUsd: 0.02 });
    const e3 = createCostEvent({ eventId: randomUUID(), taskId, costUsd: 0.03 });

    buffer.addEvent(e1);
    buffer.addEvent(e2);
    buffer.addEvent(e3);

    expect(buffer.pendingCount).toBe(3);

    buffer.markSynced([e1.eventId]);
    expect(buffer.pendingCount).toBe(2);

    buffer.close();
  });

  it("survives close and reopen (persistence test)", () => {
    // Write data, close, reopen, and verify data is still there
    const taskId = randomUUID();
    const eventId = randomUUID();

    {
      const buffer = new EventBuffer(dbPath);
      const task = createTask({ taskId, taskType: "persistent-task" });
      const event = createCostEvent({ eventId, taskId, costUsd: 0.99 });
      buffer.upsertTask(task);
      buffer.addEvent(event);
      buffer.close();
    }

    // Reopen with same path
    const buffer2 = new EventBuffer(dbPath);
    const task = buffer2.getTask(taskId);
    expect(task).toBeDefined();
    expect(task!.taskType).toBe("persistent-task");

    const pending = buffer2.getPendingEvents();
    expect(pending).toHaveLength(1);
    expect(pending[0].eventId).toBe(eventId);
    expect(pending[0].costUsd.toString()).toBe("0.99");

    buffer2.close();
  });

  it("queryEvents returns events for task descending by timestamp", () => {
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    // Add events with slightly different times
    const e1 = createCostEvent({
      eventId: randomUUID(),
      taskId,
      costUsd: 0.01,
      occurredAt: new Date("2025-01-01T00:00:01Z"),
    });
    const e2 = createCostEvent({
      eventId: randomUUID(),
      taskId,
      costUsd: 0.02,
      occurredAt: new Date("2025-01-01T00:00:02Z"),
    });
    const e3 = createCostEvent({
      eventId: randomUUID(),
      taskId,
      costUsd: 0.03,
      occurredAt: new Date("2025-01-01T00:00:03Z"),
    });

    buffer.addEvent(e1);
    buffer.addEvent(e2);
    buffer.addEvent(e3);

    const results = buffer.queryEvents(taskId);
    expect(results).toHaveLength(3);
    // DESC order: newest first
    expect(results[0].eventId).toBe(e3.eventId);
    expect(results[1].eventId).toBe(e2.eventId);
    expect(results[2].eventId).toBe(e1.eventId);

    buffer.close();
  });

  it("updateEvent modifies an existing event (is_retry flag)", () => {
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    const event = createCostEvent({
      eventId: randomUUID(),
      taskId,
      costUsd: 0.05,
      isRetry: false,
    });

    buffer.addEvent(event);

    const updated = { ...event, isRetry: true, retryReason: "timeout" };
    buffer.updateEvent(updated);

    const all = buffer.getAllEvents();
    expect(all).toHaveLength(1);
    expect(all[0].isRetry).toBe(true);
    expect(all[0].retryReason).toBe("timeout");

    buffer.close();
  });

  it("getAllTasks returns all tasks", () => {
    const buffer = new EventBuffer(dbPath);

    const t1 = createTask({ taskId: randomUUID(), taskType: "type-a" });
    const t2 = createTask({ taskId: randomUUID(), taskType: "type-b" });
    const t3 = createTask({ taskId: randomUUID(), taskType: "type-c" });

    buffer.upsertTask(t1);
    buffer.upsertTask(t2);
    buffer.upsertTask(t3);

    const all = buffer.getAllTasks();
    expect(all).toHaveLength(3);
    const types = all.map((t) => t.taskType).sort();
    expect(types).toEqual(["type-a", "type-b", "type-c"]);

    buffer.close();
  });

  it("getAllEvents returns all events including synced", () => {
    const buffer = new EventBuffer(dbPath);
    const taskId = randomUUID();

    const e1 = createCostEvent({ eventId: randomUUID(), taskId, costUsd: 0.01 });
    const e2 = createCostEvent({ eventId: randomUUID(), taskId, costUsd: 0.02 });

    buffer.addEvent(e1);
    buffer.addEvent(e2);

    // Sync one
    buffer.markSynced([e1.eventId]);

    // getAllEvents should return both (including synced)
    const all = buffer.getAllEvents();
    expect(all).toHaveLength(2);

    // Pending should only have one
    const pending = buffer.getPendingEvents();
    expect(pending).toHaveLength(1);
    expect(pending[0].eventId).toBe(e2.eventId);

    buffer.close();
  });
});
