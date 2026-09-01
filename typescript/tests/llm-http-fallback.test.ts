/**
 * Tests for the HTTP-level LLM fallback — capture of LLM calls that the
 * module-level instruments cannot intercept (ESM-only `ai` package,
 * Vercel AI SDK providers issuing raw fetch, BYOK "…-compatible" vendors).
 *
 * Regression focus: Anthropic-compatible endpoints mounted under a base-path
 * prefix (Kimi/Moonshot `https://api.kimi.com/anthropic` → request path
 * `/anthropic/v1/messages`) were previously missed by the prefix-only
 * endpoint match and degraded to a generic `network` event.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { Buffer } from "node:buffer";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { runWithTask, clearContext } from "../src/core/context.js";
import { runWithCapability } from "../src/core/capabilities.js";
import { idempotencyHash, runWithIdempotencyKey } from "../src/core/idempotency.js";
import { createTask } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import {
  trackHttp,
  untrackHttp,
  clearDomainRates,
  clearRecordedEvents,
  getRecordedEvents,
  resetServiceCatalog,
} from "../src/adapters/http.js";

let tmpDir: string;
let buffer: EventBuffer;
let pricing: PricingEngine;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-llmfb-test-"));
  buffer = new EventBuffer(join(tmpDir, "test.db"));
  pricing = new PricingEngine();
  clearDomainRates();
  clearRecordedEvents();
  untrackHttp();
  resetServiceCatalog();
  clearContext();
});

afterEach(() => {
  untrackHttp();
  clearDomainRates();
  clearRecordedEvents();
  resetServiceCatalog();
  clearContext();
  delete process.env.DEXCOST_LITELLM_PROXY_URL;
  delete process.env.LITELLM_PROXY_URL;
  vi.unstubAllGlobals();
  buffer.close();
  rmSync(tmpDir, { recursive: true, force: true });
});

/** Anthropic Messages API response body with usage. */
function anthropicJsonResponse(model = "kimi-k2-0905-preview"): Response {
  return new Response(
    JSON.stringify({
      id: "msg_01",
      type: "message",
      model,
      content: [{ type: "text", text: "hi" }],
      usage: { input_tokens: 1200, output_tokens: 340 },
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

describe("LLM HTTP fallback — anthropic-compatible base-path prefixes", () => {
  it("captures POST api.kimi.com/anthropic/v1/messages as llm_call (kodus/Kimi regression)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(anthropicJsonResponse()));
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://api.kimi.com/anthropic/v1/messages", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: "kimi-k2-0905-preview", messages: [] }),
      });
      await res.text();
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].provider).toBe("api.kimi.com");
    expect(llmEvents[0].model).toBe("kimi-k2-0905-preview");
    expect(llmEvents[0].inputTokens).toBe(1200);
    expect(llmEvents[0].outputTokens).toBe(340);
    expect(task.totalInputTokens).toBe(1200);
    expect(task.totalOutputTokens).toBe(340);
    // The call must NOT degrade to a network event.
    expect(buffer.getAllEvents().filter((e) => e.eventType === "network")).toHaveLength(0);
    // The llm_call replaces the network event for this call, so it must
    // carry the full byte picture (request AND response side).
    expect(typeof llmEvents[0].details?.request_bytes).toBe("number");
    expect(llmEvents[0].details?.response_bytes as number).toBeGreaterThan(0);
  });

  it("captures @ai-sdk/anthropic style path (baseURL + /messages, no /v1)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(anthropicJsonResponse()));
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://api.kimi.com/anthropic/messages", {
        method: "POST",
        body: "{}",
      });
      await res.text();
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].inputTokens).toBe(1200);
  });

  it("captures unknown gateway/proxy hosts by path shape alone", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(anthropicJsonResponse("claude-sonnet-4-20250514")),
    );
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://llm-gateway.internal.example.com/anthropic/v1/messages", {
        method: "POST",
        body: "{}",
      });
      await res.text();
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].provider).toBe("llm-gateway.internal.example.com");
    expect(llmEvents[0].model).toBe("claude-sonnet-4-20250514");
  });

  it("captures OpenAI-compatible prefixed paths (openrouter /api/v1/chat/completions)", async () => {
    const body = {
      id: "gen-1",
      model: "deepseek/deepseek-chat",
      choices: [],
      usage: { prompt_tokens: 900, completion_tokens: 150 },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://openrouter.ai/api/v1/chat/completions", {
        method: "POST",
        body: "{}",
      });
      await res.text();
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].inputTokens).toBe(900);
    expect(llmEvents[0].outputTokens).toBe(150);
  });

  it("captures a raw OpenAI Responses JSON payload", async () => {
    const body = {
      id: "resp-json-1",
      model: "gpt-5.6-luna",
      output: [],
      usage: { input_tokens: 320, output_tokens: 48 },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "responses-json" });
    await runWithTask(task, async () => {
      const response = await fetch("https://api.openai.com/v1/responses", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: body.model, input: "private" }),
      });
      await response.text();
    });

    const events = buffer.getAllEvents().filter((event) => event.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "api.openai.com",
      model: "gpt-5.6-luna",
      inputTokens: 320,
      outputTokens: 48,
    });
    expect(buffer.getAllEvents().filter((event) => event.eventType === "network"))
      .toHaveLength(0);
  });

  it("captures nested usage from a raw OpenAI Responses SSE completion", async () => {
    const generatedOutput = "x".repeat(20_000);
    const sse = `event: response.completed\ndata: ${JSON.stringify({
      type: "response.completed",
      response: {
        id: "resp-stream-1",
        model: "gpt-5.6-luna",
        output: [{
          type: "message",
          content: [{ type: "output_text", text: generatedOutput }],
        }],
        usage: { input_tokens: 640, output_tokens: 96 },
      },
    })}\n\n`;
    expect(Buffer.byteLength(sse, "utf-8")).toBeGreaterThan(16_384);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(sse, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    })));
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "responses-stream" });
    await runWithTask(task, async () => {
      const response = await fetch("https://api.openai.com/v1/responses", {
        method: "POST",
        body: JSON.stringify({ model: "gpt-5.6-luna", input: "private", stream: true }),
      });
      await response.text();
    });

    const events = buffer.getAllEvents().filter((event) => event.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "api.openai.com",
      model: "gpt-5.6-luna",
      inputTokens: 640,
      outputTokens: 96,
    });
    expect(events[0].details?.source).toBe("http_llm_fallback_stream");
  });

  it("attributes raw DeepSeek calls and preserves its cache-hit bucket", async () => {
    const body = {
      id: "chat-deepseek-1",
      model: "deepseek-v4-pro",
      choices: [],
      usage: {
        prompt_tokens: 20,
        completion_tokens: 10,
        prompt_cache_hit_tokens: 4,
        prompt_cache_miss_tokens: 16,
        completion_tokens_details: { reasoning_tokens: 3 },
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer, pricing);
    const task = createTask({ taskId: randomUUID(), taskType: "deepseek" });

    await runWithTask(task, async () => {
      const response = await fetch("https://api.deepseek.com/chat/completions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: "deepseek-v4-pro", messages: [] }),
      });
      await response.text();
    });

    const events = buffer.getAllEvents().filter((event) => event.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "deepseek",
      model: "deepseek-v4-pro",
      inputTokens: 20,
      outputTokens: 10,
      cachedTokens: 4,
    });
    expect(events[0].details?.attribution_usage_lines).toEqual([
      { metric: "input_tokens", quantity: "16", unit: "Tokens" },
      { metric: "output_tokens", quantity: "7", unit: "Tokens" },
      { metric: "cache_read_input_tokens", quantity: "4", unit: "Tokens" },
      { metric: "reasoning_output_tokens", quantity: "3", unit: "Tokens" },
    ]);
    expect(events[0].costUsd.toString()).toBe("0");
  });

  it.each([
    ["https://api.fireworks.ai/inference/v1/chat/completions", undefined, "default"],
    ["https://us.api.fireworks.ai/inference/v1/chat/completions", "priority", "priority"],
    ["https://api.fireworks.ai/inference/v1/chat/completions", "standard", "default"],
  ])("attributes raw Fireworks calls with an exact serving tier", async (url, tier, expectedTier) => {
    const model = "accounts/fireworks/models/kimi-k3";
    const body = {
      id: "chat-fireworks-1",
      model,
      choices: [],
      usage: {
        prompt_tokens: 20,
        completion_tokens: 10,
        prompt_tokens_details: { cached_tokens: 4 },
        completion_tokens_details: { reasoning_tokens: 3 },
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer, pricing);
    const task = createTask({ taskId: randomUUID(), taskType: "fireworks" });

    await runWithTask(task, async () => {
      const response = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          model,
          messages: [],
          ...(tier === undefined ? {} : { service_tier: tier }),
        }),
      });
      await response.text();
    });

    const events = buffer.getAllEvents().filter((event) => event.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "fireworks_ai",
      model,
      inputTokens: 20,
      outputTokens: 10,
      cachedTokens: 4,
    });
    expect(events[0].costUsd.toString()).toBe("0");
    expect(events[0].details?.attribution_dimensions).toEqual([
      { key: "service_tier", value: { type: "string", value: expectedTier } },
    ]);
  });

  it.each([
    [0, "default_short"],
    [2, undefined],
  ])("captures raw xAI exact cost and fails open for server tools", async (toolCount, expectedLane) => {
    const body = {
      id: "chat-xai-1",
      model: "grok-4.3",
      service_tier: "default",
      choices: [],
      usage: {
        prompt_tokens: 20,
        completion_tokens: 10,
        prompt_tokens_details: { cached_tokens: 4 },
        cost_in_usd_ticks: 12_345_678,
        num_server_side_tools_used: toolCount,
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer, pricing);
    const task = createTask({ taskId: randomUUID(), taskType: "xai" });

    await runWithTask(task, async () => {
      const response = await fetch("https://api.x.ai/v1/chat/completions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: "grok-4.3", messages: [] }),
      });
      await response.text();
    });

    const events = buffer.getAllEvents().filter((event) => event.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "xai",
      model: "grok-4.3",
      inputTokens: 20,
      outputTokens: 10,
      cachedTokens: 4,
      costConfidence: "exact",
      pricingSource: "provider_response",
    });
    expect(events[0].costUsd.toString()).toBe("0.0012345678");
    const dimensions = Object.fromEntries(
      (events[0].details?.attribution_dimensions as Array<{ key: string; value: { value: string } }> ?? [])
        .map((item) => [item.key, item.value.value]),
    );
    expect(dimensions.xai_pricing_lane).toBe(expectedLane);
  });

  it.each([
    ["on_demand", "on_demand", [], "public_sync"],
    ["performance", "performance", [], undefined],
    ["performance", undefined, [], undefined],
    ["auto", "auto", [], undefined],
    ["on_demand", "on_demand", [{ type: "code_interpreter" }], undefined],
  ])("captures raw Groq calls only in the public token lane", async (
    requestTier,
    responseTier,
    executedTools,
    expectedLane,
  ) => {
    const body = {
      id: "chat-groq-1",
      model: "openai/gpt-oss-120b",
      service_tier: responseTier,
      choices: [{ message: { executed_tools: executedTools } }],
      usage: {
        prompt_tokens: 20,
        completion_tokens: 10,
        prompt_tokens_details: { cached_tokens: 4 },
        completion_tokens_details: { reasoning_tokens: 3 },
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer, pricing);
    const task = createTask({ taskId: randomUUID(), taskType: "groq" });

    await runWithTask(task, async () => {
      const response = await fetch("https://api.groq.com/openai/v1/chat/completions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: body.model, service_tier: requestTier, messages: [] }),
      });
      await response.text();
    });

    const events = buffer.getAllEvents().filter((event) => event.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ provider: "groq", model: body.model });
    const dimensions = Object.fromEntries(
      (events[0].details?.attribution_dimensions as Array<{ key: string; value: { value: string } }> ?? [])
        .map((item) => [item.key, item.value.value]),
    );
    expect(dimensions.groq_pricing_lane).toBe(expectedLane);
  });

  it.each([
    ["standard", "global_standard"],
    ["priority", undefined],
    [undefined, undefined],
  ])("captures raw Mistral calls only in the confirmed global Standard lane", async (
    responseTier,
    expectedLane,
  ) => {
    const body = {
      id: "chat-mistral-1",
      model: "mistral-large-latest",
      choices: [],
      usage: {
        prompt_tokens: 20,
        completion_tokens: 10,
        prompt_tokens_details: { cached_tokens: 4 },
        completion_tokens_details: { reasoning_tokens: 3 },
        ...(responseTier === undefined ? {} : { service_tier: responseTier }),
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer, pricing);
    const task = createTask({ taskId: randomUUID(), taskType: "mistral" });

    await runWithTask(task, async () => {
      const response = await fetch("https://api.mistral.ai/v1/chat/completions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: body.model, messages: [] }),
      });
      await response.text();
    });

    const events = buffer.getAllEvents().filter((event) => event.eventType === "llm_call");
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "mistral",
      model: "mistral-large-2512",
      inputTokens: 20,
      outputTokens: 10,
      cachedTokens: 4,
    });
    expect(events[0].costUsd.toString()).toBe("0");
    expect(events[0].details?.attribution_usage_lines).toEqual([
      { metric: "input_tokens", quantity: "16", unit: "Tokens" },
      { metric: "output_tokens", quantity: "7", unit: "Tokens" },
      { metric: "cache_read_input_tokens", quantity: "4", unit: "Tokens" },
      { metric: "reasoning_output_tokens", quantity: "3", unit: "Tokens" },
    ]);
    const dimensions = Object.fromEntries(
      (events[0].details?.attribution_dimensions as Array<{ key: string; value: { value: string } }> ?? [])
        .map((item) => [item.key, item.value.value]),
    );
    expect(dimensions.mistral_pricing_lane).toBe(expectedLane);
  });

  it("captures anthropic-compatible SSE streaming responses via the stream fallback", async () => {
    const sse = [
      `event: message_start\ndata: ${JSON.stringify({
        type: "message_start",
        message: { model: "kimi-k2-0905-preview", usage: { input_tokens: 800, output_tokens: 1 } },
      })}\n\n`,
      `event: content_block_delta\ndata: ${JSON.stringify({
        type: "content_block_delta",
        delta: { type: "text_delta", text: "hello" },
      })}\n\n`,
      `event: message_delta\ndata: ${JSON.stringify({
        type: "message_delta",
        usage: { output_tokens: 220 },
      })}\n\n`,
    ].join("");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(sse, {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
      ),
    );
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://api.kimi.com/anthropic/v1/messages", {
        method: "POST",
        body: "{}",
      });
      await res.text(); // drain the stream so finalisation runs
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].model).toBe("kimi-k2-0905-preview");
    expect(llmEvents[0].inputTokens).toBe(800);
    expect(llmEvents[0].outputTokens).toBe(220);
    expect(llmEvents[0].details?.source).toBe("http_llm_fallback_stream");
  });
});

