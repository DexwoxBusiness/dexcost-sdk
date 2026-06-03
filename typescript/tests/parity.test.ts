/**
 * Focused tests for the Python-parity fixes (see PARITY.md).
 *
 * Each block targets one closed parity gap.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";

import { CostTracker } from "../src/core/tracker.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";
import {
  createTask,
  createCostEvent,
  taskToDict,
  eventToDict,
  taskFromDict,
  eventFromDict,
} from "../src/core/models.js";
import {
  validateApiKey,
  resolveConfig,
  InvalidAPIKeyError,
} from "../src/core/config.js";
import { DexcostCallbackHandler } from "../src/integrations/langchain.js";
import { runWithTask } from "../src/core/context.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-parity-"));
});

afterEach(() => {
  try {
    rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    // Windows may hold the SQLite handle briefly — ignore.
  }
});

// ── Gap 13: API-key validation + env resolution ──────────────────────────

describe("API-key validation (gap 13)", () => {
  it("accepts dx_live_ and dx_test_ keys and detects the type", () => {
    expect(validateApiKey("dx_live_abc")).toBe("live");
    expect(validateApiKey("dx_test_abc")).toBe("test");
    expect(validateApiKey(undefined)).toBeUndefined();
  });

  it("throws InvalidAPIKeyError for a malformed key", () => {
    expect(() => validateApiKey("bad-key")).toThrow(InvalidAPIKeyError);
  });

  it("resolves DEXCOST_API_KEY from the environment", () => {
    const prev = process.env.DEXCOST_API_KEY;
    process.env.DEXCOST_API_KEY = "dx_test_fromenv";
    try {
      const cfg = resolveConfig();
      expect(cfg.apiKey).toBe("dx_test_fromenv");
      expect(cfg.isSandbox).toBe(true);
      expect(cfg.storageMode).toBe("cloud");
    } finally {
      if (prev === undefined) delete process.env.DEXCOST_API_KEY;
      else process.env.DEXCOST_API_KEY = prev;
    }
  });

  it("storage:'local' forces local mode and skips env resolution", () => {
    const prev = process.env.DEXCOST_API_KEY;
    process.env.DEXCOST_API_KEY = "dx_live_should_be_ignored";
    try {
      const cfg = resolveConfig(undefined, "local");
      expect(cfg.apiKey).toBeUndefined();
      expect(cfg.storageMode).toBe("local");
    } finally {
      if (prev === undefined) delete process.env.DEXCOST_API_KEY;
      else process.env.DEXCOST_API_KEY = prev;
    }
  });
});

// ── Gap 5 + 10: pricing_version serialisation + fromDict round-trip ───────

describe("Event/Task serialisation (gaps 5 & 10)", () => {
  it("eventToDict includes pricing_version", () => {
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: randomUUID(),
      eventType: "llm_call",
      pricingVersion: "abc123",
    });
    const dict = eventToDict(event);
    expect(dict).toHaveProperty("pricing_version", "abc123");
  });

  it("eventFromDict is the inverse of eventToDict", () => {
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: randomUUID(),
      eventType: "llm_call",
      costUsd: 0.42,
      provider: "openai",
      model: "gpt-4o",
      inputTokens: 100,
      outputTokens: 50,
      pricingVersion: "v-9",
      pricingSource: "litellm",
    });
    const restored = eventFromDict(eventToDict(event));
    expect(restored.eventId).toBe(event.eventId);
    expect(restored.costUsd.toNumber()).toBe(0.42);
    expect(restored.model).toBe("gpt-4o");
    expect(restored.pricingVersion).toBe("v-9");
  });

  it("taskFromDict is the inverse of taskToDict", () => {
    const task = createTask({
      taskId: randomUUID(),
      taskType: "resolve_ticket",
      customerId: "acme",
      totalCostUsd: 1.23,
      retryCount: 2,
    });
    const restored = taskFromDict(taskToDict(task));
    expect(restored.taskId).toBe(task.taskId);
    expect(restored.taskType).toBe("resolve_ticket");
    expect(restored.customerId).toBe("acme");
    expect(restored.totalCostUsd.toNumber()).toBe(1.23);
    expect(restored.retryCount).toBe(2);
  });
});

// ── Gap 6: recordLlmCall auto-prices + error_type/details ─────────────────

describe("recordLlmCall (gap 6)", () => {
  it("auto-computes cost via the pricing engine when cost is omitted", async () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "t.db"),
      autoInstrument: [],
      trackHttp: false,
    });
    await tracker.track({ taskType: "t" }, async (task) => {
      const event = task.recordLlmCall("openai", "gpt-4o", 1000, 500);
      // gpt-4o is in the bundled cost map — cost must be computed, not 0.
      expect(event.costUsd.toNumber()).toBeGreaterThan(0);
      expect(event.costConfidence).toBe("computed");
      expect(event.pricingVersion).toBeTruthy();
    });
    tracker.close();
  });

  it("stores error_type in details", async () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "t.db"),
      autoInstrument: [],
      trackHttp: false,
    });
    await tracker.track({ taskType: "t" }, async (task) => {
      const event = task.recordLlmCall("openai", "gpt-4o", 10, 5, 0.01, 0, undefined, {
        errorType: "rate_limit",
        details: { attempt: 2 },
      });
      expect(event.details["error_type"]).toBe("rate_limit");
      expect(event.details["attempt"]).toBe(2);
    });
    tracker.close();
  });
});

// ── Gap 12: recordCost params + event-type validation ────────────────────

describe("recordCost (gap 12)", () => {
  it("accepts pricingSource and pricingVersion", async () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "t.db"),
      autoInstrument: [],
      trackHttp: false,
    });
    await tracker.track({ taskType: "t" }, async (task) => {
      const event = task.recordCost(
        "pdf_parser",
        0.01,
        {},
        "external_cost",
        "exact",
        "service_catalog",
        "cat-v1",
      );
      expect(event.pricingSource).toBe("service_catalog");
      expect(event.pricingVersion).toBe("cat-v1");
    });
    tracker.close();
  });

  it("throws for an invalid event type", async () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "t.db"),
      autoInstrument: [],
      trackHttp: false,
    });
    await tracker.track({ taskType: "t" }, async (task) => {
      expect(() =>
        // @ts-expect-error — deliberately passing an invalid event type
        task.recordCost("svc", 0.01, {}, "llm_call"),
      ).toThrow(/event_type/);
    });
    tracker.close();
  });
});

// ── Gap 9: manual startTask() ────────────────────────────────────────────

describe("startTask (gap 9)", () => {
  it("returns a TrackedTask the caller ends explicitly", () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "t.db"),
      autoInstrument: [],
      trackHttp: false,
    });
    const task = tracker.startTask({ taskType: "celery_job", customerId: "acme" });
    task.recordLlmCall("openai", "gpt-4o", 10, 5, 0.02);
    task.end("success");

    expect(task.task.taskType).toBe("celery_job");
    expect(task.task.customerId).toBe("acme");
    expect(task.task.status).toBe("success");
    expect(task.events).toHaveLength(1);
    tracker.close();
  });
});

// ── Gap 11: trace links naming + getter ──────────────────────────────────

describe("trace links (gap 11)", () => {
  it("stores links under _trace_links with trace_id keys", () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "t.db"),
      autoInstrument: [],
      trackHttp: false,
    });
    const task = tracker.startTask({ taskType: "t" });
    task.linkTrace("langfuse", "run-1");
    expect(task.task.metadata["_trace_links"]).toEqual([
      { provider: "langfuse", trace_id: "run-1" },
    ]);
    expect(task.getTraceLinks()).toEqual([
      { provider: "langfuse", trace_id: "run-1" },
    ]);
    task.end();
    tracker.close();
  });
});

// ── Gap 8: LangChain handleLLMError records a failure event ───────────────

describe("LangChain handleLLMError (gap 8)", () => {
  it("records a failure llm_call event with error_type", () => {
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "t.db"),
      autoInstrument: [],
      trackHttp: false,
    });
    const handler = new DexcostCallbackHandler(tracker);
    const task = createTask({ taskId: randomUUID(), taskType: "t" });

    runWithTask(task, () => {
      handler.handleLLMStart({ kwargs: { model_name: "gpt-4o" } }, ["hi"], "run-x");
      handler.handleLLMError(new TypeError("boom"), "run-x");
    });

    const events = tracker.buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].costUsd.toNumber()).toBe(0);
    expect(events[0].details["error_type"]).toBe("TypeError");
    tracker.close();
  });
});

// ── Gap 1 + 3: pusher redaction + auth-failure stop ──────────────────────

describe("EventPusher sync (gaps 1 & 3)", () => {
  const originalFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("redacts configured fields and hashes customer_id before POST", async () => {
    const buffer = new EventBuffer(join(tmpDir, "p.db"));
    buffer.addEvent(
      createCostEvent({
        eventId: randomUUID(),
        taskId: randomUUID(),
        eventType: "external_cost",
        details: { ssn: "123-45-6789", customer_id: "cust-1", keep: "ok" },
      }),
    );

    let sentDetails: Record<string, unknown> = {};
    globalThis.fetch = vi.fn().mockImplementation(async (_url, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as {
        events: Array<{ details: Record<string, unknown> }>;
      };
      sentDetails = body.events[0].details;
      return new Response("{}", { status: 202 });
    });

    const pusher = new EventPusher(buffer, {
      apiKey: "dx_live_x",
      redactFields: ["ssn"],
      hashCustomerId: true,
    });
    await pusher.flush();

    expect(sentDetails).not.toHaveProperty("ssn");
    expect(sentDetails["keep"]).toBe("ok");
    // customer_id is hashed (SHA-256 hex, 64 chars), not the raw value.
    expect(sentDetails["customer_id"]).not.toBe("cust-1");
    expect(String(sentDetails["customer_id"])).toHaveLength(64);

    pusher.stop();
    buffer.close();
  });

  it("stops sync permanently after an HTTP 401", async () => {
    const buffer = new EventBuffer(join(tmpDir, "a.db"));
    buffer.addEvent(
      createCostEvent({
        eventId: randomUUID(),
        taskId: randomUUID(),
        eventType: "external_cost",
      }),
    );

    let callCount = 0;
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      callCount++;
      return new Response("unauthorized", { status: 401 });
    });

    const pusher = new EventPusher(buffer, { apiKey: "dx_live_bad" });
    await pusher.flush();
    expect(pusher.authFailed).toBe(true);

    // A second flush must not POST again — sync is disabled.
    await pusher.flush();
    expect(callCount).toBe(1);

    pusher.stop();
    buffer.close();
  });
});

// ── Gap 14: trackHttp init option ────────────────────────────────────────

describe("trackHttp init option (gap 14)", () => {
  it("does not patch HTTP transports when trackHttp is false", async () => {
    const originalFetch = globalThis.fetch;
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "h.db"),
      autoInstrument: [],
      trackHttp: false,
    });
    expect(globalThis.fetch).toBe(originalFetch);
    tracker.close();
  });
});
