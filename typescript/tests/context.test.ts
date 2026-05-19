/**
 * Tests for AsyncLocalStorage-based task context propagation.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { getCurrentTask, runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { CostTracker } from "../src/core/tracker.js";
import { randomUUID } from "node:crypto";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("Context propagation", () => {
  it("getCurrentTask() returns undefined outside track()", () => {
    expect(getCurrentTask()).toBeUndefined();
  });

  it("getCurrentTask() returns the active task inside runWithTask()", () => {
    const task = createTask({
      taskId: randomUUID(),
      taskType: "test-context",
    });

    const result = runWithTask(task, () => {
      const current = getCurrentTask();
      expect(current).toBeDefined();
      expect(current!.taskId).toBe(task.taskId);
      expect(current!.taskType).toBe("test-context");
      return current;
    });

    expect(result).toBeDefined();
  });

  it("nested track() sets parentTaskId", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    let outerTaskId: string | undefined;
    let innerParentId: string | undefined;

    await tracker.track({ taskType: "outer" }, async (outerTracked) => {
      outerTaskId = outerTracked.task.taskId;

      await tracker.track({ taskType: "inner" }, async (innerTracked) => {
        innerParentId = innerTracked.task.parentTaskId;
      });
    });

    expect(outerTaskId).toBeTruthy();
    expect(innerParentId).toBe(outerTaskId);

    tracker.close();
  });

  it("context does not leak between parallel tasks", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    const taskIds: string[] = [];

    await Promise.all([
      tracker.track({ taskType: "parallel-a" }, async (tracked) => {
        await new Promise((resolve) => setTimeout(resolve, 10));
        const current = getCurrentTask();
        expect(current?.taskId).toBe(tracked.task.taskId);
        taskIds.push(tracked.task.taskId);
      }),
      tracker.track({ taskType: "parallel-b" }, async (tracked) => {
        await new Promise((resolve) => setTimeout(resolve, 10));
        const current = getCurrentTask();
        expect(current?.taskId).toBe(tracked.task.taskId);
        taskIds.push(tracked.task.taskId);
      }),
    ]);

    expect(taskIds).toHaveLength(2);
    expect(taskIds[0]).not.toBe(taskIds[1]);

    tracker.close();
  });
});
