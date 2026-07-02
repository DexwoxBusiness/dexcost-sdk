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
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { runWithTask, clearContext } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import {
  trackHttp,
  untrackHttp,
  clearDomainRates,
  clearRecordedEvents,
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
