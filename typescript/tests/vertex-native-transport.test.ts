import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { channel } from "node:diagnostics_channel";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { GoogleGenAI } from "@google/genai";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { instrumentGoogleGenAI, provideGoogleGenAIModule, uninstrumentGoogleGenAI } from "../src/instruments/google-genai.js";
import { vertexNativeFetchFixture } from "./helpers/vertex-native-fetch.js";

const example = JSON.parse(readFileSync(new URL("../../tests/fixtures/vertex-wave-five.json", import.meta.url), "utf8")).cases[0];
let network: Awaited<ReturnType<typeof vertexNativeFetchFixture>>;
let directory: string;
let buffer: EventBuffer;
const buffers: EventBuffer[] = [];
beforeAll(async () => { network = await vertexNativeFetchFixture(); });
afterAll(async () => { await network.close(); });
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "dexcost-vertex-native-proof-"));
  buffer = nextBuffer();
});
afterEach(() => {
  uninstrumentGoogleGenAI(); provideGoogleGenAIModule(undefined);
  vi.unstubAllGlobals(); vi.restoreAllMocks();
  for (const item of buffers.splice(0)) item.close();
  rmSync(directory, { recursive: true, force: true });
});
function nextBuffer() {
  const result = new EventBuffer(join(directory, `events-${buffers.length}.db`));
  buffers.push(result); return result;
}
function lanes(target = buffer) {
  return target.getAllEvents().map(event => toAttributionObservationV3(event)?.usage
    .flatMap(line => line.dimensions).find(dimension => dimension.key === "vertex_pricing_lane")?.value);
}
function response(url: string, stream: boolean, status = 200) {
  const terminal = structuredClone(example.response);
  const interim = structuredClone(terminal);
  delete interim.candidates[0].finishReason;
  const result = new Response(stream ? `data: ${JSON.stringify(interim)}\n\ndata: ${JSON.stringify(terminal)}\n\n` : JSON.stringify(terminal), {
    status, headers: { "content-type": stream ? "text/event-stream" : "application/json" },
  });
  Object.defineProperty(result, "url", { value: url });
  return result;
}
function client(stream = false) {
  const native: any = new GoogleGenAI({ vertexai: true, project: example.project, location: "global", httpOptions: { apiVersion: "v1" } });
  native.models.apiClient.clientOptions.auth.addAuthHeaders = async () => {};
  const body = { model: example.model, contents: "private native fixture prompt" };
  const send = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" || input instanceof URL ? String(input) : input.url;
    return network.fetch(input, init, async () => response(url, stream));
  });
  vi.stubGlobal("fetch", send);
  return { native, body, send, name: stream ? "generateContentStream" : "generateContent" };
}
async function install(native: any, target = buffer) {
  provideGoogleGenAIModule(native);
  await instrumentGoogleGenAI(new PricingEngine(), target);
}
async function consume(state: ReturnType<typeof client>, method = state.native.models[state.name]) {
  const result = await method.call(state.native.models, state.body);
  if (state.name.endsWith("Stream")) {
    const chunks = [];
    for await (const chunk of result) chunks.push(chunk);
    expect(chunks.at(-1).responseId).toBe(example.response.responseId);
  } else expect(result.responseId).toBe(example.response.responseId);
}