describe("LLM HTTP fallback — Gemini / Vertex format", () => {
  function geminiJsonResponse(withModelVersion = true): Response {
    return new Response(
      JSON.stringify({
        candidates: [{ content: { parts: [{ text: "hi" }] } }],
        ...(withModelVersion ? { modelVersion: "gemini-2.5-pro" } : {}),
        usageMetadata: {
          promptTokenCount: 900,
          candidatesTokenCount: 150,
          thoughtsTokenCount: 50,
          totalTokenCount: 1100,
        },
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }

  it("captures generativelanguage.googleapis.com generateContent (usageMetadata)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(geminiJsonResponse()));
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
        { method: "POST", body: "{}" },
      );
      await res.text();
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].model).toBe("gemini-2.5-pro");
    expect(llmEvents[0].inputTokens).toBe(900);
    // Thinking tokens billed as output: 150 + 50.
    expect(llmEvents[0].outputTokens).toBe(200);
  });

  it("captures Vertex regional hosts by path shape, model from the URL when body omits it", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(geminiJsonResponse(false)));
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch(
        "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent",
        { method: "POST", body: "{}" },
      );
      await res.text();
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].provider).toBe("us-central1-aiplatform.googleapis.com");
    expect(llmEvents[0].model).toBe("gemini-2.5-flash");
    expect(llmEvents[0].inputTokens).toBe(900);
  });

  it("captures Gemini SSE streaming (streamGenerateContent?alt=sse)", async () => {
    const sse = [
      `data: ${JSON.stringify({
        candidates: [{ content: { parts: [{ text: "hel" }] } }],
        modelVersion: "gemini-2.5-pro",
        usageMetadata: { promptTokenCount: 900, candidatesTokenCount: 3 },
      })}\n\n`,
      `data: ${JSON.stringify({
        candidates: [{ content: { parts: [{ text: "lo" }] } }],
        modelVersion: "gemini-2.5-pro",
        usageMetadata: { promptTokenCount: 900, candidatesTokenCount: 150, thoughtsTokenCount: 50 },
      })}\n\n`,
    ].join("");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } }),
      ),
    );
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse",
        { method: "POST", body: "{}" },
      );
      await res.text();
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].model).toBe("gemini-2.5-pro");
    expect(llmEvents[0].inputTokens).toBe(900);
    expect(llmEvents[0].outputTokens).toBe(200); // last chunk authoritative
  });
});

