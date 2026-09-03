import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { createTask } from "../src/core/models.js";
import { toAttributionObservationV3 } from "../src/attribution/v3-convert.js";
import { runWithTask, setContext, clearContext } from "../src/core/context.js";
import {
  instrumentOpenai,
  uninstrumentOpenai,
  _setCompletionsClass,
  _resetCompletionsClass,
  _setResponsesClass,
  _resetResponsesClass,
} from "../src/instruments/openai.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

function makeMockResponse(overrides: Record<string, unknown> = {}) {
  return {
    id: "chatcmpl-abc123",
    model: "gpt-4o",
    choices: [{ message: { role: "assistant", content: "Hello!" } }],
    usage: {
      prompt_tokens: 800,
      completion_tokens: 150,
      prompt_tokens_details: { cached_tokens: 50 },
    },
    ...overrides,
  };
}

class FakeCompletions {
  async create(_body: unknown, _options?: unknown): Promise<unknown> {
    return makeMockResponse();
  }
}

describe("OpenAI instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    _setCompletionsClass(FakeCompletions);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentOpenai();
    _resetCompletionsClass();
    _resetResponsesClass();
  });

  it("records llm_call event inside tracked task", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const response = await fake.create({ model: "gpt-4o", messages: [] });
      expect((response as Record<string, unknown>).model).toBe("gpt-4o");
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("openai");
    expect(events[0].model).toBe("gpt-4o");
    expect(events[0].inputTokens).toBe(800);
    expect(events[0].outputTokens).toBe(150);
    expect(events[0].cachedTokens).toBe(50);
    expect(events[0].costUsd.toNumber()).toBeGreaterThan(0);
    expect(events[0].costConfidence).toBe("computed");
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);
  });

  it("routes DeepSeek-compatible OpenAI calls with exact cache usage", async () => {
    class DeepSeekCompletions {
      _client = { baseURL: "https://api.deepseek.com" };

      async create(_body?: unknown): Promise<unknown> {
        return makeMockResponse({
          model: "deepseek-v4-flash",
          usage: {
            prompt_tokens: 20,
            completion_tokens: 10,
            prompt_cache_hit_tokens: 4,
            prompt_cache_miss_tokens: 16,
            completion_tokens_details: { reasoning_tokens: 3 },
          },
        });
      }
    }
    _setCompletionsClass(DeepSeekCompletions);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "deepseek" });

    await runWithTask(task, async () => {
      await new DeepSeekCompletions().create();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "deepseek",
      model: "deepseek-v4-flash",
      inputTokens: 20,
      outputTokens: 10,
      cachedTokens: 4,
    });
    // DeepSeek's scheduled tariff is evaluated by the server catalog.
    expect(events[0].costUsd.toString()).toBe("0");
    const observation = toAttributionObservationV3(events[0]);
    expect(observation?.provider).toMatchObject({ name: "deepseek", service: "api" });
    expect(observation?.provider.record_id).toBe("chatcmpl-abc123");
    expect(observation?.resource).toEqual({ type: "model", id: "deepseek-v4-flash" });
    expect(Object.fromEntries(observation?.usage.map((line) => [
      line.metric,
      line.quantity,
    ]) ?? [])).toEqual({
      input_tokens: "16",
      cache_read_input_tokens: "4",
      output_tokens: "7",
      reasoning_output_tokens: "3",
    });
  });

  it.each([
    "https://api.moonshot.ai/v1",
    "https://api.moonshot.cn/v1",
  ])("routes Moonshot-compatible OpenAI calls with exact cache usage for %s", async (baseURL) => {
    class MoonshotCompletions {
      _client = { baseURL };

      async create(): Promise<unknown> {
        return makeMockResponse({
          model: "kimi-k3",
          usage: {
            prompt_tokens: 20,
            completion_tokens: 10,
            cached_tokens: 4,
          },
        });
      }
    }
    _setCompletionsClass(MoonshotCompletions);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "moonshot" });

    await runWithTask(task, async () => {
      await new MoonshotCompletions().create();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "moonshot",
      model: "kimi-k3",
      inputTokens: 20,
      outputTokens: 10,
      cachedTokens: 4,
    });
    expect(events[0].costUsd.toString()).toBe("0");
    const observation = toAttributionObservationV3(events[0]);
    expect(observation?.provider).toMatchObject({ name: "moonshot", service: "api" });
    expect(observation?.resource).toEqual({ type: "model", id: "kimi-k3" });
    expect(Object.fromEntries(observation?.usage.map((line) => [
      line.metric,
      line.quantity,
    ]) ?? [])).toEqual({
      input_tokens: "16",
      cache_read_input_tokens: "4",
      output_tokens: "10",
    });
  });

  it.each([
    ["https://api.fireworks.ai/inference/v1", undefined, "default"],
    ["https://api.fireworks.ai/inference/v1", "priority", "priority"],
    ["https://us.api.fireworks.ai/inference/v1", "standard", "default"],
  ])("routes Fireworks calls and normalizes the serving tier", async (baseURL, tier, expectedTier) => {
    const model = "accounts/fireworks/models/kimi-k3";
    class FireworksCompletions {
      _client = { baseURL };

      async create(_body?: unknown): Promise<unknown> {
        return makeMockResponse({ model });
      }
    }
    _setCompletionsClass(FireworksCompletions);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "fireworks" });

    await runWithTask(task, async () => {
      await new FireworksCompletions().create({
        model,
        messages: [],
        ...(tier === undefined ? {} : { service_tier: tier }),
      });
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ provider: "fireworks_ai", model });
    expect(events[0].costUsd.toString()).toBe("0");
    const observation = toAttributionObservationV3(events[0]);
    expect(observation?.provider).toMatchObject({ name: "fireworks_ai", service: "api" });
    expect(observation?.resource).toEqual({ type: "model", id: model });
    expect(Object.fromEntries(observation?.usage[0]?.dimensions.map((item) => [
      item.key,
      item.value.value,
    ]) ?? [])).toMatchObject({ service_tier: expectedTier });
  });

  it.each([
    [0, "grok-4.6", "grok-4.6", "priority_short"],
    [1, "grok-4-1-fast-reasoning", "grok-4.3", undefined],
  ])("captures xAI exact cost and only admits tool-free catalog lanes", async (
    toolCount,
    reportedModel,
    expectedModel,
    expectedLane,
  ) => {
    class XaiCompletions {
      _client = { baseURL: "https://api.x.ai/v1" };

      async create(_body?: unknown): Promise<unknown> {
        return makeMockResponse({
          model: reportedModel,
          service_tier: "priority",
          usage: {
            prompt_tokens: 800,
            completion_tokens: 150,
            prompt_tokens_details: { cached_tokens: 50 },
            cost_in_usd_ticks: 12_345_678,
            num_server_side_tools_used: toolCount,
          },
        });
      }
    }
    _setCompletionsClass(XaiCompletions);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "xai" });

    await runWithTask(task, async () => {
      await new XaiCompletions().create({ model: reportedModel, messages: [] });
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "xai",
      model: expectedModel,
      costConfidence: "exact",
      pricingSource: "provider_response",
    });
    expect(events[0].costUsd.toString()).toBe("0.0012345678");
    const observation = toAttributionObservationV3(events[0]);
    expect(observation?.provider).toMatchObject({ name: "xai", service: "api" });
    expect(observation?.cost_evidence).toEqual({
      amount: "0.0012345678",
      currency: "USD",
      source: "provider_reported",
      confidence: "exact",
    });
    const dimensions = Object.fromEntries(observation?.usage[0]?.dimensions.map((item) => [
      item.key,
      item.value.value,
    ]) ?? []);
    expect(dimensions.xai_pricing_lane).toBe(expectedLane);
  });

  it.each([
    ["on_demand", "on_demand", [], "public_sync"],
    ["flex", "flex", [], "public_sync"],
    ["performance", "performance", [], undefined],
    ["performance", undefined, [], undefined],
    ["auto", "auto", [], undefined],
    ["on_demand", "on_demand", [{ type: "browser_search" }], undefined],
  ])("routes Groq calls only into the public synchronous pricing lane", async (
    requestTier,
    responseTier,
    executedTools,
    expectedLane,
  ) => {
    class GroqCompletions {
      _client = { baseURL: "https://api.groq.com/openai/v1" };

      async create(_body?: unknown): Promise<unknown> {
        return makeMockResponse({
          model: "openai/gpt-oss-120b",
          service_tier: responseTier,
          choices: [{ message: { executed_tools: executedTools } }],
        });
      }
    }
    _setCompletionsClass(GroqCompletions);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "groq" });

    await runWithTask(task, async () => {
      await new GroqCompletions().create({
        model: "openai/gpt-oss-120b",
        service_tier: requestTier,
        messages: [],
      });
    });

    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({ provider: "groq", model: "openai/gpt-oss-120b" });
    const observation = toAttributionObservationV3(event);
    expect(observation?.provider).toMatchObject({ name: "groq", service: "api" });
    const dimensions = Object.fromEntries(observation?.usage[0]?.dimensions.map((item) => [
      item.key,
      item.value.value,
    ]) ?? []);
    expect(dimensions.groq_pricing_lane).toBe(expectedLane);
  });

  it.each([
    ["standard", "global_standard"],
    ["priority", undefined],
    [undefined, undefined],
  ])("routes Mistral calls only into the confirmed global Standard lane", async (
    responseTier,
    expectedLane,
  ) => {
    class MistralCompletions {
      _client = { baseURL: "https://api.mistral.ai/v1" };

      async create(_body?: unknown): Promise<unknown> {
        return makeMockResponse({
          model: "mistral-large-latest",
          usage: {
            prompt_tokens: 20,
            completion_tokens: 10,
            prompt_tokens_details: { cached_tokens: 4 },
            completion_tokens_details: { reasoning_tokens: 3 },
            ...(responseTier === undefined ? {} : { service_tier: responseTier }),
          },
        });
      }
    }
    _setCompletionsClass(MistralCompletions);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "mistral" });

    await runWithTask(task, async () => {
      await new MistralCompletions().create({
        model: "mistral-large-latest",
        messages: [],
      });
    });

    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
      provider: "mistral",
      model: "mistral-large-2512",
      inputTokens: 20,
      outputTokens: 10,
      cachedTokens: 4,
    });
    expect(event.costUsd.toString()).toBe("0");
    const observation = toAttributionObservationV3(event);
    expect(observation?.provider).toMatchObject({ name: "mistral", service: "api" });
    expect(observation?.provider.record_id).toBe("chatcmpl-abc123");
    expect(observation?.resource).toEqual({ type: "model", id: "mistral-large-2512" });
    expect(Object.fromEntries(observation?.usage.map((line) => [
      line.metric,
      line.quantity,
    ]) ?? [])).toEqual({
      input_tokens: "16",
      cache_read_input_tokens: "4",
      output_tokens: "7",
      reasoning_output_tokens: "3",
    });
    const dimensions = Object.fromEntries(observation?.usage[0]?.dimensions.map((item) => [
      item.key,
      item.value.value,
    ]) ?? []);
    expect(dimensions.mistral_pricing_lane).toBe(expectedLane);
  });

  it.each(["https://api.together.ai/v1", "https://api.together.xyz/v1"])(
    "routes Together OpenAI-compatible calls with disjoint token usage: %s",
    async (baseURL) => {
      const model = "deepseek-ai/DeepSeek-V4-Pro-0813";
      class TogetherCompletions {
        _client = { baseURL };

        async create(_body?: unknown): Promise<unknown> {
          return makeMockResponse({
            model,
            usage: {
              prompt_tokens: 20,
              completion_tokens: 10,
              cached_tokens: 4,
              reasoning_tokens: 3,
            },
          });
        }
      }
      _setCompletionsClass(TogetherCompletions);
      await instrumentOpenai(pricing, buffer);
      const task = createTask({ taskId: randomUUID(), taskType: "together" });

      await runWithTask(task, async () => {
        await new TogetherCompletions().create({ model, messages: [] });
      });

      const [event] = buffer.getAllEvents();
      expect(event).toMatchObject({
        provider: "together",
        model,
        inputTokens: 20,
        outputTokens: 10,
        cachedTokens: 4,
      });
      expect(event.costUsd.toString()).toBe("0");
      const observation = toAttributionObservationV3(event);
      expect(observation?.provider).toEqual({
        name: "together",
        service: "api",
        record_id: "chatcmpl-abc123",
      });
      expect(observation?.resource).toEqual({ type: "model", id: model });
      expect(Object.fromEntries(observation?.usage.map((line) => [
        line.metric,
        line.quantity,
      ]) ?? [])).toEqual({
        input_tokens: "16",
        cache_read_input_tokens: "4",
        output_tokens: "7",
        reasoning_output_tokens: "3",
      });
    },
  );

  it("records into an auto-task when no task and no context set", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();

    const response = await fake.create({ model: "gpt-4o", messages: [] });
    expect((response as Record<string, unknown>).model).toBe("gpt-4o");
    // LLM costs are never silently lost — an auto-task is created.
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);
    expect(buffer.getAllTasks().some((t) => t.taskType === "openai.chat")).toBe(true);
  });

  it("creates auto-task when setContext is set but no explicit task", async () => {
    setContext({ customerId: "auto-test" });
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();

    const response = await fake.create({ model: "gpt-4o", messages: [] });
    expect((response as Record<string, unknown>).model).toBe("gpt-4o");

    const events = buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("openai.chat");

    clearContext();
  });

  it("aggregates cost into task", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create({ model: "gpt-4o", messages: [] });
    });

    expect(task.llmCostUsd.toNumber()).toBeGreaterThan(0);
    expect(task.totalCostUsd.toNumber()).toBeGreaterThan(0);
    expect(task.totalInputTokens).toBe(800);
    expect(task.totalOutputTokens).toBe(150);
    expect(task.totalCachedTokens).toBe(50);
  });

  it("handles missing usage gracefully", async () => {
    class NoUsageCompletions {
      async create(): Promise<unknown> {
        return { id: "chatcmpl-abc", model: "gpt-4o", choices: [] };
      }
    }
    _setCompletionsClass(NoUsageCompletions);
    await instrumentOpenai(pricing, buffer);
    const fake = new NoUsageCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].costUsd.toNumber()).toBe(0);
    expect(events[0].costConfidence).toBe("estimated");
    expect(events[0].inputTokens).toBe(0);
    expect(events[0].outputTokens).toBe(0);
  });

  it("restores original after uninstrument", async () => {
    const originalCreate = FakeCompletions.prototype.create;
    await instrumentOpenai(pricing, buffer);
    expect(FakeCompletions.prototype.create).not.toBe(originalCreate);

    uninstrumentOpenai();
    expect(FakeCompletions.prototype.create).toBe(originalCreate);
  });

  it("does not double-patch", async () => {
    await instrumentOpenai(pricing, buffer);
    const patchedCreate = FakeCompletions.prototype.create;
    await instrumentOpenai(pricing, buffer);
    expect(FakeCompletions.prototype.create).toBe(patchedCreate);
  });

  it("records latency in milliseconds", async () => {
    await instrumentOpenai(pricing, buffer);
    const fake = new FakeCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fake.create({ model: "gpt-4o", messages: [] });
    });

    const events = buffer.getAllEvents();
    expect(events[0].latencyMs).toBeDefined();
    expect(typeof events[0].latencyMs).toBe("number");
  });

  it("records disjoint Luna usage from the Responses API", async () => {
    class FakeResponses {
      async create(): Promise<unknown> {
        return {
          id: "resp_luna_123",
          model: "gpt-5.6-luna",
          usage: {
            input_tokens: 2_600,
            input_tokens_details: {
              cached_tokens: 2_000,
              cache_write_tokens: 400,
            },
            output_tokens: 300,
            output_tokens_details: { reasoning_tokens: 120 },
          },
        };
      }
    }
    _setResponsesClass(FakeResponses);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await new FakeResponses().create({ model: "gpt-5.6-luna" });
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      provider: "openai",
      model: "gpt-5.6-luna",
      inputTokens: 2_600,
      outputTokens: 300,
      cachedTokens: 2_000,
      costConfidence: "unknown",
    });
    expect(events[0].details).toMatchObject({
      provider_record_id: "resp_luna_123",
      cache_write_input_tokens: 400,
      reasoning_output_tokens: 120,
    });

    expect(toAttributionObservationV3(events[0])?.usage).toEqual([
      expect.objectContaining({ metric: "input_tokens", quantity: "200" }),
      expect.objectContaining({ metric: "cache_read_input_tokens", quantity: "2000" }),
      expect.objectContaining({ metric: "cache_write_input_tokens", quantity: "400" }),
      expect.objectContaining({ metric: "output_tokens", quantity: "180" }),
      expect.objectContaining({ metric: "reasoning_output_tokens", quantity: "120" }),
    ]);
  });

  it("keeps invalid provider usage durable but unexportable", async () => {
    class InvalidResponses {
      async create(): Promise<unknown> {
        return {
          id: "resp_invalid",
          model: "gpt-5.6-luna",
          usage: {
            input_tokens: 10,
            input_tokens_details: { cached_tokens: 8, cache_write_tokens: 4 },
            output_tokens: 1,
          },
        };
      }
    }
    _setResponsesClass(InvalidResponses);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await new InvalidResponses().create({ model: "gpt-5.6-luna" });
    });

    const [event] = buffer.getAllEvents();
    expect(event.costConfidence).toBe("unknown");
    expect(event.details.openai_usage_error).toBe(
      "cache token buckets exceed total input tokens",
    );
    expect(toAttributionObservationV3(event)).toBeNull();
  });
});

