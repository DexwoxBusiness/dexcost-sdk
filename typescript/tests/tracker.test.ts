/**
 * Tests for CostTracker and TrackedTask.
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

describe("CostTracker", () => {
  it("creates a task with correct fields", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track(
      { taskType: "summarize", customerId: "acme", projectId: "proj-1" },
      async (tracked) => {
        const task = tracked.task;
        expect(task.taskId).toBeTruthy();
        expect(task.taskType).toBe("summarize");
        expect(task.customerId).toBe("acme");
        expect(task.projectId).toBe("proj-1");
        expect(task.status).toBe("pending");
        expect(task.startedAt).toBeInstanceOf(Date);
        expect(task.schemaVersion).toBe("1");
        expect(task.llmCostUsd).toBe(0);
        expect(task.totalCostUsd).toBe(0);
      }
    );

    tracker.close();
  });

  it("records LLM call event", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "chat" }, async (tracked) => {
      const event = tracked.recordLlmCall("openai", "gpt-4o", 800, 150, 0.05);

      expect(event.eventId).toBeTruthy();
      expect(event.taskId).toBe(tracked.task.taskId);
      expect(event.eventType).toBe("llm_call");
      expect(event.provider).toBe("openai");
      expect(event.model).toBe("gpt-4o");
      expect(event.inputTokens).toBe(800);
      expect(event.outputTokens).toBe(150);
      expect(event.costUsd).toBe(0.05);
      expect(event.isRetry).toBe(false);
      expect(event.schemaVersion).toBe("1");
    });

    tracker.close();
  });

  it("records external cost event", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "ingest" }, async (tracked) => {
      const event = tracked.recordCost("pdf_parser", 0.002, {
        pages: 12,
      });

      expect(event.eventType).toBe("external_cost");
      expect(event.serviceName).toBe("pdf_parser");
      expect(event.costUsd).toBe(0.002);
      expect(event.details).toEqual({ pages: 12 });
      expect(event.isRetry).toBe(false);
    });

    tracker.close();
  });

  it("marks retry event", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "chat" }, async (tracked) => {
      const event = tracked.markRetry("rate_limit", 0.01);

      expect(event.eventType).toBe("retry_marker");
      expect(event.isRetry).toBe(true);
      expect(event.retryReason).toBe("rate_limit");
      expect(event.costUsd).toBe(0.01);
    });

    tracker.close();
  });

  it("aggregates multiple events into task totals", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "pipeline" }, async (tracked) => {
      tracked.recordLlmCall("openai", "gpt-4o", 500, 100, 0.03);
      tracked.recordLlmCall("anthropic", "claude-3", 300, 200, 0.02);
      tracked.recordCost("scraper", 0.005);
      tracked.markRetry("timeout", 0.01);

      const task = tracked.task;
      expect(task.llmCostUsd).toBeCloseTo(0.05, 10);
      expect(task.externalCostUsd).toBeCloseTo(0.005, 10);
      expect(task.totalCostUsd).toBeCloseTo(0.065, 10);
      expect(task.totalInputTokens).toBe(800);
      expect(task.totalOutputTokens).toBe(300);
      expect(task.retryCount).toBe(1);
      expect(task.retryCostUsd).toBeCloseTo(0.01, 10);
    });

    tracker.close();
  });

  it("sets ended_at and status on end()", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "test" }, async (tracked) => {
      expect(tracked.task.endedAt).toBeUndefined();
      expect(tracked.task.status).toBe("pending");

      tracked.end("success");

      expect(tracked.task.endedAt).toBeInstanceOf(Date);
      expect(tracked.task.status).toBe("success");
    });

    tracker.close();
  });

  it("stores trace links in task metadata", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "trace-test" }, async (tracked) => {
      tracked.linkTrace("langsmith", "trace-abc-123");
      tracked.linkTrace("datadog", "trace-xyz-789");

      // Stored under metadata._trace_links with trace_id keys, matching
      // the Python SDK so cross-SDK buffers interoperate.
      const links = tracked.task.metadata["_trace_links"] as Array<{
        provider: string;
        trace_id: string;
      }>;
      expect(links).toHaveLength(2);
      expect(links[0]).toEqual({
        provider: "langsmith",
        trace_id: "trace-abc-123",
      });
      expect(links[1]).toEqual({
        provider: "datadog",
        trace_id: "trace-xyz-789",
      });

      // getTraceLinks() returns the same list.
      expect(tracked.getTraceLinks()).toEqual(links);
    });

    tracker.close();
  });

  it("supports experiment_id and variant fields", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    await tracker.track(
      { taskType: "classify", experimentId: "exp-001", variant: "gpt4o-mini" },
      async (tracked) => {
        expect(tracked.task.experimentId).toBe("exp-001");
        expect(tracked.task.variant).toBe("gpt4o-mini");
      },
    );
    tracker.close();
  });

  it("serializes experiment fields in taskToDict", async () => {
    const { createTask, taskToDict } = await import("../src/core/models.js");
    const task = createTask({ taskId: "t-1", experimentId: "exp-002", variant: "control" });
    const dict = taskToDict(task);
    expect(dict.experiment_id).toBe("exp-002");
    expect(dict.variant).toBe("control");
  });

  it("auto-ends task with failure on exception", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    let capturedTask: { status: string; endedAt?: Date; failureCount: number } | undefined;

    try {
      await tracker.track({ taskType: "failing" }, async (tracked) => {
        capturedTask = tracked.task;
        throw new Error("something went wrong");
      });
    } catch {
      // Expected
    }

    expect(capturedTask).toBeDefined();
    expect(capturedTask!.status).toBe("failed");
    expect(capturedTask!.endedAt).toBeInstanceOf(Date);
    expect(capturedTask!.failureCount).toBe(1);

    tracker.close();
  });
});

describe("TrackedTask.recordUsage", () => {
  it("computes cost from registered rate (10 units × 0.005 = 0.05)", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    tracker.registerRate("ocr_service", "page", 0.005);

    await tracker.track({ taskType: "ocr" }, async (tracked) => {
      const event = tracked.recordUsage("ocr_service", 10);

      expect(event.eventType).toBe("external_cost");
      expect(event.costUsd).toBeCloseTo(0.05, 10);
      expect(event.serviceName).toBe("ocr_service");
      expect(event.costConfidence).toBe("computed");
      expect(event.pricingSource).toBe("rate_registry");
      expect(event.isRetry).toBe(false);

      const task = tracked.task;
      expect(task.externalCostUsd).toBeCloseTo(0.05, 10);
      expect(task.totalCostUsd).toBeCloseTo(0.05, 10);
    });

    tracker.close();
  });

  it("defaults to 1 unit", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });
    tracker.registerRate("sms_service", "message", 0.012);

    await tracker.track({ taskType: "notify" }, async (tracked) => {
      const event = tracked.recordUsage("sms_service");

      expect(event.costUsd).toBeCloseTo(0.012, 10);
    });

    tracker.close();
  });

  it("throws for unregistered service", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "test" }, async (tracked) => {
      expect(() => tracked.recordUsage("unknown_service")).toThrow(
        'No rate registered for service "unknown_service"'
      );
    });

    tracker.close();
  });
});

describe("TrackedTask.markNotRetry", () => {
  it("un-flags the most recent retry event", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "chat" }, async (tracked) => {
      tracked.markRetry("rate_limit", 0.01);
      tracked.markRetry("timeout", 0.02);

      expect(tracked.task.retryCount).toBe(2);

      const undone = tracked.markNotRetry();

      expect(undone).toBeDefined();
      expect(undone!.isRetry).toBe(false);
      expect(undone!.retryReason).toBeUndefined();
      expect(tracked.task.retryCount).toBe(1);
      expect(tracked.task.retryCostUsd).toBeCloseTo(0.01, 10);
    });

    tracker.close();
  });

  it("un-flags a specific event by ID", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "chat" }, async (tracked) => {
      const first = tracked.markRetry("rate_limit", 0.01);
      tracked.markRetry("timeout", 0.02);

      const undone = tracked.markNotRetry(first.eventId);

      expect(undone).toBeDefined();
      expect(undone!.eventId).toBe(first.eventId);
      expect(undone!.isRetry).toBe(false);
      expect(tracked.task.retryCount).toBe(1);
      expect(tracked.task.retryCostUsd).toBeCloseTo(0.02, 10);
    });

    tracker.close();
  });

  it("returns undefined when no retry events exist", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "chat" }, async (tracked) => {
      tracked.recordLlmCall("openai", "gpt-4o", 100, 50, 0.005);

      const result = tracked.markNotRetry();
      expect(result).toBeUndefined();
    });

    tracker.close();
  });
});

describe("TrackedTask retry heuristics", () => {
  it("auto-detects retry when heuristics enabled", async () => {
    const tracker = new CostTracker({
      enableRetryHeuristics: true,
      retryHeuristicThreshold: 0.5,
      dbPath: join(tmpDir, "test.db"),
    });

    await tracker.track({ taskType: "test" }, async (task) => {
      // First call — simulate failure by setting error_type in details after recording
      task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      const failedEvent = task.events[0];
      (failedEvent as { details: Record<string, unknown> }).details = { error_type: "rate_limit" };

      // Second call — same model, should be auto-detected as retry
      const retryEvent = task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);

      expect(retryEvent.isRetry).toBe(true);
      expect(retryEvent.retryReason).toBe("heuristic");
      expect(retryEvent.retryOf).toBe(failedEvent.eventId);
      expect(task.task.retryCount).toBe(1);
    });

    tracker.close();
  });

  it("does not auto-detect retry when heuristics disabled", async () => {
    const tracker = new CostTracker({ dbPath: join(tmpDir, "test.db") });

    await tracker.track({ taskType: "test" }, async (task) => {
      task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      (task.events[0] as { details: Record<string, unknown> }).details = { error_type: "rate_limit" };

      const event2 = task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      expect(event2.isRetry).toBe(false);
    });

    tracker.close();
  });

  it("does not flag retry for successful previous call", async () => {
    const tracker = new CostTracker({
      enableRetryHeuristics: true,
      dbPath: join(tmpDir, "test.db"),
    });

    await tracker.track({ taskType: "test" }, async (task) => {
      task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      const event2 = task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      expect(event2.isRetry).toBe(false);
    });

    tracker.close();
  });
});
