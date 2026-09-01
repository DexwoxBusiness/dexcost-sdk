import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { provideInstrumentModule } from "../src/instruments/index.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import {
  instrumentOpenai,
  uninstrumentOpenai,
  _resetCompletionsClass,
  _resetResponsesClass,
  _resetOpenAIModule,
} from "../src/instruments/openai.js";

class RichPromise<T> implements PromiseLike<T> {
  constructor(private readonly value: T) {}
  then<TResult1 = T, TResult2 = never>(
    fulfilled?: ((value: T) => TResult1 | PromiseLike<TResult1>) | null,
    rejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): Promise<TResult1 | TResult2> {
    return Promise.resolve(this.value).then(fulfilled, rejected);
  }
  asResponse(): Promise<Response> { return Promise.resolve(new Response("ok")); }
  withResponse(): Promise<{ data: T; response: Response }> {
    return Promise.resolve({ data: this.value, response: new Response("ok") });
  }
}

class ChatCompletions {
  create(body: any): RichPromise<any> {
    return new RichPromise({
      id: "chat-current", model: body.model,
      usage: { prompt_tokens: 20, completion_tokens: 5 },
    });
  }
}
class Chat {}
(Chat as any).Completions = ChatCompletions;

class Responses {
  create(body: any): RichPromise<any> {
    if (body.background) return new RichPromise({ id: "resp-bg", model: body.model, status: "queued" });
    return new RichPromise({
      id: "resp-tools", model: body.model,
      usage: { input_tokens: 100, output_tokens: 20 },
      output: [
        { type: "web_search_call", query: "private" },
        { type: "web_search_call", query: "private-2" },
        { type: "file_search_call", results: ["private"] },
        { type: "code_interpreter_call", code: "private" },
        { type: "image_generation_call", result: "private-base64" },
        { type: "mcp_call", arguments: "private" },
      ],
    });
  }
  retrieve(id: string): RichPromise<any> {
    return new RichPromise({
      id, model: "gpt-5-mini", status: "completed",
      usage: { input_tokens: 50, output_tokens: 7 },
    });
  }
  cancel(id: string): RichPromise<any> { return new RichPromise({ id, status: "cancelled" }); }
}
class Embeddings {
  create(body: any): RichPromise<any> {
    return new RichPromise({
      id: "emb-1", model: body.model, data: [{ embedding: [0.1] }],
      usage: { prompt_tokens: 12, total_tokens: 12 },
    });
  }
}
class Images {
  generate(body: any): RichPromise<any> {
    return new RichPromise({
      model: body.model, data: [{}],
      usage: {
        input_tokens: 30, input_tokens_details: { text_tokens: 20, image_tokens: 10 },
        output_tokens: 100, output_tokens_details: { image_tokens: 100 },
      },
    });
  }
  edit(body: any): RichPromise<any> { return this.generate(body); }
  createVariation(body: any): RichPromise<any> { return this.generate(body); }
}
class Speech { create(): RichPromise<Response> { return new RichPromise(new Response("audio")); } }
class Transcriptions {
  create(): RichPromise<any> {
    return new RichPromise({ duration: 60, usage: { type: "duration", seconds: 60 } });
  }
}
class Translations extends Transcriptions {}
class Audio {}
(Audio as any).Speech = Speech;
(Audio as any).Transcriptions = Transcriptions;
(Audio as any).Translations = Translations;
class Moderations { create(body: any): RichPromise<any> { return new RichPromise({ id: "mod-1", model: body.model }); } }

class Batches {
  create(): RichPromise<any> { return new RichPromise({ id: "batch-1", status: "validating" }); }
  retrieve(id: string): RichPromise<any> {
    return new RichPromise({
      id, status: "completed", model: "gpt-5-mini",
      usage: {
        input_tokens: 100, input_tokens_details: { cached_tokens: 20 },
        output_tokens: 50, output_tokens_details: { reasoning_tokens: 10 },
      },
      request_counts: { total: 3, completed: 2, failed: 1 },
    });
  }
  cancel(id: string): RichPromise<any> { return new RichPromise({ id, status: "cancelled" }); }
}
class FineTuningJobs {
  create(body: any): RichPromise<any> { return new RichPromise({ id: "ft-1", model: body.model, status: "queued" }); }
  retrieve(id: string): RichPromise<any> {
    return new RichPromise({ id, model: "gpt-4.1-mini", status: "succeeded", trained_tokens: 900 });
  }
  cancel(id: string): RichPromise<any> { return new RichPromise({ id, status: "cancelled" }); }
  pause(id: string): RichPromise<any> { return new RichPromise({ id, status: "paused" }); }
  resume(id: string): RichPromise<any> { return new RichPromise({ id, status: "running" }); }
}
class FineTuning {}
(FineTuning as any).Jobs = FineTuningJobs;
class Videos {
  create(body: any): RichPromise<any> {
    return new RichPromise({ id: "video-1", model: body.model, status: "queued", seconds: body.seconds });
  }
  retrieve(id: string): RichPromise<any> {
    return new RichPromise({ id, model: "sora-2", status: "completed", seconds: 8 });
  }
  cancel(id: string): RichPromise<any> { return new RichPromise({ id, status: "cancelled" }); }
}

class FakeOpenAI {}
Object.assign(FakeOpenAI, {
  Chat, Responses, Embeddings, Images, Audio, Moderations, Batches, FineTuning, Videos,
});

