/**
 * Tests for RetryHeuristicEngine.
 *
 * TDD: write tests first, then implement.
 */

import { describe, it, expect } from "vitest";
import { randomUUID } from "node:crypto";
import {
  RetryHeuristicEngine,
  TRANSIENT_ERRORS,
  ERROR_LIKELIHOODS,
} from "../src/core/heuristics.js";
import { createCostEvent } from "../src/core/models.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeEvent(
  taskId: string,
  model: string,
  errorType?: string,
  offsetSeconds: number = 0
) {
  const base = new Date("2025-01-01T00:00:00.000Z");
  const occurredAt = new Date(base.getTime() + offsetSeconds * 1000);
  return createCostEvent({
    eventId: randomUUID(),
    taskId,
    eventType: "llm_call",
    model,
    occurredAt,
    details: errorType ? { error_type: errorType } : {},
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("RetryHeuristicEngine", () => {
  // 1. Detects retry after transient error on same model
  it("detects retry after transient error on same model", () => {
    const engine = new RetryHeuristicEngine();
    const taskId = randomUUID();

    const failed = makeEvent(taskId, "gpt-4o", "rate_limit", 0);
    engine.record(failed);

    const candidate = makeEvent(taskId, "gpt-4o", undefined, 5);
    const result = engine.check(candidate);

    expect(result.isRetry).toBe(true);
    expect(result.confidence).toBeGreaterThan(0);
    expect(result.matchedEventId).toBe(failed.eventId);
    expect(result.reason).toBe("heuristic");
  });

  // 2. Does not flag for different model
  it("does not flag when model differs", () => {
    const engine = new RetryHeuristicEngine();
    const taskId = randomUUID();

    const failed = makeEvent(taskId, "gpt-4o", "rate_limit", 0);
    engine.record(failed);

    const candidate = makeEvent(taskId, "claude-3-opus", undefined, 5);
    const result = engine.check(candidate);

    expect(result.isRetry).toBe(false);
    expect(result.confidence).toBe(0);
    expect(result.matchedEventId).toBeUndefined();
    expect(result.reason).toBe("");
  });

  // 3. Does not flag for different task
  it("does not flag when task differs", () => {
    const engine = new RetryHeuristicEngine();

    const failed = makeEvent(randomUUID(), "gpt-4o", "rate_limit", 0);
    engine.record(failed);

    const candidate = makeEvent(randomUUID(), "gpt-4o", undefined, 5);
    const result = engine.check(candidate);

    expect(result.isRetry).toBe(false);
    expect(result.confidence).toBe(0);
  });

  // 4. Does not flag when previous call succeeded (no error_type)
  it("does not flag when previous call succeeded", () => {
    const engine = new RetryHeuristicEngine();
    const taskId = randomUUID();

    const success = makeEvent(taskId, "gpt-4o", undefined, 0); // no error
    engine.record(success);

    const candidate = makeEvent(taskId, "gpt-4o", undefined, 5);
    const result = engine.check(candidate);

    expect(result.isRetry).toBe(false);
    expect(result.confidence).toBe(0);
  });

  // 5. Does not flag outside time window
  it("does not flag when gap exceeds window", () => {
    const engine = new RetryHeuristicEngine(30); // 30-second window
    const taskId = randomUUID();

    const failed = makeEvent(taskId, "gpt-4o", "timeout", 0);
    engine.record(failed);

    // 35 seconds later — outside window
    const candidate = makeEvent(taskId, "gpt-4o", undefined, 35);
    const result = engine.check(candidate);

    expect(result.isRetry).toBe(false);
    expect(result.confidence).toBe(0);
  });

  // 6. Confidence decays with time gap
  it("confidence decays with larger time gap", () => {
    const engine = new RetryHeuristicEngine(30, 0.001); // near-zero threshold so all matches pass
    const taskId = randomUUID();

    // First scenario: 2-second gap
    const taskId1 = randomUUID();
    const failed1 = makeEvent(taskId1, "gpt-4o", "timeout", 0);
    engine.record(failed1);
    const close = makeEvent(taskId1, "gpt-4o", undefined, 2);
    const resultClose = engine.check(close);

    // Second scenario: 20-second gap
    const taskId2 = randomUUID();
    const failed2 = makeEvent(taskId2, "gpt-4o", "timeout", 0);
    engine.record(failed2);
    const far = makeEvent(taskId2, "gpt-4o", undefined, 20);
    const resultFar = engine.check(far);

    expect(resultClose.confidence).toBeGreaterThan(resultFar.confidence);
  });

  // 7. Uses correct base likelihoods per error type
  it("uses correct base likelihoods per error type", () => {
    const engine = new RetryHeuristicEngine(30, 0.001); // near-zero threshold so all pass
    const taskId = randomUUID();

    // rate_limit (1.0) vs timeout (0.9) — same gap of 1 second
    const taskIdA = randomUUID();
    const failedRL = makeEvent(taskIdA, "gpt-4o", "rate_limit", 0);
    engine.record(failedRL);
    const retryRL = makeEvent(taskIdA, "gpt-4o", undefined, 1);
    const resultRL = engine.check(retryRL);

    const taskIdB = randomUUID();
    const failedTO = makeEvent(taskIdB, "gpt-4o", "timeout", 0);
    engine.record(failedTO);
    const retryTO = makeEvent(taskIdB, "gpt-4o", undefined, 1);
    const resultTO = engine.check(retryTO);

    expect(resultRL.confidence).toBeGreaterThan(resultTO.confidence);
  });

  // 8. Prunes old events from window
  it("prunes events older than the window on record()", () => {
    const engine = new RetryHeuristicEngine(10); // 10-second window
    const taskId = randomUUID();

    // Record an event at t=0
    const old = makeEvent(taskId, "gpt-4o", "rate_limit", 0);
    engine.record(old);

    // Record a new event at t=15 — old event should be pruned
    const fresh = makeEvent(taskId, "gpt-4o", undefined, 15);
    engine.record(fresh);

    // Now check a candidate at t=16 — the old failed event should be gone
    const candidate = makeEvent(taskId, "gpt-4o", undefined, 16);
    const result = engine.check(candidate);

    // The only recorded event for this task is `fresh` (no error), so no retry detected
    expect(result.isRetry).toBe(false);
  });

  // 9. Exports TRANSIENT_ERRORS and ERROR_LIKELIHOODS
  it("exports TRANSIENT_ERRORS as a Set with expected values", () => {
    expect(TRANSIENT_ERRORS).toBeInstanceOf(Set);
    expect(TRANSIENT_ERRORS.has("rate_limit")).toBe(true);
    expect(TRANSIENT_ERRORS.has("timeout")).toBe(true);
    expect(TRANSIENT_ERRORS.has("5xx")).toBe(true);
    expect(TRANSIENT_ERRORS.has("server_error")).toBe(true);
    expect(TRANSIENT_ERRORS.has("connection_error")).toBe(true);
  });

  it("exports ERROR_LIKELIHOODS with correct values", () => {
    expect(ERROR_LIKELIHOODS["rate_limit"]).toBe(1.0);
    expect(ERROR_LIKELIHOODS["timeout"]).toBe(0.9);
    expect(ERROR_LIKELIHOODS["5xx"]).toBe(0.85);
    expect(ERROR_LIKELIHOODS["server_error"]).toBe(0.85);
    expect(ERROR_LIKELIHOODS["connection_error"]).toBe(0.8);
  });

  // 10. Defaults to window=30, threshold=0.8
  it("defaults to windowSeconds=30 and threshold=0.8", () => {
    const engine = new RetryHeuristicEngine();
    expect(engine.windowSeconds).toBe(30);
    expect(engine.threshold).toBe(0.8);
  });
});
