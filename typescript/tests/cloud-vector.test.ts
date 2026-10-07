import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { cloudVectorResourceId, instrumentQdrant, instrumentZilliz, uninstrumentQdrant, uninstrumentZilliz } from "../src/instruments/cloud-vector.js";
import { createDexcostFetch, clearRecordedEvents, getRecordedEvents, untrackHttp } from "../src/adapters/http.js";
import { currentProviderCaptureOwner } from "../src/instruments/provider-capture.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import type { CostTracker } from "../src/core/tracker.js";
const require = createRequire(import.meta.url);
let HttpClient: any, MilvusClient: any;
beforeAll(() => { ({ HttpClient, MilvusClient } = require("@zilliz/milvus2-sdk-node")); }, 30_000);
const buffers: EventBuffer[] = [];
const qhost = "cluster.us-west.cloud.qdrant.io", zhost = "in01-sample.serverless.aws-us-west-2.vectordb.zillizcloud.com";
function setup() {
  const buffer = new EventBuffer(":memory:"); buffers.push(buffer);
  const task = createTask({ taskId: randomUUID(), taskType: "memory" }); buffer.upsertTask(task);
  return { buffer, task, tracker: { buffer, pricing: new PricingEngine() } as unknown as CostTracker };
}
const bind = (host: string) => ({ billingAccountId: "account-a", region: "aws-us-west-2", clusterHost: host });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); untrackHttp(); clearRecordedEvents(); buffers.splice(0).forEach(b => b.close()); });
describe("hosted vector native capture", () => {
  it.skipIf(Number(process.versions.node.split(".")[0]) < 22)("preserves real Qdrant query result while recording the stripped CPU envelope once", async () => {
    const { QdrantClient } = require("@qdrant/js-client-rest"), { buffer, task, tracker } = setup();
    const calls: string[] = [];
    vi.stubGlobal("fetch", createDexcostFetch({ tracker, fetch: async (url) => {
      expect(currentProviderCaptureOwner()).toBe("qdrant_cloud"); calls.push(String(url));
      return new Response(JSON.stringify({ status: "ok", time: 0.1, result: { points: [] }, usage: { hardware: { cpu: 8 } } }), { status: 200, headers: { "content-type": "application/json" } });
    } }));
    const client = new QdrantClient({ url: `https://${qhost}`, checkCompatibility: false });
    const wrapped = instrumentQdrant(client, tracker, bind(qhost));
    await runWithTask(task, async () => expect(await wrapped.query("PRIVATE-COLLECTION", { query: [0.1], limit: 1 })).toEqual({ points: [] }));
    expect(calls).toHaveLength(1); expect(getRecordedEvents()).toHaveLength(0); expect(buffer.getPendingEvents()).toHaveLength(1);
    expect(buffer.getPendingEvents()[0].details.attribution_usage_lines).toEqual([{ metric: "qdrant.hardware.cpu", quantity: "8", unit: "Units" }]);
    expect(JSON.stringify(buffer.getPendingEvents())).not.toContain("PRIVATE");
  });
  it.each([6, 0, undefined, -1, 0.5, Number.MAX_SAFE_INTEGER + 1])("real Zilliz HttpClient handles response cost %s without changing it", async cost => {
    const { buffer, task, tracker } = setup();
    const response = { code: 0, data: [], ...(cost === undefined ? {} : { cost }) }, calls: string[] = [];
    const client = new HttpClient({ endpoint: `https://${zhost}`, token: "mock-not-credential", fetch: createDexcostFetch({ tracker, fetch: async url => {
      expect(currentProviderCaptureOwner()).toBe("milvus_zilliz"); calls.push(String(url));
      return new Response(JSON.stringify(response), { status: 200, headers: { "content-type": "application/json" } });
    } }) });
    const wrapped = instrumentZilliz(client, tracker, bind(zhost));
    await runWithTask(task, async () => expect(await wrapped.search({ collectionName: "PRIVATE", data: [[0.1]], limit: 1 })).toEqual(response));
    expect(calls).toHaveLength(1); expect(getRecordedEvents()).toHaveLength(0);
    expect(buffer.getPendingEvents()).toHaveLength(cost === 6 ? 1 : 0);
    if (cost === 6) expect(buffer.getPendingEvents()[0].details.attribution_usage_lines).toEqual([{ metric: "zilliz.read_vcu", quantity: "6", unit: "VCU" }]);
  });
  it.each(["failed", "route", "options", "disabled", "missing", "catch-recovery"])("Zilliz leaves %s unpriced", async reason => {
    const { buffer, task, tracker } = setup();
    const response = { status: { error_code: reason === "failed" ? "UnexpectedError" : "Success", code: 0, extra_info: reason === "missing" ? {} : { report_value: "6" } }, results: [] };
    const native = { config: { address: `https://${reason === "route" ? "localhost" : zhost}`, ssl: true }, search: vi.fn(async () => { if (reason === "catch-recovery") throw new Error("provider failed"); return response; }) };
    const wrapped = instrumentZilliz(native, tracker, bind(zhost));
    if (reason === "disabled") uninstrumentZilliz(wrapped);
    await runWithTask(task, async () => { await wrapped.search(...(reason === "options" ? [{ metadata: {} }] : [{}])).catch(() => response); });
    expect(buffer.getPendingEvents()).toHaveLength(0); expect(native.search).toHaveBeenCalledTimes(1);
  });
  it("observes the real MilvusClient gRPC search status without adding provider calls", async () => {
    const { buffer, task, tracker } = setup();
    const client = new MilvusClient({ address: `https://${zhost}:19530`, __SKIP_CONNECT__: true });
    // Public native method and its request serializer remain intact. Only the
    // no-network transport/schema response is replaced; no gRPC channel opens.
    const schema = { fields: [{ name: "id", dataType: 5, is_primary_key: true }, { name: "vector", dataType: 101 }] };
    client.describeCollection = vi.fn(async () => ({ schema, anns_fields: { vector: schema.fields[1] } }));
    const status = { error_code: "Success", code: 0, extra_info: { report_value: "12" } }, calls: unknown[] = [];
    client.channelPool = { acquire: async () => ({ Search: (request: unknown, _options: unknown, callback: Function) => {
      expect(currentProviderCaptureOwner()).toBe("milvus_zilliz"); calls.push(request);
      callback(null, { status, results: { scores: [], topks: [] } });
    } }), release: vi.fn() };
    const wrapped = instrumentZilliz(client, tracker, bind(zhost));
    await runWithTask(task, async () => expect((await wrapped.search({ collection_name: "PRIVATE", ids: [1], anns_field: "vector", limit: 1 })).status).toBe(status));
    expect(calls).toHaveLength(1); expect(buffer.getPendingEvents()).toHaveLength(1);
    expect(buffer.getPendingEvents()[0].details.attribution_usage_lines).toEqual([{ metric: "zilliz.read_vcu", quantity: "12", unit: "VCU" }]);
  });
  it("preserves overlapping Qdrant invocation ownership without modifying the native API", async () => {
    const { buffer, task, tracker } = setup();
    const api = { queryPoints: vi.fn(async () => ({ data: { status: "ok", result: { points: [] }, usage: { hardware: { cpu: 2 } } } })) };
    const client = { _restUri: `https://${qhost}:6333`, _openApiClient: api, async query(_collection: string, _request: object) { return (await this._openApiClient.queryPoints()).data.result; } };
    const wrapped = instrumentQdrant(client, tracker, bind(qhost));
    await runWithTask(task, async () => { await Promise.all([wrapped.query("one", {}), wrapped.query("two", {})]); });
    expect(client._openApiClient).toBe(api); expect(buffer.getPendingEvents()).toHaveLength(2);
    uninstrumentQdrant(wrapped); await runWithTask(task, () => wrapped.query("three", {})); expect(buffer.getPendingEvents()).toHaveLength(2);
  });
  it("isolates cloud account/region/host and rejects self-hosted/dedicated identities", () => {
    const id = cloudVectorResourceId("milvus_zilliz", "account-a", "aws-us-west-2", zhost);
    expect(id).not.toBe(cloudVectorResourceId("milvus_zilliz", "account-b", "aws-us-west-2", zhost));
    expect(() => cloudVectorResourceId("qdrant_cloud", "account-a", "aws-us-west-2", "localhost")).toThrow();
    expect(() => cloudVectorResourceId("milvus_zilliz", "account-a", "aws-us-west-2", zhost.replace(".serverless", ""))).toThrow();
  });
});
