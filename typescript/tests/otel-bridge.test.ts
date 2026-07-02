/**
 * Tests for the OTel ingestion bridge (DexcostSpanProcessor) and the
 * instrumentModules bundler escape hatch.
 *
 * The bridge is exercised BOTH against a real OpenTelemetry tracer
 * provider (dev dependency, proving interface compatibility) and with
 * hand-built spans (edge cases).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import { CostTracker } from "../src/core/tracker.js";
import { createTask } from "../src/core/models.js";
import { runWithTask } from "../src/core/context.js";
import { DexcostSpanProcessor } from "../src/integrations/otel.js";
import { registerLlmCapture, _resetLlmDedupForTests } from "../src/core/llm-dedup.js";

let tmpDir: string;
let tracker: CostTracker;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-otel-test-"));
  tracker = new CostTracker({
    dbPath: join(tmpDir, "test.db"),
    autoInstrument: [],
    trackHttp: false,
  });
  _resetLlmDedupForTests();
});

afterEach(() => {
  tracker.close();
  _resetLlmDedupForTests();
  vi.restoreAllMocks();
  rmSync(tmpDir, { recursive: true, force: true });
});

/** Minimal hand-built span for direct onStart/onEnd calls. */
function fakeSpan(attributes: Record<string, unknown>, opts: Record<string, unknown> = {}) {
  return {
    name: "ai.generateText.doGenerate",
    attributes,
    startTime: [100, 0],
    endTime: [100, 250_000_000], // 250ms
    status: { code: 0 },
    ...opts,
  };
}

describe("DexcostSpanProcessor — real OTel provider", () => {
  it("converts AI SDK telemetry spans into llm_call events on the active task", async () => {
    const processor = new DexcostSpanProcessor({ tracker });
    const provider = new BasicTracerProvider({ spanProcessors: [processor] });
    const tracer = provider.getTracer("test");

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    runWithTask(task, () => {
      const span = tracer.startSpan("ai.generateText.doGenerate");
      span.setAttribute("ai.model.id", "claude-sonnet-4-5");
      span.setAttribute("ai.model.provider", "anthropic.messages");
      span.setAttribute("ai.usage.inputTokens", 1200);
      span.setAttribute("ai.usage.outputTokens", 340);
      span.end();
    });

    const events = tracker.buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0].taskId).toBe(task.taskId);
    expect(events[0].model).toBe("claude-sonnet-4-5");
    expect(events[0].provider).toBe("anthropic.messages");
    expect(events[0].inputTokens).toBe(1200);
    expect(events[0].outputTokens).toBe(340);
    expect(events[0].details?.source).toBe("otel_bridge");
    expect(task.totalInputTokens).toBe(1200);

    await provider.shutdown();
  });

  it("ignores spans without LLM usage attributes (HTTP/DB/parent spans)", async () => {
    const processor = new DexcostSpanProcessor({ tracker });
    const provider = new BasicTracerProvider({ spanProcessors: [processor] });
    const tracer = provider.getTracer("test");

    const span = tracer.startSpan("HTTP GET");
    span.setAttribute("http.method", "GET");
    span.end();

    expect(tracker.buffer.getAllEvents()).toHaveLength(0);
    await provider.shutdown();
  });
});

