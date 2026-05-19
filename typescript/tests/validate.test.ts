/**
 * Tests for JSON schema validation (validate.ts).
 *
 * TDD: these tests are written before the implementation.
 */

import { describe, it, expect } from "vitest";
import { validate } from "../src/schema/validate.js";
import {
  createTask,
  createCostEvent,
  taskToDict,
  eventToDict,
} from "../src/core/models.js";

const VALID_UUID = "550e8400-e29b-41d4-a716-446655440000";
const VALID_UUID2 = "660e8400-e29b-41d4-a716-446655440001";

// ---------------------------------------------------------------------------
// Test 1: Validates a correct event payload
// ---------------------------------------------------------------------------
describe("validate", () => {
  it("returns empty array for a valid event payload", () => {
    const event = createCostEvent({ eventId: VALID_UUID, taskId: VALID_UUID2 });
    const dict = eventToDict(event);
    const errors = validate(dict);
    expect(errors).toEqual([]);
  });

  // -------------------------------------------------------------------------
  // Test 2: Validates a correct task payload
  // -------------------------------------------------------------------------
  it("returns empty array for a valid task payload", () => {
    const task = createTask({ taskId: VALID_UUID, taskType: "generate_report" });
    const dict = taskToDict(task);
    const errors = validate(dict);
    expect(errors).toEqual([]);
  });

  // -------------------------------------------------------------------------
  // Test 3: Returns errors for invalid event (missing required fields)
  // -------------------------------------------------------------------------
  it("returns errors for event payload missing required fields", () => {
    // Only provide event_id so it is routed to the event schema, but omit
    // required fields: task_id, event_type, occurred_at, cost_usd,
    // cost_confidence, schema_version.
    const payload: Record<string, unknown> = {
      event_id: VALID_UUID,
      schema_version: "1",
    };
    const errors = validate(payload);
    expect(errors.length).toBeGreaterThan(0);
    // At least one error must mention a missing field
    const combined = errors.join(" ");
    expect(combined).toMatch(/task_id|event_type|occurred_at|cost_usd|cost_confidence/);
  });

  // -------------------------------------------------------------------------
  // Test 4: Returns errors for invalid task (missing required fields)
  // -------------------------------------------------------------------------
  it("returns errors for task payload missing required fields", () => {
    // Only task_id + schema_version; omit task_type, status, started_at, etc.
    const payload: Record<string, unknown> = {
      task_id: VALID_UUID,
      schema_version: "1",
    };
    const errors = validate(payload);
    expect(errors.length).toBeGreaterThan(0);
    const combined = errors.join(" ");
    expect(combined).toMatch(/task_type|status|started_at|llm_cost_usd/);
  });

  // -------------------------------------------------------------------------
  // Test 5: Rejects unsupported schema version
  // -------------------------------------------------------------------------
  it("returns error for unsupported schema_version", () => {
    const payload: Record<string, unknown> = {
      event_id: VALID_UUID,
      schema_version: "99",
    };
    const errors = validate(payload);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("Unsupported schema_version: 99");
  });

  // -------------------------------------------------------------------------
  // Test 6: Rejects payload with neither task_id nor event_id
  // -------------------------------------------------------------------------
  it("returns error when neither task_id nor event_id is present", () => {
    const payload: Record<string, unknown> = {
      schema_version: "1",
      some_field: "some_value",
    };
    const errors = validate(payload);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain(
      "Cannot determine payload type: missing task_id or event_id"
    );
  });

  // -------------------------------------------------------------------------
  // Test 7: Validates event with all optional fields set to null
  // -------------------------------------------------------------------------
  it("returns empty array for event with all optional fields null", () => {
    const event = createCostEvent({ eventId: VALID_UUID, taskId: VALID_UUID2 });
    const dict = eventToDict(event);
    // Explicitly set all nullable optional fields to null
    (dict as Record<string, unknown>).pricing_source = null;
    (dict as Record<string, unknown>).pricing_version = null;
    (dict as Record<string, unknown>).provider = null;
    (dict as Record<string, unknown>).model = null;
    (dict as Record<string, unknown>).input_tokens = null;
    (dict as Record<string, unknown>).output_tokens = null;
    (dict as Record<string, unknown>).cached_tokens = null;
    (dict as Record<string, unknown>).latency_ms = null;
    (dict as Record<string, unknown>).service_name = null;
    (dict as Record<string, unknown>).retry_reason = null;
    (dict as Record<string, unknown>).retry_of = null;
    const errors = validate(dict);
    expect(errors).toEqual([]);
  });
});
