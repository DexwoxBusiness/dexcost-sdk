import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import {
  instrumentOpenRouter,
  provideOpenRouterModule,
  uninstrumentOpenRouter,
} from "../src/instruments/openrouter.js";

class FakeChat {
  async send(): Promise<unknown> {
    return {
      id: "gen-or-chat",
      model: "openai/gpt-4o",
      usage: { prompt_tokens: 100, completion_tokens: 20, cost: 0.0012 },
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
  private readonly resource = new FakeChat();
  get chat(): FakeChat { return this.resource; }
  callModel(): FakeModelResult { return new FakeModelResult(); }
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
    });
  });

  it("attributes high-level callModel when its reusable result is consumed", async () => {
    await instrumentOpenRouter(new PricingEngine(), buffer);
    const result = new FakeOpenRouter().callModel({ model: "anthropic/claude-sonnet-4" });
    expect(await result.getText()).toBe("done");
    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
      provider: "openrouter", serviceName: "responses",
      model: "openrouter/anthropic/claude-sonnet-4", inputTokens: 70, outputTokens: 9,
    });
  });
});
