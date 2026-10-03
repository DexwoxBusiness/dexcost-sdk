import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it } from "vitest";
import { bindLlamaParseJob, instrumentLlamaParse, recordLlamaParseJob, uninstrumentLlamaParse } from "../src/instruments/document-parse.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { providerJobFromDict } from "../src/core/provider-jobs.js";
import { EventBuffer } from "../src/transport/buffer.js";
import type { CostTracker } from "../src/core/tracker.js";
const data = JSON.parse(readFileSync(new URL("../../fixtures/document_parse_conformance.json", import.meta.url), "utf8"));
const scope = { billingAccountId: data.account, projectId: data.project };
const buffers: EventBuffer[] = [];
afterEach(() => buffers.splice(0).forEach(b => b.close()));
function setup() {
  const buffer = new EventBuffer(":memory:"); buffers.push(buffer);
  const task = createTask({ taskId: data.task_id, taskType: "parse" }); buffer.upsertTask(task);
  return { task, tracker: { buffer } as unknown as CostTracker,
    raw: () => buffer.getProviderJob("llamaparse", "parse", "organization_one/pjb-one"),
    job: () => providerJobFromDict(buffer.getProviderJob("llamaparse", "parse", "organization_one/pjb-one")!) };
}
describe("paired hosted LlamaParse evidence", () => {
  it("keeps complete credit snapshots, replay, zero, restoration, intervals and no contents/prices", () => {
    const { tracker, task, job } = setup(), response = structuredClone(data.response);
    runWithTask(task, () => expect(bindLlamaParseJob(tracker, response, scope)).toBe(true));
    for (const [revision, credits] of [[2, 30.5], [2, 30.5], [3, 0], [4, 15]]) {
      response.job.usage.credits = credits;
      expect(recordLlamaParseJob(tracker, response, { ...scope, revision })).toBe(true);
      expect(job().usage.map(x => x.quantity.toString())).toEqual(credits === 0 ? [] : [String(credits)]);
    }
    expect(job().taskId).toBe(task.taskId); expect(job().revision).toBe(4);
    const observation = job().toAttributionObservation();
    expect(observation.usage_period).toEqual(data.expected_period);
    expect(observation).not.toHaveProperty("cost_evidence");
    expect(JSON.stringify(observation)).not.toContain("never-retain");
    expect(() => runWithTask(task, () => bindLlamaParseJob(tracker, { job: { ...response.job, tier: "fast" } }, scope))).toThrow();
  });
  it.each([undefined, null, {}, { credits: null }])("leaves missing meter %s pending", usage => {
    const { tracker, task, job } = setup(), response = structuredClone(data.response);
    runWithTask(task, () => bindLlamaParseJob(tracker, response, scope));
    response.job.usage = usage;
    expect(recordLlamaParseJob(tracker, response, scope)).toBe(false); expect(job().revision).toBe(1);
  });
  it.each([true, -1, Infinity, NaN, 1e-13, 9007199254740992, "0x10"])("rejects invalid credits %s", credits => {
    const { tracker, task, job } = setup(), response = structuredClone(data.response);
    runWithTask(task, () => bindLlamaParseJob(tracker, response, scope)); response.job.usage.credits = credits;
    expect(() => recordLlamaParseJob(tracker, response, scope)).toThrow(); expect(job().revision).toBe(1);
  });
  it.each([1e-7, 1e-12, "0.000000000001", "30.5000000000000"])("accepts exact decimal credit domain %s", credits => {
    const { tracker, task } = setup(), response = structuredClone(data.response);
    runWithTask(task, () => bindLlamaParseJob(tracker, response, scope)); response.job.usage.credits = credits;
    expect(recordLlamaParseJob(tracker, response, scope)).toBe(true);
  });
  it.each(["PENDING", "RUNNING", "FAILED", "CANCELLED", "unknown"])("does not finalize unverified %s billing", status => {
    const { tracker, task } = setup(), response = structuredClone(data.response);
    runWithTask(task, () => bindLlamaParseJob(tracker, response, scope)); response.job.status = status;
    expect(recordLlamaParseJob(tracker, response, scope)).toBe(false);
  });
  it("preserves native receivers, APIPromise helpers, delayed credits and initial task ownership", async () => {
    const { tracker, task, job } = setup(), response = structuredClone(data.response), calls: unknown[][] = [];
    class ApiPromise extends Promise<any> {
      #response = "raw response";
      asResponse() { return this.#response; }
      withResponse() { return { response: this.#response }; }
    }
    class Parsing {
      #response = response;
      create(params: { tier: string }) { calls.push(["create", params]); const { tier: _, usage: __, ...job } = this.#response.job; return ApiPromise.resolve(job); }
      get(jobId: string, params: object) { calls.push(["get", jobId, params]); return ApiPromise.resolve(this.#response); }
    }
    const native = new Parsing(), client = instrumentLlamaParse({ parsing: native }, tracker, scope);
    await runWithTask(task, async () => {
      const pending = client.parsing.create({ tier: "agentic" });
      expect(pending).toBeInstanceOf(ApiPromise);
      expect(pending.asResponse()).toBe("raw response"); expect(pending.withResponse()).toEqual({ response: "raw response" });
      await pending;
    });
    const other = createTask({ taskType: "poller" }); tracker.buffer.upsertTask(other);
    await runWithTask(other, async () => {
      response.job.usage.credits = null;
      expect(await client.parsing.get("pjb-one", { expand: ["usage"] })).toBe(response);
      expect(job().revision).toBe(1);
      response.job.usage.credits = 30.5;
      expect(await client.parsing.get("pjb-one", { expand: ["usage"] }).catch(() => null)).toBe(response);
      expect(await client.parsing.get("pjb-one", { expand: ["usage"] }).finally(() => undefined)).toBe(response);
    });
    expect(calls).toHaveLength(4); expect(calls[1]).toEqual(["get", "pjb-one", { expand: ["usage"] }]);
    expect(job().taskId).toBe(task.taskId); expect(job().revision).toBe(2);
    uninstrumentLlamaParse(client); expect(client.parsing).toBe(native);
  });
  it("does not claim unbound reads or wrong projects; one-shot parse captures automatically", async () => {
    const { tracker, task, job, raw } = setup(), response = structuredClone(data.response);
    const client = instrumentLlamaParse({ parsing: { parse: async () => response, get: async () => response } }, tracker, scope);
    await runWithTask(task, async () => {
      expect(await client.parsing.get()).toBe(response); expect(raw()).toBeUndefined();
      response.job.project_id = "other";
      expect(await client.parsing.parse()).toBe(response); expect(raw()).toBeUndefined();
      response.job.project_id = data.project;
      expect(await client.parsing.parse()).toBe(response); expect(job().usage[0].quantity.toString()).toBe("30.5");
    });
  });
  it("preserves provider failures and requires contiguous explicit correction revisions", async () => {
    const { tracker, task } = setup(), error = new Error("native failure"), response = structuredClone(data.response);
    const client = instrumentLlamaParse({ parsing: { parse: async () => { throw error; } } }, tracker, scope);
    await expect(client.parsing.parse()).rejects.toBe(error);
    runWithTask(task, () => bindLlamaParseJob(tracker, response, scope));
    expect(() => recordLlamaParseJob(tracker, response, { ...scope, revision: 3 })).toThrow();
    expect(recordLlamaParseJob(tracker, response, scope)).toBe(true);
    response.job.usage.credits = 20;
    expect(() => recordLlamaParseJob(tracker, response, scope)).toThrow();
  });
});
