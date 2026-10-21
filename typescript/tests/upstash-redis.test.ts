import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { instrumentUpstashRedis, uninstrumentUpstashRedis, upstashRedisResourceId } from "../src/instruments/upstash-redis.js";
import { currentProviderCaptureOwner, runWithProviderCapture } from "../src/instruments/provider-capture.js";
import { createDexcostFetch, clearRecordedEvents, getRecordedEvents, untrackHttp } from "../src/adapters/http.js";
import { runWithTask } from "../src/core/context.js";
import { createTask, type CostEvent } from "../src/core/models.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import type { CostTracker } from "../src/core/tracker.js";
const require = createRequire(import.meta.url);
const { Redis } = require("@upstash/redis");
const host = "agent-memory.upstash.io";
const binding = { billingAccountId: "account-a", region: "us-east-1", databaseId: "database-a", endpointHost: host, billingPlan: "pay_as_you_go", topology: "single_region" } as const;
const cleanups: object[] = [];
afterEach(() => { cleanups.splice(0).forEach(uninstrumentUpstashRedis); vi.unstubAllGlobals(); untrackHttp(); clearRecordedEvents(); });
function setup(response: unknown = { result: null }, options: Record<string, unknown> = {}, status = 200) {
  const events: CostEvent[] = [], calls: unknown[] = [];
  const tracker = { buffer: { addEvent: (event: CostEvent) => events.push(event) }, pricing: new PricingEngine() } as unknown as CostTracker;
  const task = createTask({ taskId: randomUUID(), taskType: "memory" });
  vi.stubGlobal("fetch", createDexcostFetch({ tracker, fetch: async (_url, init) => {
    calls.push({ owner: currentProviderCaptureOwner(), body: JSON.parse(String(init?.body)) });
    const result = new Response(JSON.stringify(response), { status, headers: { "content-type": "application/json" } });
    Object.defineProperty(result, "url", { value: String(_url) });
    return result;
  } }));
  const client = new Redis({ url: `https://${host}`, token: "PRIVATE-TOKEN", retry: { retries: 0 }, enableAutoPipelining: false, responseEncoding: "utf-8", enableTelemetry: false, ...options });
  const tracked = instrumentUpstashRedis(client, tracker, binding); cleanups.push(tracked);
  return { client, tracked, tracker, task, events, calls };
}

