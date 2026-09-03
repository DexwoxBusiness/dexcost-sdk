import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { createTask } from "../src/core/models.js";
import { runWithTask, setContext, clearContext } from "../src/core/context.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import {
  instrumentAnthropic,
  uninstrumentAnthropic,
  _setMessagesClass,
  _setMessageBatchesClass,
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

class FakeMessageBatches {
  async create(): Promise<unknown> {
    return {
      id: "msgbatch_123",
      processing_status: "in_progress",
      request_counts: { processing: 2, succeeded: 0, errored: 0, canceled: 0, expired: 0 },
    };
  }

  async retrieve(id: string): Promise<unknown> {
    return {
      id,
      processing_status: "ended",
      request_counts: { processing: 0, succeeded: 2, errored: 0, canceled: 0, expired: 0 },
    };
  }

  async cancel(id: string): Promise<unknown> {
    return {
      id,
      processing_status: "canceling",
      request_counts: { processing: 2, succeeded: 0, errored: 0, canceled: 0, expired: 0 },
    };
  }

  async results(): Promise<AsyncIterable<unknown>> {
    const rows = [
      {
        custom_id: "private-custom-id",
        result: {
          type: "succeeded",
          message: {
            model: "claude-3-5-sonnet-20241022",
            content: [{ type: "text", text: "private output" }],
            usage: {
              input_tokens: 100,
              output_tokens: 20,
              cache_read_input_tokens: 10,
              cache_creation_input_tokens: 4,
            },
          },
        },
      },
      { custom_id: "private-error-id", result: { type: "errored", error: { message: "private" } } },
    ];
    return { async *[Symbol.asyncIterator]() { for (const row of rows) yield row; } };
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
    expect(events[0].costUsd.toNumber()).toBeGreaterThan(0);
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

  it.each([
    "https://api.kimi.com/anthropic",
    "https://api.moonshot.ai/anthropic",
    "https://api.moonshot.cn/anthropic",
  ])("routes the Messages-compatible host %s to Moonshot server pricing", async (baseURL) => {
    class MoonshotMessages {
      _client = { baseURL };

      async create(): Promise<unknown> {
        return makeMockResponse({
          model: "kimi-k3",
          usage: {
            input_tokens: 16,
            output_tokens: 7,
            cache_creation_input_tokens: 3,
            cache_read_input_tokens: 4,
          },
        });
      }
    }
    _setMessagesClass(MoonshotMessages);
    await instrumentAnthropic(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "moonshot-messages" });

    await runWithTask(task, async () => {
      await new MoonshotMessages().create();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "moonshot",
      model: "kimi-k3",
      inputTokens: 16,
      outputTokens: 7,
      cachedTokens: 4,
    });
    expect(events[0].costUsd.toString()).toBe("0");
    const observation = toAttributionObservationV3(events[0]);
    expect(observation?.provider).toEqual({ name: "moonshot", service: "api" });
    expect(observation?.resource).toEqual({ type: "model", id: "kimi-k3" });
    expect(Object.fromEntries(observation?.usage.map((line) => [
      line.metric,
      line.quantity,
    ]) ?? [])).toEqual({
      input_tokens: "16",
      output_tokens: "7",
      cache_read_input_tokens: "4",
      cache_write_input_tokens: "3",
    });
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
    expect(events[0].costUsd.toNumber()).toBe(0);
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

    expect(task.llmCostUsd.toNumber()).toBeGreaterThan(0);
    expect(task.totalCostUsd.toNumber()).toBeGreaterThan(0);
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

  it("persists and reconciles Message Batch lifecycle revisions", async () => {
    _setMessageBatchesClass(FakeMessageBatches);
    await instrumentAnthropic(pricing, buffer);
    const batches = new FakeMessageBatches();
    const created = await batches.create({
      requests: [
        { custom_id: "private-one", params: { model: "claude-3-5-sonnet-20241022", messages: [] } },
        { custom_id: "private-two", params: { model: "claude-3-5-sonnet-20241022", messages: [] } },
      ],
    } as any) as any;
    expect(created.id).toBe("msgbatch_123");

    await batches.retrieve("msgbatch_123");
    const revisions = buffer.getPendingLedger("provider_job");
    expect(revisions).toHaveLength(2);
    expect(revisions[0].status).toBe("submitted");
    expect(revisions[0].resource_id).toBe("claude-3-5-sonnet-20241022");
    expect(revisions[0].billing_dimensions).toEqual([
      { key: "batch_request_count", value: "2" },
      { key: "batch_model_count", value: "1" },
    ]);
    expect(revisions[1].status).toBe("succeeded");
    expect(revisions[1].usage).toEqual([
      { metric: "batch_succeeded_request_count", quantity: "2", unit: "Requests" },
    ]);
    expect(JSON.stringify(revisions)).not.toContain("private-one");
    expect(JSON.stringify(revisions)).not.toContain("private-two");
  });

  it("aggregates batch result usage only after full stream exhaustion", async () => {
    _setMessageBatchesClass(FakeMessageBatches);
    await instrumentAnthropic(pricing, buffer);
    const batches = new FakeMessageBatches();
    await batches.create({
      requests: [{ custom_id: "private", params: { model: "claude-3-5-sonnet-20241022" } }],
    } as any);
    const stream = await batches.results("msgbatch_123" as any);
    const received: unknown[] = [];
    for await (const row of stream) received.push(row);
    expect(received).toHaveLength(2);

    const revisions = buffer.getPendingLedger("provider_job");
    expect(revisions).toHaveLength(2);
    const final = revisions[1];
    expect(final.status).toBe("succeeded");
    expect(final.task_input_tokens).toBe(100);
    expect(final.task_output_tokens).toBe(20);
    expect(final.task_cached_tokens).toBe(10);
    expect(final.usage).toEqual(expect.arrayContaining([
      { metric: "anthropic_batch_input_tokens", quantity: "100", unit: "Tokens" },
      { metric: "anthropic_batch_output_tokens", quantity: "20", unit: "Tokens" },
      { metric: "batch_succeeded_request_count", quantity: "1", unit: "Requests" },
      { metric: "batch_errored_request_count", quantity: "1", unit: "Requests" },
    ]));
    expect(JSON.stringify(final)).not.toContain("private-custom-id");
    expect(JSON.stringify(final)).not.toContain("private output");
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

  it("retains partial usage and failure identity when the stream raises", async () => {
    class FailingMessages {
      async create(_body?: unknown): Promise<unknown> {
        return {
          async *[Symbol.asyncIterator]() {
            yield {
              type: "message_start",
              message: {
                model: "claude-3-5-sonnet-20241022",
                usage: { input_tokens: 41, cache_read_input_tokens: 7 },
              },
            };
            throw new Error("provider stream failed");
          },
        };
      }
    }
    _setMessagesClass(FailingMessages);
    await instrumentAnthropic(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await expect(runWithTask(task, async () => {
      const stream = await new FailingMessages().create({ stream: true });
      for await (const _chunk of stream as AsyncIterable<unknown>) { /* drain */ }
    })).rejects.toThrow("provider stream failed");

    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0]).toMatchObject({ inputTokens: 41, cachedTokens: 7 });
    expect(buffer.getAllEvents()[0].details).toMatchObject({
      attribution_operation_status: "failed",
      attribution_error_type: "error",
    });
  });

  it("records early stream close as cancelled exactly once", async () => {
    class CancelledMessages {
      async create(_body?: unknown): Promise<unknown> {
        return {
          async *[Symbol.asyncIterator]() {
            yield {
              type: "message_start",
              message: {
                model: "claude-3-5-sonnet-20241022",
                usage: { input_tokens: 17, cache_read_input_tokens: 3 },
              },
            };
            yield { type: "message_delta", usage: { output_tokens: 9 } };
          },
        };
      }
    }
    _setMessagesClass(CancelledMessages);
    await instrumentAnthropic(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = await new CancelledMessages().create({ stream: true });
      const iterator = (stream as AsyncIterable<unknown>)[Symbol.asyncIterator]();
      await iterator.next();
      await iterator.return?.();
    });

    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0].details.attribution_operation_status).toBe("cancelled");
    expect(buffer.getAllEvents()[0].inputTokens).toBe(17);
  });
});
