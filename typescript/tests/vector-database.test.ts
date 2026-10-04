import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { afterEach, describe, expect, it } from "vitest";
import { instrumentPinecone, instrumentTurbopuffer, uninstrumentVectorDatabase, vectorDatabaseResourceId } from "../src/instruments/vector-database.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import type { CostTracker } from "../src/core/tracker.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { currentProviderCaptureOwner, runWithProviderCapture } from "../src/instruments/provider-capture.js";
const data = JSON.parse(readFileSync(new URL("../../fixtures/vector_database_conformance.json", import.meta.url), "utf8"));
const buffers: EventBuffer[] = [];
afterEach(() => buffers.splice(0).forEach(b => b.close()));
function setup() { const buffer = new EventBuffer(":memory:"); buffers.push(buffer); const task = createTask({ taskId: randomUUID(), taskType: "memory" }); buffer.upsertTask(task); return { tracker: { buffer } as unknown as CostTracker, task }; }
function bind(client: any, tracker: CostTracker, provider: string, account = "account-a") {
  const scope = { billingAccountId: account, region: data[`${provider}_region`], namespace: data.namespace };
  return provider === "pinecone" ? instrumentPinecone(client, tracker, { ...scope, indexHost: data.pinecone_host }) : instrumentTurbopuffer(client, tracker, scope);
}
function native(c: any) {
  const called: unknown[] = [];
  const client: any = { target: { namespace: data.namespace, indexHostUrl: data.pinecone_host }, _client: { baseURL: `https://${data.turbopuffer_region}.turbopuffer.com`, defaultNamespace: data.namespace } };
  client[c.operation] = async (..._args: unknown[]) => { await Promise.resolve(); called.push(currentProviderCaptureOwner()); return c.response; };
  return { client, called };
}
function jobs(tracker: CostTracker) { return tracker.buffer.getPendingEvents(); }
describe("native vector meters and explicit invoice identities", () => {
  it.each(data.cases)("captures $id once without content or money", async c => {
    const { tracker, task } = setup(), { client, called } = native(c), wrapped = bind(client, tracker, c.provider);
    const promise = runWithTask(task, () => wrapped[c.operation]({ namespace: data.namespace, vector: "PRIVATE-VECTOR", filter: "PRIVATE-FILTER" }));
    expect(await promise).toBe(c.response); expect(await promise).toBe(c.response);
    const observed = jobs(tracker); expect(observed).toHaveLength(c.id === "turbo_query" ? 2 : c.usage ? 1 : 0);
    expect(called).toEqual(c.operation === "upsert" ? [undefined] : [c.provider]);
    if (observed.length) {
      const event = toAttributionObservationV3(observed[0])!;
      expect(observed.flatMap(job => toAttributionObservationV3(job)!.usage.map(u => [u.metric, u.quantity, u.unit]))).toEqual(c.usage);
      expect(event.task_id).toBe(task.taskId); expect(event).not.toHaveProperty("cost");
      expect(event).not.toHaveProperty("cost_evidence"); expect(event).not.toHaveProperty("provider_record_id");
      expect(event.usage_period!.start_at < event.usage_period!.end_at).toBe(true);
      expect(tracker.buffer.getPendingLedger("provider_job")).toHaveLength(0);
      expect(JSON.stringify(observed[0].details)).not.toContain("PRIVATE");
      expect(JSON.stringify(observed[0].details)).not.toContain(data.namespace);
    }
  });
  it("isolates every mapped identity and distinguishes empty and literal default namespace identities", () => {
    const id = (account = "account-a", region = "us-east-1", host = data.pinecone_host, ns = data.namespace) => vectorDatabaseResourceId("pinecone", account, region, host, ns);
    expect(new Set([id(), id("account-b"), id("account-a", "us-west-2"), id("account-a", "us-east-1", "other.svc.pinecone.io"), id("account-a", "us-east-1", data.pinecone_host, "other")]).size).toBe(5);
    expect(id("account-a", "us-east-1", data.pinecone_host, "")).not.toBe(id("account-a", "us-east-1", data.pinecone_host, "__default__"));
    expect(() => id("account/secret")).toThrow(); expect(() => id("account-a", "", "https://private.invalid")).toThrow();
  });
  it.each(["host", "namespace", "headers"])("fails open for unknown %s without changing native behavior", async change => {
    const { tracker, task } = setup(), c = data.cases[0], { client } = native(c);
    if (change === "host") client.target.indexHostUrl = "https://other.svc.pinecone.io";
    const options = { namespace: change === "namespace" ? "other" : data.namespace };
    await runWithTask(task, () => bind(client, tracker, "pinecone").query(options, change === "headers" ? { headers: { authorization: "PRIVATE" } } : {}));
    expect(jobs(tracker)).toHaveLength(0);
  });
  it("preserves catch/finally/raw helpers and never captures a caller-made fallback", async () => {
    const { tracker, task } = setup(), c = data.cases[0], { client } = native(c);
    client.query = () => Promise.reject(new Error("native"));
    const wrapped = bind(client, tracker, "pinecone");
    expect(await runWithTask(task, () => wrapped.query({}).catch(() => c.response))).toBe(c.response);
    expect(jobs(tracker)).toHaveLength(0);
    client.query = () => { const p: any = Promise.resolve(c.response); p.asResponse = () => "raw"; p.withResponse = async () => ({ data: c.response, response: "raw" }); return p; };
    const p = runWithTask(task, () => wrapped.query({})); expect(p.asResponse()).toBe("raw"); expect(jobs(tracker)).toHaveLength(0);
    expect((await p.withResponse()).data).toBe(c.response); await p.finally(() => undefined); await p.catch(() => undefined);
    expect(jobs(tracker)).toHaveLength(1);
  });
  it("leaves nested/disabled calls unchanged and isolates parallel billing accounts", async () => {
    const { tracker, task } = setup(), c = data.cases[0], { client } = native(c), wrapped = bind(client, tracker, "pinecone");
    await runWithTask(task, () => runWithProviderCapture("outer", () => wrapped.query({}))); uninstrumentVectorDatabase(wrapped);
    expect(await runWithTask(task, () => wrapped.query({}))).toBe(c.response); expect(jobs(tracker)).toHaveLength(0);
    await runWithTask(task, () => Promise.all(["account-a", "account-b"].map(account => bind(native(c).client, tracker, "pinecone", account).query({}))));
    expect(new Set(jobs(tracker).map(j => j.details.attribution_resource_id)).size).toBe(2);
  });
});
