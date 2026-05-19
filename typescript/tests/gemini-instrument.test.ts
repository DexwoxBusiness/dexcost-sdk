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
  instrumentGemini,
  uninstrumentGemini,
  _setGenerativeModelClass,
  _resetGenerativeModelClass,
} from "../src/instruments/gemini.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

function makeMockResponse(overrides: Record<string, unknown> = {}) {
  return {
    candidates: [{ content: { parts: [{ text: "Hello" }] } }],
    usageMetadata: {
      promptTokenCount: 100,
      candidatesTokenCount: 50,
      cachedContentTokenCount: 0,
      totalTokenCount: 150,
    },
    ...overrides,
  };
}

class FakeGenerativeModel {
  model = "gemini-1.5-pro";

  async generateContent(_request: unknown): Promise<unknown> {
    return makeMockResponse();
  }

  async generateContentStream(_request: unknown): Promise<unknown> {
    return {
      stream: {
        async *[Symbol.asyncIterator]() {
          yield {
            candidates: [{ content: { parts: [{ text: "Hello" }] } }],
            usageMetadata: {
              promptTokenCount: 100,
              candidatesTokenCount: 50,
              cachedContentTokenCount: 0,
              totalTokenCount: 150,
            },
          };
        },
      },
      response: Promise.resolve(makeMockResponse()),
    };
  }
}

describe("Gemini instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    _setGenerativeModelClass(FakeGenerativeModel);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentGemini();
    _resetGenerativeModelClass();
  });

  it("records llm_call event with provider=google and correct token counts", async () => {
    await instrumentGemini(pricing, buffer);
    const fake = new FakeGenerativeModel();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const response = await fake.generateContent({ contents: [] });
      expect(response).toBeDefined();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("google");
    expect(events[0].model).toBe("gemini-1.5-pro");
    expect(events[0].inputTokens).toBe(100);
    expect(events[0].outputTokens).toBe(50);
    expect(events[0].cachedTokens).toBe(0);
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);
  });

  it("extracts cached tokens from usageMetadata", async () => {
    class CachedGenerativeModel {
      model = "gemini-1.5-pro";

      async generateContent(): Promise<unknown> {
        return makeMockResponse({
          usageMetadata: {
            promptTokenCount: 200,
            candidatesTokenCount: 80,
            cachedContentTokenCount: 120,
            totalTokenCount: 280,
          },
        });
      }
    }
    _setGenerativeModelClass(CachedGenerativeModel);
    await instrumentGemini(pricing, buffer);
    const fake = new CachedGenerativeModel();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.generateContent();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].inputTokens).toBe(200);
    expect(events[0].outputTokens).toBe(80);
    expect(events[0].cachedTokens).toBe(120);
    expect(task.totalCachedTokens).toBe(120);
  });

  it("records into an auto-task when no task and no context set", async () => {
    await instrumentGemini(pricing, buffer);
    const fake = new FakeGenerativeModel();

    const response = await fake.generateContent({ contents: [] });
    expect(response).toBeDefined();
    // LLM costs are never silently lost — an auto-task is created.
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);
    expect(
      buffer.getAllTasks().some((t) => t.taskType === "gemini.generateContent"),
    ).toBe(true);
  });

  it("creates auto-task when setContext is set but no explicit task", async () => {
    setContext({ customerId: "auto-gemini-test" });
    await instrumentGemini(pricing, buffer);
    const fake = new FakeGenerativeModel();

    const response = await fake.generateContent({ contents: [] });
    expect(response).toBeDefined();

    const events = buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-gemini-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("gemini.generateContent");

    clearContext();
  });

  it("restores original after uninstrument", async () => {
    const originalGenerate = FakeGenerativeModel.prototype.generateContent;
    await instrumentGemini(pricing, buffer);
    expect(FakeGenerativeModel.prototype.generateContent).not.toBe(originalGenerate);

    uninstrumentGemini();
    expect(FakeGenerativeModel.prototype.generateContent).toBe(originalGenerate);
  });

  it("does not double-patch", async () => {
    await instrumentGemini(pricing, buffer);
    const patchedGenerate = FakeGenerativeModel.prototype.generateContent;
    await instrumentGemini(pricing, buffer);
    expect(FakeGenerativeModel.prototype.generateContent).toBe(patchedGenerate);
  });

  it("aggregates cost into task", async () => {
    await instrumentGemini(pricing, buffer);
    const fake = new FakeGenerativeModel();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.generateContent({ contents: [] });
    });

    expect(task.totalInputTokens).toBe(100);
    expect(task.totalOutputTokens).toBe(50);
    expect(task.totalCachedTokens).toBe(0);
  });

  it("records latency in milliseconds", async () => {
    await instrumentGemini(pricing, buffer);
    const fake = new FakeGenerativeModel();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.generateContent({ contents: [] });
    });

    const events = buffer.getAllEvents();
    expect(events[0].latencyMs).toBeDefined();
    expect(typeof events[0].latencyMs).toBe("number");
  });
});

describe("Gemini streaming instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
  });

  afterEach(() => {
    buffer.close();
    uninstrumentGemini();
    _resetGenerativeModelClass();
  });

  it("records event after stream completes with usage from chunks", async () => {
    class StreamingGenerativeModel {
      model = "gemini-1.5-pro";

      async generateContent(): Promise<unknown> {
        return makeMockResponse();
      }

      async generateContentStream(): Promise<unknown> {
        const chunks = [
          {
            candidates: [{ content: { parts: [{ text: "Hello" }] } }],
            usageMetadata: {
              promptTokenCount: 100,
              candidatesTokenCount: 25,
              cachedContentTokenCount: 10,
              totalTokenCount: 125,
            },
          },
          {
            candidates: [{ content: { parts: [{ text: " world" }] } }],
            usageMetadata: {
              promptTokenCount: 100,
              candidatesTokenCount: 50,
              cachedContentTokenCount: 10,
              totalTokenCount: 150,
            },
          },
        ];

        return {
          stream: {
            async *[Symbol.asyncIterator]() {
              for (const chunk of chunks) yield chunk;
            },
          },
          response: Promise.resolve(makeMockResponse()),
        };
      }
    }

    _setGenerativeModelClass(StreamingGenerativeModel);
    await instrumentGemini(pricing, buffer);
    const fake = new StreamingGenerativeModel();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const result = (await fake.generateContentStream()) as {
        stream: AsyncIterable<unknown>;
      };
      const received: unknown[] = [];
      for await (const chunk of result.stream) {
        received.push(chunk);
      }
      expect(received).toHaveLength(2);
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("google");
    expect(events[0].model).toBe("gemini-1.5-pro");
    expect(events[0].inputTokens).toBe(100);
    expect(events[0].outputTokens).toBe(50);
    expect(events[0].cachedTokens).toBe(10);
  });
});
