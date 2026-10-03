import { afterEach, describe, expect, it } from "vitest";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { instrumentOpenai, uninstrumentOpenai, _setCompletionsClass, _setResponsesClass, _resetCompletionsClass, _resetResponsesClass } from "../src/instruments/openai.js";
import { instrumentAnthropic, uninstrumentAnthropic, _setMessagesClass, _resetMessagesClass } from "../src/instruments/anthropic.js";
import { instrumentGoogleGenAI, uninstrumentGoogleGenAI, provideGoogleGenAIModule } from "../src/instruments/google-genai.js";

const fixture = JSON.parse(readFileSync(new URL("../../tests/fixtures/direct-foundation-pricing.json", import.meta.url), "utf8"));
let buffer: EventBuffer;
let directory: string;
afterEach(() => {
  uninstrumentOpenai(); uninstrumentAnthropic(); uninstrumentGoogleGenAI();
  _resetCompletionsClass(); _resetResponsesClass(); _resetMessagesClass();
  buffer?.close();
  if (directory) rmSync(directory, { recursive: true, force: true });
});

async function capture(testCase: any, stream = false) {
  directory = mkdtempSync(join(tmpdir(), "dexcost-direct-foundation-"));
  buffer = new EventBuffer(join(directory, "events.db"));
  const pricing = new PricingEngine();
  const response = structuredClone(testCase.response);
  const streamed = async function* (chunks: any[]) { yield* chunks; };
  if (testCase.provider === "openai") {
    class Chat { async create() { return response; } }
    class Responses {
      _client = { baseURL: testCase.endpoint };
      async create(_body: any) { return stream ? streamed([{ type: "response.completed", response }]) : response; }
    }
    _setCompletionsClass(Chat); _setResponsesClass(Responses);
    await instrumentOpenai(pricing, buffer);
    const result = await new Responses().create({ model: testCase.model, stream });
    if (stream) for await (const _chunk of result) { /* exhaust */ }
  } else if (testCase.provider === "anthropic") {
    class Messages {
      _client = { baseURL: testCase.endpoint };
      async create(_body: any) { return stream ? streamed([
        { type: "message_start", message: response },
        { type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 50 } },
        { type: "message_stop" },
      ]) : response; }
    }
    _setMessagesClass(Messages); await instrumentAnthropic(pricing, buffer);
    const result = await new Messages().create({ model: testCase.model, stream });
    if (stream) for await (const _chunk of result) { /* exhaust */ }
  } else {
    const client = { vertexai: false, models: {
      apiClient: { getBaseUrl: () => testCase.endpoint },
      generateContent: async (_body: any) => response,
      generateContentStream: async (_body: any) => streamed([response]),
    } };
    provideGoogleGenAIModule(client); await instrumentGoogleGenAI(pricing, buffer);
    const body = { model: testCase.model };
    if (stream) for await (const _chunk of await client.models.generateContentStream(body)) { /* exhaust */ }
    else await client.models.generateContent(body);
  }
  expect(buffer.getAllEvents()).toHaveLength(1);
  return toAttributionObservationV3(buffer.getAllEvents()[0]);
}

describe("raw native capture -> paired v3 -> shared server price vector", () => {
  it.each(fixture.fail_open_cases)("does not price $id", async (testCase: any) => {
    const observation = await capture(testCase);
    if (["unknown-openai-cache-overlap", "unknown-openai-reasoning-overflow"].includes(testCase.id)) {
      expect(observation).toBeNull(); // Strict usage diagnostics suppress invalid v3.
      return;
    }
    expect(observation!.usage.flatMap((line) => line.dimensions).some((dimension) => dimension.key === "direct_llm_pricing_lane")).toBe(false);
  });
  for (const stream of [false, true]) it.each(fixture.cases)(`captures $id (stream=${stream})`, async (testCase: any) => {
    const observation = await capture(testCase, stream);
    expect(observation?.component).toBe("llm");
    expect(observation?.provider).toMatchObject({ name: testCase.provider, service: testCase.service });
    expect(observation?.provider.record_id).toBeTruthy();
    expect(observation?.resource).toEqual({ type: "model", id: testCase.model });
    expect(Object.fromEntries(observation!.usage.map((line) => [line.metric, line.quantity]))).toEqual(testCase.expected_usage);
    for (const line of observation!.usage) expect(line.dimensions).toContainEqual({
      key: "direct_llm_pricing_lane", value: { type: "string", value: testCase.lane },
    });
  });
  for (const provider of ["openai", "anthropic", "google"]) it.each(["route", "tier"])(`${provider} fails open on unknown %s`, async (reason) => {
    const testCase = structuredClone(fixture.cases.find((item: any) => item.provider === provider));
    if (reason === "route") testCase.endpoint = "https://proxy.example.invalid";
    else if (provider === "openai") delete testCase.response.service_tier;
    else if (provider === "anthropic") delete testCase.response.usage.service_tier;
    else delete testCase.response.usageMetadata.serviceTier;
    const observation = await capture(testCase);
    expect(observation!.usage.flatMap((line) => line.dimensions).some((dimension) => dimension.key === "direct_llm_pricing_lane")).toBe(false);
  });
});
