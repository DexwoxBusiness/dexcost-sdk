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
  instrumentCohere,
  uninstrumentCohere,
  _setClientClass,
  _resetClientClass,
} from "../src/instruments/cohere.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

const mockResponse = {
  text: "Hello",
  meta: {
    billedUnits: {
      inputTokens: 100,
      outputTokens: 50,
    },
  },
};

class FakeCohereClient {
  async chat(_body: unknown, _options?: unknown): Promise<unknown> {
    return { ...mockResponse, meta: { ...mockResponse.meta, billedUnits: { ...mockResponse.meta.billedUnits } } };
  }

  async chatStream(_body: unknown, _options?: unknown): Promise<unknown> {
    const events = [
      { eventType: "text-generation", text: "Hello" },
      { eventType: "text-generation", text: " world" },
      {
        eventType: "stream-end",
        response: {
          text: "Hello world",
          meta: {
            billedUnits: {
              inputTokens: 100,
              outputTokens: 50,
            },
          },
        },
      },
    ];
    return {
      async *[Symbol.asyncIterator]() {
        for (const event of events) yield event;
      },
    };
  }
}

describe("Cohere instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    _setClientClass(FakeCohereClient);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentCohere();
    _resetClientClass();
  });

  it("records event with provider=cohere and tokens from billedUnits", async () => {
    await instrumentCohere(pricing, buffer);
    const client = new FakeCohereClient();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const response = await client.chat({ model: "command-r-plus", message: "Hello" });
      expect((response as Record<string, unknown>).text).toBe("Hello");
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("cohere");
    expect(events[0].model).toBe("command-r-plus");
    expect(events[0].inputTokens).toBe(100);
    expect(events[0].outputTokens).toBe(50);
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);
  });

  it("creates auto-task when no task context", async () => {
    setContext({ customerId: "auto-cohere-test" });
    await instrumentCohere(pricing, buffer);
    const client = new FakeCohereClient();

    const response = await client.chat({ model: "command-r-plus", message: "Hello" });
    expect((response as Record<string, unknown>).text).toBe("Hello");

    const events = buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-cohere-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("cohere.chat");

    clearContext();
  });

  it("uninstrument restores original", async () => {
    const originalChat = FakeCohereClient.prototype.chat;
    await instrumentCohere(pricing, buffer);
    expect(FakeCohereClient.prototype.chat).not.toBe(originalChat);

    uninstrumentCohere();
    expect(FakeCohereClient.prototype.chat).toBe(originalChat);
  });
});
