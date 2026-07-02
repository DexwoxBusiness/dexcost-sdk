/**
 * Tests for AsyncLocalStorage-based task context propagation.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  getCurrentTask,
  runWithTask,
  runWithContext,
  getContext,
  setContext,
  clearContext,
} from "../src/core/context.js";
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

describe("runWithContext (scoped ambient context)", () => {
  afterEach(() => {
    clearContext();
  });

  it("scopes the context to the callback, including async continuations", async () => {
    expect(getContext()).toBeUndefined();

    const inside = await runWithContext(
      { customerId: "org-1", projectId: "repo-a", agent: "kodus_code_review" },
      async () => {
        await new Promise((r) => setTimeout(r, 5));
        return getContext();
      },
    );

    expect(inside?.customerId).toBe("org-1");
    expect(inside?.agent).toBe("kodus_code_review");
    // Restored after the scope ends — no leak into the rest of the chain
    // (the setContext/enterWith failure mode in worker loops).
    expect(getContext()).toBeUndefined();
  });

  it("does not leak across sequential jobs on the same async chain", async () => {
    const seen: Array<string | undefined> = [];

    for (const job of ["cust-a", "cust-b"]) {
      await runWithContext({ customerId: job }, async () => {
        await new Promise((r) => setTimeout(r, 1));
        seen.push(getContext()?.customerId);
      });
      seen.push(getContext()?.customerId);
    }

    expect(seen).toEqual(["cust-a", undefined, "cust-b", undefined]);
  });

  it("restores an outer setContext after the scope ends", () => {
    setContext({ customerId: "outer" });
    runWithContext({ customerId: "inner" }, () => {
      expect(getContext()?.customerId).toBe("inner");
    });
    expect(getContext()?.customerId).toBe("outer");
  });

  it("each scope is a distinct session grouping key", async () => {
    const { SessionManager } = await import("../src/core/session.js");
    const { EventBuffer } = await import("../src/transport/buffer.js");
    const tmp = mkdtempSync(join(tmpdir(), "dexcost-ctx-test-"));
    const buffer = new EventBuffer(join(tmp, "test.db"));
    try {
      const sm = new SessionManager();
      const taskA = runWithContext({ customerId: "a" }, () =>
        sm.runInSession("http", buffer, () => getCurrentTask()!),
      );
      const taskB = runWithContext({ customerId: "b" }, () =>
        sm.runInSession("http", buffer, () => getCurrentTask()!),
      );
      expect(taskA.taskId).not.toBe(taskB.taskId);
      expect(taskA.customerId).toBe("a");
      expect(taskB.customerId).toBe("b");
    } finally {
      buffer.close();
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
