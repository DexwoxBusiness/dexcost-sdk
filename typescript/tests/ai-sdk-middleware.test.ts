/**
 * Tests for the Vercel AI SDK model middleware (`dexcostAiMiddleware`) —
 * the supported capture path for `ai` >= 5, whose ESM-only exports cannot
 * be monkey-patched.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { CostTracker } from "../src/core/tracker.js";
import { createTask } from "../src/core/models.js";
import { runWithTask, suppressNetworkEvent, clearContext } from "../src/core/context.js";
import {
  dexcostAiMiddleware,
  _resetAiMiddlewareWarningsForTests,
} from "../src/integrations/ai-sdk.js";

let tmpDir: string;
let tracker: CostTracker;

/** Fake model instance as passed to middleware by wrapLanguageModel. */
const fakeModel = { modelId: "claude-sonnet-4-5", provider: "anthropic.messages" };

function makeStream(parts: unknown[]): ReadableStream<unknown> {
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    },
  });
}

async function drain(stream: ReadableStream<unknown>): Promise<unknown[]> {
  const out: unknown[] = [];
  const reader = stream.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return out;
    out.push(value);
  }
}

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-aimw-test-"));
  tracker = new CostTracker({
    dbPath: join(tmpDir, "test.db"),
    autoInstrument: [],
    trackHttp: false,
  });
  _resetAiMiddlewareWarningsForTests();
  clearContext();
});

afterEach(() => {
  tracker.close();
  clearContext();
  vi.restoreAllMocks();
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("dexcostAiMiddleware — wrapGenerate", () => {
  it("records an llm_call with v5+ usage field names", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    const generateResult = {
      content: [{ type: "text", text: "hi" }],
      usage: { inputTokens: 1200, outputTokens: 340, cachedInputTokens: 100 },
    };
    const result = await runWithTask(task, () =>
      mw.wrapGenerate({ doGenerate: async () => generateResult, model: fakeModel, params: {} }),
    );

    // Original result object returned untouched.
    expect(result).toBe(generateResult);

    const events = tracker.buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0].model).toBe("claude-sonnet-4-5");
    expect(events[0].provider).toBe("anthropic");
    expect(events[0].inputTokens).toBe(1200);
    expect(events[0].outputTokens).toBe(340);
    expect(events[0].cachedTokens).toBe(100);
    expect(events[0].details?.source).toBe("ai_sdk_middleware_generate");
    expect(task.totalInputTokens).toBe(1200);
  });

  it("records v4 usage field names (promptTokens/completionTokens)", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, () =>
      mw.wrapGenerate({
        doGenerate: async () => ({ usage: { promptTokens: 700, completionTokens: 90 } }),
        model: fakeModel,
      }),
    );

    const events = tracker.buffer.getAllEvents();
    expect(events[0].inputTokens).toBe(700);
    expect(events[0].outputTokens).toBe(90);
  });

  it("creates and finalizes an auto-task when no task is active", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    await mw.wrapGenerate({
      doGenerate: async () => ({ usage: { inputTokens: 10, outputTokens: 5 } }),
      model: fakeModel,
    });

    const autoTask = tracker.buffer
      .getAllTasks()
      .find((t) => t.taskType === "ai-sdk.generate");
    expect(autoTask).toBeDefined();
    expect(autoTask!.status).toBe("success");
    expect(autoTask!.endedAt).not.toBeNull();
  });

  it("marks the auto-task failed and rethrows on provider error", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    const boom = new Error("provider exploded");

    await expect(
      mw.wrapGenerate({
        doGenerate: async () => {
          throw boom;
        },
        model: fakeModel,
      }),
    ).rejects.toBe(boom);

    const autoTask = tracker.buffer
      .getAllTasks()
      .find((t) => t.taskType === "ai-sdk.generate");
    expect(autoTask!.status).toBe("failed");
    expect(tracker.buffer.getAllEvents().filter((e) => e.eventType === "llm_call")).toHaveLength(0);
  });

  it("passes through when an outer dexcost capture layer is active (no double count)", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, () =>
      suppressNetworkEvent(() =>
        mw.wrapGenerate({
          doGenerate: async () => ({ usage: { inputTokens: 10, outputTokens: 5 } }),
          model: fakeModel,
        }),
      ),
    );

    expect(tracker.buffer.getAllEvents()).toHaveLength(0);
  });

  it("warns once and passes through when dexcost is not initialized", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const mw = dexcostAiMiddleware(); // no tracker; singleton not initialized in this file

    const r1 = await mw.wrapGenerate({
      doGenerate: async () => ({ text: "a" }),
      model: fakeModel,
    });
    const r2 = await mw.wrapGenerate({
      doGenerate: async () => ({ text: "b" }),
      model: fakeModel,
    });

    expect(r1).toEqual({ text: "a" });
    expect(r2).toEqual({ text: "b" });
    const dexcostWarnings = warn.mock.calls.filter((c) =>
      String(c[0]).includes("dexcostAiMiddleware"),
    );
    expect(dexcostWarnings).toHaveLength(1);
  });

  it("never lets a dexcost recording error crash user code", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    // Poison the pricing engine so _recordEvent throws internally.
    vi.spyOn(tracker.pricing, "getCost").mockImplementation(() => {
      throw new Error("pricing catalog corrupted");
    });
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    const result = await runWithTask(task, () =>
      mw.wrapGenerate({
        doGenerate: async () => ({ usage: { inputTokens: 10, outputTokens: 5 } }),
        model: fakeModel,
      }),
    );
    expect(result).toEqual({ usage: { inputTokens: 10, outputTokens: 5 } });
  });
});