describe("LLM HTTP fallback — configured LiteLLM Proxy", () => {
  const proxyUrl = "https://litellm.example.internal/v1/chat/completions";
  const request = {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "openrouter/openai/gpt-4.1", messages: [] }),
  };
  const payload = {
    id: "gen-litellm-fetch",
    model: "openai/gpt-4.1",
    choices: [],
    usage: {
      prompt_tokens: 100,
      completion_tokens: 40,
      prompt_tokens_details: { cached_tokens: 20, cache_write_tokens: 5 },
      completion_tokens_details: { reasoning_tokens: 10 },
      cost: 0.0123,
      cost_details: { upstream_inference_cost: 0.009 },
      server_tool_use: {
        tool_calls_requested: 2,
        tool_calls_executed: 1,
        web_search_requests: 1,
      },
    },
  };

  beforeEach(() => {
    process.env.DEXCOST_LITELLM_PROXY_URL = "https://litellm.example.internal";
  });

  it("records exact routed attribution for raw JSON fetch", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer, pricing);
    const task = createTask({ taskId: randomUUID(), taskType: "litellm.fetch" });

    await runWithTask(task, () => runWithCapability(
      { name: "crew-research", kind: "workflow", invocation: "automatic" },
      () => runWithIdempotencyKey("litellm-fetch-original", async () => {
        const response = await fetch(proxyUrl, request);
        await response.json();
      }),
    ));

    const [event] = buffer.getAllEvents();
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(event).toMatchObject({
      eventType: "llm_call",
      provider: "openrouter",
      serviceName: "litellm",
      model: "openrouter/openai/gpt-4.1",
      inputTokens: 100,
      outputTokens: 40,
      cachedTokens: 20,
      costConfidence: "exact",
      pricingSource: "provider_response",
    });
    expect(event.costUsd.toString()).toBe("0.0123");
    expect(event.details).toMatchObject({
      attribution_operation_name: "litellm.chat.create",
      attribution_operation_status: "succeeded",
      provider_reported_cost_usd: "0.0123",
      provider_upstream_cost_usd: "0.009",
      cache_write_input_tokens: 5,
      reasoning_output_tokens: 10,
      attribution_capability: {
        name: "crew-research", kind: "workflow", invocation: "automatic",
      },
      _dexcost_idempotency_sha256: idempotencyHash("litellm-fetch-original"),
      _dexcost_idempotency_occurrence: 0,
      attribution_dimensions: [{ key: "gateway", value: { type: "string", value: "litellm" } }],
    });
    expect(event.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      { metric: "cache_read_input_tokens", quantity: "20", unit: "Tokens" },
      { metric: "cache_write_input_tokens", quantity: "5", unit: "Tokens" },
      { metric: "reasoning_output_tokens", quantity: "10", unit: "Tokens" },
      { metric: "server_tool_calls_requested", quantity: "2", unit: "Calls" },
      { metric: "server_tool_calls_executed", quantity: "1", unit: "Calls" },
      { metric: "web_search_requests", quantity: "1", unit: "Requests" },
    ]));
  });

  it("preserves the provider rejection while recording a failure observation", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("proxy unavailable")));
    trackHttp(buffer, pricing);
    const task = createTask({ taskId: randomUUID(), taskType: "litellm.fetch.failure" });

    await expect(runWithTask(task, () => fetch(proxyUrl, request))).rejects.toThrow("proxy unavailable");

    const [event] = buffer.getAllEvents();
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(event).toMatchObject({
      provider: "openrouter",
      serviceName: "litellm",
      model: "openrouter/openai/gpt-4.1",
      costConfidence: "unknown",
    });
    expect(event.details).toMatchObject({
      attribution_operation_name: "litellm.chat.create",
      attribution_operation_status: "failed",
      attribution_error_type: "typeerror",
      attribution_usage_lines: [{ metric: "request_count", quantity: "1", unit: "Requests" }],
    });
  });

  it.each(["failed", "cancelled"] as const)("records raw SSE stream %s exactly once", async (mode) => {
    const encoder = new TextEncoder();
    let pulled = false;
    const providerStream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!pulled) {
          pulled = true;
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
          return;
        }
        if (mode === "failed") controller.error(new Error("LiteLLM proxy stream failed"));
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(providerStream, {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    })));
    trackHttp(buffer, pricing);
    const task = createTask({ taskId: randomUUID(), taskType: `litellm.fetch.${mode}` });
    const response = await runWithTask(task, () => fetch(proxyUrl, request));
    const reader = response.body!.getReader();
    await reader.read();
    if (mode === "failed") {
      await expect(reader.read()).rejects.toThrow("LiteLLM proxy stream failed");
    } else {
      await reader.cancel("caller stopped");
    }
    await Promise.resolve();

    const [event] = buffer.getAllEvents();
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(event).toMatchObject({
      provider: "openrouter",
      serviceName: "litellm",
      model: "openrouter/openai/gpt-4.1",
      costConfidence: "exact",
    });
    expect(event.details.attribution_operation_status).toBe(mode);
  });
});

