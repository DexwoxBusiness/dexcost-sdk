import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { createTask } from "../src/core/models.js";
import { runWithTask, setContext, clearContext } from "../src/core/context.js";
import {
  instrumentOpenai,
  uninstrumentOpenai,
  _setCompletionsClass,
  _resetCompletionsClass,
} from "../src/instruments/openai.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

function makeMockResponse(overrides: Record<string, unknown> = {}) {
  return {
    id: "chatcmpl-abc123",
    model: "gpt-4o",
    choices: [{ message: { role: "assistant", content: "Hello!" } }],
    usage: {
      prompt_tokens: 800,
      completion_tokens: 150,
      prompt_tokens_details: { cached_tokens: 50 },
    },
    ...overrides,
  };
}

class FakeCompletions {
  async create(_body: unknown, _options?: unknown): Promise<unknown> {
    return makeMockResponse();
  }
}

describe("OpenAI instrumentation", () => {
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

  it("records llm_call event inside tracked task", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const response = await fake.create({ model: "gpt-4o", messages: [] });
      expect((response as Record<string, unknown>).model).toBe("gpt-4o");
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("openai");
    expect(events[0].model).toBe("gpt-4o");
    expect(events[0].inputTokens).toBe(800);
    expect(events[0].outputTokens).toBe(150);
    expect(events[0].cachedTokens).toBe(50);
    expect(events[0].costUsd.toNumber()).toBeGreaterThan(0);
    expect(events[0].costConfidence).toBe("computed");
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);
  });

  it("records into an auto-task when no task and no context set", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();

    const response = await fake.create({ model: "gpt-4o", messages: [] });
    expect((response as Record<string, unknown>).model).toBe("gpt-4o");
    // LLM costs are never silently lost — an auto-task is created.
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);
    expect(buffer.getAllTasks().some((t) => t.taskType === "openai.chat")).toBe(true);
  });

  it("creates auto-task when setContext is set but no explicit task", async () => {
    setContext({ customerId: "auto-test" });
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();

    const response = await fake.create({ model: "gpt-4o", messages: [] });
    expect((response as Record<string, unknown>).model).toBe("gpt-4o");

    const events = buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("openai.chat");

    clearContext();
  });

  it("aggregates cost into task", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create({ model: "gpt-4o", messages: [] });
    });

    expect(task.llmCostUsd.toNumber()).toBeGreaterThan(0);
    expect(task.totalCostUsd.toNumber()).toBeGreaterThan(0);
    expect(task.totalInputTokens).toBe(800);
    expect(task.totalOutputTokens).toBe(150);
    expect(task.totalCachedTokens).toBe(50);
  });

  it("handles missing usage gracefully", async () => {
    class NoUsageCompletions {
      async create(): Promise<unknown> {
        return { id: "chatcmpl-abc", model: "gpt-4o", choices: [] };
      }
    }
    _setCompletionsClass(NoUsageCompletions);
    await instrumentOpenai(pricing, buffer);
    const fake = new NoUsageCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].costUsd.toNumber()).toBe(0);
    expect(events[0].costConfidence).toBe("estimated");
    expect(events[0].inputTokens).toBe(0);
    expect(events[0].outputTokens).toBe(0);
  });

  it("restores original after uninstrument", async () => {
    const originalCreate = FakeCompletions.prototype.create;
    await instrumentOpenai(pricing, buffer);
    expect(FakeCompletions.prototype.create).not.toBe(originalCreate);

    uninstrumentOpenai();
    expect(FakeCompletions.prototype.create).toBe(originalCreate);
  });

  it("does not double-patch", async () => {
    await instrumentOpenai(pricing, buffer);
    const patchedCreate = FakeCompletions.prototype.create;
    await instrumentOpenai(pricing, buffer);
    expect(FakeCompletions.prototype.create).toBe(patchedCreate);
  });

  it("records latency in milliseconds", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create({ model: "gpt-4o", messages: [] });
    });

    const events = buffer.getAllEvents();
    expect(events[0].latencyMs).toBeDefined();
    expect(typeof events[0].latencyMs).toBe("number");
  });
});

describe("OpenAI streaming instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
  });

  afterEach(() => {
    buffer.close();
    uninstrumentOpenai();
    _resetCompletionsClass();
  });

  it("records event after stream completes", async () => {
    const chunks = [
      { model: "gpt-4o", choices: [{ delta: { content: "Hello" } }] },
      { model: "gpt-4o", choices: [{ delta: { content: " world" } }] },
      {
        model: "gpt-4o",
        choices: [{ delta: {} }],
        usage: { prompt_tokens: 100, completion_tokens: 20 },
      },
    ];

    class StreamingCompletions {
      async create(body: Record<string, unknown>): Promise<unknown> {
        if (body.stream) {
          return {
            async *[Symbol.asyncIterator]() {
              for (const chunk of chunks) yield chunk;
            },
          };
        }
        return makeMockResponse();
      }
    }

    _setCompletionsClass(StreamingCompletions);
    await instrumentOpenai(pricing, buffer);
    const fake = new StreamingCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = await fake.create({
        model: "gpt-4o",
        messages: [],
        stream: true,
      });
      const received: unknown[] = [];
      for await (const chunk of stream as AsyncIterable<unknown>) {
        received.push(chunk);
      }
      expect(received).toHaveLength(3);
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].model).toBe("gpt-4o");
    expect(events[0].inputTokens).toBe(100);
    expect(events[0].outputTokens).toBe(20);
  });
});