describe("dexcostAiMiddleware — wrapStream", () => {
  it("records usage from the finish part after the stream is consumed", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    const parts = [
      { type: "stream-start", warnings: [] },
      { type: "text-delta", delta: "hel" },
      { type: "text-delta", delta: "lo" },
      { type: "finish", finishReason: "stop", usage: { inputTokens: 800, outputTokens: 220 } },
    ];
    const streamResult = { stream: makeStream(parts), rawCall: { marker: 1 } };

    const wrapped: any = await runWithTask(task, () =>
      mw.wrapStream({ doStream: async () => streamResult, model: fakeModel }),
    );

    // All parts pass through unchanged, other result props preserved.
    expect(wrapped.rawCall).toEqual({ marker: 1 });
    const seen = await drain(wrapped.stream);
    expect(seen).toEqual(parts);

    const events = tracker.buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0].inputTokens).toBe(800);
    expect(events[0].outputTokens).toBe(220);
    expect(events[0].details?.source).toBe("ai_sdk_middleware_stream");
    expect(task.totalInputTokens).toBe(800);
  });

  it("finalizes the auto-task failed (no event) when the stream errors before finish", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue({ type: "text-delta", delta: "hel" });
        controller.error(new Error("connection reset"));
      },
    });

    const wrapped: any = await mw.wrapStream({
      doStream: async () => ({ stream }),
      model: fakeModel,
    });
    await expect(drain(wrapped.stream)).rejects.toThrow("connection reset");

    const autoTask = tracker.buffer
      .getAllTasks()
      .find((t) => t.taskType === "ai-sdk.stream");
    expect(autoTask!.status).toBe("failed");
    expect(tracker.buffer.getAllEvents().filter((e) => e.eventType === "llm_call")).toHaveLength(0);
  });

  it("finalizes the auto-task on consumer cancel without usage", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    const wrapped: any = await mw.wrapStream({
      doStream: async () => ({
        stream: makeStream([{ type: "text-delta", delta: "x" }]),
      }),
      model: fakeModel,
    });

    const reader = wrapped.stream.getReader();
    await reader.read();
    await reader.cancel("user aborted");

    const autoTask = tracker.buffer
      .getAllTasks()
      .find((t) => t.taskType === "ai-sdk.stream");
    expect(autoTask).toBeDefined();
    expect(autoTask!.status).toBe("success");
    expect(autoTask!.endedAt).not.toBeNull();
  });

  it("finalizes the auto-task when the result carries no readable stream", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    const oddResult = { notAStream: true };
    const result = await mw.wrapStream({
      doStream: async () => oddResult,
      model: fakeModel,
    });
    expect(result).toBe(oddResult);

    const autoTask = tracker.buffer
      .getAllTasks()
      .find((t) => t.taskType === "ai-sdk.stream");
    expect(autoTask!.status).toBe("success");
  });

  it("marks the auto-task failed and rethrows when doStream itself rejects", async () => {
    const mw = dexcostAiMiddleware({ tracker });
    await expect(
      mw.wrapStream({
        doStream: async () => {
          throw new Error("auth failed");
        },
        model: fakeModel,
      }),
    ).rejects.toThrow("auth failed");

    const autoTask = tracker.buffer
      .getAllTasks()
      .find((t) => t.taskType === "ai-sdk.stream");
    expect(autoTask!.status).toBe("failed");
  });
});

describe("dexcostAiMiddleware — interplay with the fetch fallback", () => {
  it("suppresses the HTTP LLM fallback so one call records exactly one event", async () => {
    const { trackHttp, untrackHttp, clearRecordedEvents } = await import(
      "../src/adapters/http.js"
    );
    const anthropicBody = JSON.stringify({
      model: "claude-sonnet-4-5",
      usage: { input_tokens: 999, output_tokens: 111 },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(anthropicBody, {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    trackHttp(tracker.buffer, tracker.pricing);
    try {
      const mw = dexcostAiMiddleware({ tracker });
      const task = createTask({ taskId: randomUUID(), taskType: "test" });

      await runWithTask(task, () =>
        mw.wrapGenerate({
          doGenerate: async () => {
            // Simulate the provider's HTTP call to an LLM endpoint.
            const res = await fetch("https://api.anthropic.com/v1/messages", {
              method: "POST",
              body: "{}",
            });
            await res.text();
            return { usage: { inputTokens: 1200, outputTokens: 340 } };
          },
          model: fakeModel,
        }),
      );

      const events = tracker.buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
      expect(events).toHaveLength(1);
      // The middleware's event (exact usage), not the fallback's parse.
      expect(events[0].inputTokens).toBe(1200);
      expect(events[0].details?.source).toBe("ai_sdk_middleware_generate");
    } finally {
      untrackHttp();
      clearRecordedEvents();
      vi.unstubAllGlobals();
    }
  });
});