describe("Vertex monetary admission requires fresh native wire evidence", () => {
  for (const stream of [false, true]) {
    for (const beforeInstall of [false, true]) it(`rejects fabricated global fetch metadata / stream=${stream} / beforeInstall=${beforeInstall}`, async () => {
      const state = client(stream);
      const fake = vi.fn(async (input: string) => response(input, stream));
      if (beforeInstall) vi.stubGlobal("fetch", fake);
      await install(state.native);
      if (!beforeInstall) vi.stubGlobal("fetch", fake);
      await consume(state);
      expect(fake).toHaveBeenCalledOnce(); expect(state.send).not.toHaveBeenCalled();
      expect(lanes()).toEqual([undefined]);
    });

    it(`prices a fresh response but not a subsequent global cache hit / stream=${stream}`, async () => {
      const state = client(stream);
      let cached: Response | undefined;
      vi.stubGlobal("fetch", async (input: string, init?: RequestInit) => {
        if (cached) return cached.clone();
        const fresh = await state.send(input, init);
        cached = fresh.clone(); return fresh;
      });
      await install(state.native); await consume(state); await consume(state);
      expect(state.send).toHaveBeenCalledOnce();
      expect(lanes()[0]).toEqual({ type: "string", value: example.lane });
      expect(lanes()[1]).toBeUndefined();
    });

    for (const failure of ["status", "connection", "body"] as const) it(`does not price global recovery after native ${failure} failure / stream=${stream}`, async () => {
      const state = client(stream);
      vi.stubGlobal("fetch", async (input: string, init?: RequestInit) => {
        try {
          const failed = await network.fetch(input, init, async () => {
            if (failure === "connection") throw new Error("native connection unavailable");
            if (failure === "status") return response(input, stream, 503);
            return new Response(new ReadableStream({ start(controller) {
              controller.enqueue(new TextEncoder().encode("partial provider body"));
              setTimeout(() => controller.error(new Error("body interrupted")), 5);
            } }), { status: 200 });
          });
          await failed.text();
        } catch { /* Application fallback preserves its output, not a money claim. */ }
        return response(input, stream);
      });
      await install(state.native); await consume(state);
      expect(lanes()).toEqual([undefined]);
    });

    for (const extra of [false, true]) it(`rejects mismatched or additional native wire requests / stream=${stream} / extra=${extra}`, async () => {
      const state = client(stream);
      vi.stubGlobal("fetch", async (input: string, init?: RequestInit) => {
        const mismatched = input.replace("aiplatform.googleapis.com", "gateway.example");
        const other = await state.send(mismatched, init); await other.text();
        return extra ? state.send(input, init) : response(input, stream);
      });
      await install(state.native); await consume(state);
      expect(state.send).toHaveBeenCalledTimes(extra ? 2 : 1);
      expect(lanes()).toEqual([undefined]);
    });

    it(`keeps a retained public method unpriced across reinstall / stream=${stream}`, async () => {
      const state = client(stream); await install(state.native);
      const oldMethod = state.native.models[state.name];
      uninstrumentGoogleGenAI();
      const replacement = nextBuffer(); await install(state.native, replacement);
      await consume(state, oldMethod);
      expect(lanes()).toEqual([undefined]); expect(lanes(replacement)).toEqual([]);
      await consume(state);
      expect(lanes(replacement)).toEqual([{ type: "string", value: example.lane }]);
    });
  }

  for (const restart of [false, true]) for (const firstRead of [false, true]) it(`invalidates a lazy stream across teardown / restart=${restart} / firstRead=${firstRead}`, async () => {
    const state = client(true); await install(state.native);
    const iterator = (await state.native.models.generateContentStream(state.body))[Symbol.asyncIterator]();
    if (firstRead) expect((await iterator.next()).done).toBe(false);
    uninstrumentGoogleGenAI();
    const replacement = nextBuffer();
    if (restart) await install(state.native, replacement);
    // Public invocation installs the new client's apiCall observer even though
    // its native stream has not begun I/O. The old stream must not borrow it.
    const fresh = restart ? await state.native.models.generateContentStream(state.body) : undefined;
    while (!(await iterator.next()).done) { /* Original caller keeps all output. */ }
    expect(lanes()).toEqual([undefined]); expect(lanes(replacement)).toEqual([]);
    if (!restart) await install(state.native, replacement);
    if (fresh) for await (const _ of fresh) { /* Only this new generation admits money. */ }
    else await consume(state);
    expect(lanes(replacement)).toEqual([{ type: "string", value: example.lane }]);
  });

  it("removes exactly its native diagnostic observers before reinstalling", async () => {
    const names = ["undici:request:create", "undici:client:sendHeaders", "undici:request:headers", "undici:request:trailers", "undici:request:error"];
    const subscribersBefore = names.map(name => channel(name).hasSubscribers);
    const state = client(); await install(state.native); await consume(state);
    expect(names.map(name => channel(name).hasSubscribers)).toEqual(names.map(() => true));
    uninstrumentGoogleGenAI();
    expect(names.map(name => channel(name).hasSubscribers)).toEqual(subscribersBefore);
    await install(state.native); await consume(state);
    expect(names.map(name => channel(name).hasSubscribers)).toEqual(names.map(() => true));
    uninstrumentGoogleGenAI();
    expect(names.map(name => channel(name).hasSubscribers)).toEqual(subscribersBefore);
    expect(lanes()).toEqual([{ type: "string", value: example.lane }, { type: "string", value: example.lane }]);
  });
});
