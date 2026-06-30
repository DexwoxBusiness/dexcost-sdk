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
  instrumentVercelAi,
  uninstrumentVercelAi,
  _setAiModule,
  _resetAiModule,
} from "../src/instruments/vercel-ai.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

function makeMockGenerateTextResult(overrides: Record<string, unknown> = {}) {
  return {
    text: "Hello! How can I help you?",
    usage: {
      promptTokens: 600,
      completionTokens: 120,
    },
    finishReason: "stop",
    ...overrides,
  };
}

function createFakeAiModule() {
  return {
    generateText: async function (_opts: unknown): Promise<unknown> {
      return makeMockGenerateTextResult();
    },
    streamText: function (_opts: unknown): AsyncIterable<unknown> & { usage: unknown } {
      const chunks = [
        { type: "text-delta", textDelta: "Hello" },
        { type: "text-delta", textDelta: " world" },
      ];
      const usageData = { promptTokens: 400, completionTokens: 80 };
      const iterable = {
        usage: usageData,
        async *[Symbol.asyncIterator]() {
          for (const chunk of chunks) yield chunk;
        },
      };
      return iterable;
    },
  };
}

describe("Vercel AI SDK instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;
  let fakeAi: ReturnType<typeof createFakeAiModule>;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    fakeAi = createFakeAiModule();
    _setAiModule(fakeAi);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentVercelAi();
    _resetAiModule();
  });

  it("is registered in instrument registry", async () => {
    const { ALL_SUPPORTED_INSTRUMENTS } = await import("../src/instruments/index.js");
    expect(ALL_SUPPORTED_INSTRUMENTS).toContain("vercel-ai");
  });

  it("exports test helpers", async () => {
    const mod = await import("../src/instruments/vercel-ai.js");
    expect(typeof mod._setAiModule).toBe("function");
    expect(typeof mod._resetAiModule).toBe("function");
  });

  it("records llm_call event inside tracked task for generateText", async () => {
    await instrumentVercelAi(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const result = await fakeAi.generateText({
        model: { modelId: "gpt-4o" },
        prompt: "Hello",
      });
      expect((result as Record<string, unknown>).text).toBe("Hello! How can I help you?");
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("vercel-ai");
    expect(events[0].model).toBe("gpt-4o");
    expect(events[0].inputTokens).toBe(600);
    expect(events[0].outputTokens).toBe(120);
    expect(events[0].costUsd.toNumber()).toBeGreaterThanOrEqual(0);
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);
  });

  it("records into an auto-task when no task and no context set for generateText", async () => {
    await instrumentVercelAi(pricing, buffer);

    const result = await fakeAi.generateText({
      model: { modelId: "gpt-4o" },
      prompt: "Hello",
    });
    expect((result as Record<string, unknown>).text).toBe("Hello! How can I help you?");
    // LLM costs are never silently lost — an auto-task is created.
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);
    expect(
      buffer.getAllTasks().some((t) => t.taskType === "vercel-ai.generateText"),
    ).toBe(true);
  });

  it("creates auto-task when setContext is set but no explicit task for generateText", async () => {
    setContext({ customerId: "auto-vercel-test" });
    await instrumentVercelAi(pricing, buffer);

    const result = await fakeAi.generateText({
      model: { modelId: "gpt-4o" },
      prompt: "Hello",
    });
    expect((result as Record<string, unknown>).text).toBe("Hello! How can I help you?");

    const events = buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-vercel-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("vercel-ai.generateText");

    clearContext();
  });

  it("aggregates cost into task for generateText", async () => {
    await instrumentVercelAi(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fakeAi.generateText({
        model: { modelId: "gpt-4o" },
        prompt: "Hello",
      });
    });

    expect(task.totalInputTokens).toBe(600);
    expect(task.totalOutputTokens).toBe(120);
  });

  it("handles missing usage gracefully", async () => {
    const noUsageAi = {
      generateText: async function (): Promise<unknown> {
        return { text: "Hello", finishReason: "stop" };
      },
      streamText: function (): unknown {
        return { async *[Symbol.asyncIterator]() { /* empty */ } };
      },
    };
    _setAiModule(noUsageAi);
    await instrumentVercelAi(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await noUsageAi.generateText();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].costUsd.toNumber()).toBe(0);
    expect(events[0].costConfidence).toBe("estimated");
    expect(events[0].inputTokens).toBe(0);
    expect(events[0].outputTokens).toBe(0);
  });

  it("extracts model from string", async () => {
    await instrumentVercelAi(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fakeAi.generateText({
        model: "claude-3-haiku",
        prompt: "Hi",
      });
    });

    const events = buffer.getAllEvents();
    expect(events[0].model).toBe("claude-3-haiku");
  });

  it("extracts model from modelId object", async () => {
    await instrumentVercelAi(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fakeAi.generateText({
        model: { modelId: "gpt-4-turbo", provider: "openai" },
        prompt: "Hello",
      });
    });

    const events = buffer.getAllEvents();
    expect(events[0].model).toBe("gpt-4-turbo");
  });

  it("restores originals after uninstrument", async () => {
    const originalGenerateText = fakeAi.generateText;
    const originalStreamText = fakeAi.streamText;
    await instrumentVercelAi(pricing, buffer);
    expect(fakeAi.generateText).not.toBe(originalGenerateText);
    expect(fakeAi.streamText).not.toBe(originalStreamText);

    uninstrumentVercelAi();
    expect(fakeAi.generateText).toBe(originalGenerateText);
    expect(fakeAi.streamText).toBe(originalStreamText);
  });

  it("does not double-patch", async () => {
    await instrumentVercelAi(pricing, buffer);
    const patchedGenerateText = fakeAi.generateText;
    await instrumentVercelAi(pricing, buffer);
    expect(fakeAi.generateText).toBe(patchedGenerateText);
  });

  it("records latency in milliseconds", async () => {
    await instrumentVercelAi(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fakeAi.generateText({
        model: { modelId: "gpt-4o" },
        prompt: "Hello",
      });
    });

    const events = buffer.getAllEvents();
    expect(events[0].latencyMs).toBeDefined();
    expect(typeof events[0].latencyMs).toBe("number");
  });

  it("gracefully handles missing ai package via registry", async () => {
    // Reset so no mock module is injected
    _resetAiModule();
    uninstrumentVercelAi();

    const { instrumentProvider } = await import("../src/instruments/index.js");
    const result = await instrumentProvider("vercel-ai", pricing, buffer);
    // Should return false because the ai package is not installed
    expect(result).toBe(false);
  });
});

