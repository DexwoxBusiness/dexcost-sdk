import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { OpenRouter as OfficialOpenRouter } from "@openrouter/sdk";
import { OpenRouter as OfficialAgentOpenRouter } from "@openrouter/agent";
import {
  instrumentOpenRouter,
  provideOpenRouterModule,
  uninstrumentOpenRouter,
} from "../src/instruments/openrouter.js";

class FakeChat {
  async send(request: any): Promise<unknown> {
    const body = request?.chatRequest ?? request ?? {};
    if (body.stream === true) {
      const mode = String(body.streamMode ?? "success");
      return {
        async *[Symbol.asyncIterator](): AsyncGenerator<unknown> {
          yield {
            id: `gen-or-${mode}`,
            model: "openai/gpt-4o",
            usage: { prompt_tokens: 37, completion_tokens: 11, cost: 0.0007 },
          };
          if (mode === "failure") throw new Error("openrouter stream failed");
          if (mode === "cancel") yield { choices: [{ delta: { content: "private-fragment" } }] };
        },
      };
    }
    return {
      id: "gen-or-chat",
      model: "openai/gpt-4o",
      provider: "Fireworks",
      service_tier: "priority",
      usage: {
        prompt_tokens: 100,
        prompt_tokens_details: { cached_tokens: 20 },
        completion_tokens: 20,
        completion_tokens_details: { reasoning_tokens: 5 },
        cost: 0.0012,
        cost_details: { upstream_inference_cost: 0.001 },
        is_byok: true,
        server_tool_use_details: {
          tool_calls_requested: 2, tool_calls_executed: 1, web_search_requests: 1,
        },
      },
    };
  }
}

class FakeResponses {
  async send(request: any): Promise<unknown> {
    return {
      id: "gen-or-response", model: request.responsesRequest.model, status: "completed",
      usage: { input_tokens: 70, output_tokens: 9, cost: 0.004 },
    };
  }
}

class FakeEmbeddings {
  async generate(request: any): Promise<unknown> {
    return {
      id: "gen-or-embedding", model: request.requestBody.model,
      data: [{ embedding: [0.1] }, { embedding: [0.2] }],
      usage: { prompt_tokens: 12, total_tokens: 12, cost: 0.0002 },
    };
  }
}

class FakeImages {
  async generate(request: any): Promise<unknown> {
    return {
      id: "gen-or-image", model: request.imageGenerationRequest.model, data: [{ b64_json: "private" }],
      usage: { prompt_tokens: 3, completion_tokens: 8, cost: 0.04 },
    };
  }
}

class FakeSTT {
  async createTranscriptionMultipart(): Promise<unknown> {
    return {
      id: "gen-or-stt",
      usage: { input_tokens: 8, output_tokens: 2, seconds: 9.2, cost: 0.005 },
    };
  }
}

class FakeTTS {
  async createSpeech(): Promise<unknown> {
    return {
      headers: new Headers({ "x-generation-id": "gen-or-tts" }),
      usage: { cost: 0.006 },
    };
  }
}

class FakeRerank {
  async rerank(request: any): Promise<unknown> {
    return {
      id: "gen-or-rerank", model: request.requestBody.model, provider: "Cohere",
      results: [{ index: 0, relevance_score: 0.9 }],
      usage: { total_tokens: 20, search_units: 1, cost: 0.002 },
    };
  }
}

class FakeVideoGeneration {
  async generate(): Promise<unknown> {
    return { id: "job-or-video", status: "pending", generation_id: "gen-or-video" };
  }
  async getGeneration(jobId: string): Promise<unknown> {
    return {
      id: jobId, status: "completed", generation_id: "gen-or-video",
      unsigned_urls: ["https://private.example/video"],
      usage: { cost: 0.5, is_byok: false },
    };
  }
}

class FakeGenerations {
  async getGeneration(): Promise<unknown> {
    return {
      data: {
        id: "gen-or-chat", model: "openai/gpt-4o", provider_name: "Fireworks",
        native_tokens_prompt: 10, native_tokens_cached: 2,
        native_tokens_completion: 5, native_tokens_reasoning: 2,
        num_media_prompt: 1, num_media_completion: 0,
        num_search_results: 3, num_fetches: 1,
        total_cost: 0.013, upstream_inference_cost: 0.011,
        is_byok: false, data_region: "us", service_tier: "priority", web_search_engine: "exa",
      },
    };
  }
}

class FakeModelResult {
  async getResponse(): Promise<unknown> {
    return {
      id: "gen-or-model",
      model: "anthropic/claude-sonnet-4",
      usage: { input_tokens: 70, output_tokens: 9, cost: 0.004 },
    };
  }
  async getText(): Promise<string> { return "done"; }
}

class FakeOpenRouter {
  get chat(): FakeChat { return new FakeChat(); }
  get responses(): FakeResponses { return new FakeResponses(); }
  get embeddings(): FakeEmbeddings { return new FakeEmbeddings(); }
  get images(): FakeImages { return new FakeImages(); }
  get stt(): FakeSTT { return new FakeSTT(); }
  get tts(): FakeTTS { return new FakeTTS(); }
  get rerank(): FakeRerank { return new FakeRerank(); }
  get videoGeneration(): FakeVideoGeneration { return new FakeVideoGeneration(); }
  get generations(): FakeGenerations { return new FakeGenerations(); }
}

