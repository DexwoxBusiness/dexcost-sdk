/**
 * Tests for the `network` event type (Task 1).
 *
 * Mirrors the Python tests at
 *   python/tests/test_models.py::test_event_type_network_value
 *   python/tests/test_schema.py::test_network_event_type_validates
 */

import { describe, it, expect } from "vitest";
import {
  createCostEvent,
  eventToDict,
  type EventType,
} from "../src/core/models.js";
import { validate } from "../src/schema/validate.js";

const VALID_UUID = "550e8400-e29b-41d4-a716-446655440000";
const VALID_UUID2 = "660e8400-e29b-41d4-a716-446655440001";

describe("EventType network", () => {
  it("'network' is assignable to EventType", () => {
    const t: EventType = "network";
    expect(t).toBe("network");
  });

  it("network events round-trip through eventToDict", () => {
    const event = createCostEvent({
      eventId: VALID_UUID,
      taskId: VALID_UUID2,
      eventType: "network",
      costConfidence: "unknown",
    });
    const dict = eventToDict(event);
    expect(dict["event_type"]).toBe("network");
  });

  it("network event payload validates against the schema", () => {
    const event = createCostEvent({
      eventId: VALID_UUID,
      taskId: VALID_UUID2,
      eventType: "network",
      costConfidence: "unknown",
    });
    const errors = validate(eventToDict(event));
    expect(errors).toEqual([]);
  });
});
