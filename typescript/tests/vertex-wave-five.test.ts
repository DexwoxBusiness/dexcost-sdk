import { afterEach, describe, expect, it } from "vitest";
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
function setup() {
  directory = mkdtempSync(join(tmpdir(), "dexcost-vertex-wave-five-"));
  buffer = new EventBuffer(join(directory, "events.db"));
}
afterEach(() => {
  uninstrumentGoogleGenAI(); provideGoogleGenAIModule(undefined);
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
  opts.httpOptions.fetch = async (url: any) => {
    requests.push(String(url));
    if (code !== 200) return new Response(JSON.stringify({ error: { code, message: "fixture failure", status: "INVALID_ARGUMENT" } }), { status: code });
    const interim = structuredClone(raw);
    delete interim.candidates[0].finishReason;
    return new Response(stream ? "data: " + JSON.stringify(interim) + "\n\ndata: " + JSON.stringify(raw) + "\n\n" : JSON.stringify(raw), {
      status: 200, headers: { "content-type": stream ? "text/event-stream" : "application/json" },
    });
  };
  const native: any = new GoogleGenAI(opts);
  // Mock only native authentication and I/O; no ADC lookup, secret or paid call.
  native.models.apiClient.clientOptions.auth.addAuthHeaders = async () => {};
  return { native, raw, body, requests };
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
});
