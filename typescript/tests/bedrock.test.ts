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

/** Simulates an InvokeModelCommand -- the instrument checks constructor.name. */
class InvokeModelCommand {
  input: { modelId: string; body?: string };
  constructor(input: { modelId: string; body?: string }) {
    this.input = input;
  }
}

const mockResponseBody = {
  usage: { input_tokens: 100, output_tokens: 50 },
  content: [{ text: "Hello" }],
};

const mockResponse = {
  body: new TextEncoder().encode(JSON.stringify(mockResponseBody)),
};

class FakeBedrockClient {
  async send(command: unknown, ..._rest: unknown[]): Promise<unknown> {
    void command;
    return {
      body: new TextEncoder().encode(JSON.stringify(mockResponseBody)),
    };
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

  it("records event with provider=aws-bedrock and model from input", async () => {
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

  it("parses Anthropic response format correctly", async () => {
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

  it("creates auto-task when no task context", async () => {
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

  it("uninstrument restores original", async () => {
    const originalSend = FakeBedrockClient.prototype.send;
    await instrumentBedrock(pricing, buffer);
    expect(FakeBedrockClient.prototype.send).not.toBe(originalSend);

    uninstrumentBedrock();
    expect(FakeBedrockClient.prototype.send).toBe(originalSend);
  });
});
