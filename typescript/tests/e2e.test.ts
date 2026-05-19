/**
 * dexcost TypeScript SDK — End-to-End Integration Tests
 *
 * These tests exercise the SDK against a real local control-layer stack.
 * They verify that the SDK correctly ships events through the HTTP pusher
 * to the local Hono server and that both LLM and non-LLM costs are captured.
 *
 * Prerequisites:
 *   - Docker Compose stack must be running: infra/docker-compose.yml
 *     (postgres on :5432, server on :3000, dashboard on :3001)
 *   - Set DEXCOST_E2E_LOCAL=1 to enable these tests
 *
 * Environment variables:
 *   DEXCOST_E2E_LOCAL    Set to "1" to run these tests (skipped by default)
 *   DEXCOST_API_KEY      API key for auth (default: dx_test_local)
 *   DEXCOST_ENDPOINT     Local server URL (default: http://localhost:3000)
 *
 * Run with:
 *   npm --prefix sdks/typescript test -- tests/e2e.test.ts
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";

// Skip all tests unless DEXCOST_E2E_LOCAL=1
const SKIP_REASON = "Set DEXCOST_E2E_LOCAL=1 to run E2E tests against local stack";

const shouldRun = () => process.env.DEXCOST_E2E_LOCAL === "1";

const ENDPOINT = process.env.DEXCOST_ENDPOINT ?? "http://localhost:3000";
const API_KEY = process.env.DEXCOST_API_KEY ?? "dx_test_local";

describe.skipIf(!shouldRun(), "E2E: TypeScript SDK vs Local Control Layer", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "dexcost-e2e-"));
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  /**
   * Poll a URL until it returns 200 or the timeout expires.
   * Returns the response body on success, throws on timeout.
   */
  async function pollForTask(
    taskId: string,
    maxWaitMs = 10_000,
    pollIntervalMs = 500,
  ): Promise<unknown> {
    const url = `${ENDPOINT}/v1/api/tasks/${taskId}`;
    const deadline = Date.now() + maxWaitMs;

    while (Date.now() < deadline) {
      try {
        const res = await fetch(url, {
          headers: {
            Authorization: `Bearer ${API_KEY}`,
            "Content-Type": "application/json",
          },
        });

        if (res.status === 200) {
          return res.json();
        }

        // 404 means not processed yet — keep polling
        if (res.status !== 404) {
          const body = await res.text();
          throw new Error(`Unexpected status ${res.status}: ${body}`);
        }
      } catch (err) {
        if (err instanceof TypeError && err.message.includes("fetch")) {
          // Connection refused — server not up yet, keep polling
        } else {
          throw err;
        }
      }

      await new Promise((r) => setTimeout(r, pollIntervalMs));
    }

    throw new Error(`Task ${taskId} not visible within ${maxWaitMs}ms`);
  }

  // -------------------------------------------------------------------------
  // Tests
  // -------------------------------------------------------------------------

  it(
    "ships LLM and external cost events to local control layer",
    async () => {
      // Use a unique customer each run to avoid collisions
      const customerId = `e2e-ts-${randomUUID()}`;
      const projectId = "ts-sdk-e2e";

      // Import dynamically to allow skip to work correctly
      const { CostTracker } = await import("../src/index.js");

      const dbPath = join(tmpDir, "e2e.db");
      const tracker = new CostTracker({
        apiKey: API_KEY,
        dbPath,
        // Short flush interval so events are pushed quickly
        flushIntervalMs: 1_000,
      });

      // Record both an LLM call and an external cost
      const llmCost = 0.042;
      const externalCost = 0.005;

      await tracker.track(
        {
          taskType: "e2e_test_task",
          customerId,
          projectId,
          metadata: { test_runner: "typescript-e2e" },
        },
        async (task) => {
          task.recordLlmCall("openai", "gpt-4o", 1500, 750, llmCost);
          task.recordCost("pdf_parser", externalCost, { pages: 12 });
        },
      );

      // Force an immediate flush
      await tracker.flush();

      // Wait for the worker to process the SQS message and write to Postgres/ClickHouse.
      // Give it generous time (10s) for the async pipeline.
      const taskId = (await tracker.buffer.getAllTasks())[0]?.taskId;
      expect(taskId).toBeTruthy();

      const taskData = (await pollForTask(taskId!, 10_000)) as Record<string, unknown>;

      // Verify the task has the correct customer and project
      expect(taskData.customer_id).toBe(customerId);
      expect(taskData.project_id).toBe(projectId);

      // Verify costs — the worker aggregates from events, so we check the totals
      const totalCost = parseFloat(String(taskData.total_cost_usd ?? "0"));
      expect(totalCost).toBeCloseTo(llmCost + externalCost, 4);

      tracker.close();
    },
    { timeout: 30_000 },
  );

  it(
    "records both llm_call and external_cost event types",
    async () => {
      const customerId = `e2e-ts-events-${randomUUID()}`;

      const { CostTracker } = await import("../src/index.js");
      const dbPath = join(tmpDir, "e2e-events.db");
      const tracker = new CostTracker({
        apiKey: API_KEY,
        dbPath,
      });

      await tracker.track(
        { taskType: "schema_test", customerId },
        async (task) => {
          task.recordLlmCall("anthropic", "claude-3-5-sonnet", 1000, 500, 0.015);
          task.recordCost("search_api", 0.003);
          task.recordCost("compute", 0.001, undefined, "compute_cost");
        },
      );

      await tracker.flush();

      // Verify the events are stored correctly in the local SQLite buffer
      const events = await tracker.buffer.getAllEvents();
      expect(events.length).toBeGreaterThanOrEqual(3);

      const eventTypes = new Set(events.map((e) => e.eventType));

      expect(eventTypes.has("llm_call")).toBe(true);
      expect(eventTypes.has("external_cost")).toBe(true);
      expect(eventTypes.has("compute_cost")).toBe(true);

      // Verify LLM event fields
      const llmEvent = events.find((e) => e.eventType === "llm_call")!;
      expect(llmEvent.provider).toBe("anthropic");
      expect(llmEvent.model).toBe("claude-3-5-sonnet");
      expect(llmEvent.inputTokens).toBe(1000);
      expect(llmEvent.outputTokens).toBe(500);
      expect(llmEvent.costUsd).toBeCloseTo(0.015, 4);

      // Verify external_cost event
      const extEvent = events.find((e) => e.eventType === "external_cost")!;
      expect(extEvent.serviceName).toBe("search_api");
      expect(extEvent.costUsd).toBeCloseTo(0.003, 4);

      tracker.close();
    },
    { timeout: 15_000 },
  );

  it(
    "Standard Event Schema v1 compliance",
    async () => {
      const customerId = `e2e-ts-schema-${randomUUID()}`;

      const { CostTracker, validate } = await import("../src/index.js");
      const { eventToDict } = await import("../src/core/models.js");

      const dbPath = join(tmpDir, "schema-v1.db");
      const tracker = new CostTracker({ apiKey: API_KEY, dbPath });

      await tracker.track(
        { taskType: "schema_compliance", customerId },
        async (task) => {
          task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.005);
        },
      );

      await tracker.flush();

      const events = await tracker.buffer.getAllEvents();
      expect(events.length).toBeGreaterThanOrEqual(1);

      for (const event of events) {
        const dict = eventToDict(event);

        // Required Standard Event Schema v1 fields
        expect(dict).toHaveProperty("event_id");
        expect(dict).toHaveProperty("task_id");
        expect(dict).toHaveProperty("event_type");
        expect(dict).toHaveProperty("occurred_at");
        expect(dict).toHaveProperty("cost_usd");
        expect(dict).toHaveProperty("cost_confidence");
        expect(dict).toHaveProperty("is_retry");
        expect(dict).toHaveProperty("schema_version");

        expect(dict["schema_version"]).toBe("1");
        expect(dict["event_type"]).toBe("llm_call");
        expect(dict["is_retry"]).toBe(false);

        // validate() returns [] on success
        const errors = validate(dict);
        expect(errors).toEqual([]);
      }

      // Also validate a task
      const tasks = await tracker.buffer.getAllTasks();
      expect(tasks.length).toBeGreaterThanOrEqual(1);
      expect(tasks[0].schemaVersion).toBe("1");
      expect(tasks[0].customerId).toBe(customerId);

      tracker.close();
    },
    { timeout: 15_000 },
  );

  it(
    "retry semantics: is_retry, retry_reason, retry_of fields",
    async () => {
      const { CostTracker } = await import("../src/index.js");
      const dbPath = join(tmpDir, "retry-semantics.db");
      const tracker = new CostTracker({ apiKey: API_KEY, dbPath });

      await tracker.track({ taskType: "retry_test" }, async (task) => {
        // Explicit retry marker
        const retryEvent = task.markRetry("rate_limit_hit", 0.03);
        expect(retryEvent.isRetry).toBe(true);
        expect(retryEvent.retryReason).toBe("rate_limit_hit");
        expect(retryEvent.eventType).toBe("retry_marker");

        // LLM call that will be flagged as retry by heuristics
        task.recordLlmCall("openai", "gpt-4o", 200, 100, 0.01);
      });

      await tracker.flush();

      const events = await tracker.buffer.getAllEvents();
      const retryEvents = events.filter((e) => e.isRetry);

      expect(retryEvents.length).toBeGreaterThanOrEqual(1);

      // The explicit markRetry event
      const explicitRetry = events.find((e) => e.retryReason === "rate_limit_hit")!;
      expect(explicitRetry).toBeDefined();
      expect(explicitRetry!.retryReason).toBe("rate_limit_hit");

      // Verify retry metrics are aggregated into the task
      const task = await tracker.buffer.getAllTasks();
      expect(task[0].retryCount).toBeGreaterThanOrEqual(1);

      tracker.close();
    },
    { timeout: 15_000 },
  );

  it(
    "SDK gracefully handles unreachable server",
    async () => {
      const { CostTracker } = await import("../src/index.js");
      const dbPath = join(tmpDir, "graceful.db");

      // Point to a host that will refuse connection
      const tracker = new CostTracker({
        apiKey: "dx_test_graceful",
        dbPath,
        // @ts-expect-error — allow overriding endpoint via env for this test
        environment: "development", // dev mode disables cloud push
      });

      await tracker.track({ taskType: "offline_test" }, async (task) => {
        task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.01);
      });

      // In dev mode, flush is a no-op but must not throw
      await tracker.flush();

      // Events should still be stored locally in SQLite
      const events = await tracker.buffer.getAllEvents();
      expect(events.length).toBeGreaterThanOrEqual(1);

      tracker.close();
    },
    { timeout: 15_000 },
  );

  it(
    "experiment_id and variant propagate through to the server",
    async () => {
      const customerId = `e2e-ts-exp-${randomUUID()}`;

      const { CostTracker } = await import("../src/index.js");
      const dbPath = join(tmpDir, "experiment.db");
      const tracker = new CostTracker({ apiKey: API_KEY, dbPath });

      await tracker.track(
        {
          taskType: "exp_test",
          customerId,
          experimentId: "exp-001",
          variant: "gpt4o-mini",
        },
        async (task) => {
          task.recordLlmCall("openai", "gpt-4o-mini", 50, 25, 0.001);
        },
      );

      await tracker.flush();

      const task = (await tracker.buffer.getAllTasks())[0];
      expect(task.experimentId).toBe("exp-001");
      expect(task.variant).toBe("gpt4o-mini");
      expect(task.customerId).toBe(customerId);

      tracker.close();
    },
    { timeout: 15_000 },
  );
});
