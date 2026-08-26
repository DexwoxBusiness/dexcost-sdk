import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  CostTracker,
  canonicalDecimal,
  capabilityContext,
  capabilityToDict,
  getCapability,
  getIdempotencyKey,
  idempotencyHash,
  idempotencyKey,
  runWithCapability,
  runWithIdempotencyKey,
  validateCapability,
} from "../src/index.js";
import { canonicalToolCapabilityName } from "../src/core/capabilities.js";
import type { CapabilityIdentity } from "../src/core/capabilities.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";

const roots: string[] = [];
const instances: CostTracker[] = [];
function tracker(name: string): CostTracker {
  const root = mkdtempSync(join(tmpdir(), `dexcost-context-${name}-`));
  roots.push(root);
  const instance = new CostTracker({ dbPath: join(root, "test.db"), autoInstrument: [], trackHttp: false });
  instances.push(instance);
  return instance;
}
afterEach(() => {
  for (const instance of instances.splice(0)) {
    try { instance.close(); } catch { /* already closed */ }
  }
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function workflow(name = "support.resolve"): CapabilityIdentity {
  return validateCapability({
    name, kind: "workflow", namespace: "dexcost.agent", version: "2026-08-21",
    source: "project", sourceId: `${name}/v1`, invocation: "automatic",
  });
}

describe("capability attribution", () => {
  it("validates, nests, restores, and isolates async scopes", async () => {
    const outer = workflow("outer.run");
    const inner = workflow("inner.run");
    expect(capabilityToDict(workflow())).toEqual({
      name: "support.resolve", kind: "workflow", namespace: "dexcost.agent",
      version: "2026-08-21", source: "project", source_id: "support.resolve/v1",
      invocation: "automatic",
    });
    expect(() => validateCapability({ name: "Support Resolve", kind: "workflow" })).toThrow(/canonical/);
    expect(() => validateCapability({ name: "support.resolve", kind: "workflow", sourceId: "v1" }))
      .toThrow(/requires source/);
    expect(getCapability()).toBeUndefined();
    await runWithCapability(outer, async () => {
      expect(getCapability()).toEqual(outer);
      runWithCapability(inner, () => expect(getCapability()).toEqual(inner));
      expect(getCapability()).toEqual(outer);
      const observed = await Promise.all([
        runWithCapability(outer, async () => { await Promise.resolve(); return getCapability(); }),
        runWithCapability(inner, async () => { await Promise.resolve(); return getCapability(); }),
      ]);
      expect(observed).toEqual([outer, inner]);
    });
    expect(getCapability()).toBeUndefined();
    // Public alias remains a real scoped helper.
    capabilityContext(outer, () => expect(getCapability()).toEqual(outer));
  });

  it("snapshots richer context and uses collision-safe direct tool identity", () => {
    const instance = tracker("capability");
    const task = instance.startTask({ taskType: "agent.run" });
    const direct = task.recordToolCall("Web Search / V2");
    const nested = runWithCapability(workflow(), () => task.recordToolCall("Web Search / V2"));
    task.end();
    expect(canonicalToolCapabilityName("Web Search / V2")).toBe("web-search-v2-162bba78a1e0");
    expect(direct.details.attribution_capability).toEqual({
      name: "web-search-v2-162bba78a1e0", kind: "tool", invocation: "explicit",
    });
    expect(nested.details.attribution_capability).toEqual(capabilityToDict(workflow()));
    expect(toAttributionObservationV3(nested)!.capability).toEqual(capabilityToDict(workflow()));
    instance.close();
  });
});

describe("cross-SDK idempotency", () => {
  it("validates and restores nested caller-key scopes", () => {
    expect(getIdempotencyKey()).toBeUndefined();
    runWithIdempotencyKey("order-42", () => {
      expect(getIdempotencyKey()).toBe("order-42");
      idempotencyKey("order-42-step-2", () => expect(getIdempotencyKey()).toBe("order-42-step-2"));
      expect(getIdempotencyKey()).toBe("order-42");
    });
    expect(getIdempotencyKey()).toBeUndefined();
    for (const invalid of ["", "x".repeat(256), "contains space", "snowman-☃"]) {
      expect(() => runWithIdempotencyKey(invalid, () => undefined)).toThrow();
    }
  });

  it("matches Python UUIDv5, collapses repeats, and rejects economic conflicts", () => {
    const instance = tracker("idempotency");
    const task = instance.startTask({
      taskType: "order.run", taskId: "11111111-1111-4111-8111-111111111111",
    });
    const first = task.recordToolCall("payments", { costUsd: "0.01", idempotencyKey: "order-42" });
    const repeated = task.recordToolCall("payments", { costUsd: "0.01", idempotencyKey: "order-42" });
    expect(idempotencyHash("order-42")).toBe("3bf8b157c4238eefe5ae4a66eca81c6b887d4dcedb58dd674271859f4dc2edfd");
    expect(first.eventId).toBe("82e97847-f20d-5376-aa15-08548bbf4f16");
    expect(repeated.eventId).toBe(first.eventId);
    expect(repeated.occurredAt).toEqual(first.occurredAt);
    expect(instance.buffer.queryEvents(task.task.taskId)).toHaveLength(1);
    expect(canonicalDecimal(task.task.totalCostUsd)).toBe("0.01");
    expect(() => task.recordToolCall("payments", {
      costUsd: "0.02", idempotencyKey: "order-42",
    })).toThrow(/different economic facts/);
    expect(String(first.details)).not.toContain("order-42");
    task.end();
    instance.close();
  });

  it("lets one key identify distinct economic operations", () => {
    const instance = tracker("components");
    const task = instance.startTask({ taskType: "workflow.run", taskId: randomUUID() });
    const [search, payments] = runWithIdempotencyKey("workflow-9", () => [
      task.recordToolCall("search"), task.recordToolCall("payments"),
    ]);
    expect(search.eventId).not.toBe(payments.eventId);
    expect(instance.buffer.queryEvents(task.task.taskId)).toHaveLength(2);
    task.end();
    instance.close();
  });

  it("distinguishes repeated operations inside one ambient scope and replays deterministically", () => {
    const instance = tracker("occurrences");
    const task = instance.startTask({ taskType: "workflow.run", taskId: randomUUID() });
    const [first, second] = runWithIdempotencyKey("workflow-10", () => [
      task.recordToolCall("search", { costUsd: "0.01" }),
      task.recordToolCall("search", { costUsd: "0.01" }),
    ]);
    const [firstReplay, secondReplay] = runWithIdempotencyKey("workflow-10", () => [
      task.recordToolCall("search", { costUsd: "0.01" }),
      task.recordToolCall("search", { costUsd: "0.01" }),
    ]);
    expect(first.eventId).not.toBe(second.eventId);
    expect(firstReplay.eventId).toBe(first.eventId);
    expect(secondReplay.eventId).toBe(second.eventId);
    expect(first.details._dexcost_idempotency_occurrence).toBe(0);
    expect(second.details._dexcost_idempotency_occurrence).toBe(1);
    expect(instance.buffer.queryEvents(task.task.taskId)).toHaveLength(2);
    task.end();
    instance.close();
  });
});
