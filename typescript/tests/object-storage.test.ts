import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { instrumentObjectStorage, uninstrumentObjectStorage, type ObjectStorageBinding } from "../src/instruments/object-storage.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { providerJobFromDict } from "../src/core/provider-jobs.js";
import { EventBuffer } from "../src/transport/buffer.js";
import type { CostTracker } from "../src/core/tracker.js";
import { runWithProviderCapture } from "../src/instruments/provider-capture.js";
const data = JSON.parse(readFileSync(new URL("../../fixtures/object_storage_conformance.json", import.meta.url), "utf8"));
const buffers: EventBuffer[] = [];
afterEach(() => { buffers.splice(0).forEach(b => b.close()); vi.useRealTimers(); });
function setup() {
  const buffer = new EventBuffer(":memory:"); buffers.push(buffer);
  const task = createTask({ taskId: data.task_id, taskType: "storage" }); buffer.upsertTask(task);
  return { tracker: { buffer } as unknown as CostTracker, task };
}
function scope(provider: "aws_s3" | "r2_cloudflare"): ObjectStorageBinding {
  return { provider, billingAccountId: provider === "aws_s3" ? data.aws_payer : data.r2_account, bucket: data.bucket, region: provider === "aws_s3" ? "us-east-1" : "auto", ...(provider === "aws_s3" ? { bucketOwnerAccountId: data.aws_owner, ownerPays: true } : {}) };
}
const names: Record<string, string> = { get_object: "GetObjectCommand", put_object: "PutObjectCommand", list_objects_v2: "ListObjectsV2Command", delete_object: "DeleteObjectCommand" };
function native(c: any) {
  const input = { Bucket: data.bucket, Key: "PRIVATE-KEY", Body: "PRIVATE-CONTENT", ...c.request };
  const result = { $metadata: { requestId: c.id, httpStatusCode: c.status ?? 200, attempts: c.attempts === undefined ? 1 : c.attempts }, ...c.response };
  const command = { constructor: { name: names[c.operation] }, input };
  const config = { serviceId: "S3", region: async () => c.client_region ?? scope(c.provider).region,
    endpoint: async () => c.endpoint ?? (c.provider === "aws_s3" ? "https://s3.us-east-1.amazonaws.com" : `https://${data.r2_account}.r2.cloudflarestorage.com`) };
  class Client {
    #calls = 0;
    middleware: any;
    middlewareStack = { add: (middleware: any) => { this.middleware = middleware; } };
    config = config;
    async send(value: unknown) {
      this.#calls++; expect(value).toBe(command);
      if (!c.missing_transport && this.middleware) {
        const url = new URL(c.transport_endpoint ?? await config.endpoint());
        for (let i = 0; i < (c.transport_attempts ?? 1); i++) await this.middleware(async () => result)({ request: { hostname: url.hostname, protocol: url.protocol, path: `/${c.transport_bucket ?? data.bucket}/PRIVATE-KEY`, method: c.operation === "put_object" ? "PUT" : "GET" } });
      }
      return result;
    }
    count() { return this.#calls; }
  }
  return { client: new Client(), command, result };
}
describe("paired object-storage native evidence", () => {
  it.each(data.cases)("captures only verified $id", async c => {
    const { tracker, task } = setup(), { client, command, result } = native(c);
    const wrapped = instrumentObjectStorage(client, tracker, scope(c.provider));
    expect(await runWithTask(task, () => wrapped.send(command))).toBe(result);
    expect(wrapped.count()).toBe(1);
    const resource = c.provider === "aws_s3" ? `${data.aws_payer}/${data.aws_owner}.us-east-1.${data.bucket}` : `${data.r2_account}/r2`;
    const raw = tracker.buffer.getProviderJob(c.provider, "object_storage", `${resource}/${c.id}`);
    if (!c.metric) { expect(raw).toBeUndefined(); return; }
    const job = providerJobFromDict(raw!), event = job.toAttributionObservation();
    expect(event.usage[0]).toMatchObject({ metric: c.metric, quantity: "1", unit: "Requests" });
    expect(event.resource.id).toBe(resource); expect(event.task_id).toBe(task.taskId);
    expect(JSON.stringify(job.toDict())).not.toContain("PRIVATE"); expect(event).not.toHaveProperty("cost");
    await runWithTask(task, () => wrapped.send(command));
    expect(tracker.buffer.getProviderJob(c.provider, "object_storage", `${resource}/${c.id}`)).toEqual(raw);
  });
  it("keeps nested capture and disabled wrappers transparent, and retains native errors", async () => {
    const { tracker, task } = setup(), { client, command, result } = native(data.cases.find((c: any) => c.id === "aws_get"));
    const wrapped = instrumentObjectStorage(client, tracker, scope("aws_s3"));
    await runWithTask(task, () => runWithProviderCapture("outer", () => wrapped.send(command)));
    uninstrumentObjectStorage(wrapped); expect(await wrapped.send(command)).toBe(result);
    expect(tracker.buffer.getProviderJob("aws_s3", "object_storage", `${data.aws_payer}/${data.aws_owner}.us-east-1.${data.bucket}/aws_get`)).toBeUndefined();
    const error = new Error("native"); client.send = async () => { throw error; };
    await expect(runWithTask(task, () => instrumentObjectStorage(client, tracker, scope("aws_s3")).send(command))).rejects.toBe(error);
  });
  it("retains the complete request interval across a billing boundary", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-30T23:59:59Z"));
    const { tracker, task } = setup(), { client, command, result } = native(data.cases.find((c: any) => c.id === "aws_get"));
    const send = client.send.bind(client);
    client.send = async value => { const response = await send(value); vi.setSystemTime(new Date("2026-10-01T00:00:01Z")); return response; };
    await runWithTask(task, () => instrumentObjectStorage(client, tracker, scope("aws_s3")).send(command));
    const raw = tracker.buffer.getProviderJob("aws_s3", "object_storage", `${data.aws_payer}/${data.aws_owner}.us-east-1.${data.bucket}/aws_get`)!;
    expect(providerJobFromDict(raw).toAttributionObservation().usage_period).toEqual({ start_at: "2026-09-30T23:59:59.000000Z", end_at: "2026-10-01T00:00:01.000000Z" });
  });
  it("requires explicit AWS ownership mapping", () => {
    const { tracker } = setup();
    for (const change of [{ ownerPays: false }, { bucketOwnerAccountId: undefined }, { billingAccountId: "invalid" }, { bucket: "bucket--x-s3" }]) expect(() => instrumentObjectStorage({}, tracker, { ...scope("aws_s3"), ...change })).toThrow();
  });
  it("does not share actual route evidence between parallel accounts with the same bucket/request ID", async () => {
    const { tracker, task } = setup(), accounts = [data.r2_account, "b".repeat(32)];
    await runWithTask(task, () => Promise.all(accounts.map(account => {
      const c = { id: "same-request", provider: "r2_cloudflare", operation: "get_object", response: { StorageClass: "STANDARD" }, endpoint: `https://${account}.r2.cloudflarestorage.com` };
      const { client, command } = native(c);
      return instrumentObjectStorage(client, tracker, { ...scope("r2_cloudflare"), billingAccountId: account }).send(command);
    })));
    for (const account of accounts) expect(providerJobFromDict(tracker.buffer.getProviderJob("r2_cloudflare", "object_storage", `${account}/r2/same-request`)!).resourceId).toBe(`${account}/r2`);
  });
});
