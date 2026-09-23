import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { createTask } from "../src/core/models.js";
import { runWithTask } from "../src/core/context.js";
import { runWithCapability } from "../src/core/capabilities.js";
import { idempotencyHash, runWithIdempotencyKey } from "../src/core/idempotency.js";
import {
  instrumentLiteLlm,
  provideLiteLlmModule,
  uninstrumentLiteLlm,
} from "../src/instruments/litellm.js";
import {
  _resetCompletionsClass,
  _setCompletionsClass,
  instrumentOpenai,
  uninstrumentOpenai,
} from "../src/instruments/openai.js";
import { installOpenAIModern, uninstallOpenAIModern } from "../src/instruments/openai-modern.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";

function openRouterResponse(id = "gen-litellm-current") {
  return {
    id,
    model: "openai/gpt-4.1",
    usage: {
      prompt_tokens: 100,
      completion_tokens: 40,
      prompt_tokens_details: { cached_tokens: 20, cache_write_tokens: 5 },
      completion_tokens_details: { reasoning_tokens: 10 },
      cost_details: { upstream_inference_cost: 0.009 },
      server_tool_use: {
        tool_calls_requested: 2,
        tool_calls_executed: 1,
        web_search_requests: 1,
      },
    },
    _hidden_params: {
      custom_llm_provider: "openrouter",
      additional_headers: { "llm_provider-x-litellm-response-cost": 0.0123 },
    },
  };
}

