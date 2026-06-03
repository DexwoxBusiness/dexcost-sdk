/**
 * Integration tests for CostTracker auto-instrumentation.
 *
 * Verifies that CostTracker with autoInstrument option correctly
 * instruments LLM SDKs and captures events end-to-end.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { CostTracker } from "../src/core/tracker.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});
import {
  _setCompletionsClass,
  _resetCompletionsClass,
} from "../src/instruments/openai.js";
import {
  _setMessagesClass,
  _resetMessagesClass,
} from "../src/instruments/anthropic.js";

describe("CostTracker auto-instrumentation integration", () => {
  afterEach(() => {
    _resetCompletionsClass();
    _resetMessagesClass();
  });

  it("auto-instruments and captures OpenAI LLM calls via track()", async () => {
    class FakeCompletions {
      async create(): Promise<any> {
        return {
          model: "gpt-4o",
          choices: [{ message: { role: "assistant", content: "Hi" } }],
          usage: { prompt_tokens: 500, completion_tokens: 100 },
        };
      }
    }
    _setCompletionsClass(FakeCompletions);

    const tracker = new CostTracker({ autoInstrument: ["openai"], dbPath: join(tmpDir, "test.db") });
    // Explicitly await to ensure patch is applied
    await tracker.instrument("openai");

    await tracker.track({ taskType: "chat", customerId: "acme" }, async (task) => {
      const fake = new FakeCompletions();
      await fake.create();

      // Event should be auto-recorded
      expect(task.task.llmCostUsd.toNumber()).toBeGreaterThan(0);
      expect(task.task.totalInputTokens).toBe(500);
      expect(task.task.totalOutputTokens).toBe(100);
    });

    const events = tracker.buffer.getAllEvents();
    expect(events.some((e) => e.eventType === "llm_call")).toBe(true);

    const llmEvent = events.find((e) => e.eventType === "llm_call")!;
    expect(llmEvent.provider).toBe("openai");
    expect(llmEvent.model).toBe("gpt-4o");
    expect(llmEvent.costConfidence).toBe("computed");
    expect(llmEvent.pricingSource).toBe("litellm");

    tracker.close();
  });

  it("auto-instruments and captures Anthropic LLM calls via track()", async () => {
    class FakeMessages {
      async create(): Promise<any> {
        return {
          id: "msg_abc123",
          type: "message",
          model: "claude-3-5-sonnet-20241022",
          role: "assistant",
          content: [{ type: "text", text: "Hello!" }],
          usage: {
            input_tokens: 300,
            output_tokens: 80,
            cache_creation_input_tokens: 0,
            cache_read_input_tokens: 0,
          },
        };
      }
    }
    _setMessagesClass(FakeMessages);

    const tracker = new CostTracker({ autoInstrument: ["anthropic"], dbPath: join(tmpDir, "test.db") });
    await tracker.instrument("anthropic");

    await tracker.track({ taskType: "summarize", customerId: "acme" }, async (task) => {
      const fake = new FakeMessages();
      await fake.create();

      expect(task.task.llmCostUsd.toNumber()).toBeGreaterThan(0);
      expect(task.task.totalInputTokens).toBe(300);
      expect(task.task.totalOutputTokens).toBe(80);
    });

    const events = tracker.buffer.getAllEvents();
    const llmEvent = events.find((e) => e.eventType === "llm_call")!;
    expect(llmEvent.provider).toBe("anthropic");
    expect(llmEvent.model).toBe("claude-3-5-sonnet-20241022");

    tracker.close();
  });

  it("autoInstrument=[] disables all instrumentation", async () => {
    const tracker = new CostTracker({ autoInstrument: [], dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "chat" }, async () => {
      // No instrumentation — manual recording still works
    });

    expect(tracker.buffer.getAllEvents()).toHaveLength(0);
    tracker.close();
  });

  it("manual recording still works alongside auto-instrumentation", async () => {
    const tracker = new CostTracker({ autoInstrument: [], dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "mixed" }, async (task) => {
      task.recordLlmCall("openai", "gpt-4o", 500, 100, 0.03);
      task.recordCost("pdf_parser", 0.002);

      expect(task.task.llmCostUsd.toNumber()).toBeCloseTo(0.03);
      expect(task.task.externalCostUsd.toNumber()).toBeCloseTo(0.002);
      expect(task.task.totalCostUsd.toNumber()).toBeCloseTo(0.032);
    });

    expect(tracker.buffer.getAllEvents()).toHaveLength(2);
    tracker.close();
  });

  it("exposes pricing engine via getter", () => {
    const tracker = new CostTracker({ autoInstrument: [], dbPath: join(tmpDir, "test.db") });
    expect(tracker.pricing).toBeDefined();
    expect(tracker.pricing.pricingVersion).toBeTruthy();
    tracker.close();
  });

  it("close() uninstruments all providers", async () => {
    class FakeCompletions {
      async create(): Promise<any> {
        return {
          model: "gpt-4o",
          usage: { prompt_tokens: 100, completion_tokens: 50 },
        };
      }
    }
    _setCompletionsClass(FakeCompletions);
    const originalCreate = FakeCompletions.prototype.create;

    const tracker = new CostTracker({ autoInstrument: ["openai"], dbPath: join(tmpDir, "test.db") });
    await tracker.instrument("openai");

    // Verify it's patched
    expect(FakeCompletions.prototype.create).not.toBe(originalCreate);

    tracker.close();

    // After close, should be restored
    expect(FakeCompletions.prototype.create).toBe(originalCreate);
  });
});
