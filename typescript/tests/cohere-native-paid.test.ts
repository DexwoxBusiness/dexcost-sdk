import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRequire } from "node:module";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { bindProviderBilling } from "../src/core/provider-billing.js";
import { instrumentCohere, uninstrumentCohere, _setClientClass, _resetClientClass } from "../src/instruments/cohere.js";

// Always exercise the installed package's public entry point, including its
// exports map. Absolute-path overrides can hide package-resolution failures.
const require = createRequire(import.meta.url);
const { CohereClient, CohereClientV2 } = require("cohere-ai");
const operations = ["chat", "chatStream", "embed", "rerank"] as const;
type Operation = typeof operations[number];
let directory: string;
let buffer: EventBuffer;
let fetchStub: ReturnType<typeof vi.fn>;

const billed = { input_tokens: 100, output_tokens: 50 };
function nativeResponse(v2: boolean, operation: Operation): Record<string, unknown> {
  if (operation === "embed") return {
    ...(!v2 ? { response_type: "embeddings_by_type" } : {}),
    id: "native-embed", embeddings: { float: [[0.1]] }, texts: ["test"],
    meta: { billed_units: { input_tokens: 100 } },
  };
  if (operation === "rerank") return {
    id: "native-rerank", results: [{ index: 0, relevance_score: 0.9 }],
    meta: { billed_units: { search_units: 1 } },
  };
  return v2 ? {
    id: "native-chat", finish_reason: "COMPLETE", message: { role: "assistant", content: [] },
    usage: { billed_units: billed, tokens: billed },
  } : {
    generation_id: "native-chat", finish_reason: "COMPLETE", text: "test",
    meta: { billed_units: billed },
  };
}

function respond(v2: boolean, operation: Operation, status = 200): void {
  const response = nativeResponse(v2, operation);
  let body = JSON.stringify(status === 200 ? response : { message: "fixture rejected" });
  if (operation === "chatStream" && status === 200) {
    body = v2
      ? `data: ${JSON.stringify({ type: "message-start", id: "native-chat" })}\n\ndata: ${JSON.stringify({ type: "message-end", delta: response })}\n\ndata: [DONE]\n\n`
      : `${JSON.stringify({ event_type: "stream-end", response })}\n`;
  }
  fetchStub.mockImplementation(async () => new Response(body, {
    status,
    headers: { "content-type": operation === "chatStream" ? "text/event-stream" : "application/json", "x-fixture": "native-sdk" },
  }));
}

function request(v2: boolean, operation: Operation): Record<string, unknown> {
  if (operation === "embed") return { model: "embed-v4.0", texts: ["test"], inputType: "search_document", embeddingTypes: ["float"] };
  if (operation === "rerank") return { model: "rerank-v4.0-fast", query: "test", documents: ["test"] };
  return { model: "command-r-08-2024", ...(v2 ? { messages: [{ role: "user", content: "test" }] } : { message: "test" }) };
}

function makeClient(v2: boolean, options: Record<string, unknown> = {}): any {
  const client = new (v2 ? CohereClientV2 : CohereClient)({ token: "no-network-test-token", maxRetries: 0, ...options });
  bindProviderBilling(client, { provider: "cohere", tier: "paid", endpoint: "https://api.cohere.com" });
  return client;
}
function admitted(): boolean {
  return buffer.getAllEvents().some(event => toAttributionObservationV3(event)?.usage.some(line =>
    line.dimensions?.some(dim => dim.key === "provider_billing_lane")));
}

beforeEach(async () => {
  directory = mkdtempSync(join(tmpdir(), "dexcost-cohere-native-"));
  buffer = new EventBuffer(join(directory, "events.db"));
  // Use the real native transport and generated options, but replace the global
  // network boundary so every request is local and no account is contacted.
  fetchStub = vi.fn(() => { throw new Error("unexpected fixture request"); });
  vi.stubGlobal("fetch", fetchStub);
  _setClientClass([CohereClient, CohereClientV2]);
  await instrumentCohere(new PricingEngine(), buffer);
});
afterEach(() => {
  uninstrumentCohere(); _resetClientClass(); vi.unstubAllGlobals();
  buffer.close(); rmSync(directory, { recursive: true, force: true });
});

