import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import {
  instrumentGroq,
  provideGroqModule,
  uninstrumentGroq,
} from "../src/instruments/groq.js";

class FakeCompletions {
  create(body: any): Promise<any> {
    const response: Record<string, unknown> = {
      id: "groq-native-1",
      model: body.model,
      choices: [{ message: { executed_tools: body.executed_tools ?? [] } }],
      usage: {
        prompt_tokens: 100,
        completion_tokens: 30,
        prompt_tokens_details: { cached_tokens: 20 },
        completion_tokens_details: { reasoning_tokens: 10 },
        total_tokens: 130,
      },
    };
    if (!body.omit_response_service_tier) {
      response.service_tier = body.service_tier ?? "on_demand";
    }
    return Promise.resolve(response);
  }
}

describe("current official Groq attribution", () => {
  let directory: string;
  let buffer: EventBuffer;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-groq-"));
    buffer = new EventBuffer(join(directory, "events.db"));
    provideGroqModule({ Chat: { Completions: FakeCompletions } });
  });

  afterEach(() => {
    uninstrumentGroq();
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("patches the installed official groq-sdk 1.x resource surface", async () => {
    provideGroqModule(undefined);
    await expect(instrumentGroq(new PricingEngine(), buffer)).resolves.toBeUndefined();
  });

  it.each([
    ["on_demand", false, [], "public_sync"],
    ["flex", false, [], "public_sync"],
    ["performance", false, [], undefined],
    ["performance", true, [], undefined],
    ["auto", false, [], undefined],
    ["on_demand", false, [{ type: "browser_search" }], undefined],
  ])("meters native chat while failing open outside the public token lane", async (
    serviceTier,
    omitResponseServiceTier,
    executedTools,
    expectedLane,
  ) => {
    await instrumentGroq(new PricingEngine(), buffer);
    await new FakeCompletions().create({
      model: "openai/gpt-oss-120b",
      service_tier: serviceTier,
      omit_response_service_tier: omitResponseServiceTier,
      executed_tools: executedTools,
      messages: [{ role: "user", content: "private" }],
    });

    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
      provider: "groq",
      serviceName: "chat",
      model: "openai/gpt-oss-120b",
      inputTokens: 100,
      outputTokens: 30,
      cachedTokens: 20,
    });
    expect(event.costUsd.toString()).toBe("0");
    expect(event.details.attribution_usage_lines).toEqual([
      { metric: "input_tokens", quantity: "80", unit: "Tokens" },
      { metric: "output_tokens", quantity: "20", unit: "Tokens" },
      { metric: "cache_read_input_tokens", quantity: "20", unit: "Tokens" },
      { metric: "reasoning_output_tokens", quantity: "10", unit: "Tokens" },
    ]);
    const dimensions = Object.fromEntries(
      (event.details.attribution_dimensions as Array<{ key: string; value: { value: string } }> ?? [])
        .map((item) => [item.key, item.value.value]),
    );
    expect(dimensions).toMatchObject({ gateway: "groq" });
    expect(dimensions.groq_pricing_lane).toBe(expectedLane);
    expect(JSON.stringify(event)).not.toContain("private");
  });
});