describe("DexcostSpanProcessor — span shapes and edge cases", () => {
  it("reads GenAI semconv attribute names and span latency", () => {
    const processor = new DexcostSpanProcessor({ tracker });
    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    const span = fakeSpan({
      "gen_ai.provider.name": "anthropic",
      "gen_ai.response.model": "claude-sonnet-4-5",
      "gen_ai.usage.input_tokens": 700,
      "gen_ai.usage.output_tokens": 90,
      "gen_ai.usage.cached_input_tokens": 100,
    });
    runWithTask(task, () => {
      processor.onStart(span);
    });
    processor.onEnd(span); // onEnd outside the task scope — WeakMap carries it

    const events = tracker.buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].taskId).toBe(task.taskId);
    expect(events[0].inputTokens).toBe(700);
    expect(events[0].cachedTokens).toBe(100);
    expect(events[0].latencyMs).toBe(250);
  });

  it("never reads prompt/completion content — details carry only source + span name", () => {
    const processor = new DexcostSpanProcessor({ tracker });
    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    const span = fakeSpan({
      "ai.usage.inputTokens": 10,
      "ai.usage.outputTokens": 5,
      "ai.prompt.messages": '[{"role":"user","content":"SECRET PROMPT"}]',
      "gen_ai.completion": "SECRET COMPLETION",
    });
    runWithTask(task, () => processor.onStart(span));
    processor.onEnd(span);

    const event = tracker.buffer.getAllEvents()[0];
    expect(Object.keys(event.details ?? {}).sort()).toEqual(["source", "span_name"]);
    expect(JSON.stringify(event)).not.toContain("SECRET");
  });

  it("drops span-derived events already captured by another layer (dedup)", () => {
    const processor = new DexcostSpanProcessor({ tracker });
    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    // The fetch fallback captured this call moments ago.
    registerLlmCapture(task.taskId, 1200, 340);

    const span = fakeSpan({
      "ai.usage.inputTokens": 1200,
      "ai.usage.outputTokens": 340,
    });
    runWithTask(task, () => processor.onStart(span));
    processor.onEnd(span);

    expect(tracker.buffer.getAllEvents()).toHaveLength(0);
  });

  it("dedupe: false records regardless", () => {
    const processor = new DexcostSpanProcessor({ tracker, dedupe: false });
    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    registerLlmCapture(task.taskId, 1200, 340);

    const span = fakeSpan({ "ai.usage.inputTokens": 1200, "ai.usage.outputTokens": 340 });
    runWithTask(task, () => processor.onStart(span));
    processor.onEnd(span);

    expect(tracker.buffer.getAllEvents()).toHaveLength(1);
  });

  it("creates and finalizes an auto-task when no task context exists", () => {
    const processor = new DexcostSpanProcessor({ tracker });
    const span = fakeSpan({ "ai.usage.inputTokens": 10, "ai.usage.outputTokens": 5 });
    processor.onStart(span); // no active task
    processor.onEnd(span);

    const autoTask = tracker.buffer.getAllTasks().find((t) => t.taskType === "otel.llm_span");
    expect(autoTask).toBeDefined();
    expect(autoTask!.status).toBe("success");
  });

  it("errored spans finalize the auto-task as failed", () => {
    const processor = new DexcostSpanProcessor({ tracker });
    const span = fakeSpan(
      { "ai.usage.inputTokens": 10, "ai.usage.outputTokens": 5 },
      { status: { code: 2 } },
    );
    processor.onStart(span);
    processor.onEnd(span);
    const autoTask = tracker.buffer.getAllTasks().find((t) => t.taskType === "otel.llm_span");
    expect(autoTask!.status).toBe("failed");
  });

  it("malformed spans never throw into the tracing pipeline", () => {
    const processor = new DexcostSpanProcessor({ tracker });
    expect(() => processor.onEnd(null)).not.toThrow();
    expect(() => processor.onEnd({ attributes: null })).not.toThrow();
    expect(() =>
      processor.onEnd({ attributes: { "ai.usage.inputTokens": "not-a-number-🎈" } }),
    ).not.toThrow();
    expect(tracker.buffer.getAllEvents()).toHaveLength(0);
  });

  it("warns once and drops spans when dexcost is not initialized", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const processor = new DexcostSpanProcessor(); // no tracker, no singleton
    const span = fakeSpan({ "ai.usage.inputTokens": 10, "ai.usage.outputTokens": 5 });
    processor.onEnd(span);
    processor.onEnd(span);
    expect(
      warn.mock.calls.filter((c) => String(c[0]).includes("DexcostSpanProcessor")),
    ).toHaveLength(1);
  });
});

describe("instrumentModules (bundler escape hatch)", () => {
  it("patches an explicitly provided Anthropic class (alias-normalized)", async () => {
    const createFn = async () => ({
      model: "claude-sonnet-4-5",
      usage: { input_tokens: 100, output_tokens: 20 },
    });
    class Messages {
      create = createFn;
    }
    Messages.prototype.create = createFn as any;
    class FakeAnthropic {
      static Messages = Messages;
    }
    const original = Messages.prototype.create;

    const t = new CostTracker({
      dbPath: join(tmpDir, "im.db"),
      autoInstrument: [],
      trackHttp: false,
      instrumentModules: { anthropic: FakeAnthropic },
    });
    try {
      // instrument activation is async — let it settle
      await new Promise((r) => setImmediate(r));
      await new Promise((r) => setImmediate(r));
      expect(Messages.prototype.create).not.toBe(original);

      const task = createTask({ taskId: randomUUID(), taskType: "test" });
      await runWithTask(task, () => Messages.prototype.create.call({}, { model: "x" }));
      const events = t.buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
      expect(events).toHaveLength(1);
      expect(events[0].inputTokens).toBe(100);
    } finally {
      t.close();
      const { _resetMessagesClass } = await import("../src/instruments/anthropic.js");
      _resetMessagesClass();
    }
  });

  it("warns on unknown provider keys", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const t = new CostTracker({
      dbPath: join(tmpDir, "im2.db"),
      autoInstrument: [],
      trackHttp: false,
      instrumentModules: { "not-a-provider": {} },
    });
    t.close();
    expect(
      warn.mock.calls.some((c) => String(c[0]).includes("unknown provider 'not-a-provider'")),
    ).toBe(true);
  });
});
