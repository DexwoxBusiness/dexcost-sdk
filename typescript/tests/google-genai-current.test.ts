import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import {
  instrumentGoogleGenAI,
  provideGoogleGenAIModule,
  uninstrumentGoogleGenAI,
} from "../src/instruments/google-genai.js";

function currentClient() {
  return {
    vertexai: false,
    models: {
      generateContent: async (body: any) => ({
        responseId: "google-response-1",
        modelVersion: body.model,
        usageMetadata: {
          promptTokenCount: 100,
          cachedContentTokenCount: 20,
          candidatesTokenCount: 30,
          thoughtsTokenCount: 5,
          toolUsePromptTokenCount: 4,
        },
      }),
      generateVideos: async () => ({ name: "operations/video-1", done: false }),
    },
    operations: {
      getVideosOperation: async () => ({
        name: "operations/video-1",
        done: true,
        response: { generatedVideos: [{ video: { uri: "gs://redacted" } }] },
      }),
    },
    batches: {
      create: async (body: any) => ({ name: "batches/1", model: body.model, state: "JOB_STATE_PENDING" }),
      get: async () => ({
        name: "batches/1", model: "gemini-2.5-flash", state: "JOB_STATE_SUCCEEDED",
        completionStats: { successfulCount: 2 },
      }),
    },
  };
}

describe("current @google/genai attribution", () => {
  let directory: string;
  let buffer: EventBuffer;
  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-google-genai-"));
    buffer = new EventBuffer(join(directory, "events.db"));
  });
  afterEach(() => {
    uninstrumentGoogleGenAI();
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("captures disjoint current usage metadata from an exact client instance", async () => {
    const client = currentClient();
    provideGoogleGenAIModule(client);
    await instrumentGoogleGenAI(new PricingEngine(), buffer);
    await client.models.generateContent({ model: "gemini-2.5-flash", contents: "not retained" });
    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
      provider: "google", serviceName: "gemini",
      inputTokens: 104, outputTokens: 35, cachedTokens: 20,
    });
    expect(event.details.attribution_usage_lines).toEqual(expect.arrayContaining([
      expect.objectContaining({ metric: "input_tokens", quantity: "80" }),
      expect.objectContaining({ metric: "cache_read_input_tokens", quantity: "20" }),
      expect.objectContaining({ metric: "tool_input_tokens", quantity: "4" }),
      expect.objectContaining({ metric: "reasoning_output_tokens", quantity: "5" }),
    ]));
  });

  it("tracks video and batch provider-job lifecycles", async () => {
    const client = currentClient();
    provideGoogleGenAIModule(client);
    await instrumentGoogleGenAI(new PricingEngine(), buffer);
    await client.models.generateVideos({
      model: "veo-3.1", source: { prompt: "not retained" }, config: { durationSeconds: 8 },
    });
    expect(buffer.getProviderJob("google", "gemini", "operations/video-1")?.status).toBe("submitted");
    await client.operations.getVideosOperation({ operation: { name: "operations/video-1" } });
    expect(buffer.getProviderJob("google", "gemini", "operations/video-1")).toMatchObject({
      status: "succeeded", revision: 2,
    });

    await client.batches.create({ model: "gemini-2.5-flash", src: "gs://redacted" });
    await client.batches.get({ name: "batches/1" });
    expect(buffer.getProviderJob("google", "gemini", "batches/1")).toMatchObject({
      status: "succeeded", revision: 2,
    });
  });
});
