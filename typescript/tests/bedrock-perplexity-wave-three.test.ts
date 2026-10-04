import { afterEach, describe, expect, it } from "vitest";
import { createRequire } from "node:module";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { providerJobFromDict } from "../src/core/provider-jobs.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { instrumentBedrock, uninstrumentBedrock, _setClientClass, _resetClientClass } from "../src/instruments/bedrock.js";
import { instrumentPerplexity, uninstrumentPerplexity, providePerplexityModule } from "../src/instruments/perplexity.js";

const require = createRequire(import.meta.url);
const fixture = JSON.parse(readFileSync(new URL("../../tests/fixtures/bedrock-perplexity-wave-three.json", import.meta.url), "utf8"));
let directory: string;
let buffer: EventBuffer;
const setup = () => {
  directory = mkdtempSync(join(tmpdir(), "dexcost-wave-three-"));
  buffer = new EventBuffer(join(directory, "events.db"));
  return new PricingEngine();
};
afterEach(() => {
  uninstrumentBedrock(); _resetClientClass(); uninstrumentPerplexity(); providePerplexityModule(undefined);
  buffer?.close(); if (directory) rmSync(directory, { recursive: true, force: true });
});
function observed() {
  expect(buffer.getAllEvents()).toHaveLength(1);
  return toAttributionObservationV3(buffer.getAllEvents()[0])!;
}
function lane(observation: any) { return observation.usage.flatMap((line: any) => line.dimensions).find((d: any) => d.key === "bedrock_pricing_lane")?.value.value; }

async function nova(caseValue: any, stream: boolean, change?: (input: any, response: any, config: any) => void) {
  const input = structuredClone(caseValue.request);
  const response = { ...structuredClone(caseValue.response), $metadata: { requestId: "nova-request" } };
  const config = { region: async () => "us-east-1", endpointProvider: function defaultEndpointResolver() {} };
  change?.(input, response, config);
  class ConverseCommand { constructor(public input: any) {} }
  class ConverseStreamCommand { constructor(public input: any) {} }
  class Client {
    config = config;
    async send(_command: any) { return stream ? {
      $metadata: response.$metadata,
      stream: (async function* () {
        yield { messageStop: { stopReason: response.stopReason } };
        yield { metadata: response };
      })(),
    } : response; }
  }
  _setClientClass(Client); await instrumentBedrock(setup(), buffer);
  const result: any = await new Client().send(stream ? new ConverseStreamCommand(input) : new ConverseCommand(input));
  if (stream) for await (const _chunk of result.stream) { /* consume */ }
  return observed();
}

describe("paired Bedrock native response -> v3 server vectors", () => {
  for (const stream of [false, true]) {
    it.each(fixture.bedrock_cases)("captures $id stream=" + stream, async testCase => {
      const event = await nova(testCase, stream);
      expect(event.provider).toEqual({ name: "aws", service: "bedrock", record_id: "nova-request", region: testCase.region });
      expect(event.resource).toEqual({ type: "model", id: testCase.model });
      expect(Object.fromEntries(event.usage.map((line: any) => [line.metric, line.quantity]))).toEqual(testCase.expected_usage);
      expect(lane(event)).toBe("us_east_1_nova_standard_no_cache");
      expect(buffer.getAllEvents()[0].pricingSource).toBe("unknown"); // No SDK tariff.
    });
    it.each(["region", "missing_region", "endpoint", "resolver", "profile", "tier", "latency", "cache", "cache_bool", "usage", "total", "guardrail", "multimodal", "missing_tier"])("rejects %s stream=" + stream, async reason => {
      const event = await nova(fixture.bedrock_cases[0], stream, (input, response, config) => {
        if (reason === "region") config.region = async () => "us-west-2";
        if (reason === "missing_region") delete config.region;
        if (reason === "endpoint") config.endpoint = async () => new URL("https://gateway.example");
        if (reason === "resolver") config.endpointProvider = function custom() {};
        if (reason === "profile") input.modelId = "us.amazon.nova-micro-v1:0";
        if (reason === "tier") response.serviceTier.type = "priority";
        if (reason === "latency") response.performanceConfig.latency = "optimized";
        if (reason === "cache") response.usage.cacheReadInputTokens = 1;
        if (reason === "cache_bool") response.usage.cacheReadInputTokens = false;
        if (reason === "usage") delete response.usage.inputTokens;
        if (reason === "total") response.usage.totalTokens++;
        if (reason === "guardrail") input.guardrailConfig = { guardrailIdentifier: "id" };
        if (reason === "multimodal") input.messages[0].content = [{ image: {} }];
        if (reason === "missing_tier") delete response.serviceTier;
      });
      expect(lane(event)).toBeUndefined();
      if (["region", "missing_region", "endpoint", "resolver"].includes(reason)) expect(event.provider.region).toBeUndefined();
    });
  }
});

