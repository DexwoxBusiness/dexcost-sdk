import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { wrapRuntimeHandler, instrumentE2bSandbox, uninstrumentE2bSandbox } from "../src/instruments/runtime.js";
import { createTask, type CostEvent } from "../src/core/models.js";
import { runWithTask } from "../src/core/context.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import type { CostTracker } from "../src/core/tracker.js";

const cases = JSON.parse(readFileSync(new URL("../../fixtures/runtime_conformance.json", import.meta.url), "utf8")).cases;
const config = { serviceKey: "modal_compute" as const, billingAccountId: "acct", resourceId: "resource", vcpuCount: 2, memoryMiB: 4096 };
function recording() {
  const events: CostEvent[] = [], clock = { ms: 0 };
  vi.spyOn(performance, "now").mockImplementation(() => clock.ms);
  const tracker = { buffer: { addEvent(e: CostEvent) { events.push(e); } } } as unknown as CostTracker;
  return { events, tracker, clock, task: createTask({ taskId: randomUUID(), taskType: "agent" }) };
}
afterEach(() => vi.restoreAllMocks());
describe("paired runtime evidence", () => {
  it.each([false, true])("does not time iterator setup as completed work (async=%s)", async asynchronous => {
    const { tracker, events, clock, task } = recording();
    const setup = () => { clock.ms += 100; return [1, 2].values(); };
    const fn = wrapRuntimeHandler(asynchronous ? async () => setup() : setup, tracker, config);
    await runWithTask(task, async () => expect([...await fn()]).toEqual([1, 2]));
    expect(events).toEqual([]);
  });
  it.each(cases)("shared corpus $id", async (c: any) => {
    const { events, tracker, clock, task } = recording();
    const error = new Error("private-code");
    const sync = (arg: string) => { clock.ms += c.ms; if (c.failed) throw error; return arg; };
    const fn = wrapRuntimeHandler(c.async ? async (arg: string) => sync(arg) : sync, tracker, { ...config, serviceKey: c.service });
    await runWithTask(task, async () => {
      if (c.failed) { try { await fn("private-result"); expect.fail("must throw"); } catch (caught) { expect(caught).toBe(error); } }
      else expect(await fn("private-result")).toBe("private-result");
    });
    if (c.quantity === null) { expect(events).toEqual([]); return; }
    expect(events).toHaveLength(1);
    const obs = toAttributionObservationV3(events[0])!;
    expect(obs.provider).toEqual({ name: c.service, service: "runtime" });
    expect(obs.resource).toEqual({ type: "instance", id: "acct/resource" });
    expect(obs.usage[0]).toMatchObject({ metric: "runtime.task_seconds", quantity: c.quantity, unit: "Seconds" });
    expect(obs.operation.status).toBe(c.failed ? "failed" : "succeeded");
    expect(obs.cost_evidence).toBeUndefined();
    expect(events[0].costConfidence).toBe("unknown");
    expect(JSON.stringify(events)).not.toContain("private");
    expect(Date.parse(obs.usage_period!.end_at!) - Date.parse(obs.usage_period!.start_at)).toBe(c.ms);
  });
  it("suppresses nested capture, preserves this, snapshots config and fails open", () => {
    const { tracker, events, clock, task } = recording();
    const options = { ...config };
    const raw = { value: 42, work() { clock.ms += 10; return this.value; } };
    const inner = wrapRuntimeHandler(raw.work, tracker, options);
    const outer = wrapRuntimeHandler(inner, tracker, options);
    options.resourceId = "mutated";
    runWithTask(task, () => expect(outer.call(raw)).toBe(42));
    expect(events).toHaveLength(1);
    expect(toAttributionObservationV3(events[0])!.resource!.id).toBe("acct/resource");
    expect(outer.call(raw)).toBe(42); expect(events).toHaveLength(1);
    tracker.buffer.addEvent = () => { throw new Error("storage"); };
    runWithTask(task, () => expect(outer.call(raw)).toBe(42));
  });
  it("E2B delegates lifecycle, skips background work and stops capture without killing", async () => {
    const { tracker, events, clock, task } = recording();
    class Commands { #secret = "private-output"; async run(_command: string, _opts?: { background?: boolean }) { clock.ms += 100; return this.#secret; } }
    const raw = { sandboxId: "sb-1", commands: new Commands(), runCode: async (_code: string) => { clock.ms += 100; return "private-output"; },
      pause: vi.fn(), kill: vi.fn(), getInfo: () => ({ endAt: new Date("2099-01-01") }) };
    const capture = instrumentE2bSandbox(raw, tracker, { billingAccountId: "acct" });
    expect(() => instrumentE2bSandbox(capture.sandbox, tracker, { billingAccountId: "acct" })).toThrow("already");
    await runWithTask(task, async () => {
      expect(await capture.sandbox.commands.run("private-command")).toBe("private-output");
      await capture.sandbox.runCode("private-code");
      await capture.sandbox.commands.run("background", { background: true });
      capture.sandbox.getInfo(); capture.sandbox.pause(); capture.sandbox.kill();
      uninstrumentE2bSandbox(capture); uninstrumentE2bSandbox(capture); await capture.sandbox.runCode("after-close");
    });
    expect(events).toHaveLength(2); expect(raw.kill).toHaveBeenCalledTimes(1);
  });
  it("keeps concurrent task identity across out-of-order completion", async () => {
    const { tracker, events, clock, task } = recording();
    const other = createTask({ taskId: randomUUID(), taskType: "other" });
    let a!: () => void, b!: () => void;
    const fn = wrapRuntimeHandler((promise: Promise<void>) => promise, tracker, config);
    const one = runWithTask(task, () => fn(new Promise<void>(r => { a = r; })));
    const two = runWithTask(other, () => fn(new Promise<void>(r => { b = r; })));
    clock.ms = 10; b(); await two; clock.ms = 20; a(); await one;
    expect(events.map(e => e.taskId)).toEqual([other.taskId, task.taskId]);
  });
});