describe("OpenAI streaming instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
  });

  afterEach(() => {
    buffer.close();
    uninstrumentOpenai();
    _resetCompletionsClass();
    _resetResponsesClass();
  });

  it("records event after stream completes", async () => {
    const chunks = [
      { model: "gpt-4o", choices: [{ delta: { content: "Hello" } }] },
      { model: "gpt-4o", choices: [{ delta: { content: " world" } }] },
      {
        model: "gpt-4o",
        choices: [{ delta: {} }],
        usage: { prompt_tokens: 100, completion_tokens: 20 },
      },
    ];

    class StreamingCompletions {
      async create(body: Record<string, unknown>): Promise<unknown> {
        if (body.stream) {
          return {
            async *[Symbol.asyncIterator]() {
              for (const chunk of chunks) yield chunk;
            },
          };
        }
        return makeMockResponse();
      }
    }

    _setCompletionsClass(StreamingCompletions);
    await instrumentOpenai(pricing, buffer);
    const fake = new StreamingCompletions();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = await fake.create({
        model: "gpt-4o",
        messages: [],
        stream: true,
      });
      const received: unknown[] = [];
      for await (const chunk of stream as AsyncIterable<unknown>) {
        received.push(chunk);
      }
      expect(received).toHaveLength(3);
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].model).toBe("gpt-4o");
    expect(events[0].inputTokens).toBe(100);
    expect(events[0].outputTokens).toBe(20);
  });

  it("records a Responses completed stream event", async () => {
    class StreamingResponses {
      async create(): Promise<unknown> {
        return {
          async *[Symbol.asyncIterator]() {
            yield { type: "response.output_text.delta", delta: "Hello" };
            yield {
              type: "response.completed",
              response: {
                id: "resp_stream_luna",
                model: "gpt-5.6-luna",
                usage: {
                  input_tokens: 1_300,
                  input_tokens_details: {
                    cached_tokens: 1_000,
                    cache_write_tokens: 100,
                  },
                  output_tokens: 90,
                  output_tokens_details: { reasoning_tokens: 30 },
                },
              },
            };
          },
        };
      }
    }

    _setCompletionsClass(FakeCompletions);
    _setResponsesClass(StreamingResponses);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = await new StreamingResponses().create({
        model: "gpt-5.6-luna",
        stream: true,
      });
      for await (const _chunk of stream as AsyncIterable<unknown>) {
        // drain
      }
    });

    const [event] = buffer.getAllEvents();
    expect(event).toMatchObject({
      model: "gpt-5.6-luna",
      inputTokens: 1_300,
      outputTokens: 90,
      cachedTokens: 1_000,
    });
    expect(event.details).toMatchObject({
      provider_record_id: "resp_stream_luna",
      cache_write_input_tokens: 100,
      reasoning_output_tokens: 30,
    });
  });

  it("retains partial usage and failure identity when the stream raises", async () => {
    class FailingCompletions {
      async create(_body?: unknown): Promise<unknown> {
        return {
          async *[Symbol.asyncIterator]() {
            yield {
              id: "chatcmpl-partial",
              model: "gpt-4o",
              usage: { prompt_tokens: 37, completion_tokens: 11 },
            };
            throw new Error("openai stream failed");
          },
        };
      }
    }
    _setCompletionsClass(FailingCompletions);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await expect(runWithTask(task, async () => {
      const stream = await new FailingCompletions().create({ model: "gpt-4o", stream: true });
      for await (const _chunk of stream as AsyncIterable<unknown>) { /* drain */ }
    })).rejects.toThrow("openai stream failed");

    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0]).toMatchObject({ inputTokens: 37, outputTokens: 11 });
    expect(buffer.getAllEvents()[0].details).toMatchObject({
      attribution_operation_status: "failed",
      attribution_error_type: "error",
      provider_record_id: "chatcmpl-partial",
    });
  });

  it("records early stream close as cancelled exactly once", async () => {
    class CancelledCompletions {
      async create(_body?: unknown): Promise<unknown> {
        return {
          async *[Symbol.asyncIterator]() {
            yield {
              id: "chatcmpl-cancelled",
              model: "gpt-4o",
              usage: { prompt_tokens: 29, completion_tokens: 7 },
            };
            yield { choices: [] };
          },
        };
      }
    }
    _setCompletionsClass(CancelledCompletions);
    await instrumentOpenai(pricing, buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      const stream = await new CancelledCompletions().create({ model: "gpt-4o", stream: true });
      const iterator = (stream as AsyncIterable<unknown>)[Symbol.asyncIterator]();
      await iterator.next();
      await iterator.return?.();
    });

    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(buffer.getAllEvents()[0].details.attribution_operation_status).toBe("cancelled");
    expect(buffer.getAllEvents()[0]).toMatchObject({ inputTokens: 29, outputTokens: 7 });
  });
});
