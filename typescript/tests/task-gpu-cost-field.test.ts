/**
 * Task 0 — Phase 2 GPU foundation schema groundwork.
 *
 * Mirrors python/tests/test_task_gpu_cost_field.py:
 *  - EventType union admits "gpu_cost" and "gpu_utilization_signal"
 *  - Task.gpuCostUsd defaults to 0 and round-trips through taskToDict / taskFromDict
 *  - Old payloads (without gpu_cost_usd) decode with gpuCostUsd = 0
 *  - Event/Task JSON schemas accept the new event types and the new task field
 */

import { describe, it, expect } from "vitest";
import {
  createCostEvent,
  createTask,
  eventToDict,
  taskFromDict,
  taskToDict,
  Decimal,
  type EventType,
} from "../src/core/models.js";
import { validate } from "../src/schema/validate.js";

const VALID_UUID = "550e8400-e29b-41d4-a716-446655440000";
const VALID_UUID2 = "660e8400-e29b-41d4-a716-446655440001";

describe("EventType GPU values", () => {
  it("'gpu_cost' is assignable to EventType", () => {
    const t: EventType = "gpu_cost";
    expect(t).toBe("gpu_cost");
  });

  it("'gpu_utilization_signal' is assignable to EventType", () => {
    const t: EventType = "gpu_utilization_signal";
    expect(t).toBe("gpu_utilization_signal");
  });

  it("gpu_cost event payload validates against the schema", () => {
    const event = createCostEvent({
      eventId: VALID_UUID,
      taskId: VALID_UUID2,
      eventType: "gpu_cost",
      costConfidence: "unknown",
    });
    const errors = validate(eventToDict(event));
    expect(errors).toEqual([]);
  });

  it("gpu_utilization_signal event payload validates against the schema", () => {
    const event = createCostEvent({
      eventId: VALID_UUID,
      taskId: VALID_UUID2,
      eventType: "gpu_utilization_signal",
      costConfidence: "unknown",
    });
    const errors = validate(eventToDict(event));
    expect(errors).toEqual([]);
  });
});

describe("Task.gpuCostUsd", () => {
  it("defaults to 0", () => {
    const t = createTask({ taskId: VALID_UUID });
    expect(t.gpuCostUsd.toString()).toBe("0");
  });

  it("round-trips through taskToDict / taskFromDict", () => {
    const t = createTask({ taskId: VALID_UUID });
    t.gpuCostUsd = new Decimal("3.99");
    const d = taskToDict(t);
    expect(d["gpu_cost_usd"]).toBe("3.99");
    const t2 = taskFromDict(d);
    expect(t2.gpuCostUsd.toString()).toBe("3.99");
  });

  it("taskFromDict defaults gpuCostUsd to 0 for old payloads (no gpu_cost_usd key)", () => {
    const d = taskToDict(createTask({ taskId: VALID_UUID }));
    delete (d as Record<string, unknown>)["gpu_cost_usd"];
    const t = taskFromDict(d);
    expect(t.gpuCostUsd.toString()).toBe("0");
  });

  it("task payload with gpu_cost_usd validates against the task schema", () => {
    const t = createTask({
      taskId: VALID_UUID,
      taskType: "test",
    });
    t.gpuCostUsd = new Decimal("1.23");
    const errors = validate(taskToDict(t));
    expect(errors).toEqual([]);
  });
});