describe("current LiteLLM direct-module attribution", () => {
  let directory: string;
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-litellm-current-"));
    buffer = new EventBuffer(join(directory, "events.db"));
    pricing = new PricingEngine();
  });

  afterEach(() => {
    uninstrumentLiteLlm();
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("preserves exact OpenRouter attribution and every billable usage bucket", async () => {
    const module = { completion: () => openRouterResponse() };
    provideLiteLlmModule(module);
    await instrumentLiteLlm(pricing, buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "litellm.direct" });
    runWithTask(task, () => module.completion());

    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
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
      attribution_operation_name: "litellm.completion",
      attribution_operation_status: "succeeded",
      provider_record_id: "gen-litellm-current",
      provider_reported_cost_usd: "0.0123",
      provider_upstream_cost_usd: "0.009",
      cache_write_input_tokens: 5,
      reasoning_output_tokens: 10,
      attribution_dimensions: [{ key: "gateway", value: { type: "string", value: "litellm" } }],
    });
    expect(event.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      { metric: "input_tokens", quantity: "75", unit: "Tokens" },
      { metric: "cache_read_input_tokens", quantity: "20", unit: "Tokens" },
      { metric: "cache_write_input_tokens", quantity: "5", unit: "Tokens" },
      { metric: "output_tokens", quantity: "30", unit: "Tokens" },
      { metric: "reasoning_output_tokens", quantity: "10", unit: "Tokens" },
      { metric: "server_tool_calls_requested", quantity: "2", unit: "Calls" },
      { metric: "server_tool_calls_executed", quantity: "1", unit: "Calls" },
      { metric: "web_search_requests", quantity: "1", unit: "Requests" },
    ]));
  });

  it.each([
    ["azure", "azure/private-deployment", "gpt-5-mini", "azure_openai", "azure/gpt-5-mini"],
    ["vertex_ai", "vertex_ai/gemini-3-flash", "gemini-3-flash", "google", "vertex_ai/gemini-3-flash"],
    ["cohere", "cohere/command-r", "command-r", "cohere", "command-r"],
    ["together_ai", "together_ai/meta-llama/model", "meta-llama/model", "together", "meta-llama/model"],
    ["moonshot_ai", "moonshot_ai/kimi-k3", "moonshot_ai/kimi-k3", "moonshot", "kimi-k3"],
    ["kimi", "kimi/kimi-k2.6", "kimi/kimi-k2.6", "moonshot", "kimi-k2.6"],
    ["fal_ai", "fal_ai/fal-ai/flux/schnell", "fal-ai/flux/schnell", "fal_ai", "fal_ai/fal-ai/flux/schnell"],
  ])("matches Python identity for %s", async (raw, requested, responseModel, provider, model) => {
    const module = {
      completion: () => ({
        id: `provider-${raw}`,
        model: responseModel,
        usage: { prompt_tokens: 10, completion_tokens: 5 },
        _hidden_params: { custom_llm_provider: raw },
      }),
    };
    provideLiteLlmModule(module);
    await instrumentLiteLlm(pricing, buffer);
    module.completion({ model: requested });
    const event = buffer.getAllEvents()[0];
    expect(event).toMatchObject({ provider, model });
    if (provider === "fal_ai") {
      expect(toAttributionObservationV3(event)?.provider).toEqual({
        name: "fal_ai",
        service: "inference",
        record_id: "provider-fal_ai",
      });
    }
  });

  it("captures the promise-based acompletion surface without changing its result", async () => {
    const response = openRouterResponse("gen-litellm-async");
    const module = { acompletion: async () => response };
    provideLiteLlmModule(module);
    await instrumentLiteLlm(pricing, buffer);

    await expect(module.acompletion()).resolves.toBe(response);
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0]).toMatchObject({
      provider: "openrouter",
      serviceName: "litellm",
      model: "openrouter/openai/gpt-4.1",
      costConfidence: "exact",
    });
  });

  it.each(["failure", "cancellation"])("records stream %s exactly once", async (mode) => {
    const module = {
      completion: () => ({
        *[Symbol.iterator](): Generator<unknown> {
          yield {
            id: `gen-${mode}`,
            model: "openai/gpt-4.1",
            _hidden_params: { custom_llm_provider: "openrouter" },
          };
          if (mode === "failure") throw new Error("LiteLLM stream failed");
          yield openRouterResponse(`gen-${mode}`);
        },
      }),
    };
    provideLiteLlmModule(module);
    await instrumentLiteLlm(pricing, buffer);
    const stream = module.completion({ model: "openrouter/openai/gpt-4.1", stream: true });
    const iterator = stream[Symbol.iterator]();
    iterator.next();
    if (mode === "failure") expect(() => iterator.next()).toThrow("LiteLLM stream failed");
    else iterator.return?.();

    const [event] = buffer.getAllEvents();
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(event.provider).toBe("openrouter");
    expect(event.details.attribution_operation_status).toBe(
      mode === "failure" ? "failed" : "cancelled",
    );
    expect(event.costConfidence).toBe("unknown");
  });

  it("meters LiteLLM image, audio, OCR, embedding, and rerank operations", async () => {
    const module = {
      image_generation: () => ({ id: "img-1", model: "dall-e-3", data: [{ url: "private" }], _hidden_params: { custom_llm_provider: "openai" } }),
      transcription: () => ({ id: "audio-1", duration: 12.5, _hidden_params: { custom_llm_provider: "openai" } }),
      speech: () => ({ id: "speech-1", _hidden_params: { custom_llm_provider: "openai" } }),
      ocr: () => ({ id: "ocr-1", usage_info: { pages_processed: 3, doc_size_bytes: 4096 }, _hidden_params: { custom_llm_provider: "mistral" } }),
      embedding: () => ({ id: "emb-1", model: "text-embedding-3-small", data: [{ embedding: [0.1] }], usage: { prompt_tokens: 19 }, _hidden_params: { custom_llm_provider: "openai" } }),
      rerank: () => ({ id: "rank-1", model: "rerank-v3.5", meta: { billed_units: { search_units: 1, total_tokens: 77 } }, _hidden_params: { custom_llm_provider: "cohere" } }),
    };
    provideLiteLlmModule(module);
    await instrumentLiteLlm(pricing, buffer);
    module.image_generation({ model: "openai/dall-e-3", prompt: "private", n: 1 } as any);
    module.transcription({ model: "openai/whisper-1", file: "private" } as any);
    module.speech({ model: "openai/tts-1", input: "hello" } as any);
    module.ocr({ model: "mistral/ocr-latest", document: "private" } as any);
    module.embedding({ model: "openai/text-embedding-3-small", input: "private" } as any);
    module.rerank({ model: "cohere/rerank-v3.5", query: "private", documents: ["private"] } as any);

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(6);
    expect(events.map((event) => event.details.attribution_usage_lines)).toEqual(expect.arrayContaining([
      [{ metric: "image_count", quantity: "1", unit: "Images" }],
      [{ metric: "audio_seconds", quantity: "12.5", unit: "Seconds" }],
      [{ metric: "characters", quantity: "5", unit: "Characters" }],
      [
        { metric: "page_count", quantity: "3", unit: "Pages" },
        { metric: "document_bytes", quantity: "4096", unit: "Bytes" },
      ],
    ]));
    const embedding = events.find((event) => event.serviceName === "embeddings")!;
    expect(embedding.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      { metric: "input_tokens", quantity: "19", unit: "Tokens" },
      { metric: "embedding_count", quantity: "1", unit: "Embeddings" },
    ]));
    const rerank = events.find((event) => event.serviceName === "rerank")!;
    expect(rerank.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      { metric: "search_units", quantity: "1", unit: "SearchUnits" },
      { metric: "input_tokens", quantity: "77", unit: "Tokens" },
    ]));
    expect(JSON.stringify(events.map((event) => event.details))).not.toContain("private");
  });

  it("reconciles LiteLLM background Responses, video, batch, and fine-tuning jobs", async () => {
    const module = {
      responses: () => ({ id: "resp-1", status: "in_progress", model: "gpt-4.1", _hidden_params: { custom_llm_provider: "openai" } }),
      get_responses: () => ({ id: "resp-1", status: "completed", model: "gpt-4.1", usage: { input_tokens: 20, output_tokens: 5 }, _hidden_params: { custom_llm_provider: "openai" } }),
      video_generation: () => ({ id: "video-1", status: "processing", model: "video-model", _hidden_params: { custom_llm_provider: "openai" } }),
      video_status: () => ({ id: "video-1", status: "completed", model: "video-model", seconds: 8, _hidden_params: { custom_llm_provider: "openai" } }),
      create_batch: () => ({ id: "batch-1", status: "in_progress", _hidden_params: { custom_llm_provider: "openai" } }),
      retrieve_batch: () => ({ id: "batch-1", status: "completed", usage: { input_tokens: 30, output_tokens: 9 }, request_counts: { total: 2, completed: 2, failed: 0 }, _hidden_params: { custom_llm_provider: "openai" } }),
      create_fine_tuning_job: () => ({ id: "ft-1", status: "queued", model: "gpt-4.1-mini", _hidden_params: { custom_llm_provider: "openai" } }),
      retrieve_fine_tuning_job: () => ({ id: "ft-1", status: "succeeded", model: "gpt-4.1-mini", trained_tokens: 1234, _hidden_params: { custom_llm_provider: "openai" } }),
    };
    provideLiteLlmModule(module);
    await instrumentLiteLlm(pricing, buffer);
    module.responses({ model: "openai/gpt-4.1", background: true } as any);
    module.get_responses({ response_id: "resp-1", model: "openai/gpt-4.1" } as any);
    module.video_generation({ model: "openai/video-model", prompt: "private" } as any);
    module.video_status({ video_id: "video-1", model: "openai/video-model" } as any);
    module.create_batch({ endpoint: "/v1/responses", model: "openai/gpt-4.1" } as any);
    module.retrieve_batch({ batch_id: "batch-1", model: "openai/gpt-4.1" } as any);
    module.create_fine_tuning_job({ model: "openai/gpt-4.1-mini" } as any);
    module.retrieve_fine_tuning_job({ fine_tuning_job_id: "ft-1", model: "openai/gpt-4.1-mini" } as any);

    const revisions = buffer.getPendingLedger("provider_job");
    expect(revisions).toHaveLength(8);
    for (const service of ["litellm.responses", "litellm.videos", "litellm.batches", "litellm.fine_tuning"]) {
      const history = revisions.filter((revision) => revision.service === service);
      expect(history).toHaveLength(2);
      expect(history[0].status).toBe("submitted");
      expect(history[1].status).toBe("succeeded");
    }
    expect(revisions.find((revision) => revision.service === "litellm.videos" && revision.revision === 2)?.usage)
      .toEqual(expect.arrayContaining([
        { metric: "output_video_count", quantity: "1", unit: "Videos" },
        { metric: "output_video_seconds", quantity: "8", unit: "Seconds" },
      ]));
    expect(JSON.stringify(revisions)).not.toContain("private");
  });
});

