/**
 * Task lifecycle + attribution regression tests.
 *
 * Covers the audit findings:
 * 1. Session tasks are REUSED across sibling calls (grouping key = ambient
 *    DexcostContext, or a global key), instead of one single-call
 *    "agent_session" task per HTTP call.
 * 2. Auto-created tasks (instruments / HTTP adapter / sessions) register a
 *    NetworkAccountant and get byte aggregates + egress dollars at
 *    finalization — previously only explicit track() tasks did.
 * 3. Auto/session tasks transition out of "pending" (previously the HTTP
 *    adapter's fallback auto-tasks were never finalized).
 * 4. Nested track() calls link child tasks to their parent.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { SessionManager } from "../src/core/session.js";
import { createAutoTask, finalizeAutoTask } from "../src/core/auto-task.js";
import { getAccountant } from "../src/adapters/network-accountant.js";
import {
  getCurrentTask,
  setContext,
  clearContext,
} from "../src/core/context.js";
import {
  trackHttp,
  untrackHttp,
  clearDomainRates,
  clearRecordedEvents,
  resetServiceCatalog,
  getSessionManager,
} from "../src/adapters/http.js";

let tmpDir: string;
let buffer: EventBuffer;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-lifecycle-test-"));
  buffer = new EventBuffer(join(tmpDir, "test.db"));
  clearContext();
});

afterEach(() => {
  untrackHttp();
  clearDomainRates();
  clearRecordedEvents();
  resetServiceCatalog();
  clearContext();
  vi.unstubAllGlobals();
  buffer.close();
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("session reuse (parent/child attribution of ambient calls)", () => {
  it("sibling calls with no ambient context reuse ONE session task", () => {
    const sm = new SessionManager();

    const task1 = sm.runInSession("http", buffer, () => getCurrentTask()!);
    // A second, structurally independent call (NOT nested in the first).
    const task2 = sm.runInSession("http", buffer, () => getCurrentTask()!);

    expect(task2.taskId).toBe(task1.taskId);
    expect(sm.activeSessionCount).toBe(1);
  });

  it("distinct ambient contexts get distinct session tasks", () => {
    const sm = new SessionManager();

    setContext({ customerId: "customer-a" });
    const taskA = sm.runInSession("http", buffer, () => getCurrentTask()!);
    setContext({ customerId: "customer-b" });
    const taskB = sm.runInSession("http", buffer, () => getCurrentTask()!);

    expect(taskA.taskId).not.toBe(taskB.taskId);
    expect(taskA.customerId).toBe("customer-a");
    expect(taskB.customerId).toBe("customer-b");
    expect(sm.activeSessionCount).toBe(2);
  });
});

describe("network accounting on auto/session tasks", () => {
  it("session finalization drains bytes into byte aggregates + egress cost", () => {
    const sm = new SessionManager();
    const task = sm.runInSession("http", buffer, () => getCurrentTask()!);

    // The session registered an accountant at creation; simulate the
    // patched fetch recording one call: 105KB down, 500MB up (external).
    const accountant = getAccountant(task.taskId);
    expect(accountant).toBeDefined();
    accountant!.record("api.kimi.com", 105_000, 500_000_000, false);

    sm.finalizeAllSessions(buffer);

    expect(task.status).toBe("success");
    expect(task.endedAt).not.toBeNull();
    expect(task.networkCallCount).toBe(1);
    expect(task.networkBytesIn).toBe(105_000);
    expect(task.networkBytesOut).toBe(500_000_000);
    // 0.5 GB of external egress must be priced (> $0 at any catalog rate).
    expect(task.networkCostUsd.toNumber()).toBeGreaterThan(0);
    // Egress rolls into the task total.
    expect(task.totalCostUsd.toNumber()).toBeGreaterThanOrEqual(
      task.networkCostUsd.toNumber(),
    );
    // Accountant is drained — registry entry must not leak.
    expect(getAccountant(task.taskId)).toBeUndefined();
  });

  it("createAutoTask registers an accountant; finalizeAutoTask prices and persists it", () => {
    const task = createAutoTask("vercel-ai.generateText");
    const accountant = getAccountant(task.taskId);
    expect(accountant).toBeDefined();

    accountant!.record("api.anthropic.com", 50_000, 200_000_000, false);
    finalizeAutoTask(task, "success", buffer);

    expect(task.status).toBe("success");
    expect(task.endedAt).not.toBeNull();
    expect(task.networkBytesOut).toBe(200_000_000);
    expect(task.networkCostUsd.toNumber()).toBeGreaterThan(0);
    expect(getAccountant(task.taskId)).toBeUndefined();

    const stored = buffer.getAllTasks().find((t) => t.taskId === task.taskId);
    expect(stored).toBeDefined();
    expect(stored!.status).toBe("success");
  });

  it("finalizeAutoTask with failed status increments failureCount", () => {
    const task = createAutoTask("anthropic.messages");
    finalizeAutoTask(task, "failed", buffer);
    expect(task.status).toBe("failed");
    expect(task.failureCount).toBe(1);
    expect(getAccountant(task.taskId)).toBeUndefined();
  });
});

describe("ambient (kodus-style) end-to-end: capture + grouping + egress", () => {
  it("un-wrapped LLM calls share one session task that finalizes with tokens AND network bytes", async () => {
    const pricing = new PricingEngine();
    const anthropicBody = JSON.stringify({
      id: "msg_01",
      type: "message",
      model: "kimi-k2-0905-preview",
      content: [{ type: "text", text: "hi" }],
      usage: { input_tokens: 1200, output_tokens: 340 },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () =>
        new Response(anthropicBody, {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    trackHttp(buffer, pricing);

    // Two calls, no explicit task, no ambient context — the kodus scenario.
    for (let i = 0; i < 2; i++) {
      const res = await fetch("https://api.kimi.com/anthropic/v1/messages", {
        method: "POST",
        body: JSON.stringify({ model: "kimi-k2-0905-preview", messages: [] }),
      });
      await res.text();
    }

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(2);
    // Both calls attributed to the SAME session task (grouping fix).
    expect(llmEvents[1].taskId).toBe(llmEvents[0].taskId);

    const sm = getSessionManager();
    expect(sm).not.toBeNull();
    sm!.finalizeAllSessions(buffer);

    const sessionTask = buffer
      .getAllTasks()
      .find((t) => t.taskId === llmEvents[0].taskId);
    expect(sessionTask).toBeDefined();
    // No longer stuck pending.
    expect(sessionTask!.status).toBe("success");
    // LLM dimension: tokens aggregated across both calls.
    expect(sessionTask!.totalInputTokens).toBe(2400);
    expect(sessionTask!.totalOutputTokens).toBe(680);
    // Network dimension: bytes of the same calls counted separately.
    expect(sessionTask!.networkCallCount).toBe(2);
    expect(sessionTask!.networkBytesOut).toBeGreaterThan(0);
    expect(sessionTask!.networkBytesIn).toBeGreaterThan(0);
  });
});

describe("parent → child task attribution", () => {
  it("nested track() links the child to its parent task", async () => {
    const { CostTracker } = await import("../src/index.js");
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "tracker.db"),
      autoInstrument: [],
      trackHttp: false,
    });

    let parentId = "";
    let childId = "";
    await tracker.track({ taskType: "parent_flow" }, async () => {
      parentId = getCurrentTask()!.taskId;
      await tracker.track({ taskType: "child_step" }, async () => {
        childId = getCurrentTask()!.taskId;
      });
    });

    const tasks = tracker.buffer.getAllTasks();
    const parent = tasks.find((t) => t.taskId === parentId)!;
    const child = tasks.find((t) => t.taskId === childId)!;

    expect(parent.parentTaskId).toBeUndefined();
    expect(child.parentTaskId).toBe(parentId);
    expect(parent.status).toBe("success");
    expect(child.status).toBe("success");

    tracker.close();
  });
});
