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
  instrumentAnthropic,
  uninstrumentAnthropic,
  _setMessagesClass,
  _resetMessagesClass,
} from "../src/instruments/anthropic.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

function makeMockResponse(overrides: Record<string, unknown> = {}) {
  return {
    id: "msg_abc123",
    type: "message",
    model: "claude-3-5-sonnet-20241022",
    role: "assistant",
    content: [{ type: "text", text: "Hello!" }],
    usage: {
      input_tokens: 500,
      output_tokens: 100,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 50,
    },
    ...overrides,
  };
}

class FakeMessages {
  async create(_body: unknown, _options?: unknown): Promise<unknown> {
    return makeMockResponse();
  }
}

describe("Anthropic instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    _setMessagesClass(FakeMessages);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentAnthropic();
    _resetMessagesClass();
  });

  it("records llm_call event inside tracked task", async () => {
    await instrumentAnthropic(pricing, buffer);
    const fake = new FakeMessages();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const response = await fake.create({
        model: "claude-3-5-sonnet-20241022",
        max_tokens: 1024,
        messages: [{ role: "user", content: "Hello" }],
      });
      expect((response as Record<string, unknown>).model).toBe(
        "claude-3-5-sonnet-20241022",
      );
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("anthropic");
    expect(events[0].model).toBe("claude-3-5-sonnet-20241022");
    expect(events[0].inputTokens).toBe(500);
    expect(events[0].outputTokens).toBe(100);
    expect(events[0].cachedTokens).toBe(50);
    expect(events[0].costUsd).toBeGreaterThan(0);
    expect(events[0].costConfidence).toBe("computed");
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);
  });

  it("stores cache_creation_input_tokens in event details", async () => {
    class CacheCreationMessages {
      async create(): Promise<unknown> {
        return makeMockResponse({
          usage: {
            input_tokens: 500,
            output_tokens: 100,
            cache_creation_input_tokens: 300,
            cache_read_input_tokens: 50,
          },
        });
      }
    }
    _setMessagesClass(CacheCreationMessages);
    await instrumentAnthropic(pricing, buffer);
    const fake = new CacheCreationMessages();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].details["cache_creation_input_tokens"]).toBe(300);
    expect(events[0].cachedTokens).toBe(50);
  });

  it("records into an auto-task when no task and no context set", async () => {
    await instrumentAnthropic(pricing, buffer);
    const fake = new FakeMessages();

    const response = await fake.create({
      model: "claude-3-5-sonnet-20241022",
      max_tokens: 1024,
      messages: [{ role: "user", content: "Hello" }],
    });
    expect((response as Record<string, unknown>).model).toBe(
      "claude-3-5-sonnet-20241022",
    );
    // LLM costs are never silently lost — an auto-task is created.
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);
    expect(
      buffer.getAllTasks().some((t) => t.taskType === "anthropic.messages"),
    ).toBe(true);
  });

  it("creates auto-task when setContext is set but no explicit task", async () => {
    setContext({ customerId: "auto-anthropic-test" });
    await instrumentAnthropic(pricing, buffer);
    const fake = new FakeMessages();

    const response = await fake.create({
      model: "claude-3-5-sonnet-20241022",
      max_tokens: 1024,
      messages: [{ role: "user", content: "Hello" }],
    });
    expect((response as Record<string, unknown>).model).toBe(
      "claude-3-5-sonnet-20241022",
    );

    const events = buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-anthropic-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("anthropic.messages");

    clearContext();
  });

  it("handles missing usage gracefully", async () => {
    class NoUsageMessages {
      async create(): Promise<unknown> {
        return {
          id: "msg_abc",
          type: "message",
          model: "claude-3-5-sonnet-20241022",
          role: "assistant",
          content: [],
        };
      }
    }
    _setMessagesClass(NoUsageMessages);
    await instrumentAnthropic(pricing, buffer);
    const fake = new NoUsageMessages();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].costUsd).toBe(0);
    expect(events[0].costConfidence).toBe("estimated");
    expect(events[0].inputTokens).toBe(0);
    expect(events[0].outputTokens).toBe(0);
  });

  it("restores original after uninstrument", async () => {
    const originalCreate = FakeMessages.prototype.create;
    await instrumentAnthropic(pricing, buffer);
    expect(FakeMessages.prototype.create).not.toBe(originalCreate);

    uninstrumentAnthropic();
    expect(FakeMessages.prototype.create).toBe(originalCreate);
  });

  it("does not double-patch", async () => {
    await instrumentAnthropic(pricing, buffer);
    const patchedCreate = FakeMessages.prototype.create;
    await instrumentAnthropic(pricing, buffer);
    expect(FakeMessages.prototype.create).toBe(patchedCreate);
  });

  it("aggregates cost into task", async () => {
    await instrumentAnthropic(pricing, buffer);
    const fake = new FakeMessages();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create({
        model: "claude-3-5-sonnet-20241022",
        max_tokens: 1024,
        messages: [{ role: "user", content: "Hello" }],
      });
    });

    expect(task.llmCostUsd).toBeGreaterThan(0);
    expect(task.totalCostUsd).toBeGreaterThan(0);
    expect(task.totalInputTokens).toBe(500);
    expect(task.totalOutputTokens).toBe(100);
    expect(task.totalCachedTokens).toBe(50);
  });

  it("records latency in milliseconds", async () => {
    await instrumentAnthropic(pricing, buffer);
    const fake = new FakeMessages();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create({
        model: "claude-3-5-sonnet-20241022",
        max_tokens: 1024,
        messages: [{ role: "user", content: "Hello" }],
      });
    });

    const events = buffer.getAllEvents();
    expect(events[0].latencyMs).toBeDefined();
    expect(typeof events[0].latencyMs).toBe("number");
  });
});

