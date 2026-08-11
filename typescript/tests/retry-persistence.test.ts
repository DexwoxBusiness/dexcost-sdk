/**
 * Fix 1 — heuristic retry flag must be persisted to the SQLite row.
 *
 * Before the fix, `recordLlmCall` inserted the event into the buffer
 * BEFORE the heuristic engine mutated `isRetry`/`retryReason`/`retryOf`,
 * so the persisted row kept `is_retry=0` even when a retry was detected.
 * These tests record a retry-eligible sequence and assert that the row
 * read back from the buffer reflects the detected retry.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { CostTracker } from "../src/core/tracker.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-retry-persist-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("Fix 1 — heuristic retry flag persistence", () => {
  it("persists is_retry=1 / retry reason on the buffered row for a detected retry", async () => {
    const tracker = new CostTracker({
      enableRetryHeuristics: true,
      retryHeuristicThreshold: 0.5,
      dbPath: join(tmpDir, "test.db"),
    });

    let taskId = "";
    let retryEventId = "";
    let failedEventId = "";

    await tracker.track({ taskType: "test" }, async (task) => {
      taskId = task.task.taskId;

      // First call ends in a transient error — set error_type so the
      // heuristic engine treats it as a failed predecessor.
      const failed = task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      failedEventId = failed.eventId;
      (failed as { details: Record<string, unknown> }).details = {
        error_type: "rate_limit",
      };

      // Second call to the same model — should be flagged as a retry.
      const retry = task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      retryEventId = retry.eventId;

      // In-memory event object is flagged.
      expect(retry.isRetry).toBe(true);
      expect(retry.retryReason).toBe("heuristic");
    });

    // Read the row back from the buffer (the persisted SQLite row).
    const persisted = tracker.buffer
      .queryEvents(taskId)
      .find((e) => e.eventId === retryEventId);

    expect(persisted).toBeDefined();
    // The bug: this was `false` because the row was written before the
    // heuristic ran. After the fix it must be persisted as a retry.
    expect(persisted!.isRetry).toBe(true);
    expect(persisted!.retryReason).toBe("heuristic");
    expect(persisted!.retryOf).toBe(failedEventId);
    // Python parity: the heuristic confidence is recorded in details.
    expect(typeof persisted!.details["retry_confidence"]).toBe("number");

    tracker.close();
  });

  it("persists is_retry=0 for a non-retry call (no false positives)", async () => {
    const tracker = new CostTracker({
      enableRetryHeuristics: true,
      dbPath: join(tmpDir, "test2.db"),
    });

    let taskId = "";
    let secondEventId = "";

    await tracker.track({ taskType: "test" }, async (task) => {
      taskId = task.task.taskId;
      // First call succeeds (no error_type) — second call is NOT a retry.
      task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      const second = task.recordLlmCall("openai", "gpt-4o", 100, 50, 0.05);
      secondEventId = second.eventId;
      expect(second.isRetry).toBe(false);
    });

    const persisted = tracker.buffer
      .queryEvents(taskId)
      .find((e) => e.eventId === secondEventId);
    expect(persisted).toBeDefined();
    expect(persisted!.isRetry).toBe(false);
    expect(persisted!.retryReason).toBeUndefined();

    tracker.close();
  });

  it("persists one operation root and increasing attempt numbers across a retry chain", async () => {
    const tracker = new CostTracker({
      enableRetryHeuristics: true,
      retryHeuristicThreshold: 0.5,
      dbPath: join(tmpDir, "chain.db"),
    });

    let taskId = "";
    let rootEventId = "";
    let secondEventId = "";
    let thirdEventId = "";
    await tracker.track({ taskType: "test" }, async (task) => {
      taskId = task.task.taskId;
      const first = task.recordLlmCall(
        "openai", "gpt-4o", 100, 50, 0.05, undefined, undefined,
        { errorType: "rate_limit" },
      );
      const second = task.recordLlmCall(
        "openai", "gpt-4o", 100, 50, 0.05, undefined, undefined,
        { errorType: "rate_limit" },
      );
      const third = task.recordLlmCall(
        "openai", "gpt-4o", 100, 50, 0.05, undefined, undefined,
        { errorType: "rate_limit" },
      );
      rootEventId = first.eventId;
      secondEventId = second.eventId;
      thirdEventId = third.eventId;
      expect(second.retryOf).toBe(rootEventId);
      expect(third.retryOf).toBe(secondEventId);
    });

    const third = tracker.buffer.queryEvents(taskId)
      .find((event) => event.eventId === thirdEventId);
    expect(third).toBeDefined();
    expect(third!.details.attribution_operation_id).toBe(rootEventId);
    expect(third!.details.attribution_attempt_number).toBe(3);
    expect(toAttributionObservationV3(third!)).toMatchObject({
      operation: {
        id: rootEventId,
        attempt: { id: thirdEventId, number: 3, retry_of: secondEventId },
      },
    });

    tracker.close();
  });
});
