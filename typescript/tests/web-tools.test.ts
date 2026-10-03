import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it } from "vitest";
import { bindApifyRun, bindFirecrawlJob, instrumentApify, instrumentFirecrawl, recordApifyRun, recordFirecrawlJob, recordFirecrawlSearch, uninstrumentApify, uninstrumentFirecrawl } from "../src/instruments/web-tools.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { providerJobFromDict } from "../src/core/provider-jobs.js";
import { EventBuffer } from "../src/transport/buffer.js";
import type { CostTracker } from "../src/core/tracker.js";
const data = JSON.parse(readFileSync(new URL("../../fixtures/web_tool_conformance.json", import.meta.url), "utf8"));
const buffers: EventBuffer[] = [];
afterEach(() => buffers.splice(0).forEach(b => b.close()));
function setup() {
  const buffer = new EventBuffer(":memory:"); buffers.push(buffer);
  const task = createTask({ taskId: data.task_id, taskType: "web" }); buffer.upsertTask(task);
  const tracker = { buffer } as unknown as CostTracker;
  return { tracker, task, job: (provider: string, service: string, id: string) => providerJobFromDict(buffer.getProviderJob(provider, service, id)!) };
}
describe("paired web tool evidence", () => {
  it("uses full credit snapshots, replay, zero and restore without money", () => {
    const { tracker, task, job } = setup();
    const binding = { billingAccountId: "account", resourceId: "resource", jobId: "job", startedAt: new Date(data.started_at), operation: "crawl" as const };
    runWithTask(task, () => expect(bindFirecrawlJob(tracker, binding)).toBe(true));
    for (const [revision, creditsUsed] of [[2, 12], [2, 12], [3, 0], [4, 8]]) expect(recordFirecrawlJob(tracker, { ...data.firecrawl, creditsUsed }, { billingAccountId: "account", jobId: "job", revision })).toBe(true);
    const result = job("firecrawl", "web", "account/job");
    expect(result.taskId).toBe(task.taskId); expect(result.revision).toBe(4);
    expect(result.usage[0].quantity.toString()).toBe("8");
    expect(JSON.stringify(result.toAttributionObservation())).not.toContain("not-retained");
    expect(result.toAttributionObservation()).not.toHaveProperty("cost_evidence");
    expect(() => runWithTask(task, () => bindFirecrawlJob(tracker, { ...binding, resourceId: "other" }))).toThrow();
  });
  it.each([undefined, true, -1, 1.5, "2", 9007199254740992])("rejects invalid meter %s", value => {
    const { tracker, task } = setup();
    expect(() => runWithTask(task, () => recordFirecrawlSearch(tracker, { success: true, id: "search", creditsUsed: value }, { billingAccountId: "account", resourceId: "resource" }))).toThrow();
    expect(tracker.buffer.getProviderJob("firecrawl", "web", "account/search")).toBeUndefined();
  });
  it("captures Apify identity only and ignores running cost", () => {
    const { tracker, task, job } = setup();
    expect(recordApifyRun(tracker, data.apify)).toBe(false);
    runWithTask(task, () => bindApifyRun(tracker, data.apify));
    expect(recordApifyRun(tracker, { ...data.apify, status: "RUNNING" })).toBe(false);
    expect(recordApifyRun(tracker, data.apify)).toBe(true);
    expect(recordApifyRun(tracker, data.apify)).toBe(true);
    expect(job("apify", "actor_runs", "run_one").toAttributionObservation()).not.toHaveProperty("cost_evidence");
  });
  it("preserves private receivers and native responses with no extra Apify calls", async () => {
    const { tracker, task, job } = setup(); const calls: string[] = [];
    class Actor { #value = data.apify; async start() { calls.push("start"); return { ...this.#value, status: "RUNNING" }; } }
    class Run { #value = data.apify; async get() { calls.push("get"); return this.#value; } }
    const native = { actor: (_: string) => new Actor(), run: (_: string) => new Run() };
    const client = instrumentApify(native, tracker, { billingAccountId: "account" });
    await runWithTask(task, () => client.actor("actor_one").start());
    expect(await client.run("run_one").get()).toBe(data.apify);
    await client.run("run_one").get();
    expect(calls).toEqual(["start", "get", "get"]);
    expect(job("apify", "actor_runs", "run_one").taskId).toBe(task.taskId);
    expect(job("apify", "actor_runs", "run_one").revision).toBe(2);
    uninstrumentApify(client);
    expect(client.actor("actor_one")).toBeInstanceOf(Actor);
  });
  it("captures Firecrawl scrape metadata; missing search meters stay unknown", async () => {
    const { tracker, task, job } = setup();
    const response = { metadata: { scrapeId: "scrape", creditsUsed: 4 }, markdown: "secret" };
    const client = instrumentFirecrawl({ scrape: async (_: string) => response, search: async (_: string) => ({ web: [] }) }, tracker, { billingAccountId: "account", resourceId: "resource" });
    await runWithTask(task, async () => {
      expect(await client.scrape("private-url")).toBe(response);
      expect(await client.search("private-query")).toEqual({ web: [] });
    });
    expect(job("firecrawl", "web", "account/scrape").operation).toBe("firecrawl.scrape");
    expect(JSON.stringify(job("firecrawl", "web", "account/scrape").toAttributionObservation())).not.toContain("secret");
    uninstrumentFirecrawl(client); response.metadata.scrapeId = "after";
    await runWithTask(task, () => client.scrape("url"));
    expect(tracker.buffer.getProviderJob("firecrawl", "web", "account/after")).toBeUndefined();
  });
  it("does not capture account mismatch and preserves provider errors", async () => {
    const { tracker, task } = setup();
    const client = instrumentApify({ actor: () => ({ call: async () => data.apify }) }, tracker, { billingAccountId: "other" });
    expect(await runWithTask(task, () => client.actor().call())).toBe(data.apify);
    expect(tracker.buffer.getProviderJob("apify", "actor_runs", "run_one")).toBeUndefined();
    const error = new Error("native");
    const firecrawl = instrumentFirecrawl({ scrape: async () => { throw error; } }, tracker, { billingAccountId: "account", resourceId: "resource" });
    await expect(firecrawl.scrape()).rejects.toBe(error);
  });
});