describe("Anthropic streaming instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
  });

  afterEach(() => {
    buffer.close();
    uninstrumentAnthropic();
    _resetMessagesClass();
  });

  it("records event after stream completes", async () => {
    const streamEvents = [
      {
        type: "message_start",
        message: {
          model: "claude-3-5-sonnet-20241022",
          usage: {
            input_tokens: 300,
            cache_creation_input_tokens: 0,
            cache_read_input_tokens: 0,
          },
        },
      },
      {
        type: "content_block_delta",
        delta: { type: "text_delta", text: "Hello" },
      },
      {
        type: "content_block_delta",
        delta: { type: "text_delta", text: " world" },
      },
      {
        type: "message_delta",
        usage: { output_tokens: 50 },
      },
      {
        type: "message_stop",
      },
    ];

    class StreamingMessages {
      async create(body: Record<string, unknown>): Promise<unknown> {
        if (body.stream) {
          return {
            async *[Symbol.asyncIterator]() {
              for (const event of streamEvents) yield event;
            },
          };
        }
        return makeMockResponse();
      }
    }

    _setMessagesClass(StreamingMessages);
    await instrumentAnthropic(pricing, buffer);
    const fake = new StreamingMessages();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = await fake.create({
        model: "claude-3-5-sonnet-20241022",
        max_tokens: 1024,
        messages: [{ role: "user", content: "Hello" }],
        stream: true,
      });
      const received: unknown[] = [];
      for await (const chunk of stream as AsyncIterable<unknown>) {
        received.push(chunk);
      }
      expect(received).toHaveLength(5);
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("anthropic");
    expect(events[0].model).toBe("claude-3-5-sonnet-20241022");
    expect(events[0].inputTokens).toBe(300);
    expect(events[0].outputTokens).toBe(50);
  });

  it("records cache tokens from streaming message_start", async () => {
    const streamEvents = [
      {
        type: "message_start",
        message: {
          model: "claude-3-5-sonnet-20241022",
          usage: {
            input_tokens: 400,
            cache_creation_input_tokens: 200,
            cache_read_input_tokens: 100,
          },
        },
      },
      {
        type: "message_delta",
        usage: { output_tokens: 75 },
      },
      {
        type: "message_stop",
      },
    ];

    class CacheStreamingMessages {
      async create(body: Record<string, unknown>): Promise<unknown> {
        if (body.stream) {
          return {
            async *[Symbol.asyncIterator]() {
              for (const event of streamEvents) yield event;
            },
          };
        }
        return makeMockResponse();
      }
    }

    _setMessagesClass(CacheStreamingMessages);
    await instrumentAnthropic(pricing, buffer);
    const fake = new CacheStreamingMessages();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = await fake.create({
        model: "claude-3-5-sonnet-20241022",
        max_tokens: 1024,
        messages: [],
        stream: true,
      });
      for await (const _chunk of stream as AsyncIterable<unknown>) {
        // consume
      }
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].cachedTokens).toBe(100);
    expect(events[0].details["cache_creation_input_tokens"]).toBe(200);
    expect(events[0].inputTokens).toBe(400);
    expect(events[0].outputTokens).toBe(75);
  });
});