describe("LLM HTTP fallback — false-positive guards", () => {
  it("ignores non-POST requests to message-like paths", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(anthropicJsonResponse()));
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://api.kimi.com/anthropic/v1/messages"); // GET
      await res.text();
    });

    expect(buffer.getAllEvents().filter((e) => e.eventType === "llm_call")).toHaveLength(0);
  });

  it("does not emit llm_call for unknown hosts whose usage shape does not match", async () => {
    // A chat-history style API that happens to live under /messages and
    // carries a differently-shaped `usage` object.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ items: [], usage: { credits: 3 } }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://api.somechatapp.example.com/v2/messages", {
        method: "POST",
        body: "{}",
      });
      await res.text();
    });

    expect(buffer.getAllEvents().filter((e) => e.eventType === "llm_call")).toHaveLength(0);
  });

  it("usage-less JSON on an LLM-looking path still emits a network event when large", async () => {
    // Regression: _tryExtractLlmFromResponse drains the wrapped body via
    // clone().json() BEFORE the placeholder event exists. Finalisation used
    // to run once at that moment, find no placeholder, and bail — so the
    // fall-through path never re-typed the placeholder and large usage-less
    // calls lost their network event entirely.
    const bigBody = JSON.stringify({ items: "x".repeat(150_000) });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(bigBody, {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://api.somechatapp.example.com/v2/messages", {
        method: "POST",
        body: "{}",
      });
      await res.text();
    });

    const events = buffer.getAllEvents();
    expect(events.filter((e) => e.eventType === "llm_call")).toHaveLength(0);
    const network = events.filter((e) => e.eventType === "network");
    expect(network).toHaveLength(1);
    expect(network[0].details?.cost_pending).toBe(true);
    expect(network[0].details?.response_bytes as number).toBeGreaterThan(100_000);
  });

  it("usage-less JSON on an LLM-looking path leaves no phantom $0 event when small", async () => {
    // Same race, small-body variant: the too-late placeholder was never
    // dropped from the in-memory recorded-events list.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ items: [], usage: { credits: 3 } }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://api.somechatapp.example.com/v2/messages", {
        method: "POST",
        body: "{}",
      });
      await res.text();
    });

    expect(buffer.getAllEvents()).toHaveLength(0);
    // The in-memory placeholder must be dropped, not leaked as a $0
    // external_cost phantom.
    expect(
      getRecordedEvents().filter(
        (e) => e.details?.url === "https://api.somechatapp.example.com/v2/messages",
      ),
    ).toHaveLength(0);
  });

  it("still captures canonical non-prefixed endpoints on known hosts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(anthropicJsonResponse("claude-sonnet-4-20250514")),
    );
    trackHttp(buffer, pricing);

    const task = createTask({ taskId: randomUUID(), taskType: "review" });
    await runWithTask(task, async () => {
      const res = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        body: "{}",
      });
      await res.text();
    });

    const llmEvents = buffer.getAllEvents().filter((e) => e.eventType === "llm_call");
    expect(llmEvents).toHaveLength(1);
    expect(llmEvents[0].model).toBe("claude-sonnet-4-20250514");
  });
});
