import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it } from "vitest";
import { createYouSearchFetch, uninstrumentYouSearch } from "../src/instruments/you-search.js";
import { currentProviderCaptureOwner } from "../src/instruments/provider-capture.js";
import { createDexcostFetch, clearRecordedEvents, getRecordedEvents, untrackHttp } from "../src/adapters/http.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { providerJobFromDict } from "../src/core/provider-jobs.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import type { CostTracker } from "../src/core/tracker.js";

const data = JSON.parse(readFileSync(new URL("../../tests/fixtures/you-search.json", import.meta.url), "utf8"));
const buffers: EventBuffer[] = [];
afterEach(() => { buffers.splice(0).forEach(buffer => buffer.close()); untrackHttp(); clearRecordedEvents(); });
function setup() {
  const buffer = new EventBuffer(":memory:"); buffers.push(buffer);
  const task = createTask({ taskId: randomUUID(), taskType: "search" }); buffer.upsertTask(task);
  const tracker = { buffer, pricing: new PricingEngine() } as unknown as CostTracker;
  return { buffer, task, tracker };
}
const record = (account = data.account, id = data.response.metadata.search_uuid) => createHash("sha256").update(JSON.stringify([account, id])).digest("hex");
const request = (body = data.request): RequestInit => ({ method: "POST", headers: { "X-API-Key": "PRIVATE_KEY", "Content-Type": "application/json" }, body: JSON.stringify(body) });