describe("current official OpenAI TypeScript surface", () => {
  let directory: string;
  let buffer: EventBuffer;
  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-openai-current-"));
    buffer = new EventBuffer(join(directory, "events.db"));
    provideInstrumentModule("openai", FakeOpenAI);
  });
  afterEach(() => {
    uninstrumentOpenai();
    _resetCompletionsClass();
    _resetResponsesClass();
    _resetOpenAIModule();
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("preserves APIPromise helpers and emits built-in tool meters without content", async () => {
    await instrumentOpenai(new PricingEngine(), buffer);
    const chat = new ChatCompletions().create({ model: "gpt-4o" });
    expect(typeof chat.asResponse).toBe("function");
    expect(typeof chat.withResponse).toBe("function");
    await chat;
    await new Responses().create({ model: "gpt-5-mini", tools: [{ type: "web_search" }] });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(7);
    const operations = new Map(events.map((event) => [
      event.details.attribution_operation_name, event.details.attribution_usage_lines,
    ]));
    expect(operations.get("openai.responses.web_search")).toEqual([
      expect.objectContaining({ metric: "web_search_calls", quantity: "2" }),
    ]);
    expect(operations.get("openai.responses.file_search")).toEqual([
      expect.objectContaining({ metric: "file_search_calls", quantity: "1" }),
    ]);
    expect(operations.get("openai.responses.container")).toEqual([
      expect.objectContaining({ metric: "container_reference_count", quantity: "1" }),
    ]);
    expect(operations.get("openai.responses.image_generation")).toEqual([
      expect.objectContaining({ metric: "output_image_count", quantity: "1" }),
    ]);
    expect(operations.get("openai.responses.mcp")).toEqual([
      expect.objectContaining({ metric: "mcp_tool_calls", quantity: "1" }),
    ]);
    expect(JSON.stringify(events)).not.toContain("private");
  });

  it("covers embeddings, image, transcription, speech, and moderation meters", async () => {
    const originalImage = Images.prototype.generate;
    await instrumentOpenai(new PricingEngine(), buffer);
    expect(Images.prototype.generate).not.toBe(originalImage);
    await new Embeddings().create({ model: "text-embedding-3-small", input: "private" });
    await new Images().generate({ model: "gpt-image-1", prompt: "private" });
    await new Transcriptions().create({ model: "whisper-1", file: "private" });
    await new Speech().create({ model: "tts-1", input: "do not retain" });
    await new Moderations().create({ model: "omni-moderation-latest", input: "private" });

    const byService = new Map(buffer.getAllEvents().map((event) => [event.serviceName, event]));
    expect([...byService.keys()]).toEqual(expect.arrayContaining([
      "embeddings", "images", "speech_to_text", "text_to_speech", "moderations",
    ]));
    expect(byService.get("embeddings")?.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "embedding_count", quantity: "1" }),
    ]));
    expect(byService.get("images")?.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "input_image_tokens", quantity: "10" }),
      expect.objectContaining({ metric: "output_image_tokens", quantity: "100" }),
      expect.objectContaining({ metric: "image_count", quantity: "1" }),
    ]));
    expect(byService.get("speech_to_text")?.details.attribution_usage_lines).toEqual([
      expect.objectContaining({ metric: "audio_seconds", quantity: "60" }),
    ]);
    expect(byService.get("text_to_speech")?.details.attribution_usage_lines).toEqual([
      expect.objectContaining({ metric: "characters", quantity: "13" }),
    ]);
    expect(JSON.stringify(buffer.getAllEvents())).not.toContain("do not retain");
  });

  it("keeps Fireworks embedding provider and resource identity unprefixed", async () => {
    class FireworksEmbeddings extends Embeddings {
      _client = { baseURL: "https://api.fireworks.ai/inference/v1" };
    }
    const model = "accounts/fireworks/models/qwen3-embedding-8b";
    await instrumentOpenai(new PricingEngine(), buffer);
    await new FireworksEmbeddings().create({ model, input: "private embedding input" });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "fireworks_ai",
      model,
      serviceName: "embeddings",
    });
    expect(events[0].costUsd.toString()).toBe("0");
    const observation = toAttributionObservationV3(events[0]);
    expect(observation?.provider).toMatchObject({
      name: "fireworks_ai",
      service: "embeddings",
    });
    expect(observation?.resource).toEqual({ type: "model", id: model });
    expect(observation?.usage).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "input_tokens", quantity: "12" }),
    ]));
    expect(JSON.stringify(events)).not.toContain("private embedding input");
  });

  it("reconciles Responses, batch, fine-tuning, and video jobs", async () => {
    await instrumentOpenai(new PricingEngine(), buffer);
    const responses = new Responses();
    await responses.create({ model: "gpt-5-mini", input: "private", background: true });
    await responses.retrieve("resp-bg");
    const batches = new Batches();
    await batches.create({ input_file_id: "private", endpoint: "/v1/responses", completion_window: "24h" });
    await batches.retrieve("batch-1");
    const tuning = new FineTuningJobs();
    await tuning.create({ model: "gpt-4.1-mini", training_file: "private" });
    await tuning.retrieve("ft-1");
    const videos = new Videos();
    await videos.create({ model: "sora-2", prompt: "private", seconds: 8 });
    await videos.retrieve("video-1");

    expect(buffer.getProviderJob("openai", "responses", "resp-bg")).toMatchObject({ status: "succeeded", revision: 2 });
    expect(buffer.getProviderJob("openai", "batches", "batch-1")).toMatchObject({ status: "succeeded", revision: 2 });
    expect(buffer.getProviderJob("openai", "fine_tuning", "ft-1")).toMatchObject({ status: "succeeded", revision: 2 });
    expect(buffer.getProviderJob("openai", "videos", "video-1")).toMatchObject({ status: "succeeded", revision: 2 });
    expect(JSON.stringify(buffer.getPendingLedger())).not.toContain("private");
  });
});
