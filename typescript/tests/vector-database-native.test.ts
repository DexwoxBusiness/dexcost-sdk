/** Actual provider SDK clients, mock fetch only. Native modules are dev dependencies. */
import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { instrumentPinecone, instrumentTurbopuffer } from "../src/instruments/vector-database.js";
import { currentProviderCaptureOwner } from "../src/instruments/provider-capture.js";
import { createDexcostFetch, clearRecordedEvents, getRecordedEvents, untrackHttp } from "../src/adapters/http.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import type { CostTracker } from "../src/core/tracker.js";
const require = createRequire(import.meta.url);
const { Turbopuffer } = require("@turbopuffer/turbopuffer");
const supportsNativePinecone = Number(process.versions.node.split(".")[0]) >= 22;
let Pinecone: any;
beforeAll(() => {
  // Generated SDK loading is setup, not part of the mocked request deadline.
  // Do not import the upstream Node22-only package on the Node20 test job.
  if (supportsNativePinecone) {
    ({ Pinecone } = require("@pinecone-database/pinecone"));
  }
}, 30_000);
const buffers: EventBuffer[] = [];
afterEach(() => { untrackHttp(); clearRecordedEvents(); buffers.splice(0).forEach(buffer => buffer.close()); });
describe("real vector SDK serialization and HTTP deduplication", () => {
  for (const provider of ["pinecone", "turbopuffer"]) {
    // Provider SDK 9 officially requires Node22; the core facade/conformance gate
    // still runs on the SDK's complete Node matrix without a runtime dependency.
    it.skipIf(provider === "pinecone" && !supportsNativePinecone)(`observes ${provider} native responses once with generic HTTP enabled`, async () => {
    const buffer = new EventBuffer(":memory:"); buffers.push(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "memory" }); buffer.upsertTask(task);
    const tracker = { buffer, pricing: new PricingEngine() } as unknown as CostTracker;
    const owners: unknown[] = [], urls: string[] = [];
    const fakeFetch: typeof fetch = async (url, init) => {
      owners.push(currentProviderCaptureOwner()); urls.push(String(url));
      expect(init?.headers).toBeDefined();
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : {};
      const response = provider === "pinecone"
        ? { namespace: "memory", matches: [], vectors: {}, usage: { readUnits: 0.25 } }
        : body.upsert_rows ? { billing: { billable_logical_bytes_written: 8192, query: { billable_logical_bytes_queried: 1280000000, billable_logical_bytes_returned: 0 } } }
        : { rows: [], billing: { billable_logical_bytes_queried: 1280000000, billable_logical_bytes_returned: 150 }, performance: { server_total_ms: 1 } };
      return new Response(JSON.stringify(response), { status: 200, headers: { "content-type": "application/json" } });
    };
    const trackedFetch = createDexcostFetch({ tracker, fetch: fakeFetch });
    await runWithTask(task, async () => {
      if (provider === "pinecone") {
        const indexHost = "memory-abc.svc.aped-4627-b74a.pinecone.io";
        const index = new Pinecone({ apiKey: "mock-not-a-credential", fetchApi: trackedFetch }).index({ host: indexHost, namespace: "memory" });
        const wrapped = instrumentPinecone(index, tracker, { billingAccountId: "account-a", region: "us-east-1", indexHost, namespace: "memory" });
        expect((await wrapped.query({ vector: [0.1], topK: 1 })).usage.readUnits).toBe(0.25);
        expect((await wrapped.fetch({ ids: ["PRIVATE-ID"] })).usage.readUnits).toBe(0.25);
      } else {
        const native = new Turbopuffer({ apiKey: "mock-not-a-credential", region: "gcp-us-central1", fetch: trackedFetch, maxRetries: 0 });
        const wrapped = instrumentTurbopuffer(native.namespace("memory"), tracker, { billingAccountId: "account-a", region: "gcp-us-central1", namespace: "memory" });
        const query = wrapped.query({ top_k: 1 });
        expect(typeof query.asResponse).toBe("function"); expect(typeof query.withResponse).toBe("function");
        expect((await query.withResponse()).data.billing.billable_logical_bytes_returned).toBe(150);
        await query; // Parsing the same APIPromise again cannot duplicate usage.
        await wrapped.write({ upsert_rows: [{ id: "PRIVATE-ID", text: "PRIVATE-DOCUMENT" }] });
      }
    });
    expect(owners).toEqual([provider, provider]); expect(urls).toHaveLength(2);
    expect(getRecordedEvents()).toHaveLength(0);
    const jobs = buffer.getPendingEvents(); expect(jobs).toHaveLength(provider === "pinecone" ? 2 : 3);
    expect(JSON.stringify(jobs)).not.toContain("PRIVATE"); expect(JSON.stringify(jobs)).not.toContain("mock-not-a-credential");
    if (provider === "pinecone") expect(jobs.map(job => (job.details.attribution_usage_lines as any[])[0].quantity)).toEqual(["0.25", "0.25"]);
  });
  }
});