describe("real native Upstash single-command evidence", () => {
  it.each([
    ["get", ["PRIVATE"], null], ["set", ["PRIVATE", "PRIVATE-VALUE"], "OK"], ["mget", ["PRIVATE1", "PRIVATE2"], ["one", null]],
    ["del", ["PRIVATE1", "PRIVATE2"], 2], ["exists", ["PRIVATE"], 1], ["incr", ["PRIVATE"], 2],
  ])("counts native %s once, not keys, without HTTP duplication", async (method, args, result) => {
    const { tracked, task, events, calls } = setup({ result });
    await runWithTask(task, async () => expect(await tracked[String(method)](...args as unknown[])).toEqual(result));
    expect(calls).toHaveLength(1); expect(calls[0]).toMatchObject({ owner: "upstash_redis" });
    expect(getRecordedEvents()).toHaveLength(0); expect(events).toHaveLength(1);
    const wire = toAttributionObservationV3(events[0])!;
    expect(wire.provider).toEqual({ name: "upstash_redis", service: "redis", region: "us-east-1" });
    expect(wire.resource?.id).toBe("account-a/fce4ffd12fd1d7649a4d7c2e40a7a841a994ad08fb872eb172d549061cd417f6");
    expect(wire.usage[0]).toMatchObject({ metric: "upstash_redis.payg_single_region_commands", quantity: "1", unit: "Commands" });
    expect(JSON.stringify(wire)).not.toContain("PRIVATE"); expect(wire).not.toHaveProperty("reported_cost");
  });
  it.each(["retry", "retry-false", "auto-pipeline", "route", "signal", "disabled", "failed", "missing-result", "status", "outer-capture", "no-task"])("does not invent commands from %s", async reason => {
    const response = reason === "failed" ? { error: "PRIVATE failure" } : reason === "missing-result" ? {} : { result: "OK" };
    const options = reason === "retry" ? { retry: { retries: 1 } } : reason === "retry-false" ? { retry: false } : reason === "auto-pipeline" ? { enableAutoPipelining: true } : reason === "route" ? { url: "https://other.upstash.io" } : reason === "signal" ? { signal: new AbortController().signal } : {};
    const { tracked, task, events, calls } = setup(response, options, reason === "status" ? 401 : 200);
    if (reason === "disabled") uninstrumentUpstashRedis(tracked);
    const invoke = () => tracked.get("PRIVATE").catch(() => "caller fallback");
    if (reason === "no-task") await invoke();
    else await runWithTask(task, () => reason === "outer-capture" ? runWithProviderCapture("outer", invoke) : invoke());
    expect(calls).toHaveLength(1); expect(events.filter(event => event.details.redis_capture_basis)).toHaveLength(0);
  });
  it("does not observe pipeline, transaction, scripts or operational commands as one command", async () => {
    const { tracked, task, events } = setup({ result: "PONG" });
    await runWithTask(task, async () => expect(await tracked.ping()).toBe("PONG"));
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify([{ result: "OK" }, { result: "value" }]), { status: 200 }));
    await runWithTask(task, async () => {
      expect(await tracked.pipeline().set("PRIVATE", "VALUE").get("PRIVATE").exec()).toEqual(["OK", "value"]);
      expect(await tracked.multi().set("PRIVATE", "VALUE").get("PRIVATE").exec()).toEqual(["OK", "value"]);
    });
    expect(events.filter(event => event.details.redis_capture_basis)).toHaveLength(0);
  });
  it("preserves concurrent task ownership and restores the transport when disabled", async () => {
    const { client, tracked, task, events, calls } = setup({ result: "OK" });
    const second = createTask({ taskId: randomUUID(), taskType: "memory" });
    await Promise.all([runWithTask(task, () => tracked.get("PRIVATE1")), runWithTask(second, () => tracked.get("PRIVATE2"))]);
    expect(events.map(event => event.taskId).sort()).toEqual([task.taskId, second.taskId].sort());
    expect(() => instrumentUpstashRedis(client, {} as CostTracker, binding)).toThrow("already instrumented");
    uninstrumentUpstashRedis(tracked); uninstrumentUpstashRedis(tracked);
    expect(client.client.request).toBe(Object.getPrototypeOf(client.client).request);
    await runWithTask(task, async () => expect(await tracked.get("PRIVATE3")).toBe("OK"));
    expect(calls).toHaveLength(3); expect(events.filter(event => event.details.redis_capture_basis)).toHaveLength(2);
  });
  it("does not trust a custom requester or fabricated middleware result", async () => {
    const { tracker, task, events } = setup();
    const native = new Redis({ request: async () => ({ result: "invented" }) });
    const custom = instrumentUpstashRedis(native, tracker, binding); cleanups.push(custom);
    await runWithTask(task, async () => expect(await custom.get("PRIVATE")).toBe("invented"));
    expect(events).toHaveLength(0);
  });
  it.each(["redirect", "missing-url", "wrong-host", "middleware-recovery"])("requires original actual-response route evidence for %s", async reason => {
    const events: CostEvent[] = [], task = createTask({ taskId: randomUUID(), taskType: "memory" });
    const tracker = { buffer: { addEvent: (event: CostEvent) => events.push(event) } } as unknown as CostTracker;
    const transport = vi.fn(async () => {
      const result = new Response(JSON.stringify({ result: "value" }), { status: 200 });
      Object.defineProperty(result, "url", { value: reason === "missing-url" ? "" : reason === "wrong-host" ? "https://other.upstash.io/" : `https://${host}/` });
      Object.defineProperty(result, "redirected", { value: reason === "redirect" });
      return result;
    });
    vi.stubGlobal("fetch", createDexcostFetch({ tracker: { ...tracker, pricing: new PricingEngine() } as CostTracker, fetch: transport }));
    const client = new Redis({ url: `https://${host}`, token: "PRIVATE", retry: { retries: 0 }, enableAutoPipelining: false, responseEncoding: "utf-8" });
    const tracked = instrumentUpstashRedis(client, tracker, binding); cleanups.push(tracked);
    if (reason === "middleware-recovery") client.use(async () => ({ result: "value" }));
    await runWithTask(task, async () => expect(await tracked.get("PRIVATE")).toBe("value"));
    expect(events).toHaveLength(0);
    expect(transport).toHaveBeenCalledTimes(reason === "middleware-recovery" ? 0 : 1);
  });
  it.each([{ billingPlan: "free" }, { billingPlan: "fixed" }, { topology: "global" }, { endpointHost: "agent-memory.upstash.io.attacker.example" }, { region: "" }])("rejects unsupported binding %j", changes => {
    expect(() => instrumentUpstashRedis({}, {} as CostTracker, { ...binding, ...changes } as any)).toThrow();
  });
  it("isolates account, database, region and endpoint", () => {
    const original = upstashRedisResourceId("account-a", "us-east-1", "database-a", host);
    const values = [["account-b", "us-east-1", "database-a", host], ["account-a", "eu-west-1", "database-a", host], ["account-a", "us-east-1", "database-b", host], ["account-a", "us-east-1", "database-a", "other.upstash.io"]];
    for (const [account, region, database, endpoint] of values) expect(upstashRedisResourceId(account, region, database, endpoint)).not.toBe(original);
  });
  it.each([false, true])("preserves HTTP route metadata and clone bodies (redirected=%s)", async redirected => {
    const { tracker, task } = setup();
    const raw = new Response("native body", { status: 200 });
    Object.defineProperties(raw, { url: { value: `https://${host}/` }, redirected: { value: redirected }, type: { value: "basic" } });
    const instrumented = createDexcostFetch({ tracker, fetch: async () => raw });
    const response = await runWithTask(task, () => runWithProviderCapture("upstash_redis", () => instrumented(`https://${host}/`)));
    const clone = response.clone(), nested = clone.clone();
    for (const copy of [response, clone, nested]) {
      expect(copy.url).toBe(`https://${host}/`); expect(copy.redirected).toBe(redirected); expect(copy.type).toBe("basic");
    }
    expect(await Promise.all([response.text(), clone.text(), nested.text()])).toEqual(["native body", "native body", "native body"]);
  });
});
