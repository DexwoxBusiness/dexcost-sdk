import { afterEach, describe, expect, it } from "vitest";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { bindProviderBilling, hasPaidProviderBilling, type ProviderBillingTier } from "../src/core/provider-billing.js";
import { instrumentCohere, uninstrumentCohere, _setClientClass, _resetClientClass } from "../src/instruments/cohere.js";
import { instrumentGoogleGenAI, uninstrumentGoogleGenAI, provideGoogleGenAIModule } from "../src/instruments/google-genai.js";

const cases = JSON.parse(readFileSync(new URL("../../tests/fixtures/direct-paid-pricing.json", import.meta.url), "utf8")).cases;
// Official cohere-ai 8.1.0 BaseClient.normalizeClientOptions adds these even
// when the application never supplies custom headers.
const COHERE_NORMALIZED_HEADERS = {
  "X-Fern-Language": "JavaScript", "X-Fern-SDK-Name": "cohere-ai",
  "X-Fern-SDK-Version": "8.1.0", "User-Agent": "cohere-ai/8.1.0",
  "X-Fern-Runtime": "node", "X-Fern-Runtime-Version": "22",
  "X-Client-Name": undefined,
};
let buffer: EventBuffer;
let directory: string;
afterEach(() => {
  uninstrumentCohere(); uninstrumentGoogleGenAI(); _resetClientClass();
  buffer?.close();
  if (directory) rmSync(directory, { recursive: true, force: true });
});
function camel(value: any): any {
  if (Array.isArray(value)) return value.map(camel);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) =>
    [key.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase()), camel(item)]));
  return value;
}

async function capture(testCase: any, { stream = false, tier = "paid", endpoint = testCase.endpoint,
  options = undefined, rebind = false, response = testCase.response, cancel = false, omitTerminal = false, delegated = false,
  clientHeaders = COHERE_NORMALIZED_HEADERS, v1 = false }:
  { stream?: boolean; tier?: ProviderBillingTier | null; endpoint?: string; options?: any;
    rebind?: boolean; response?: any; cancel?: boolean; omitTerminal?: boolean; delegated?: boolean;
    clientHeaders?: unknown; v1?: boolean } = {}) {
  directory = mkdtempSync(join(tmpdir(), "dexcost-direct-paid-"));
  buffer = new EventBuffer(join(directory, "events.db"));
  const pricing = new PricingEngine();
  const raw = camel(structuredClone(response));
  let native: any;
  const assertion = { provider: testCase.provider, endpoint: testCase.endpoint };
  const duringRequest = async () => {
    if (rebind) bindProviderBilling(native, { ...assertion, tier: "paid" });
    await Promise.resolve();
    return raw;
  };
  if (testCase.provider === "cohere") {
    class Client {
      _options = { environment: endpoint, headers: clientHeaders };
      async chat(_body: any, _options?: any) { return duringRequest(); }
      async embed(_body: any, _options?: any) { return duringRequest(); }
      async rerank(_body: any, _options?: any) { return duringRequest(); }
      async chatStream(_body: any, _options?: any) {
        await duringRequest();
        return (async function* () {
          if (!v1) yield { type: "message-start", id: raw.id };
          if (!omitTerminal) yield v1 ? {eventType: "stream-end", response: raw} : { type: "message-end", delta: raw };
        })();
      }
    }
    // Match the official CohereClientV2: methods are bound instance fields
    // targeting a private generated V2Client, not methods on the public prototype.
    class DelegatedClient {
      clientV2 = new Client();
      chat = this.clientV2.chat.bind(this.clientV2);
      chatStream = this.clientV2.chatStream.bind(this.clientV2);
      embed = this.clientV2.embed.bind(this.clientV2);
      rerank = this.clientV2.rerank.bind(this.clientV2);
    }
    const NativeClient = delegated ? DelegatedClient : Client;
    _setClientClass(NativeClient);
    await instrumentCohere(pricing, buffer);
    native = new NativeClient();
    const other = new NativeClient();
    bindProviderBilling(other, { ...assertion, tier: "paid" });
    if (tier) bindProviderBilling(native, { ...assertion, tier });
    const body = { model: testCase.model };
    if (stream) {
      const result = await native.chatStream(body, options);
      for await (const _ of result) { if (cancel) break; }
    } else await native[testCase.service](body, options);
  } else {
    native = { vertexai: false, models: {
      apiClient: { getBaseUrl: () => endpoint },
      generateContent: async (_body: any) => duringRequest(),
      generateContentStream: async (_body: any) => {
        await duringRequest();
        return (async function* () { if (!omitTerminal) yield raw; })();
      },
    } };
    provideGoogleGenAIModule(native); await instrumentGoogleGenAI(pricing, buffer);
    bindProviderBilling({ models: { apiClient: {} } }, { ...assertion, tier: "paid" });
    if (tier) bindProviderBilling(native, { ...assertion, tier });
    const body = { model: testCase.model, ...(options ?? {}) };
    if (stream) for await (const _ of await native.models.generateContentStream(body)) { if (cancel) break; }
    else await native.models.generateContent(body);
  }
  expect(buffer.getAllEvents()).toHaveLength(1);
  return toAttributionObservationV3(buffer.getAllEvents()[0]);
}
function admitted(observation: any): boolean {
  return Boolean(observation?.usage.some((line: any) => line.dimensions?.some((dim: any) =>
    ["provider_billing_lane", "direct_llm_pricing_lane"].includes(dim.key))));
}

