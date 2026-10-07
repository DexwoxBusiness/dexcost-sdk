import { afterEach, describe, expect, it } from "vitest";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { instrumentOpenai, uninstrumentOpenai, _setCompletionsClass, _setResponsesClass, _resetCompletionsClass, _resetResponsesClass } from "../src/instruments/openai.js";
import { instrumentBedrock, uninstrumentBedrock, _setClientClass, _resetClientClass } from "../src/instruments/bedrock.js";

const require = createRequire(import.meta.url);
const fixture = JSON.parse(readFileSync(new URL("../../tests/fixtures/llm-wave-four.json", import.meta.url), "utf8"));
// Official openai7.30.0 requires Node22; core facade capture stays tested on20.
const nativeOpenAiSupported = Number(process.versions.node.split(".")[0]) >= 22;
let directory: string;
let buffer: EventBuffer;
const setup = () => {
  directory = mkdtempSync(join(tmpdir(), "dexcost-llm-wave-four-"));
  buffer = new EventBuffer(join(directory, "events.db"));
  return new PricingEngine();
};
afterEach(() => {
  uninstrumentOpenai(); _resetCompletionsClass(); _resetResponsesClass();
  uninstrumentBedrock(); _resetClientClass();
  buffer?.close(); if (directory) rmSync(directory, { recursive: true, force: true });
});
function observed() {
  expect(buffer.getAllEvents()).toHaveLength(1);
  return toAttributionObservationV3(buffer.getAllEvents()[0]);
}
function lane(event: any) { return event?.usage.flatMap((l: any) => l.dimensions).find((d: any) => ["bedrock_pricing_lane", "direct_llm_pricing_lane"].includes(d.key))?.value.value; }
function assertVector(event: any, testCase: any) {
  expect(event.provider).toMatchObject({ name: testCase.provider, service: testCase.service });
  expect(event.provider.record_id).toBeTruthy();
  expect(event.resource).toEqual({ type: "model", id: testCase.model });
  expect(Object.fromEntries(event.usage.map((l: any) => [l.metric, l.quantity]))).toEqual(testCase.expected_usage);
  expect(lane(event)).toBe(testCase.lane);
}

async function chat(testCase: any, stream: boolean, reason = "positive", native = true) {
  const raw = structuredClone(testCase.response);
  const request = { ...structuredClone(testCase.request), stream, ...(stream ? { stream_options: { include_usage: true } } : {}) };
  if (reason === "missing_usage") delete raw.usage;
  if (reason === "missing_input") delete raw.usage.prompt_tokens;
  if (reason === "wrong_total") raw.usage.total_tokens++;
  if (reason === "tier") raw.service_tier = "priority";
  if (reason === "missing_tier") delete raw.service_tier;
  if (reason === "cache_overlap") raw.usage.prompt_tokens_details.cached_tokens = raw.usage.prompt_tokens;
  if (reason === "reasoning_overlap") raw.usage.completion_tokens_details.reasoning_tokens = 5001;
  if (reason === "malformed_details") raw.usage.prompt_tokens_details = "not-an-object";
  if (reason === "audio") raw.usage.prompt_tokens_details.audio_tokens = 1;
  if (reason === "bool_audio") raw.usage.prompt_tokens_details.audio_tokens = false;
  if (reason === "tools") request.tools = [];
  if (reason === "missing_model") delete raw.model;
  if (reason === "missing_id") delete raw.id;
  if (reason === "unfinished") raw.choices[0].finish_reason = null;
  const final = { ...raw, object: "chat.completion.chunk", choices: [] };
  if (reason === "unfinished") final.choices = [{ index: 0, delta: {}, finish_reason: null }];
  const chunks = [ { ...raw, object: "chat.completion.chunk", usage: null, choices: [{ index: 0, delta: { content: "fixture" }, finish_reason: "stop" }] }, final ];
  let calls = 0;
  const baseURL = reason === "gateway" ? "https://gateway.example/v1" : reason === "regional" ? "https://eu.api.openai.com/v1" : testCase.endpoint;
  let client: any;
  if (native) {
    const { default: OpenAI } = await import("openai");
    client = new OpenAI({ apiKey: "fixture", maxRetries: 0, baseURL,
    fetch: async (url: any) => {
      calls++; expect(new URL(String(url)).pathname).toBe("/v1/chat/completions");
      if (reason === "failed") return new Response('{"error":{"message":"fixture"}}', { status: 500, headers: { "content-type": "application/json" } });
      return new Response(stream ? chunks.map(c => "data: " + JSON.stringify(c) + "\n\n").join("") + "data: [DONE]\n\n" : JSON.stringify(raw), {
        headers: { "content-type": stream ? "text/event-stream" : "application/json" },
      });
    },
    });
  } else {
    class Chat {
      _client = { baseURL };
      async create(_request: any): Promise<any> {
        calls++;
        if (reason === "failed") throw new Error("fixture");
        return stream ? (async function* () { yield* chunks; })() : raw;
      }
    }
    class Responses { async create() { return {}; } }
    _setCompletionsClass(Chat); _setResponsesClass(Responses);
    client = { chat: { completions: new Chat() } };
  }
  await instrumentOpenai(setup(), buffer);
  const promise = client.chat.completions.create(request);
  if (native) expect(typeof promise.withResponse).toBe("function");
  if (reason === "failed") await expect(promise).rejects.toThrow();
  else {
    const result: any = await promise;
    if (stream) for await (const _chunk of result) { if (reason === "cancelled") break; }
    else expect(result.choices[0].message.content).toBe("fixture");
  }
  expect(calls).toBe(1);
  return observed();
}
for (const native of [false, true]) describe.skipIf(native && !nativeOpenAiSupported)(`${native ? "native openai7.30" : "paired facade"} Chat Completions -> v3 -> exact shared server vectors`, () => {
  for (const stream of [false, true]) {
    it.each(fixture.cases.filter((c: any) => c.provider === "openai"))("captures $id stream=" + stream, async testCase => {
      assertVector(await chat(testCase, stream, "positive", native), testCase);
    });
    it.each(["missing_usage", "missing_input", "wrong_total", "tier", "missing_tier", "cache_overlap", "reasoning_overlap", "malformed_details", "audio", "bool_audio", "tools", "missing_model", "missing_id", "unfinished", "gateway", "regional", "failed"])("fails open %s stream=" + stream, async reason => {
      expect(lane(await chat(fixture.cases[0], stream, reason, native))).toBeUndefined();
    });
  }
  it("does not price cancelled stream before final usage", async () => {
    expect(lane(await chat(fixture.cases[0], true, "cancelled", native))).toBeUndefined();
  });
});