describe("real official packages, mocked I/O only", () => {
  it("keeps canonical Agent identity aligned with the shared v3 corpus", () => {
    const corpus = JSON.parse(readFileSync(new URL("../../fixtures/attribution_v3/conformance.json", import.meta.url), "utf8"));
    expect(corpus.valid_observations.find((item: any) => item.id === "observation.explicit_provider_zero").event.provider.service)
      .toBe(fixture.perplexity_expected_provider_service);
  });

  for (const action of ["retrieve", "cancel"] as const) it.each(["completed", "failed", "cancelled"])("ignores another Agent job's %s status via " + action, async status => {
    const { Perplexity } = await import("@perplexity-ai/perplexity_ai");
    const raw = structuredClone(fixture.perplexity_response);
    const wrong = { ...raw, id: "another-agent-job", status };
    let answer = wrong;
    const client = new Perplexity({ apiKey: "fixture", fetch: async (url: any, init: any) => {
      const result = init?.method === "POST" && new URL(String(url)).pathname === "/v1/responses"
        ? { ...raw, status: "queued", usage: null } : answer;
      return new Response(JSON.stringify(result), { headers: { "content-type": "application/json" } });
    } });
    await instrumentPerplexity(setup(), buffer);
    await client.responses.create({ model: raw.model, input: "fixture", background: true });
    const before = buffer.getProviderJob("perplexity", "agent", raw.id)!;
    const result = await client.responses[action](raw.id);
    expect(result.id).toBe(wrong.id);
    expect(buffer.getProviderJob("perplexity", "agent", raw.id)).toEqual(before);
    answer = raw;
    await client.responses.retrieve(raw.id);
    expect(buffer.getProviderJob("perplexity", "agent", raw.id)).toMatchObject({ status: "succeeded", cost_amount: "0.02665", task_cached_tokens: fixture.perplexity_expected_cached_tokens });
  });

  it("reconciles a native background Agent request to explicit zero exactly once", async () => {
    const { Perplexity } = await import("@perplexity-ai/perplexity_ai");
    const raw = structuredClone(fixture.perplexity_response); raw.usage.cost.total_cost = 0;
    const client = new Perplexity({ apiKey: "fixture", fetch: async (url: any, init: any) => {
      const result = init?.method === "POST" ? { ...raw, status: "queued", usage: null } : raw;
      return new Response(JSON.stringify(result), { headers: { "content-type": "application/json" } });
    } });
    await instrumentPerplexity(setup(), buffer);
    await client.responses.create({ model: raw.model, input: "fixture", background: true });
    expect(buffer.getProviderJob("perplexity", "agent", raw.id)?.cost_amount).toBeUndefined();
    await client.responses.retrieve(raw.id);
    const final = buffer.getProviderJob("perplexity", "agent", raw.id)!;
    expect(final.cost_amount).toBe("0");
    expect(final.cost_source).toBe("provider_reported");
    expect(final.task_cached_tokens).toBe(fixture.perplexity_expected_cached_tokens);
    expect(buffer.getAllTasks()[0]?.totalCachedTokens).toBe(fixture.perplexity_expected_cached_tokens);
    expect(providerJobFromDict(final).toAttributionObservation().provider).toEqual({
      name: "perplexity", service: "agent", record_id: raw.id,
    });
    expect(final.usage).toEqual([{ metric: "request_count", quantity: "1", unit: "Requests" }]);
    await client.responses.retrieve(raw.id);
    expect(buffer.getProviderJob("perplexity", "agent", raw.id)?.cost_amount).toBe("0");
    expect(buffer.getProviderJob("perplexity", "agent", raw.id)?.revision).toBe(final.revision);
    expect(buffer.getAllEvents()).toHaveLength(0); // No second synchronous event for polling.
  });

  it("observes Perplexity helpers once and does not price caller-recovered responses", async () => {
    const { Perplexity } = await import("@perplexity-ai/perplexity_ai");
    let requests = 0;
    const client = new Perplexity({ apiKey: "fixture", maxRetries: 0,
      fetch: async () => { requests++; return new Response(JSON.stringify(fixture.perplexity_response), { headers: { "content-type": "application/json" } }); },
    });
    await instrumentPerplexity(setup(), buffer);
    const promise = client.responses.create({ model: "fixture", input: "fixture" });
    const payload = await promise.withResponse();
    expect(await promise.finally(() => undefined)).toEqual(payload.data);
    expect(await promise).toEqual(payload.data);
    expect(requests).toBe(1);
    expect(buffer.getAllEvents()).toHaveLength(1);
    const failing = new Perplexity({ apiKey: "fixture", maxRetries: 0,
      fetch: async () => new Response(JSON.stringify({ error: { message: "fixture failure" } }), { status: 500, headers: { "content-type": "application/json" } }),
    });
    await failing.responses.create({ model: "fixture", input: "fixture" }).catch(() => fixture.perplexity_response);
    expect(buffer.getAllEvents()).toHaveLength(2);
    const failed = buffer.getAllEvents().find(e => e.details?.attribution_operation_status === "failed")!;
    expect(failed.pricingSource).toBe("unknown");
    expect(failed.details?.provider_reported_cost_usd).toBeUndefined();
  });

  it("keeps asResponse body readable without eagerly consuming it", async () => {
    const { Perplexity } = await import("@perplexity-ai/perplexity_ai");
    const client = new Perplexity({ apiKey: "fixture", fetch: async () => new Response(JSON.stringify(fixture.perplexity_response), { headers: { "content-type": "application/json" } }) });
    await instrumentPerplexity(setup(), buffer);
    const response = await client.responses.create({ model: "fixture", input: "fixture" }).asResponse();
    expect(await response.json()).toEqual(fixture.perplexity_response);
    expect(buffer.getAllEvents()).toHaveLength(0); // Raw-only helpers intentionally do not inspect bodies.
  });

  it("admits an actual AWS generated client and command without losing native response", async () => {
    const { BedrockRuntimeClient, ConverseCommand } = require("@aws-sdk/client-bedrock-runtime");
    const sent: any[] = [];
    const body = fixture.bedrock_cases[0];
    const client = new BedrockRuntimeClient({
      region: "us-east-1", credentials: { accessKeyId: "fixture", secretAccessKey: "fixture" },
      requestHandler: { handle: async (request: any) => {
        sent.push({ host: request.hostname, path: request.path });
        return { response: { statusCode: 200, headers: { "content-type": "application/json", "x-amzn-requestid": "native-nova" },
          body: Buffer.from(JSON.stringify(body.response)) } };
      } },
    });
    await instrumentBedrock(setup(), buffer);
    const response = await client.send(new ConverseCommand(body.request));
    expect(response.usage.totalTokens).toBe(1250);
    expect(sent).toHaveLength(1);
    expect(sent[0].host).toBe("bedrock-runtime.us-east-1.amazonaws.com");
    expect(lane(observed())).toBe("us_east_1_nova_standard_no_cache");
    expect(observed().provider.region).toBe(body.region);
    client.destroy();
  });

  for (const stream of [false, true]) it.each(["positive", "zero", "missing", "currency", "incomplete", "malformed", "gateway"])("Perplexity %s stream=" + stream, async reason => {
    const { Perplexity } = await import("@perplexity-ai/perplexity_ai");
    const raw = structuredClone(fixture.perplexity_response);
    if (reason === "zero") raw.usage.cost.total_cost = 0;
    if (reason === "missing") delete raw.usage.cost.total_cost;
    if (reason === "currency") raw.usage.cost.currency = "EUR";
    if (reason === "incomplete") raw.status = "in_progress";
    if (reason === "malformed") raw.usage.cost.total_cost = true;
    const requests: string[] = [];
    const client = new Perplexity({
      apiKey: "fixture-not-real", baseURL: reason === "gateway" ? "https://gateway.example" : "https://api.perplexity.ai",
      fetch: async (url: any) => {
        requests.push(String(url));
        return new Response(stream ? "event: response.completed\ndata: " + JSON.stringify({ type: "response.completed", response: raw }) + "\n\ndata: [DONE]\n\n" : JSON.stringify(raw),
          { headers: { "content-type": stream ? "text/event-stream" : "application/json" } });
      },
    });
    await instrumentPerplexity(setup(), buffer);
    const promise = client.responses.create({ model: raw.model, input: "fixture", stream });
    expect(typeof promise.withResponse).toBe("function");
    expect(typeof promise.asResponse).toBe("function");
    const result = await promise;
    if (stream) for await (const _chunk of result) { /* consume */ }
    expect(requests).toHaveLength(1);
    expect(new URL(requests[0]).pathname).toBe("/v1/responses");
    const event = buffer.getAllEvents()[0];
    expect(buffer.getAllEvents()).toHaveLength(1);
    const observation = toAttributionObservationV3(event)!;
    expect(observation.provider).toEqual({ name: "perplexity", service: "agent", record_id: raw.id });
    expect(event.inputTokens).toBe(5870);
    expect(event.outputTokens).toBe(679);
    expect(event.cachedTokens).toBe(fixture.perplexity_expected_cached_tokens);
    expect(buffer.getAllTasks()[0]?.totalCachedTokens).toBe(fixture.perplexity_expected_cached_tokens);
    expect(event.details?.attribution_usage_lines).toEqual([{ metric: "request_count", quantity: "1", unit: "Requests" }]);
    if (["positive", "zero"].includes(reason)) {
      expect(event.pricingSource).toBe("provider_response");
      expect(event.costUsd.toString()).toBe(reason === "zero" ? "0" : "0.02665");
      expect(event.details?.provider_reported_cost_usd).toBe(reason === "zero" ? "0" : "0.02665");
      expect(observation.cost_evidence).toEqual({
        amount: reason === "zero" ? "0" : "0.02665", currency: "USD", source: "provider_reported", confidence: "exact",
      });
    } else {
      expect(event.pricingSource).toBe("unknown");
      expect(observation.cost_evidence).toBeUndefined();
    }
  });
});