describe("caller-attested paid native capture -> shared exact server vectors", () => {
  for (const stream of [false, true]) it.each(cases.filter((item: any) =>
    item.provider === "cohere" && item.service === "chat"
  ))(`admits normalized V1 metadata headers $id (stream=${stream})`, async (testCase: any) => {
    const response = {generation_id: testCase.response.id, finish_reason: "COMPLETE", meta: testCase.response.usage};
    expect(admitted(await capture(testCase, {stream, response, v1: true}))).toBe(true);
  });
  for (const delegated of [false, true]) it.each(["Authorization", "aUtHoRiZaTiOn", "X-API-Key", "Host", "X-Forwarded-Host"])(
    `rejects custom auth/routing header %s on normalized client (delegated=${delegated})`, async name => {
      const headers = {...COHERE_NORMALIZED_HEADERS};
      Object.defineProperty(headers, name, {enumerable: true, get() { throw new Error("secret must not be inspected"); }});
      expect(admitted(await capture(cases[0], {delegated, clientHeaders: headers}))).toBe(false);
    });
  it("never evaluates normalized metadata header values or suppliers", async () => {
    const headers = {};
    for (const name of Object.keys(COHERE_NORMALIZED_HEADERS)) {
      Object.defineProperty(headers, name.toLowerCase(), {enumerable: true, get() { throw new Error("metadata values must not be inspected"); }});
    }
    expect(admitted(await capture(cases[0], {delegated: true, clientHeaders: headers}))).toBe(true);
  });
  for (const stream of [false, true]) it.each(cases.filter((item: any) =>
    item.provider === "cohere" && (!stream || item.service === "chat")
  ))(`captures real delegated V2 shape $id (stream=${stream})`, async (testCase: any) => {
    const observation = await capture(testCase, {stream, delegated: true});
    expect(admitted(observation)).toBe(true);
    expect(Object.fromEntries(observation!.usage.map(line => [line.metric, line.quantity]))).toEqual(testCase.expected_usage);
  });
  for (const stream of [false, true]) it.each(cases.filter((item: any) => !stream || item.component === "llm"))(
    `captures $id (stream=${stream})`, async (testCase: any) => {
      const event = await capture(testCase, { stream });
      expect(admitted(event)).toBe(true);
      expect(event?.component).toBe(testCase.component);
      expect(event?.provider).toMatchObject({name: testCase.provider, service: testCase.service});
      expect(event?.provider.record_id).toBeTruthy();
      expect(event?.resource).toEqual({ type: "model", id: testCase.model });
      expect(Object.fromEntries(event!.usage.map(line => [line.metric, line.quantity]))).toEqual(testCase.expected_usage);
      for (const line of event!.usage) expect(line.dimensions).toContainEqual({
        key: testCase.dimension, value: {type: "string", value: testCase.lane},
      });
    });
  for (const tier of [null, "free", "unknown"] as const) it.each(cases)(
    `keeps default/trial/free client isolated from other paid client $id (tier=${tier})`, async (testCase: any) => {
      expect(admitted(await capture(testCase, { tier }))).toBe(false);
    });
  for (const reason of ["route", "headers", "pending-rebind", "missing-usage", "invalid-counter"]) it.each(cases)(
    `fails open for $id (${reason})`, async (testCase: any) => {
      const response = structuredClone(testCase.response);
      let options: any;
      if (reason === "headers") options = testCase.provider === "google"
        ? { config: { httpOptions: { headers: { authorization: "test-only" } } } }
        : { headers: { authorization: "test-only" } };
      if (reason === "missing-usage") { delete response.usageMetadata; delete response.usage; delete response.meta; }
      if (reason === "invalid-counter") {
        if (testCase.provider === "google") response.usageMetadata.promptTokenCount = true;
        else (response.usage ?? response.meta).billed_units[testCase.service === "rerank" ? "search_units" : "input_tokens"] = true;
      }
      expect(admitted(await capture(testCase, { response, options,
        endpoint: reason === "route" ? "https://gateway.example.invalid" : testCase.endpoint,
        tier: reason === "pending-rebind" ? "free" : "paid", rebind: reason === "pending-rebind",
      }))).toBe(false);
    });
  it.each(cases.filter((item: any) => item.component === "llm"))("does not admit an incomplete stream $id", async (testCase: any) => {
    expect(admitted(await capture(testCase, {stream: true, omitTerminal: true}))).toBe(false);
  });
  it("isolates bindings and makes old unbind callbacks harmless", () => {
    const client = {};
    const other = {};
    const old = bindProviderBilling(client, {provider: "cohere", tier: "free", endpoint: "https://api.cohere.com"});
    const current = bindProviderBilling(client, {provider: "cohere", tier: "paid", endpoint: "https://api.cohere.com"});
    old();
    expect(hasPaidProviderBilling(client, "cohere", "https://api.cohere.com")).toBe(true);
    expect(hasPaidProviderBilling(other, "cohere", "https://api.cohere.com")).toBe(false);
    expect(hasPaidProviderBilling(client, "google", "https://generativelanguage.googleapis.com")).toBe(false);
    current(); current();
    expect(hasPaidProviderBilling(client, "cohere", "https://api.cohere.com")).toBe(false);
    for (const endpoint of ["https://api.cohere.com/proxy", "https://api.cohere.com?api_key=x",
      "https://user@api.cohere.com", "https://proxy.example.invalid", "http://api.cohere.com", "https://api.cohere.com:8443"]) {
      expect(() => bindProviderBilling(client, {provider: "cohere", tier: "paid", endpoint})).toThrow();
    }
  });
  for (const missing of ["promptTokensDetails", "cacheTokensDetails", "candidatesTokensDetails"]) it.each(
    cases.filter((item: any) => item.provider === "google")
  )(`requires positive TEXT evidence in $id (${missing})`, async (testCase: any) => {
    const response = structuredClone(testCase.response);
    delete response.usageMetadata[missing];
    expect(admitted(await capture(testCase, {response}))).toBe(false);
  });
  it("keeps concurrent paid and free native clients separate", async () => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-direct-paid-concurrent-"));
    buffer = new EventBuffer(join(directory, "events.db"));
    const response = camel(cases[0].response);
    class Client {
      _options = { environment: "https://api.cohere.com" };
      async chat(_body: any) { await Promise.resolve(); return response; }
    }
    _setClientClass(Client); await instrumentCohere(new PricingEngine(), buffer);
    const paid = new Client(); const free = new Client();
    bindProviderBilling(paid, {provider: "cohere", tier: "paid", endpoint: "https://api.cohere.com"});
    bindProviderBilling(free, {provider: "cohere", tier: "free", endpoint: "https://api.cohere.com"});
    await Promise.all([paid.chat({model: cases[0].model}), free.chat({model: cases[0].model})]);
    const observations = buffer.getAllEvents().map(event => toAttributionObservationV3(event));
    expect(observations).toHaveLength(2);
    expect(observations.filter(admitted)).toHaveLength(1);
  });
});