async function claude(testCase: any, stream: boolean, reason = "positive") {
  const input = structuredClone(testCase.request);
  const response = { ...structuredClone(testCase.response), $metadata: { requestId: "claude-global-request" } };
  const config: any = { region: async () => "us-east-1", endpointProvider: function defaultEndpointResolver() {} };
  if (reason === "region") config.region = async () => "us-west-2";
  if (reason === "endpoint") config.endpoint = async () => new URL("https://gateway.example");
  if (reason === "bare_model") input.modelId = input.modelId.replace("global.", "");
  if (reason === "geo_profile") input.modelId = input.modelId.replace("global.", "us.");
  if (reason === "tier") response.serviceTier.type = "reserved";
  if (reason === "cache") response.usage.cacheWriteInputTokens = 1;
  if (reason === "tools") input.toolConfig = { tools: [] };
  if (reason === "multimodal") input.messages[0].content = [{ image: {} }];
  if (reason === "missing_tier") delete response.serviceTier;
  if (reason === "wrong_total") response.usage.totalTokens++;
  class ConverseCommand { constructor(public input: any) {} }
  class ConverseStreamCommand { constructor(public input: any) {} }
  class Client {
    config = config;
    async send(_command: any) {
      if (reason === "failed") throw new Error("native failure");
      return stream ? { $metadata: response.$metadata, stream: (async function* () {
        yield { messageStart: { role: "assistant" } };
        yield { messageStop: { stopReason: response.stopReason } };
        if (reason !== "missing_terminal") yield { metadata: response };
      })() } : response;
    }
  }
  _setClientClass(Client); await instrumentBedrock(setup(), buffer);
  if (reason === "failed") await expect(new Client().send(new ConverseCommand(input))).rejects.toThrow("native failure");
  else {
    const result: any = await new Client().send(stream ? new ConverseStreamCommand(input) : new ConverseCommand(input));
    if (stream) for await (const _chunk of result.stream) { if (reason === "cancelled") break; }
  }
  return observed();
}
describe("bounded Claude global profiles sourced from us-east-1", () => {
  for (const stream of [false, true]) {
    it.each(fixture.cases.filter((c: any) => c.provider === "aws"))("captures $id stream=" + stream, async testCase => {
      const event = await claude(testCase, stream); assertVector(event, testCase);
      expect(event!.provider.region).toBe("us-east-1");
    });
    it.each(["region", "endpoint", "bare_model", "geo_profile", "tier", "cache", "tools", "multimodal", "missing_tier", "wrong_total", "failed"])("fails open %s stream=" + stream, async reason => {
      expect(lane(await claude(fixture.cases.find((c: any) => c.provider === "aws"), stream, reason))).toBeUndefined();
    });
  }
  it.each(["missing_terminal", "cancelled"])("does not price %s stream", async reason => {
    expect(lane(await claude(fixture.cases.find((c: any) => c.provider === "aws"), true, reason))).toBeUndefined();
  });
  it.each(fixture.cases.filter((c: any) => c.provider === "aws"))("real AWS client preserves $id source region and global profile", async testCase => {
    const { BedrockRuntimeClient, ConverseCommand } = require("@aws-sdk/client-bedrock-runtime");
    const paths: string[] = [];
    const client = new BedrockRuntimeClient({ region: "us-east-1", credentials: { accessKeyId: "fixture", secretAccessKey: "fixture" },
      requestHandler: { handle: async (request: any) => {
        expect(request.hostname).toBe("bedrock-runtime.us-east-1.amazonaws.com"); paths.push(request.path);
        return { response: { statusCode: 200, headers: { "content-type": "application/json", "x-amzn-requestid": "real-claude" }, body: Buffer.from(JSON.stringify(testCase.response)) } };
      } },
    });
    await instrumentBedrock(setup(), buffer);
    expect((await client.send(new ConverseCommand(testCase.request))).usage.totalTokens).toBe(1500);
    expect(paths).toHaveLength(1);
    expect(decodeURIComponent(paths[0])).toContain(testCase.model);
    assertVector(observed(), testCase); expect(observed()!.provider.region).toBe("us-east-1");
    client.destroy();
  });
});