describe("current official OpenRouter attribution", () => {
  let directory: string;
  let buffer: EventBuffer;
  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-openrouter-"));
    buffer = new EventBuffer(join(directory, "events.db"));
    provideOpenRouterModule({ OpenRouter: FakeOpenRouter });
  });
  afterEach(() => {
    uninstrumentOpenRouter();
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("patches lazy official resources created after instrumentation", async () => {
    await instrumentOpenRouter(new PricingEngine(), buffer);
    const response = await new FakeOpenRouter().chat.send({
      chatRequest: { model: "openai/gpt-4o" },
    });
    expect((response as { id: string }).id).toBe("gen-or-chat");
    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
      provider: "openrouter", serviceName: "chat", model: "openrouter/openai/gpt-4o",
      inputTokens: 100, outputTokens: 20, costConfidence: "exact",
    });
    expect(event.details).toMatchObject({
      provider_record_id: "gen-or-chat", provider_reported_cost_usd: "0.0012",
      provider_upstream_cost_usd: "0.001",
    });
    expect(event.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "input_tokens", quantity: "80" }),
      expect.objectContaining({ metric: "cache_read_input_tokens", quantity: "20" }),
      expect.objectContaining({ metric: "output_tokens", quantity: "15" }),
      expect.objectContaining({ metric: "reasoning_output_tokens", quantity: "5" }),
      expect.objectContaining({ metric: "server_tool_calls_requested", quantity: "2" }),
    ]));
    expect(event.details.attribution_dimensions).toEqual(expect.arrayContaining([
      expect.objectContaining({ key: "gateway", value: { type: "string", value: "openrouter" } }),
      expect.objectContaining({ key: "upstream_provider", value: { type: "string", value: "Fireworks" } }),
      expect.objectContaining({ key: "is_byok", value: { type: "string", value: "true" } }),
      expect.objectContaining({ key: "service_tier", value: { type: "string", value: "priority" } }),
    ]));
  });

  it("patches and restores the installed official @openrouter/sdk 1.x surface", async () => {
    const before = new OfficialOpenRouter({ apiKey: "test" }).chat.send;
    provideOpenRouterModule({ OpenRouter: OfficialOpenRouter });
    await instrumentOpenRouter(new PricingEngine(), buffer);
    const during = new OfficialOpenRouter({ apiKey: "test" }).chat.send;
    expect(during).not.toBe(before);
    expect(typeof new OfficialOpenRouter({ apiKey: "test" }).videoGeneration.getGeneration).toBe("function");
    expect(typeof new OfficialOpenRouter({ apiKey: "test" }).generations.getGeneration).toBe("function");
    uninstrumentOpenRouter();
    expect(new OfficialOpenRouter({ apiKey: "test" }).chat.send).toBe(before);
  });

  it("patches and restores an exact installed @openrouter/agent instance", async () => {
    const client = new OfficialAgentOpenRouter({ apiKey: "test" });
    const before = client.callModel;
    provideOpenRouterModule(client);
    await instrumentOpenRouter(new PricingEngine(), buffer);
    expect(client.callModel).not.toBe(before);
    uninstrumentOpenRouter();
    expect(client.callModel).toBe(before);
  });

  it("covers every current official billable resource without retaining payloads", async () => {
    await instrumentOpenRouter(new PricingEngine(), buffer);
    const client = new FakeOpenRouter();
    await client.responses.send({ responsesRequest: { model: "anthropic/claude-sonnet-4", input: "private-response" } });
    await client.embeddings.generate({ requestBody: { model: "openai/text-embedding-3-small", input: "private-embedding" } });
    await client.images.generate({ imageGenerationRequest: { model: "google/gemini-image", prompt: "private-image" } });
    await client.stt.createTranscriptionMultipart({ requestBody: { model: "openai/whisper-large-v3", file: "private-audio" } });
    await client.tts.createSpeech({ requestBody: { model: "openai/gpt-4o-mini-tts", input: "private-speech-input" } });
    await client.rerank.rerank({ requestBody: { model: "cohere/rerank-v3.5", query: "private-query", documents: ["private-document"] } });

    const byService = new Map(buffer.getAllEvents().map((event) => [event.serviceName, event]));
    expect([...byService.keys()]).toEqual(expect.arrayContaining([
      "responses", "embeddings", "image_generation", "speech_to_text", "text_to_speech", "rerank",
    ]));
    expect(byService.get("embeddings")?.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "embedding_count", quantity: "2" }),
    ]));
    expect(byService.get("image_generation")?.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "output_image_count", quantity: "1" }),
    ]));
    expect(byService.get("speech_to_text")?.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "audio_seconds", quantity: "9.2" }),
    ]));
    expect(byService.get("text_to_speech")?.details).toMatchObject({
      provider_record_id: "gen-or-tts", provider_reported_cost_usd: "0.006",
    });
    expect(byService.get("rerank")?.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "total_tokens", quantity: "20" }),
      expect.objectContaining({ metric: "search_units", quantity: "1" }),
      expect.objectContaining({ metric: "result_count", quantity: "1" }),
    ]));
    expect(JSON.stringify(buffer.getAllEvents())).not.toMatch(/private-(response|embedding|image|audio|speech|query|document)/);
  });

  it("retains partial usage for stream success, failure, and early cancellation", async () => {
    await instrumentOpenRouter(new PricingEngine(), buffer);
    const chat = new FakeOpenRouter().chat;
    const succeeded = await chat.send({ chatRequest: { model: "openai/gpt-4o", stream: true } });
    for await (const _chunk of succeeded as AsyncIterable<unknown>) { /* drain */ }

    const failed = await chat.send({
      chatRequest: { model: "openai/gpt-4o", stream: true, streamMode: "failure" },
    });
    await expect(async () => {
      for await (const _chunk of failed as AsyncIterable<unknown>) { /* drain */ }
    }).rejects.toThrow("openrouter stream failed");

    const cancelled = await chat.send({
      chatRequest: { model: "openai/gpt-4o", stream: true, streamMode: "cancel" },
    });
    const iterator = (cancelled as AsyncIterable<unknown>)[Symbol.asyncIterator]();
    await iterator.next();
    await iterator.return?.();

    expect(buffer.getAllEvents()).toHaveLength(3);
    const byStatus = new Map(buffer.getAllEvents().map((event) => [
      event.details.attribution_operation_status, event,
    ]));
    expect([...byStatus.keys()].sort()).toEqual(["cancelled", "failed", "succeeded"]);
    for (const event of byStatus.values()) {
      expect(event).toMatchObject({ inputTokens: 37, outputTokens: 11 });
    }
    expect(byStatus.get("failed")?.details).toMatchObject({
      attribution_error_type: "error", provider_record_id: "gen-or-failure",
    });
    expect(JSON.stringify(buffer.getAllEvents())).not.toContain("private-fragment");
  });

  it("persists and reconciles the asynchronous video lifecycle", async () => {
    await instrumentOpenRouter(new PricingEngine(), buffer);
    const videos = new FakeOpenRouter().videoGeneration;
    await videos.generate({
      videoGenerationRequest: {
        model: "google/veo-3.1", prompt: "private-video", duration: 8,
        resolution: "720p", aspect_ratio: "16:9",
      },
    });
    expect(buffer.getProviderJob("openrouter", "video_generation", "job-or-video")).toMatchObject({
      status: "submitted", revision: 1,
    });
    await videos.getGeneration("job-or-video");
    expect(buffer.getProviderJob("openrouter", "video_generation", "job-or-video")).toMatchObject({
      status: "succeeded", revision: 2, cost_amount: "0.5", cost_source: "provider_reported",
      billing_dimensions: expect.arrayContaining([
        { key: "duration", value: "8" }, { key: "resolution", value: "720p" },
        { key: "aspect_ratio", value: "16:9" },
      ]),
    });
    expect(JSON.stringify(buffer.getPendingLedger("provider_job"))).not.toMatch(/private-video|private\.example/);
  });

  it("reconciles generation metadata to exact disjoint usage and task totals", async () => {
    await instrumentOpenRouter(new PricingEngine(), buffer);
    const client = new FakeOpenRouter();
    await client.chat.send({ chatRequest: { model: "openai/gpt-4o", messages: ["private"] } });
    await client.generations.getGeneration({ id: "gen-or-chat" });

    expect(buffer.getAllEvents()).toHaveLength(1);
    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({ inputTokens: 10, outputTokens: 5, cachedTokens: 2, costConfidence: "exact" });
    expect(event.costUsd.toString()).toBe("0.013");
    expect(event.details).toMatchObject({
      provider_reported_cost_usd: "0.013", provider_upstream_cost_usd: "0.011",
    });
    expect(event.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "input_tokens", quantity: "8" }),
      expect.objectContaining({ metric: "cache_read_input_tokens", quantity: "2" }),
      expect.objectContaining({ metric: "output_tokens", quantity: "3" }),
      expect.objectContaining({ metric: "reasoning_output_tokens", quantity: "2" }),
      expect.objectContaining({ metric: "web_search_result_count", quantity: "3" }),
      expect.objectContaining({ metric: "web_fetch_count", quantity: "1" }),
    ]));
    expect(buffer.getTask(event.taskId)).toMatchObject({
      totalInputTokens: 10, totalOutputTokens: 5, totalCachedTokens: 2,
    });
    expect(buffer.getTask(event.taskId)?.totalCostUsd.toString()).toBe("0.013");
  });

  it("retains compatibility with a mutable high-level callModel client", async () => {
    const agentClient = { callModel: () => new FakeModelResult() };
    provideOpenRouterModule(agentClient);
    await instrumentOpenRouter(new PricingEngine(), buffer);
    const result = agentClient.callModel();
    expect(await result.getText()).toBe("done");
    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
      provider: "openrouter", serviceName: "responses",
      model: "openrouter/anthropic/claude-sonnet-4", inputTokens: 70, outputTokens: 9,
    });
  });
});
