/**
 * Task model carries the four network-capture fields.
 *
 * Mirrors python/tests/test_task_network_fields.py.
 */

import { describe, it, expect } from "vitest";
import {
  createTask,
  taskFromDict,
  taskToDict,
} from "../src/core/models.js";
import { validate } from "../src/schema/validate.js";

const VALID_UUID = "550e8400-e29b-41d4-a716-446655440000";

describe("Task network capture fields", () => {
  it("defaults the four network fields", () => {
    const t = createTask({ taskId: VALID_UUID, taskType: "x" });
    expect(t.networkBytesIn).toBe(0);
    expect(t.networkBytesOut).toBe(0);
    expect(t.networkCallCount).toBe(0);
    expect(t.networkByHost).toEqual({ hosts: [] });
  });

  it("round-trips the four network fields through taskToDict/taskFromDict", () => {
    const t = createTask({ taskId: VALID_UUID, taskType: "x" });
    t.networkBytesIn = 4096;
    t.networkBytesOut = 512;
    t.networkCallCount = 3;
    t.networkByHost = {
      hosts: [
        { host: "a.com", calls: 3, bytes_in: 4096, bytes_out: 512 },
      ],
    };
    const restored = taskFromDict(taskToDict(t));
    expect(restored.networkBytesIn).toBe(4096);
    expect(restored.networkBytesOut).toBe(512);
    expect(restored.networkCallCount).toBe(3);
    expect(restored.networkByHost).toEqual({
      hosts: [
        { host: "a.com", calls: 3, bytes_in: 4096, bytes_out: 512 },
      ],
    });
  });

  it("serialised dict uses snake_case keys", () => {
    const t = createTask({ taskId: VALID_UUID, taskType: "x" });
    t.networkBytesIn = 11;
    t.networkBytesOut = 22;
    t.networkCallCount = 1;
    t.networkByHost = { hosts: [] };
    const dict = taskToDict(t);
    expect(dict["network_bytes_in"]).toBe(11);
    expect(dict["network_bytes_out"]).toBe(22);
    expect(dict["network_call_count"]).toBe(1);
    expect(dict["network_by_host"]).toEqual({ hosts: [] });
  });

  it("legacy task dict (no network_* keys) restores to defaults", () => {
    const legacy = taskToDict(createTask({ taskId: VALID_UUID, taskType: "x" }));
    delete (legacy as Record<string, unknown>)["network_bytes_in"];
    delete (legacy as Record<string, unknown>)["network_bytes_out"];
    delete (legacy as Record<string, unknown>)["network_call_count"];
    delete (legacy as Record<string, unknown>)["network_by_host"];
    const restored = taskFromDict(legacy);
    expect(restored.networkBytesIn).toBe(0);
    expect(restored.networkBytesOut).toBe(0);
    expect(restored.networkCallCount).toBe(0);
    expect(restored.networkByHost).toEqual({ hosts: [] });
  });

  it("task dict with network fields validates against the schema", () => {
    const t = createTask({ taskId: VALID_UUID, taskType: "x" });
    t.networkBytesIn = 100;
    t.networkBytesOut = 200;
    t.networkCallCount = 1;
    t.networkByHost = {
      hosts: [{ host: "h", calls: 1, bytes_in: 100, bytes_out: 200 }],
    };
    const errors = validate(taskToDict(t));
    expect(errors).toEqual([]);
  });

  it("default task dict still validates against the schema", () => {
    const t = createTask({ taskId: VALID_UUID, taskType: "x" });
    const errors = validate(taskToDict(t));
    expect(errors).toEqual([]);
  });
});
