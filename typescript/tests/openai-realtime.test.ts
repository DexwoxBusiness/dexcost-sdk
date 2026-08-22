import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import {
  installOpenAIRealtime,
  openAIRealtimeMeasurement,
  uninstallOpenAIRealtime,
} from "../src/instruments/openai-realtime.js";

class FakeSocket {
  private readonly listeners = new Map<string, Array<(event?: unknown) => void>>();
  addEventListener(name: string, listener: (event?: unknown) => void): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }
  dispatch(name: string, event?: unknown): void {
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }
}

class OpenAIRealtimeWebSocket {
  readonly socket = new FakeSocket();
  readonly url: URL;
  readonly sent: unknown[] = [];
  closed = false;
  failSend?: Error;

  constructor(model = "gpt-realtime") {
    this.url = new URL(`wss://api.openai.com/v1/realtime?model=${model}`);
  }

  send(event: unknown): void {
    if (this.failSend !== undefined) throw this.failSend;
    this.sent.push(event);
  }

  close(): void {
    this.closed = true;
    this.socket.dispatch("close");
  }

  _emit(_name: string, _event: unknown): void {}

  serverEvent(event: Record<string, unknown>): void {
    this._emit("event", event);
    this._emit(String(event.type), event);
  }
}

function completedResponse(id = "resp_rt_123"): Record<string, unknown> {
  return {
    id,
    object: "realtime.response",
    status: "completed",
    output: [],
    output_modalities: ["audio"],
    usage: {
      input_tokens: 100,
      input_token_details: {
        text_tokens: 60,
        audio_tokens: 30,
        image_tokens: 10,
        cached_tokens: 20,
        cached_tokens_details: { text_tokens: 10, audio_tokens: 5, image_tokens: 5 },
      },
      output_tokens: 50,
      output_token_details: { text_tokens: 20, audio_tokens: 30 },
      total_tokens: 150,
    },
  };
}

describe("official OpenAI Node Realtime lifecycle metering", () => {
  let directory: string;
  let buffer: EventBuffer;
  let pricing: PricingEngine;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "dexcost-openai-realtime-"));
    buffer = new EventBuffer(join(directory, "events.db"));
    pricing = new PricingEngine();
    installOpenAIRealtime({ OpenAIRealtimeWebSocket }, pricing, buffer);
  });

  afterEach(() => {
    uninstallOpenAIRealtime();
    buffer.close();
    rmSync(directory, { recursive: true, force: true });
  });

  it("meters terminal multimodal usage once and retains no request content", () => {
    const connection = new OpenAIRealtimeWebSocket();
    connection.send({
      type: "response.create",
      response: { output_modalities: ["audio"], instructions: "private instructions" },
    });
    connection.serverEvent({
      event_id: "evt_created",
      type: "response.created",
      response: { id: "resp_rt_123", status: "in_progress" },
    });
    const done = { event_id: "evt_done", type: "response.done", response: completedResponse() };
    connection.serverEvent(done);
    connection.serverEvent(done);

    const [event] = buffer.getAllEvents();
    expect(buffer.getAllEvents()).toHaveLength(1);
    expect(event.eventType).toBe("llm_call");
    expect(event.model).toBe("gpt-realtime");
    expect(event.inputTokens).toBe(100);
    expect(event.outputTokens).toBe(50);
    expect(event.cachedTokens).toBe(20);
    expect(event.details.provider_record_id).toBe("resp_rt_123");
    expect(event.details.attribution_operation_status).toBe("succeeded");
    expect(event.details.attribution_usage_lines).toEqual([
      { metric: "input_tokens", quantity: "50", unit: "Tokens" },
      { metric: "cache_read_input_tokens", quantity: "10", unit: "Tokens" },
      { metric: "input_audio_tokens", quantity: "25", unit: "Tokens" },
      { metric: "cache_read_input_audio_tokens", quantity: "5", unit: "Tokens" },
      { metric: "input_image_tokens", quantity: "5", unit: "Tokens" },
      { metric: "cache_read_input_image_tokens", quantity: "5", unit: "Tokens" },
      { metric: "output_tokens", quantity: "20", unit: "Tokens" },
      { metric: "output_audio_tokens", quantity: "30", unit: "Tokens" },
    ]);
    expect(JSON.stringify(buffer.getAllEvents())).not.toContain("private");
  });

  it("creates a lifecycle for server/VAD responses that have no response.create send", () => {
    const connection = new OpenAIRealtimeWebSocket("gpt-realtime-mini");
    connection.serverEvent({
      type: "response.created",
      response: { id: "resp_vad", status: "in_progress" },
    });
    connection.serverEvent({
      type: "response.done",
      response: { ...completedResponse("resp_vad"), status: "cancelled", usage: undefined },
    });
    const [event] = buffer.getAllEvents();
    expect(event.model).toBe("gpt-realtime-mini");
    expect(event.details.attribution_operation_status).toBe("cancelled");
    expect(event.costUsd.toNumber()).toBe(0);
  });

  it("cancels an in-flight response when the connection closes", () => {
    const connection = new OpenAIRealtimeWebSocket();
    connection.send({ type: "response.create", response: { instructions: "private" } });
    connection.serverEvent({
      type: "response.created",
      response: { id: "resp_cancel", status: "in_progress" },
    });
    connection.close();

    const [event] = buffer.getAllEvents();
    expect(event.details.attribution_operation_status).toBe("cancelled");
    expect(event.costUsd.toNumber()).toBe(0);
    expect(connection.closed).toBe(true);
  });

  it("preserves native send exceptions and records a content-free failure", () => {
    const connection = new OpenAIRealtimeWebSocket();
    const native = new TypeError("private provider failure");
    connection.failSend = native;
    expect(() => connection.send({ type: "response.create", response: { instructions: "private" } }))
      .toThrow(native);

    const [event] = buffer.getAllEvents();
    expect(event.details.attribution_operation_status).toBe("failed");
    expect(event.details.attribution_error_type).toBe("typeerror");
    expect(JSON.stringify(event)).not.toContain("private provider failure");
  });

  it("keeps incomplete cached splits explicit and unpriced", () => {
    const measurement = openAIRealtimeMeasurement({
      id: "resp_split",
      usage: {
        input_tokens: 12,
        input_token_details: { text_tokens: 10, cached_tokens: 4 },
        output_tokens: 2,
      },
    }, "gpt-realtime");
    expect(measurement.usageLines).toEqual([
      { metric: "realtime_input_text_tokens_gross", quantity: 10, unit: "Tokens" },
      { metric: "realtime_unclassified_input_tokens", quantity: 2, unit: "Tokens" },
      { metric: "realtime_unclassified_cached_input_tokens", quantity: 4, unit: "Tokens" },
      { metric: "realtime_unclassified_output_tokens", quantity: 2, unit: "Tokens" },
    ]);
  });
});
