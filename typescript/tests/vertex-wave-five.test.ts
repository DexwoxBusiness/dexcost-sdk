import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { GoogleGenAI } from "@google/genai";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { instrumentGoogleGenAI, provideGoogleGenAIModule, uninstrumentGoogleGenAI } from "../src/instruments/google-genai.js";

const fixture = JSON.parse(readFileSync(new URL("../../tests/fixtures/vertex-wave-five.json", import.meta.url), "utf8"));
let directory: string;
let buffer: EventBuffer;
const transports: { project: string; send(url: string, init?: RequestInit): Promise<Response> }[] = [];
function setup() {
  directory = mkdtempSync(join(tmpdir(), "dexcost-vertex-wave-five-"));
  buffer = new EventBuffer(join(directory, "events.db"));
}
afterEach(() => {
  uninstrumentGoogleGenAI(); provideGoogleGenAIModule(undefined);
  vi.unstubAllGlobals(); transports.length = 0;
  buffer?.close(); if (directory) rmSync(directory, { recursive: true, force: true });
});
function lane(event: any) {
  return event?.usage.flatMap((l: any) => l.dimensions).find((d: any) => d.key === "vertex_pricing_lane")?.value.value;
}
function assertVector(event: any, c: any) {
  expect(event.provider).toMatchObject({ name: "google", service: "vertex_ai", region: "global", record_id: c.id });
  expect(event.resource).toEqual({ type: "model", id: c.model });
  expect(Object.fromEntries(event.usage.map((l: any) => [l.metric, l.quantity]))).toEqual(c.expected_usage);
  expect(lane(event)).toBe(c.lane);
  for (const line of event.usage) expect(line.dimensions).toContainEqual({ key: "cloud_project", value: { type: "string", value: c.project } });
}
function client(c: any, stream = false, edit: (raw: any, body: any, opts: any) => void = () => {}, code = 200) {
  const raw = structuredClone(c.response);
  const body: any = { model: c.model, contents: "private fixture prompt" };
  const opts: any = { vertexai: true, project: c.project, location: "global", httpOptions: { apiVersion: "v1" } };
  edit(raw, body, opts);
  const requests: string[] = [];
  const network = { send: async (url: string, _init?: RequestInit) => {
    requests.push(String(url));
    if (code !== 200) return new Response(JSON.stringify({ error: { code, message: "fixture failure", status: "INVALID_ARGUMENT" } }), { status: code });
    const interim = structuredClone(raw);
    delete interim.candidates[0].finishReason;
    const response = new Response(stream ? "data: " + JSON.stringify(interim) + "\n\ndata: " + JSON.stringify(raw) + "\n\n" : JSON.stringify(raw), {
      status: 200, headers: { "content-type": stream ? "text/event-stream" : "application/json" },
    });
    // Mock the network boundary, not GenAI's custom fetch option. Real fetch
    // supplies this final URL; a bare/synthetic Response intentionally does not.
    Object.defineProperty(response, "url", { value: String(url), configurable: true });
    return response;
  } };
  transports.push({ project: c.project, send: (url, init) => network.send(url, init) });
  vi.stubGlobal("fetch", async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" || input instanceof URL ? String(input) : input.url;
    const transport = transports.find(item => url.includes(`/projects/${item.project}/`)) ?? transports.at(-1)!;
    return transport.send(url, init);
  });
  const native: any = new GoogleGenAI(opts);
  // Mock only native authentication and I/O; no ADC lookup, secret or paid call.
  native.models.apiClient.clientOptions.auth.addAuthHeaders = async () => {};
  return { native, raw, body, requests, network };
}
async function install(native: any) {
  provideGoogleGenAIModule(native);
  await instrumentGoogleGenAI(new PricingEngine(), buffer);
}
async function invoke(c: any, stream = false, edit?: (raw: any, body: any, opts: any) => void) {
  setup();
  const state = client(c, stream, edit);
  await install(state.native);
  const before = structuredClone(state.body);
  if (stream) {
    const chunks = [];
    for await (const chunk of await state.native.models.generateContentStream(state.body)) chunks.push(chunk);
    expect(chunks).toHaveLength(2);
    expect(chunks[0].responseId).toBe(state.raw.responseId);
  } else {
    const result = await state.native.models.generateContent(state.body);
    expect(result.responseId).toBe(state.raw.responseId);
  }
  expect(state.body).toEqual(before);
  expect(state.requests).toHaveLength(1);
  expect(buffer.getAllEvents()).toHaveLength(1);
  expect(JSON.stringify(buffer.getAllEvents())).not.toContain("private fixture prompt");
  return { ...state, event: toAttributionObservationV3(buffer.getAllEvents()[0]) };
}
describe("native Vertex global standard PAYG", () => {
  for (const stream of [false, true]) it.each(fixture.cases)("captures $id / stream=" + stream, async c => {
    const { event, requests } = await invoke(c, stream);
    assertVector(event, c);
    expect(requests[0]).toContain("https://aiplatform.googleapis.com/v1/projects/" + c.project + "/locations/global/publishers/google/models/" + c.model);
  });
  const negatives: [string, (raw: any, body: any, opts: any) => void][] = [
    ["missing traffic", r => { delete r.usageMetadata.trafficType; }],
    ...["PROVISIONED_THROUGHPUT", "ON_DEMAND_PRIORITY", "ON_DEMAND_FLEX", "TRAFFIC_TYPE_UNSPECIFIED"].map(t => [t, (r: any) => { r.usageMetadata.trafficType = t; }] as [string, (r: any) => void]),
    ...["promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount"].map(k => ["missing " + k, (r: any) => { delete r.usageMetadata[k]; }] as [string, (r: any) => void]),
    ["missing response model", r => { delete r.modelVersion; }],
    ["missing response id", r => { delete r.responseId; }],
    ["alias", r => { r.modelVersion += "-preview"; }],
    ["wrong total", r => { r.usageMetadata.totalTokenCount++; }],
    ["overlapping cache", r => { r.usageMetadata.cachedContentTokenCount = 1001; }],
    ["boolean count", r => { r.usageMetadata.cachedContentTokenCount = false; }],
    ["missing modality", r => { delete r.usageMetadata.promptTokensDetails; }],
    ["audio modality", r => { r.usageMetadata.promptTokensDetails[0].modality = "AUDIO"; }],
    ["contradictory modality", r => { r.usageMetadata.promptTokensDetails[0].tokenCount++; }],
    ["unfinished", r => { delete r.candidates[0].finishReason; }],
    ["grounded", r => { r.candidates[0].groundingMetadata = {}; }],
    ["output media", r => { r.candidates[0].content.parts = [{ inlineData: { mimeType: "image/png", data: "AA==" } }]; }],
    ["tool usage", r => { r.usageMetadata.toolUsePromptTokenCount = 1; }],
    ["regional", (_r, _b, o) => { o.location = "us-central1"; }],
    ["gateway", (_r, _b, o) => { o.httpOptions.baseUrl = "https://gateway.example"; }],
    ["custom headers", (_r, _b, o) => { o.httpOptions.headers = { Authorization: "fixture" }; }],
    ["extra body", (_r, _b, o) => { o.httpOptions.extraBody = {}; }],
    ["per-call headers", (_r, b) => { b.config = { httpOptions: { headers: { Authorization: "fixture" } } }; }],
    ["explicit cache", (_r, b) => { b.config = { cachedContent: "projects/fixture-project/locations/global/cachedContents/fixture" }; }],
    ["tools", (_r, b) => { b.config = { tools: [{ googleSearch: {} }] }; }],
    ["input media", (_r, b) => { b.contents = [{ role: "user", parts: [{ text: "safe" }, { inlineData: { mimeType: "image/png", data: "AA==" } }] }]; }],
    ["cross project model", (_r, b) => { b.model = "projects/other-project/locations/global/publishers/google/models/" + b.model; }],
  ];
  for (const stream of [false, true]) it.each(negatives)("fails open for %s / stream=" + stream, async (_name, edit) => {
    const { event } = await invoke(fixture.cases[0], stream, edit);
    expect(lane(event)).toBeUndefined();
  });
  it("preserves native errors without a priced event", async () => {
    setup(); const s = client(fixture.cases[0], false, undefined, 400); await install(s.native);
    await expect(s.native.models.generateContent(s.body)).rejects.toThrow("fixture failure");
    expect(buffer.getAllEvents().every(e => lane(toAttributionObservationV3(e)) === undefined)).toBe(true);
  });
  it("early stream return is cancelled and cannot be billed as success", async () => {
    setup(); const s = client(fixture.cases[0], true); await install(s.native);
    const stream = await s.native.models.generateContentStream(s.body);
    for await (const _chunk of stream) break;
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(toAttributionObservationV3(buffer.getAllEvents()[0])?.operation?.status).toBe("cancelled");
  });
  it("two actual clients keep project and regional eligibility isolated", async () => {
    setup();
    const a = client(fixture.cases[0]);
    const b = client({ ...fixture.cases[0], project: "second-project" }, false, (_r, _b, o) => { o.location = "us-central1"; });
    await install({ a: a.native, b: b.native });
    await Promise.all([a.native.models.generateContent(a.body), b.native.models.generateContent(b.body)]);
    const events = buffer.getAllEvents().map(e => toAttributionObservationV3(e));
    expect(events.filter(e => lane(e) !== undefined)).toHaveLength(1);
    assertVector(events.find(e => lane(e) !== undefined), fixture.cases[0]);
  });
  it("snapshots an unknown route before awaiting native I/O", async () => {
    setup();
    const s = client(fixture.cases[0], false, (_r, _b, o) => { o.location = "us-central1"; });
    let release!: () => void;
    let started!: () => void;
    const waiting = new Promise<void>(resolve => { release = resolve; });
    const entered = new Promise<void>(resolve => { started = resolve; });
    s.native.models.apiClient.clientOptions.httpOptions.fetch = async () => {
      started(); await waiting;
      return new Response(JSON.stringify(s.raw), { headers: { "content-type": "application/json" } });
    };
    await install(s.native);
    const pending = s.native.models.generateContent(s.body);
    await entered;
    s.native.models.apiClient.clientOptions.location = "global";
    s.native.models.apiClient.clientOptions.httpOptions.baseUrl = "https://aiplatform.googleapis.com/";
    release(); await pending;
    expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
  });

  for (const stream of [false, true]) {
    const finalRoutes: [string, (url: string) => string, boolean][] = [
      ["gateway response", () => "https://gateway.example/result", false],
      ["regional response", url => url.replace("aiplatform.googleapis.com", "us-central1-aiplatform.googleapis.com"), false],
      ["another project", url => url.replace(/\/projects\/[^/]+\//, "/projects/other-project/"), false],
      ["another model", url => url.replace("gemini-3.5-flash-lite", "gemini-3.8-flash"), false],
      ["missing final URL", () => "", false],
      ["extra query", url => url + (url.includes("?") ? "&unknown=1" : "?unknown=1"), false],
      ["redirect back to original URL", url => url, true],
    ];
    it.each(finalRoutes)("does not admit %s / stream=" + stream, async (_name, finalUrl, redirected) => {
      setup(); const s = client(fixture.cases[0], stream);
      const original = s.network.send;
      s.network.send = async (url, init) => {
        const response = await original(url, init);
        Object.defineProperties(response, {
          url: { value: finalUrl(url), configurable: true }, redirected: { value: redirected },
        });
        return response;
      };
      await install(s.native);
      if (stream) {
        const result = [];
        for await (const chunk of await s.native.models.generateContentStream(s.body)) result.push(chunk);
        expect(result).toHaveLength(2);
      } else expect((await s.native.models.generateContent(s.body)).responseId).toBe(s.raw.responseId);
      expect(s.requests).toHaveLength(1);
      expect(buffer.getAllEvents()).toHaveLength(1);
      expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
    });

    it("rejects an actual request on another route despite the configured base / stream=" + stream, async () => {
      setup(); const s = client(fixture.cases[0], stream);
      const apiClient = s.native.models.apiClient;
      const constructUrl = apiClient.constructUrl;
      apiClient.constructUrl = function (...args: any[]) {
        const url = constructUrl.apply(this, args);
        url.hostname = "gateway.example";
        return url;
      };
      await install(s.native);
      if (stream) for await (const _ of await s.native.models.generateContentStream(s.body)) { /* Consume native result. */ }
      else await s.native.models.generateContent(s.body);
      expect(s.requests[0]).toContain("https://gateway.example/");
      expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
    });

    it("rejects a custom fetch even when it forges matching final route metadata / stream=" + stream, async () => {
      setup(); const s = client(fixture.cases[0], stream);
      const custom = vi.fn((url: string, init?: RequestInit) => s.network.send(url, init));
      s.native.models.apiClient.clientOptions.httpOptions.fetch = custom;
      await install(s.native);
      if (stream) for await (const _ of await s.native.models.generateContentStream(s.body)) { /* Preserve native stream. */ }
      else await s.native.models.generateContent(s.body);
      expect(custom).toHaveBeenCalledOnce();
      expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
    });

    for (const recovery of [false, true]) it("rejects synthetic output / recovery=" + recovery + " / stream=" + stream, async () => {
      setup(); const s = client(fixture.cases[0], stream);
      const name = stream ? "generateContentStream" : "generateContent";
      const nativeMethod = s.native.models[name];
      const recovered = async () => {
        if (recovery) {
          try {
            const output = await nativeMethod(s.body);
            if (stream) for await (const _ of output) { /* Exercise the real transport failure. */ }
          } catch { /* Caller recovery must not manufacture monetary evidence. */ }
        }
        return stream ? (async function* () { yield s.raw; })() : s.raw;
      };
      s.network.send = async () => { throw new Error("network unavailable"); };
      s.native.models[name] = recovered;
      await install(s.native);
      if (stream) {
        const result = [];
        for await (const chunk of await s.native.models[name](s.body)) result.push(chunk);
        expect(result).toEqual([s.raw]);
      } else expect(await s.native.models[name](s.body)).toBe(s.raw);
      expect(buffer.getAllEvents()).toHaveLength(1);
      expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
    });

    it("rejects a replaced apiCall before instrumentation / stream=" + stream, async () => {
      setup(); const s = client(fixture.cases[0], stream);
      s.native.models.apiClient.apiCall = (url: string, init: RequestInit) => s.network.send(url, init);
      await install(s.native);
      if (stream) for await (const _ of await s.native.models.generateContentStream(s.body)) { /* Preserve caller transport. */ }
      else await s.native.models.generateContent(s.body);
      expect(s.requests).toHaveLength(1);
      expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
    });

    it("preserves configured retry behavior but leaves its usage unpriced / stream=" + stream, async () => {
      setup(); const s = client(fixture.cases[0], stream);
      const retryOptions = { attempts: 2, initialDelay: 0, jitter: 0 };
      s.native.models.apiClient.clientOptions.httpOptions.retryOptions = retryOptions;
      const send = s.network.send;
      let attempts = 0;
      s.network.send = async (url, init) => {
        attempts++;
        if (attempts === 1) return new Response(JSON.stringify({ error: { code: 503, message: "temporary", status: "UNAVAILABLE" } }), { status: 503 });
        return send(url, init);
      };
      await install(s.native);
      if (stream) for await (const _ of await s.native.models.generateContentStream(s.body)) { /* Keep native retries. */ }
      else await s.native.models.generateContent(s.body);
      expect(attempts).toBe(2);
      expect(s.native.models.apiClient.clientOptions.httpOptions.retryOptions).toBe(retryOptions);
      expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
    });

    it("requires the observed native request to use POST / stream=" + stream, async () => {
      setup(); const s = client(fixture.cases[0], stream);
      const apiClient = s.native.models.apiClient;
      const method = stream ? "streamApiCall" : "unaryApiCall";
      const native = apiClient[method];
      apiClient[method] = function (...args: any[]) { args[2] = "GET"; return native.apply(this, args); };
      await install(s.native);
      if (stream) for await (const _ of await s.native.models.generateContentStream(s.body)) { /* Keep provider output. */ }
      else await s.native.models.generateContent(s.body);
      expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
    });

    it("does not admit a non-200 successful HTTP status / stream=" + stream, async () => {
      setup(); const s = client(fixture.cases[0], stream);
      const send = s.network.send;
      s.network.send = async (url, init) => {
        const response = await send(url, init);
        Object.defineProperty(response, "status", { value: 201 });
        return response;
      };
      await install(s.native);
      if (stream) for await (const _ of await s.native.models.generateContentStream(s.body)) { /* Preserve body. */ }
      else await s.native.models.generateContent(s.body);
      expect(lane(toAttributionObservationV3(buffer.getAllEvents()[0]))).toBeUndefined();
    });
  }

  it("keeps lazy stream and unary route evidence isolated under concurrent clients", async () => {
    setup(); const a = client(fixture.cases[0], true);
    const b = client({ ...fixture.cases[0], project: "second-project" });
    const send = b.network.send;
    b.network.send = async (url, init) => {
      const response = await send(url, init);
      Object.defineProperty(response, "url", { value: "https://gateway.example/" });
      return response;
    };
    await install({ a: a.native, b: b.native });
    const lazy = await a.native.models.generateContentStream(a.body);
    await Promise.all([(async () => { for await (const _ of lazy) { /* Resume after public call context ends. */ } })(),
      b.native.models.generateContent(b.body)]);
    const events = buffer.getAllEvents().map(e => toAttributionObservationV3(e));
    expect(events).toHaveLength(2);
    expect(events.filter(e => lane(e) !== undefined)).toHaveLength(1);
    assertVector(events.find(e => lane(e) !== undefined), fixture.cases[0]);
  });

  it("restores the native transport and supports instrumenting the same client again", async () => {
    setup(); const s = client(fixture.cases[0]); const apiClient = s.native.models.apiClient;
    const original = apiClient.apiCall;
    await install(s.native); await s.native.models.generateContent(s.body);
    uninstrumentGoogleGenAI();
    expect(apiClient.apiCall).toBe(original);
    expect(Object.hasOwn(apiClient, "apiCall")).toBe(false);
    await install(s.native); await s.native.models.generateContent(s.body);
    expect(buffer.getAllEvents()).toHaveLength(2);
    for (const event of buffer.getAllEvents()) expect(lane(toAttributionObservationV3(event))).toBe(fixture.cases[0].lane);
  });
});