describe("Vercel AI SDK streaming instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;
  let fakeAi: ReturnType<typeof createFakeAiModule>;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    fakeAi = createFakeAiModule();
    _setAiModule(fakeAi);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentVercelAi();
    _resetAiModule();
  });

  it("records event after stream completes", async () => {
    await instrumentVercelAi(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = fakeAi.streamText({
        model: { modelId: "gpt-4o" },
        prompt: "Hello",
      });
      const received: unknown[] = [];
      for await (const chunk of stream as AsyncIterable<unknown>) {
        received.push(chunk);
      }
      expect(received).toHaveLength(2);
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("vercel-ai");
    expect(events[0].model).toBe("gpt-4o");
    expect(events[0].inputTokens).toBe(400);
    expect(events[0].outputTokens).toBe(80);
  });

  it("records stream into an auto-task when no task and no context set", async () => {
    await instrumentVercelAi(pricing, buffer);

    const stream = fakeAi.streamText({
      model: { modelId: "gpt-4o" },
      prompt: "Hello",
    });
    const received: unknown[] = [];
    for await (const chunk of stream as AsyncIterable<unknown>) {
      received.push(chunk);
    }
    expect(received).toHaveLength(2);
    // LLM costs are never silently lost — an auto-task is created.
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);
    expect(
      buffer.getAllTasks().some((t) => t.taskType === "vercel-ai.streamText"),
    ).toBe(true);
  });

  it("aggregates streaming usage into task", async () => {
    await instrumentVercelAi(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = fakeAi.streamText({
        model: { modelId: "gpt-4o" },
        prompt: "Hello",
      });
      for await (const _chunk of stream as AsyncIterable<unknown>) {
        // consume
      }
    });

    expect(task.totalInputTokens).toBe(400);
    expect(task.totalOutputTokens).toBe(80);
  });

  it("finalizes the auto-task when streamText returns a non-iterable result", async () => {
    // Regression: when the underlying streamText returns a result that is
    // NOT async-iterable, patchedStreamText takes a fallback path that
    // returns the result directly. Previously it skipped finalization,
    // leaking the auto-created task as "pending" forever.
    const nonIterableAi = {
      generateText: async (): Promise<unknown> => makeMockGenerateTextResult(),
      streamText: function (_opts: unknown): unknown {
        return { text: "no stream here" };
      },
    };
    _setAiModule(nonIterableAi);
    await instrumentVercelAi(pricing, buffer);

    const result = (await nonIterableAi.streamText({
      model: { modelId: "gpt-4o" },
      prompt: "Hello",
    })) as Record<string, unknown>;
    expect(result.text).toBe("no stream here");

    const autoTask = buffer
      .getAllTasks()
      .find((t) => t.taskType === "vercel-ai.streamText");
    expect(autoTask).toBeDefined();
    expect(autoTask!.status).toBe("success");
    expect(autoTask!.endedAt).not.toBeNull();
  });
});
