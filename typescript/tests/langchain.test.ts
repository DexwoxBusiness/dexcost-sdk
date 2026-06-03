/**
 * Tests for DexcostCallbackHandler — LangChain duck-typed callback handler.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { CostTracker } from "../src/core/tracker.js";
import { Decimal } from "../src/core/models.js";
import { DexcostCallbackHandler } from "../src/integrations/langchain.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-langchain-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("DexcostCallbackHandler", () => {
  it("records LLM event on handleLLMEnd inside tracker.track", async () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "test.db"),
      autoInstrument: [],
    });
    const handler = new DexcostCallbackHandler(tracker);

    const runId = randomUUID();
    const serialized = {
      kwargs: { model_name: "gpt-4o" },
      id: ["langchain", "chat_models", "openai", "ChatOpenAI"],
    };
    const output = {
      llmOutput: {
        tokenUsage: {
          promptTokens: 200,
          completionTokens: 50,
        },
      },
    };

    await tracker.track({ taskType: "langchain-test" }, async () => {
      handler.handleLLMStart(serialized, ["Hello, world!"], runId);
      handler.handleLLMEnd(output, runId);
    });

    const events = tracker.buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("langchain");
    expect(events[0].model).toBe("gpt-4o");
    expect(events[0].inputTokens).toBe(200);
    expect(events[0].outputTokens).toBe(50);
    // Costs are now exact Decimals (decimal.js), not float64 numbers.
    expect(events[0].costUsd).toBeInstanceOf(Decimal);
    expect(events[0].costUsd.toNumber()).toBeGreaterThanOrEqual(0);
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);

    tracker.close();
  });

  it("handles handleLLMError without crashing", () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "test.db"),
      autoInstrument: [],
    });
    const handler = new DexcostCallbackHandler(tracker);

    const runId = randomUUID();
    const serialized = {
      kwargs: { model_name: "gpt-4o" },
      id: ["ChatOpenAI"],
    };

    handler.handleLLMStart(serialized, ["Hello"], runId);
    // Should not throw
    expect(() => handler.handleLLMError(new Error("LLM failed"), runId)).not.toThrow();

    // Pending entry should be cleaned up — no error on subsequent end either
    const output = {
      llmOutput: { tokenUsage: { promptTokens: 10, completionTokens: 5 } },
    };
    expect(() => handler.handleLLMEnd(output, runId)).not.toThrow();

    tracker.close();
  });

  it("ignores handleLLMEnd with no active task — no throw", async () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "test.db"),
      autoInstrument: [],
    });
    const handler = new DexcostCallbackHandler(tracker);

    const runId = randomUUID();
    const serialized = {
      kwargs: { model_name: "claude-3-5-sonnet-20241022" },
      id: ["ChatAnthropic"],
    };
    const output = {
      llmOutput: {
        tokenUsage: {
          promptTokens: 100,
          completionTokens: 30,
        },
      },
    };

    // Called outside any tracker.track context — should not throw
    handler.handleLLMStart(serialized, ["test prompt"], runId);
    expect(() => handler.handleLLMEnd(output, runId)).not.toThrow();

    // No events should have been recorded
    expect(tracker.buffer.getAllEvents()).toHaveLength(0);

    tracker.close();
  });

  it("extracts model name from serialized kwargs.model_name preferring over id array", async () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "test.db"),
      autoInstrument: [],
    });
    const handler = new DexcostCallbackHandler(tracker);

    const runId = randomUUID();
    // kwargs.model_name should be preferred over last element of id array
    const serialized = {
      kwargs: { model_name: "gpt-4-turbo" },
      id: ["langchain", "ChatOpenAI", "some-other-name"],
    };
    const output = {
      llmOutput: {
        tokenUsage: { promptTokens: 50, completionTokens: 10 },
      },
    };

    await tracker.track({ taskType: "model-extraction-test" }, async () => {
      handler.handleLLMStart(serialized, ["test"], runId);
      handler.handleLLMEnd(output, runId);
    });

    const events = tracker.buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].model).toBe("gpt-4-turbo");

    tracker.close();
  });

  it("extracts model from last element of serialized.id when kwargs.model_name absent", async () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "test.db"),
      autoInstrument: [],
    });
    const handler = new DexcostCallbackHandler(tracker);

    const runId = randomUUID();
    // No kwargs.model_name — fall back to last element of id array
    const serialized = {
      kwargs: {},
      id: ["langchain", "chat_models", "ChatOpenAI"],
    };
    const output = {
      llmOutput: {
        tokenUsage: { promptTokens: 30, completionTokens: 8 },
      },
    };

    await tracker.track({ taskType: "model-fallback-test" }, async () => {
      handler.handleLLMStart(serialized, ["test"], runId);
      handler.handleLLMEnd(output, runId);
    });

    const events = tracker.buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].model).toBe("ChatOpenAI");

    tracker.close();
  });
});