describe("official OpenAI client routed through LiteLLM Proxy", () => {
  let directory: string;
  let buffer: EventBuffer;

  class ProxyCompletions {
    _client = { baseURL: "https://litellm.example.internal/v1" };

    create(_body?: unknown): AsyncIterable<unknown> {
      return {
        async *[Symbol.asyncIterator](): AsyncGenerator<unknown> {
          yield { id: "gen-proxy", model: "openai/gpt-4.1" };
          yield openRouterResponse("gen-proxy");
        },
      };
    }
  }

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-litellm-proxy-"));
    buffer = new EventBuffer(join(directory, "events.db"));
    process.env.DEXCOST_LITELLM_PROXY_URL = "https://litellm.example.internal";
    _setCompletionsClass(ProxyCompletions);
  });

  afterEach(() => {
    uninstrumentOpenai();
    uninstallOpenAIModern();
    _resetCompletionsClass();
    delete process.env.DEXCOST_LITELLM_PROXY_URL;
    delete process.env.LITELLM_PROXY_URL;
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("attributes the upstream provider and keeps invocation-time context through streaming", async () => {
    await instrumentOpenai(new PricingEngine(), buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "litellm.proxy" });
    const stream = runWithTask(task, () => runWithCapability(
      { name: "research", kind: "skill", source: "plugin", sourceId: "crew" },
      () => runWithIdempotencyKey(
        "litellm-proxy-original",
        () => new ProxyCompletions().create({ model: "openrouter/openai/gpt-4.1", stream: true }),
      ),
    ));

    for await (const _chunk of stream) { /* consume the terminal usage */ }

    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
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
      attribution_capability: {
        name: "research",
        kind: "skill",
        source: "plugin",
        source_id: "crew",
      },
      _dexcost_idempotency_sha256: idempotencyHash("litellm-proxy-original"),
      _dexcost_idempotency_occurrence: 0,
      attribution_dimensions: [{ key: "gateway", value: { type: "string", value: "litellm" } }],
    });
    expect(event.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      { metric: "server_tool_calls_requested", quantity: "2", unit: "Calls" },
      { metric: "server_tool_calls_executed", quantity: "1", unit: "Calls" },
      { metric: "web_search_requests", quantity: "1", unit: "Requests" },
    ]));
  });

  it("covers OpenAI-compatible proxy embeddings, media, and durable jobs", async () => {
    class ProxyResource {
      _client = { baseURL: "https://litellm.example.internal/v1" };
    }
    class Embeddings extends ProxyResource {
      async create(body: any): Promise<any> {
        return {
          id: "emb-proxy", model: "text-embedding-3-small", data: [{ embedding: [0.1] }],
          usage: { prompt_tokens: 17 },
          _hidden_params: { custom_llm_provider: "openrouter" },
        };
      }
    }
    class Images extends ProxyResource {
      async generate(): Promise<any> {
        return {
          id: "image-proxy", model: "dall-e-3", data: [{}],
          _hidden_params: { custom_llm_provider: "openrouter" },
        };
      }
    }
    class Responses extends ProxyResource {
      async create(body: any): Promise<any> {
        return {
          id: "resp-proxy", model: body.model, status: "queued",
          _hidden_params: { custom_llm_provider: "openrouter" },
        };
      }
      async retrieve(id: string): Promise<any> {
        return {
          id, model: "openrouter/openai/gpt-4.1", status: "completed",
          usage: { input_tokens: 21, output_tokens: 6 },
          _hidden_params: { custom_llm_provider: "openrouter" },
        };
      }
      async cancel(id: string): Promise<any> { return { id, status: "cancelled" }; }
    }
    class Batches extends ProxyResource {
      async create(): Promise<any> {
        return {
          id: "batch-proxy", status: "validating",
          _hidden_params: { custom_llm_provider: "openrouter" },
        };
      }
      async retrieve(id: string): Promise<any> {
        return {
          id, status: "completed",
          usage: { input_tokens: 30, output_tokens: 9 },
          request_counts: { total: 2, completed: 2, failed: 0 },
          _hidden_params: { custom_llm_provider: "openrouter" },
        };
      }
      async cancel(id: string): Promise<any> { return { id, status: "cancelled" }; }
    }
    class ProxyOpenAI {}
    Object.assign(ProxyOpenAI, { Embeddings, Images, Responses, Batches });

    expect(installOpenAIModern(ProxyOpenAI, new PricingEngine(), buffer)).toBe(true);
    await new Embeddings().create({ model: "openrouter/openai/text-embedding-3-small", input: "private" });
    await new Images().generate({ model: "openrouter/openai/dall-e-3", prompt: "private" });
    const responses = new Responses();
    await responses.create({ model: "openrouter/openai/gpt-4.1", input: "private", background: true });
    await responses.retrieve("resp-proxy");
    const batches = new Batches();
    await batches.create({ input_file_id: "private", endpoint: "/v1/responses", completion_window: "24h" });
    await batches.retrieve("batch-proxy");

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(2);
    expect(events.map((event) => event.serviceName)).toEqual(["litellm", "litellm"]);
    for (const event of events) {
      expect(event.provider).toBe("openrouter");
      expect(event.details.attribution_dimensions).toEqual(expect.arrayContaining([
        { key: "gateway", value: { type: "string", value: "litellm" } },
      ]));
    }
    expect(events[0].details.attribution_usage_lines).toEqual(expect.arrayContaining([
      { metric: "input_tokens", quantity: "17", unit: "Tokens" },
      { metric: "embedding_count", quantity: "1", unit: "Embeddings" },
    ]));
    expect(events[1].details.attribution_usage_lines).toEqual([
      { metric: "image_count", quantity: "1", unit: "Images" },
    ]);

    expect(buffer.getProviderJob("openrouter", "litellm.responses", "resp-proxy"))
      .toMatchObject({ status: "succeeded", revision: 2 });
    expect(buffer.getProviderJob("openrouter", "litellm.batches", "batch-proxy"))
      .toMatchObject({ status: "succeeded", revision: 2 });
    expect(JSON.stringify([...events, ...buffer.getPendingLedger("provider_job")])).not.toContain("private");
  });
});
