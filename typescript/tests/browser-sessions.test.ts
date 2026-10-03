import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it } from "vitest";
import { bindBrowserSession, recordBrowserSession, instrumentBrowserbase, uninstrumentBrowserbase, type BrowserSessionUsage } from "../src/instruments/browser-sessions.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { providerJobFromDict } from "../src/core/provider-jobs.js";
import { EventBuffer } from "../src/transport/buffer.js";
import type { CostTracker } from "../src/core/tracker.js";

const data = JSON.parse(readFileSync(new URL("../../fixtures/browser_session_conformance.json", import.meta.url), "utf8"));
const start = new Date(data.started_at), end = new Date(data.ended_at);
const buffers: EventBuffer[] = [];
afterEach(() => buffers.splice(0).forEach(b => b.close()));
function setup() {
  const buffer = new EventBuffer(":memory:"); buffers.push(buffer);
  const task = createTask({ taskId: data.task_id, taskType: "browser" }); buffer.upsertTask(task);
  const tracker = { buffer } as unknown as CostTracker;
  return { tracker, task, job: (service: string, id: string) => providerJobFromDict(buffer.getProviderJob(service, "browser", `account/${id}`)!) };
}
describe("paired browser sessions", () => {
  it("snapshots proxy provenance before awaiting and rejects unknown native start times", async () => {
    const { tracker, task, job } = setup();
    const result = { id: "session", projectId: "resource", startedAt: start.toISOString() as unknown, status: "COMPLETED", endedAt: end.toISOString(), proxyBytes: 1000 };
    const options = { proxies: false };
    const client = instrumentBrowserbase({ sessions: { create: async (_options: unknown) => result } }, tracker, { billingAccountId: "account" });
    await runWithTask(task, async () => {
      const request = client.sessions.create(options); options.proxies = true; await request;
      expect(job("browserbase", "session").usage.map(u => u.metric)).toEqual(["browser.session_seconds"]);
      for (const bad of [null, "2026-09-01T12:00:00", "2026-09-01T12:00:00.000001Z"]) {
        result.id = "invalid"; result.startedAt = bad; await client.sessions.create(options);
        expect(tracker.buffer.getProviderJob("browserbase", "browser", "account/invalid")).toBeUndefined();
      }
    });
  });
  it.each(["catch", "finally"] as const)("captures native Promise %s chains without inventing helper methods", async method => {
    const { tracker, task, job } = setup();
    const result = { id: "chain", projectId: "resource", startedAt: start.toISOString(), status: "COMPLETED", endedAt: end.toISOString() };
    const client = instrumentBrowserbase({ sessions: { create: async () => result } }, tracker, { billingAccountId: "account" });
    await runWithTask(task, async () => {
      const request = client.sessions.create();
      expect((request as any).withResponse).toBeUndefined();
      expect(await request[method](() => undefined)).toBe(result);
    });
    expect(job("browserbase", "chain").revision).toBe(2);
  });
  it("fails open on telemetry, preserves rejection and rejects cross-provider meters", async () => {
    const { tracker, task } = setup();
    const binding = { serviceKey: "browserbase" as const, billingAccountId: "account", resourceId: "resource", sessionId: "session", startedAt: start };
    const usage = { ...binding, endedAt: end, status: "failed" as const };
    expect(recordBrowserSession(tracker, usage)).toBe(false);
    runWithTask(task, () => bindBrowserSession(tracker, binding));
    expect(() => recordBrowserSession(tracker, { ...usage, proxyBytes: "10" })).toThrow();
    expect(() => recordBrowserSession(tracker, { ...usage, timeUnits: "10" })).toThrow();
    expect(() => recordBrowserSession(tracker, { ...usage, revision: 3 })).toThrow();
    const result = { id: "new", projectId: "resource", startedAt: start.toISOString(), status: "COMPLETED", endedAt: end.toISOString() };
    const error = new Error("private-error");
    const client = instrumentBrowserbase({ sessions: { create: async () => result, retrieve: async () => { throw error; } } }, tracker, { billingAccountId: "account" });
    tracker.buffer.getProviderJob = () => { throw new Error("telemetry"); };
    expect(await runWithTask(task, () => client.sessions.create())).toBe(result);
    await expect(client.sessions.retrieve()).rejects.toBe(error);
  });
  it.each(data.cases)("meters and replay: $id", (c: any) => {
    const { tracker, task, job } = setup();
    const identity = { serviceKey: c.service, billingAccountId: "account", sessionId: c.id };
    const binding = { ...identity, resourceId: "resource", startedAt: start, managedProxy: c.managed };
    expect(bindBrowserSession(tracker, binding)).toBe(false);
    runWithTask(task, () => expect(bindBrowserSession(tracker, binding)).toBe(true));
    const meters = Object.fromEntries(Object.entries(c.meters).map(([k, v]) => [k.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase()), v]));
    const usage = { ...identity, endedAt: end, status: c.status, ...meters };
    expect(recordBrowserSession(tracker, usage)).toBe(true);
    expect(recordBrowserSession(tracker, usage)).toBe(true);
    runWithTask(task, () => expect(bindBrowserSession(tracker, binding)).toBe(true));
    const final = job(c.service, c.id), obs = final.toAttributionObservation() as any;
    expect(final.revision).toBe(2); expect(final.taskId).toBe(task.taskId);
    expect(Object.fromEntries(final.usage.map(u => [u.metric, u.quantity.toFixed()]))).toEqual(c.usage);
    expect(obs.cost_evidence).toBeUndefined(); expect(obs.resource).toEqual({ type: "endpoint", id: "account/resource" });
    expect(Date.parse(obs.usage_period!.end_at!)).toBe(end.getTime());
  });
  it("rejects ownership changes, secrets, invalid meters; accepts explicit full corrections", () => {
    const { tracker, task, job } = setup();
    const binding = { serviceKey: "browserless" as const, billingAccountId: "account", resourceId: "resource", sessionId: "connection", startedAt: start };
    runWithTask(task, () => {
      bindBrowserSession(tracker, binding);
      for (const field of ["sessionId", "resourceId", "billingAccountId"]) expect(() => bindBrowserSession(tracker, { ...binding, [field]: "wss://private?token=secret" })).toThrow();
      expect(() => bindBrowserSession(tracker, { ...binding, resourceId: "wrong" })).toThrow();
    });
    runWithTask(createTask({ taskId: "22222222-2222-4222-8222-222222222222", taskType: "other" }), () => expect(() => bindBrowserSession(tracker, binding)).toThrow());
    const usage = { ...binding, endedAt: end, status: "succeeded" as const };
    for (const bad of ["-1", "NaN", "1e3", "0.0000000000001", true, 1.5]) expect(() => recordBrowserSession(tracker, { ...usage, timeUnits: bad } as BrowserSessionUsage)).toThrow();
    expect(() => recordBrowserSession(tracker, { ...usage, proxyBytes: "1" })).toThrow();
    for (const [index, timeUnits] of ["3", "0", "2"].entries()) recordBrowserSession(tracker, { ...usage, timeUnits, revision: index + 2 });
    expect(job("browserless", "connection").revision).toBe(4);
    expect(job("browserless", "connection").usage[0].quantity.toFixed()).toBe("2");
    expect(recordBrowserSession(tracker, { ...usage, timeUnits: "3", revision: 2 })).toBe(false);
    expect(() => recordBrowserSession(tracker, { ...usage, timeUnits: "3", revision: 4 })).toThrow();
  });
  it("preserves native promise helpers and private receivers; terminal-only and no secrets", async () => {
    const { tracker, task, job } = setup();
    const response = { id: "session", projectId: "project", startedAt: start.toISOString(), status: "RUNNING", endedAt: null as string | null,
      expiresAt: "2099-01-01", proxyBytes: 25, connectUrl: "wss://private", signingKey: "private" };
    class APIPromise extends Promise<typeof response> {
      #body = response;
      asResponse() { return "raw-response"; }
      async withResponse() { return { data: this.#body, request_id: "id" }; }
    }
    class Sessions {
      #body = response;
      create(_options: unknown) { return new APIPromise(resolve => resolve(this.#body)); }
      retrieve(_id: string) { return this.create({}); }
      update(_id: string, _options: unknown) { return this.create({}); }
    }
    const client = instrumentBrowserbase({ sessions: new Sessions() }, tracker, { billingAccountId: "account" });
    await runWithTask(task, async () => {
      const request = client.sessions.create({ proxies: true });
      expect(request.asResponse()).toBe("raw-response");
      expect((await request.withResponse()).data).toBe(response);
      expect(await request).toBe(response);
    });
    await client.sessions.update("session", { status: "REQUEST_RELEASE" });
    expect(job("browserbase", "session").revision).toBe(1);
    response.status = "TIMED_OUT"; response.endedAt = end.toISOString();
    expect(await client.sessions.retrieve("session")).toBe(response);
    expect(await client.sessions.retrieve("session")).toBe(response);
    expect(job("browserbase", "session").revision).toBe(2);
    expect(job("browserbase", "session").status).toBe("failed");
    expect(JSON.stringify(job("browserbase", "session").toDict())).not.toContain("private");
    response.proxyBytes = 1; await client.sessions.retrieve("session"); // stale native polling cannot overwrite final facts
    expect(job("browserbase", "session").usage[1].quantity.toFixed()).toBe("25");
    uninstrumentBrowserbase(client); response.proxyBytes = 26; await client.sessions.retrieve("session");
    expect(job("browserbase", "session").revision).toBe(2);
    expect(() => instrumentBrowserbase(client, tracker, { billingAccountId: "account" })).toThrow("already");
  });
});
