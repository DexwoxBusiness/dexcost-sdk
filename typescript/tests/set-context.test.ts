/**
 * Tests for setContext / getContext / clearContext and auto-task creation
 * in the OpenAI instrument when no explicit task is active.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import {
  setContext,
  getContext,
  clearContext,
} from "../src/core/context.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import {
  instrumentOpenai,
  uninstrumentOpenai,
  _setCompletionsClass,
  _resetCompletionsClass,
} from "../src/instruments/openai.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-ctx-test-"));
  clearContext();
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
  clearContext();
});

// ---------------------------------------------------------------------------
// Basic setContext / getContext / clearContext
// ---------------------------------------------------------------------------

describe("setContext / getContext / clearContext", () => {
  it("getContext() returns undefined when not set", () => {
    // clearContext() was already called in beforeEach
    const ctx = getContext();
    // After clearContext() the store is set to {} (not undefined) so we check
    // that customerId and projectId are absent.
    expect(ctx?.customerId).toBeUndefined();
    expect(ctx?.projectId).toBeUndefined();
  });

  it("setContext() stores customerId and projectId", () => {
    setContext({ customerId: "acme", projectId: "proj-1" });
    const ctx = getContext();
    expect(ctx).toBeDefined();
    expect(ctx!.customerId).toBe("acme");
    expect(ctx!.projectId).toBe("proj-1");
  });

  it("setContext() stores metadata", () => {
    setContext({ customerId: "acme", metadata: { env: "prod", tier: 2 } });
    const ctx = getContext();
    expect(ctx!.metadata).toEqual({ env: "prod", tier: 2 });
  });

  it("setContext() defaults metadata to empty object", () => {
    setContext({ customerId: "acme" });
    const ctx = getContext();
    expect(ctx!.metadata).toEqual({});
  });

  it("clearContext() removes customerId and projectId", () => {
    setContext({ customerId: "acme", projectId: "proj-1" });
    clearContext();
    const ctx = getContext();
    expect(ctx?.customerId).toBeUndefined();
    expect(ctx?.projectId).toBeUndefined();
  });

  it("setContext() is overwritten by a subsequent call", () => {
    setContext({ customerId: "acme" });
    setContext({ customerId: "beta", projectId: "proj-2" });
    const ctx = getContext();
    expect(ctx!.customerId).toBe("beta");
    expect(ctx!.projectId).toBe("proj-2");
  });
});

// ---------------------------------------------------------------------------
// Auto-task creation in OpenAI instrument
// ---------------------------------------------------------------------------

function makeMockResponse(): Record<string, unknown> {
  return {
    id: "chatcmpl-ctx-test",
    model: "gpt-4o",
    choices: [{ message: { role: "assistant", content: "Hi!" } }],
    usage: {
      prompt_tokens: 100,
      completion_tokens: 20,
      prompt_tokens_details: { cached_tokens: 0 },
    },
  };
}

class FakeCompletions {
  async create(_body: unknown, _options?: unknown): Promise<unknown> {
    return makeMockResponse();
  }
}

describe("OpenAI instrument auto-task creation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    _setCompletionsClass(FakeCompletions);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentOpenai();
    _resetCompletionsClass();
  });

  it("creates auto-task when context has customerId but no explicit task", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();

    setContext({ customerId: "ctx-customer", projectId: "ctx-project" });
    const response = await fake.create({ model: "gpt-4o", messages: [] });

    expect((response as Record<string, unknown>).model).toBe("gpt-4o");

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("openai");
    expect(events[0].inputTokens).toBe(100);
    expect(events[0].outputTokens).toBe(20);
  });

  it("auto-task carries customerId from context", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();

    setContext({ customerId: "ctx-customer", projectId: "ctx-project" });
    await fake.create({ model: "gpt-4o", messages: [] });

    const tasks = buffer.getAllTasks();
    expect(tasks).toHaveLength(1);
    expect(tasks[0].customerId).toBe("ctx-customer");
    expect(tasks[0].projectId).toBe("ctx-project");
    expect(tasks[0].taskType).toBe("openai.chat");
  });

  it("records into an unattributed auto-task when no task and no context", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();

    // No setContext() call; clearContext() already called in beforeEach.
    // An auto-task is still created so LLM costs are never silently lost.
    const response = await fake.create({ model: "gpt-4o", messages: [] });
    expect((response as Record<string, unknown>).model).toBe("gpt-4o");
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);

    const tasks = buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.taskType === "openai.chat");
    expect(autoTask).toBeDefined();
    expect(autoTask!.customerId).toBeUndefined();
  });

  it("explicit task takes priority over context", async () => {
    const { runWithTask } = await import("../src/core/context.js");
    const { createTask } = await import("../src/core/models.js");

    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();
    const explicitTask = createTask({
      taskId: randomUUID(),
      taskType: "explicit-task",
      customerId: "explicit-customer",
    });

    setContext({ customerId: "ctx-customer" });

    await runWithTask(explicitTask, async () => {
      await fake.create({ model: "gpt-4o", messages: [] });
    });

    const tasks = buffer.getAllTasks();
    expect(tasks).toHaveLength(1);
    expect(tasks[0].customerId).toBe("explicit-customer");
    expect(tasks[0].taskType).toBe("explicit-task");
  });
});