describe("current You.com base Search fetch capture", () => {
  it.each(["paid", "free", "unknown"] as const)("preserves response/body, nested HTTP dedup and durable replay for %s", async billingTier => {
    const { buffer, task, tracker } = setup(); let calls = 0;
    const native = createDexcostFetch({ tracker, fetch: async () => {
      expect(currentProviderCaptureOwner()).toBe("you_com"); calls++;
      return new Response(JSON.stringify(data.response), { status: 200, headers: { "content-type": "application/json" } });
    } });
    const fetch = createYouSearchFetch(tracker, { billingAccountId: data.account, endpoint: data.endpoint, billingTier, fetch: native });
    await runWithTask(task, async () => {
      for (let n = 0; n < 2; n++) expect(await (await fetch(`${data.endpoint}/v1/search`, request())).json()).toEqual(data.response);
    });
    const job = providerJobFromDict(buffer.getProviderJob("you_com", "search", record())!);
    expect(job.usage[0].quantity.toFixed()).toBe(data.quantity);
    expect(job.usage[0].metric).toBe(data.metric);
    expect(job.billingDimensions).toEqual(billingTier === "paid" ? [["you_search_billing_lane", "public_payg_base"]] : []);
    expect(job.costAmount).toBeUndefined(); expect(job.taskId).toBe(task.taskId);
    expect(JSON.stringify(job.toDict())).not.toContain("PRIVATE");
    expect(buffer.getProviderJobHistory(job.eventId)).toHaveLength(1);
    expect(calls).toBe(2); expect(getRecordedEvents()).toHaveLength(0);
  });
  it.each(data.excluded_parameters)("excludes add-on request %j", async extra => {
    const { buffer, task, tracker } = setup();
    const fetch = createYouSearchFetch(tracker, { billingAccountId: data.account, endpoint: data.endpoint, billingTier: "paid", fetch: async () => new Response(JSON.stringify(data.response)) });
    await runWithTask(task, () => fetch(`${data.endpoint}/v1/search`, request({ ...data.request, ...extra })));
    expect(buffer.getProviderJob("you_com", "search", record())).toBeUndefined();
  });
  it.each(["missing-id", "invalid-id", "missing-results", "failed", "redirect", "wrong-route", "unknown-parameter", "duplicate-parameter", "missing-key", "machine-payment", "disabled", "catch-recovery"])("fails open for %s without altering provider result", async reason => {
    const { buffer, task, tracker } = setup(); const result = structuredClone(data.response);
    if (reason === "missing-id") delete result.metadata.search_uuid;
    if (reason === "invalid-id") result.metadata.search_uuid = "PRIVATE_NOT_ID";
    if (reason === "missing-results") delete result.results;
    const response = new Response(JSON.stringify(result), { status: reason === "failed" ? 500 : 200 });
    if (reason === "redirect") Object.defineProperty(response, "redirected", { value: true });
    const fetch = createYouSearchFetch(tracker, { billingAccountId: data.account, endpoint: data.endpoint, billingTier: "paid", fetch: async () => { if (reason === "catch-recovery") throw new Error("provider failed"); return response; } });
    if (reason === "disabled") uninstrumentYouSearch(fetch);
    const init = request(reason === "unknown-parameter" ? { ...data.request, new_billable_feature: true } : data.request);
    if (reason === "duplicate-parameter") init.body = '{"query":"PRIVATE_QUERY","count":1,"count":10}';
    if (reason === "missing-key") init.headers = { "Content-Type": "application/json" };
    if (reason === "machine-payment") init.headers = { ...init.headers, "Payment-Signature": "PRIVATE_PAYMENT" };
    const url = reason === "wrong-route" ? "https://api.you.com/v1/search" : `${data.endpoint}/v1/search`;
    await runWithTask(task, async () => expect(await fetch(url, init).catch(() => response)).toBe(response));
    expect(buffer.getProviderJob("you_com", "search", record())).toBeUndefined();
  });
  it("supports direct GET, empty results and separate genuine successful requests", async () => {
    const { buffer, task, tracker } = setup(); let calls = 0;
    const ids = [data.response.metadata.search_uuid, randomUUID()];
    const fetch = createYouSearchFetch(tracker, { billingAccountId: data.account, endpoint: data.endpoint, billingTier: "paid", fetch: async () => new Response(JSON.stringify({ results: { web: [] }, metadata: { search_uuid: ids[calls++] } })) });
    await runWithTask(task, async () => { for (let n = 0; n < 2; n++) await fetch(`${data.endpoint}/v1/search?query=PRIVATE&count=100`, { headers: { "x-api-key": "PRIVATE" } }); });
    for (const id of ids) expect(buffer.getProviderJob("you_com", "search", record(data.account, id))).toBeDefined();
    expect(calls).toBe(2);
  });
  it("retains original task on replay, nested facades and account isolation", async () => {
    const { buffer, task, tracker } = setup();
    const native: typeof globalThis.fetch = async () => new Response(JSON.stringify(data.response));
    const options = { billingAccountId: data.account, endpoint: data.endpoint, billingTier: "paid" as const, fetch: native };
    const fetch = createYouSearchFetch(tracker, options);
    await runWithTask(task, () => fetch(`${data.endpoint}/v1/search`, request()));
    const other = createTask({ taskId: randomUUID(), taskType: "search" }); buffer.upsertTask(other);
    await runWithTask(other, () => createYouSearchFetch(tracker, { ...options, fetch })(`${data.endpoint}/v1/search`, request()));
    expect(buffer.getProviderJob("you_com", "search", record())!.task_id).toBe(task.taskId);
    await runWithTask(other, () => createYouSearchFetch(tracker, { ...options, billingAccountId: "account-b" })(`${data.endpoint}/v1/search`, request()));
    expect(buffer.getProviderJob("you_com", "search", record("account-b"))).toBeDefined();
  });
  it("dispatches synchronously before caller mutation and preserves native Response identity", async () => {
    const { buffer, task, tracker } = setup();
    const response = new Response(JSON.stringify(data.response)); let sent: unknown;
    const fetch = createYouSearchFetch(tracker, { billingAccountId: data.account, endpoint: data.endpoint, billingTier: "paid", fetch: async (_input, init) => { sent = init?.body; return response; } });
    const init = request();
    await runWithTask(task, async () => {
      const pending = fetch(`${data.endpoint}/v1/search`, init);
      init.body = JSON.stringify({ ...data.request, extraction: { extraction_mode: "full_page" } });
      expect(await pending).toBe(response);
    });
    expect(sent).toBe(JSON.stringify(data.request));
    expect(buffer.getProviderJob("you_com", "search", record())).toBeDefined();
  });
  it("captures Request input without consuming its returned body and ignores cancellation", async () => {
    const { buffer, task, tracker } = setup();
    const fetch = createYouSearchFetch(tracker, { billingAccountId: data.account, endpoint: data.endpoint, billingTier: "paid", fetch: async () => new Response(JSON.stringify(data.response)) });
    const source = new Request(`${data.endpoint}/v1/search`, request());
    await runWithTask(task, async () => expect(await (await fetch(source)).json()).toEqual(data.response));
    const abort = new DOMException("cancelled", "AbortError");
    const failed = createYouSearchFetch(tracker, { billingAccountId: "aborted-account", endpoint: data.endpoint, billingTier: "paid", fetch: async () => { throw abort; } });
    await runWithTask(task, async () => expect(failed(`${data.endpoint}/v1/search`, request())).rejects.toBe(abort));
    expect(buffer.getProviderJob("you_com", "search", record("aborted-account"))).toBeUndefined();
  });
});
