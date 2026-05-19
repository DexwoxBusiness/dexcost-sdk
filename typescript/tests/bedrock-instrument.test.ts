import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { createTask } from "../src/core/models.js";
import { runWithTask, setContext, clearContext } from "../src/core/context.js";
import {
  instrumentBedrock,
  uninstrumentBedrock,
  _setClientClass,
  _resetClientClass,
} from "../src/instruments/bedrock.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-test-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

/** Simulates an InvokeModelCommand — the instrument checks constructor.name. */
class InvokeModelCommand {
  input: { modelId: string; body?: string };
  constructor(input: { modelId: string; body?: string }) {
    this.input = input;
  }
}

function makeMockResponse(bodyObj: Record<string, unknown> = {}) {
  const defaultBody = {
    usage: { input_tokens: 100, output_tokens: 50 },
    content: [{ text: "Hello" }],
  };
  return {
    body: new TextEncoder().encode(
      JSON.stringify({ ...defaultBody, ...bodyObj }),
    ),
  };
}

class FakeBedrockClient {
  async send(command: unknown, ..._rest: unknown[]): Promise<unknown> {
    void command;
    return makeMockResponse();
  }
}

describe("Bedrock instrumentation", () => {
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    buffer = new EventBuffer(join(tmpDir, "test.db"));
    pricing = new PricingEngine();
    _setClientClass(FakeBedrockClient);
  });

  afterEach(() => {
    buffer.close();
    uninstrumentBedrock();
    _resetClientClass();
  });

  it("records llm_call event with provider=aws-bedrock and model from input", async () => {
    await instrumentBedrock(pricing, buffer);
    const client = new FakeBedrockClient();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });
    const command = new InvokeModelCommand({
      modelId: "anthropic.claude-3-sonnet-20240229-v1:0",
    });

    await runWithTask(task, async () => {
      const response = await client.send(command);
      expect(response).toBeDefined();
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].eventType).toBe("llm_call");
    expect(events[0].provider).toBe("aws-bedrock");
    expect(events[0].model).toBe("anthropic.claude-3-sonnet-20240229-v1:0");
    expect(events[0].inputTokens).toBe(100);
    expect(events[0].outputTokens).toBe(50);
    expect(events[0].latencyMs).toBeGreaterThanOrEqual(0);
  });

  it("parses Anthropic response format correctly from Uint8Array body", async () => {
    class AnthropicBedrockClient {
      async send(_command: unknown): Promise<unknown> {
        return {
          body: new TextEncoder().encode(
            JSON.stringify({
              usage: { input_tokens: 300, output_tokens: 120 },
              content: [{ text: "Response from Claude on Bedrock" }],
            }),
          ),
        };
      }
    }
    _setClientClass(AnthropicBedrockClient);
    await instrumentBedrock(pricing, buffer);
    const client = new AnthropicBedrockClient();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });
    const command = new InvokeModelCommand({
      modelId: "anthropic.claude-3-haiku-20240307-v1:0",
    });

    await runWithTask(task, async () => {
      await client.send(command);
    });

    const events = buffer.getAllEvents();
    expect(events).toHaveLength(1);
    expect(events[0].inputTokens).toBe(300);
    expect(events[0].outputTokens).toBe(120);
  });

  it("records into an auto-task when no task and no context set", async () => {
    await instrumentBedrock(pricing, buffer);
    const client = new FakeBedrockClient();
    const command = new InvokeModelCommand({
      modelId: "anthropic.claude-3-sonnet-20240229-v1:0",
    });

    const response = await client.send(command);
    expect(response).toBeDefined();
    // LLM costs are never silently lost — an auto-task is created.
    expect(buffer.getAllEvents().length).toBeGreaterThanOrEqual(1);
    expect(
      buffer.getAllTasks().some((t) => t.taskType === "bedrock.invokeModel"),
    ).toBe(true);
  });

  it("creates auto-task when setContext is set but no explicit task", async () => {
    setContext({ customerId: "auto-bedrock-test" });
    await instrumentBedrock(pricing, buffer);
    const client = new FakeBedrockClient();
    const command = new InvokeModelCommand({
      modelId: "anthropic.claude-3-sonnet-20240229-v1:0",
    });

    const response = await client.send(command);
    expect(response).toBeDefined();

    const events = buffer.getAllEvents();
    expect(events.length).toBeGreaterThanOrEqual(1);

    const tasks = buffer.getAllTasks();
    const autoTask = tasks.find((t) => t.customerId === "auto-bedrock-test");
    expect(autoTask).toBeDefined();
    expect(autoTask!.taskType).toBe("bedrock.invokeModel");

    clearContext();
  });

  it("restores original after uninstrument", async () => {
    const originalSend = FakeBedrockClient.prototype.send;
    await instrumentBedrock(pricing, buffer);
    expect(FakeBedrockClient.prototype.send).not.toBe(originalSend);

    uninstrumentBedrock();
    expect(FakeBedrockClient.prototype.send).toBe(originalSend);
  });

  it("does not double-patch", async () => {
    await instrumentBedrock(pricing, buffer);
    const patchedSend = FakeBedrockClient.prototype.send;
    await instrumentBedrock(pricing, buffer);
    expect(FakeBedrockClient.prototype.send).toBe(patchedSend);
  });

  it("skips non-InvokeModelCommand commands", async () => {
    await instrumentBedrock(pricing, buffer);
    const client = new FakeBedrockClient();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    // A command that is NOT InvokeModelCommand
    class ListModelsCommand {
      input = {};
    }

    await runWithTask(task, async () => {
      await client.send(new ListModelsCommand());
    });

    // The non-InvokeModelCommand should pass through without recording
    expect(buffer.getAllEvents()).toHaveLength(0);
  });

  it("aggregates cost into task", async () => {
    await instrumentBedrock(pricing, buffer);
    const client = new FakeBedrockClient();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });
    const command = new InvokeModelCommand({
      modelId: "anthropic.claude-3-sonnet-20240229-v1:0",
    });

    await runWithTask(task, async () => {
      await client.send(command);
    });

    expect(task.totalInputTokens).toBe(100);
    expect(task.totalOutputTokens).toBe(50);
  });

  it("records latency in milliseconds", async () => {
    await instrumentBedrock(pricing, buffer);
    const client = new FakeBedrockClient();
    const task = createTask({ taskId: randomUUID(), taskType: "test" });
    const command = new InvokeModelCommand({
      modelId: "anthropic.claude-3-sonnet-20240229-v1:0",
    });

    await runWithTask(task, async () => {
      await client.send(command);
    });

    const events = buffer.getAllEvents();
    expect(events[0].latencyMs).toBeDefined();
    expect(typeof events[0].latencyMs).toBe("number");
  });
});