describe("real cohere-ai 8.1.0 no-network transport", () => {
  it("pins the actual generated SDK shape under test", () => {
    for (const v2 of [false, true]) {
      const client = makeClient(v2);
      const resource = v2 ? client.clientV2 : client;
      // package.json is not a public Cohere subpath. The generated transport's
      // own version metadata verifies the implementation actually under test.
      expect(resource._options.headers).toMatchObject({
        "x-fern-sdk-name": "cohere-ai",
        "x-fern-sdk-version": "8.1.0",
        "user-agent": "cohere-ai/8.1.0",
      });
      for (const operation of operations) {
        expect(typeof resource[operation]).toBe("function");
        expect(typeof client[operation]).toBe("function");
        if (v2) expect(Object.hasOwn(client, operation)).toBe(true);
      }
      if (v2) expect(resource).not.toBe(client);
    }
    expect(fetchStub).not.toHaveBeenCalled();
  });
  for (const v2 of [false, true]) {
    for (const operation of operations) {
      it(`captures normalized native V${v2 ? 2 : 1} ${operation} once on ordinary await`, async () => {
        respond(v2, operation);
        const client = makeClient(v2);
        const normalized = (v2 ? client.clientV2 : client)._options;
        expect(normalized.headers["x-fern-sdk-version"]).toBe("8.1.0");
        const pending = client[operation](request(v2, operation));
        expect(typeof pending.withRawResponse).toBe("function");
        const data = await pending;
        expect(await pending).toBe(data);
        if (operation === "chatStream") for await (const _ of data) { /* exhaust native stream */ }
        expect(fetchStub).toHaveBeenCalledTimes(1);
        expect(buffer.getAllEvents()).toHaveLength(1);
        expect(admitted()).toBe(true);
      });

      it(`preserves V${v2 ? 2 : 1} ${operation}.withRawResponse() without a second request`, async () => {
        respond(v2, operation);
        const pending = makeClient(v2)[operation](request(v2, operation));
        // Extract the helper as applications may do: its native receiver must
        // remain bound, and repeated calls return the same provider data.
        const withRawResponse = pending.withRawResponse;
        const raw = await withRawResponse();
        expect(await withRawResponse()).toBe(raw);
        expect(raw.rawResponse.status).toBe(200);
        expect(raw.rawResponse.headers.get("x-fixture")).toBe("native-sdk");
        if (operation === "chatStream") {
          expect(typeof raw.data[Symbol.asyncIterator]).toBe("function");
          const chunks = [];
          for await (const chunk of raw.data) chunks.push(chunk);
          expect(chunks.length).toBeGreaterThan(0);
          // Native raw-helper stream consumption is intentionally usage-only
          // unsupported; it must not synthesize a completed paid observation.
          expect(admitted()).toBe(false);
        } else {
          expect(await pending).toBe(raw.data);
          expect(buffer.getAllEvents()).toHaveLength(1);
          expect(admitted()).toBe(true);
        }
        expect(fetchStub).toHaveBeenCalledTimes(1);
      });

      it(`does not price caller recovery after V${v2 ? 2 : 1} ${operation} rejects`, async () => {
        respond(v2, operation, 400);
        const pending = makeClient(v2)[operation](request(v2, operation));
        const recovered = { usage: { billedUnits: { inputTokens: 100, outputTokens: 50 } }, finishReason: "COMPLETE" };
        let finalized = false;
        expect(await pending.finally(() => { finalized = true; }).catch(() => recovered)).toBe(recovered);
        expect(finalized).toBe(true);
        await expect(pending.withRawResponse()).rejects.toThrow();
        expect(fetchStub).toHaveBeenCalledTimes(1);
        expect(buffer.getAllEvents()).toHaveLength(1);
        expect(admitted()).toBe(false);
      });
    }
    for (const headers of [{ Authorization: "test-alternate-account" }, { "X-Forwarded-Host": "gateway.example.invalid" }]) {
      it(`rejects override header ${Object.keys(headers)[0]} on real normalized V${v2 ? 2 : 1}`, async () => {
        respond(v2, "chat");
        await makeClient(v2, { headers }).chat(request(v2, "chat"));
        expect(buffer.getAllEvents()).toHaveLength(1);
        expect(admitted()).toBe(false);
      });
    }
  }
});
