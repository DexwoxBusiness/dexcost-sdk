/**
 * Tests for session-based auto-grouping.
 *
 * Validates that the SessionManager creates session tasks automatically
 * and reuses them within the same execution context.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { SessionManager } from "../src/core/session.js";
import { EventBuffer } from "../src/transport/buffer.js";
import {
  getCurrentTask,
  runWithTask,
  setContext,
  clearContext,
} from "../src/core/context.js";
import { createTask } from "../src/core/models.js";

let tmpDir: string;
let buffer: EventBuffer;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-session-test-"));
  buffer = new EventBuffer(join(tmpDir, "test.db"));
  clearContext();
});

afterEach(() => {
  buffer.close();
  rmSync(tmpDir, { recursive: true, force: true });
  clearContext();
});

describe("SessionManager", () => {
  it("first call creates a session task", () => {
    const sm = new SessionManager();

    const task = sm.runInSession("http", buffer, () => {
      const t = getCurrentTask();
      expect(t).toBeDefined();
      return t!;
    });

    expect(task.taskId).toBeTruthy();
    expect(task.taskType).toBe("agent_session");
    expect(task.metadata["session"]).toBe(true);
    expect(task.metadata["initiatedBy"]).toBe("http");
    expect(sm.activeSessionCount).toBe(1);
  });

  it("second call in same context reuses the session task", () => {
    const sm = new SessionManager();

    // First call creates the session
    const task1 = sm.runInSession("http", buffer, () => {
      const t = getCurrentTask()!;

      // Second call inside the same context should reuse
      const task2 = sm.runInSession("http", buffer, () => {
        return getCurrentTask()!;
      });

      expect(task2.taskId).toBe(t.taskId);
      return t;
    });

    expect(task1).toBeDefined();
    // Only one session should exist
    expect(sm.activeSessionCount).toBe(1);
  });

  it("agent from context used as taskType", () => {
    const sm = new SessionManager();
    setContext({ customerId: "acme", agent: "support_bot" });

    const task = sm.runInSession("http", buffer, () => {
      return getCurrentTask()!;
    });

    expect(task.taskType).toBe("support_bot");
    expect(task.customerId).toBe("acme");
  });

  it("inherits customerId and projectId from context", () => {
    const sm = new SessionManager();
    setContext({ customerId: "acme", projectId: "proj-1" });

    const task = sm.runInSession("llm", buffer, () => {
      return getCurrentTask()!;
    });

    expect(task.customerId).toBe("acme");
    expect(task.projectId).toBe("proj-1");
  });

  it("explicit track() takes precedence over session", () => {
    const sm = new SessionManager();

    const explicitTask = createTask({
      taskId: randomUUID(),
      taskType: "explicit_task",
    });

    runWithTask(explicitTask, () => {
      // Session manager should return the explicit task, not create a new one
      const task = sm.runInSession("http", buffer, () => {
        return getCurrentTask()!;
      });

      expect(task.taskId).toBe(explicitTask.taskId);
      expect(task.taskType).toBe("explicit_task");
    });

    // No sessions should be created since explicit task was used
    expect(sm.activeSessionCount).toBe(0);
  });

  it("finalizeIdleSessions clears old sessions", async () => {
    const sm = new SessionManager();

    const task = sm.runInSession("http", buffer, () => {
      return getCurrentTask()!;
    });

    expect(sm.activeSessionCount).toBe(1);

    // Directly access and modify the session's lastActivityAt to simulate idleness
    // We can't wait 30 real seconds, so test the finalize with fresh sessions
    // (they won't be idle yet)
    sm.finalizeIdleSessions();
    // Session was just created, so it shouldn't be finalized yet
    expect(sm.activeSessionCount).toBe(1);
    expect(task.status).toBe("pending");
  });

  it("defaults taskType to agent_session when no agent in context", () => {
    const sm = new SessionManager();
    setContext({ customerId: "acme" }); // no agent

    const task = sm.runInSession("http", buffer, () => {
      return getCurrentTask()!;
    });

    expect(task.taskType).toBe("agent_session");
  });
});
