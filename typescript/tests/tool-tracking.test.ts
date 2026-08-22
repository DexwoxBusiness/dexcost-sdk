import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  CostTracker,
  Decimal,
  ToolUsage,
  close,
  getCurrentTask,
  init,
  reportToolCall,
  trackTool,
} from "../src/index.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";

const roots: string[] = [];
function db(name: string): string {
  const root = mkdtempSync(join(tmpdir(), `dexcost-tool-${name}-`));
  roots.push(root);
  return join(root, "test.db");
}

afterEach(() => {
  close();
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("ToolUsage exactness", () => {
  it("rejects binary fractions, zero, and non-canonical meters", () => {
    expect(ToolUsage.fromInput("2.500").quantity.equals(new Decimal("2.500"))).toBe(true);
    expect(() => ToolUsage.fromInput(1.5)).toThrow(/Decimal, safe integer/);
    expect(() => ToolUsage.fromInput(0)).toThrow(/positive/);
    expect(() => ToolUsage.fromInput(1, { metric: "Page Count" })).toThrow(/canonical lowercase/);
  });
});

describe("manual tool calls", () => {
  it("maps to strict v3 without inputs or outputs", () => {
    const tracker = new CostTracker({ dbPath: db("manual"), autoInstrument: [], trackHttp: false });
    const taskId = randomUUID();
    const task = tracker.startTask({ taskType: "support.resolve", taskId });
    const event = task.recordToolCall("customer-database", {
      operation: "lookup",
      durationMs: 125,
      usage: ToolUsage.fromInput(3),
      costUsd: "0.015",
      provider: "postgresql",
      providerRecordId: "query-42",
      dimensions: { cache_hit: true, rows: 3, tier: "primary" },
    });
    task.end();

    const observation = toAttributionObservationV3(event, "production")!;
    expect(observation.resource).toEqual({ type: "tool", id: "customer-database" });
    expect(observation.capability).toEqual({
      name: "customer-database", kind: "tool", invocation: "explicit",
    });
    expect(observation.operation).toMatchObject({ name: "tool.lookup", status: "succeeded", latency_ms: 125 });
    expect(observation.provider.record_id).toBe("query-42");
    expect(observation.usage[0].quantity).toBe("3");
    expect(observation.usage[0].dimensions.map((item) => item.key)).toEqual(["cache_hit", "rows", "tier"]);
    expect(JSON.stringify(event.details)).not.toContain("inputs");
    expect(JSON.stringify(event.details)).not.toContain("output");
    tracker.close();
  });

  it("preserves explicit retry correlation", () => {
    const tracker = new CostTracker({ dbPath: db("retry"), autoInstrument: [], trackHttp: false });
    const operationId = randomUUID();
    const firstId = randomUUID();
    const secondId = randomUUID();
    const task = tracker.startTask({ taskType: "retry.run" });
    const first = task.recordToolCall("browser", { operationId, attemptId: firstId });
    const second = task.recordToolCall("browser", {
      operationId, attemptId: secondId, attemptNumber: 2, retryOf: firstId,
    });
    task.end();
    expect(first.eventId).toBe(firstId);
    expect(toAttributionObservationV3(second)!.operation).toMatchObject({
      id: operationId,
      attempt: { id: secondId, number: 2, retry_of: firstId },
    });
    tracker.close();
  });
});

describe("execution-shape-safe decorators", () => {
  it("preserves sync results and original failures", () => {
    const tracker = new CostTracker({ dbPath: db("sync"), autoInstrument: [], trackHttp: false });
    const task = tracker.startTask({ taskType: "tool.run" });
    const upper = task.trackTool("search", { operation: "query" })((value: string) => value.toUpperCase());
    const boom = new Error("customer secret");
    const fail = task.trackTool("payments", { operation: "authorize" })(() => { throw boom; });
    expect(upper("private prompt")).toBe("PRIVATE PROMPT");
    expect(() => fail()).toThrow(boom);
    task.end();
    const events = tracker.buffer.queryEvents(task.task.taskId);
    expect(events).toHaveLength(2);
    const failed = events.find((event) => event.serviceName === "payments")!;
    expect(toAttributionObservationV3(failed)!.operation.error).toEqual({ type: "error" });
    expect(JSON.stringify(failed.details)).not.toContain("customer secret");
    tracker.close();
  });

  it("records Promise success and cancellation", async () => {
    const tracker = new CostTracker({ dbPath: db("async"), autoInstrument: [], trackHttp: false });
    const task = tracker.startTask({ taskType: "async.run" });
    const succeed = task.trackTool("async-search")(async () => 7);
    const cancelError = Object.assign(new Error("stop"), { name: "AbortError" });
    const cancel = task.trackTool("async-cancel")(async () => { throw cancelError; });
    expect(await succeed()).toBe(7);
    await expect(cancel()).rejects.toBe(cancelError);
    task.end();
    const statuses = Object.fromEntries(tracker.buffer.queryEvents(task.task.taskId).map((event) => [
      event.serviceName, event.details.attribution_operation_status,
    ]));
    expect(statuses).toMatchObject({ "async-search": "succeeded", "async-cancel": "cancelled" });
    tracker.close();
  });

  it("distinguishes generator exhaustion from early close", () => {
    const tracker = new CostTracker({ dbPath: db("generator"), autoInstrument: [], trackHttp: false });
    const task = tracker.startTask({ taskType: "generator.run" });
    const pages = task.trackTool("pages")(function* () { yield 1; yield 2; });
    expect([...pages()]).toEqual([1, 2]);
    const partial = pages();
    expect(partial.next().value).toBe(1);
    partial.return(undefined);
    task.end();
    expect(tracker.buffer.queryEvents(task.task.taskId).map(
      (event) => event.details.attribution_operation_status,
    ).sort()).toEqual(["cancelled", "succeeded"]);
    tracker.close();
  });

  it("records async-generator early close as cancelled", async () => {
    const tracker = new CostTracker({ dbPath: db("async-generator"), autoInstrument: [], trackHttp: false });
    const task = tracker.startTask({ taskType: "async_generator.run" });
    const pages = task.trackTool("async-pages")(async function* () { yield 1; yield 2; });
    const stream = pages();
    expect((await stream.next()).value).toBe(1);
    await stream.return(undefined);
    task.end();
    expect(tracker.buffer.queryEvents(task.task.taskId)[0].details.attribution_operation_status)
      .toBe("cancelled");
    tracker.close();
  });
});

describe("top-level and cross-process tool APIs", () => {
  it("can be declared before init and creates one terminal auto task", () => {
    const parse = trackTool("document-parser", { operation: "parse" })(() => 42);
    const tracker = init({ dbPath: db("global"), autoInstrument: [], trackHttp: false });
    expect(parse()).toBe(42);
    const tasks = tracker.buffer.getAllTasks();
    expect(tasks).toHaveLength(1);
    expect(tasks[0]).toMatchObject({ taskType: "tool.document-parser", status: "success" });
    expect(tracker.buffer.queryEvents(tasks[0].taskId)).toHaveLength(1);
  });

  it("reports against a remote task UUID without inserting a shadow task", () => {
    const tracker = init({ dbPath: db("remote"), autoInstrument: [], trackHttp: false });
    const taskId = randomUUID();
    const event = reportToolCall("remote-worker", { taskId });
    expect(event.taskId).toBe(taskId);
    expect(tracker.buffer.getTask(taskId)).toBeUndefined();
    expect(tracker.buffer.queryEvents(taskId)).toEqual([event]);
  });

  it("attachment scopes automatic work but cannot end or rewrite the task", () => {
    const tracker = new CostTracker({ dbPath: db("attach"), autoInstrument: [], trackHttp: false });
    const taskId = randomUUID();
    const attached = tracker.attachTask(taskId);
    attached.run(() => {
      expect(getCurrentTask()?.taskId).toBe(taskId);
      attached.recordToolCall("remote-tool");
    });
    expect(getCurrentTask()).toBeUndefined();
    expect(tracker.buffer.getTask(taskId)).toBeUndefined();
    expect(() => attached.end()).toThrow(/do not own task lifecycle/);
    tracker.close();
  });
});
