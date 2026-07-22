/**
 * Tests for the HTTP adapter v2 — service catalog integration,
 * session auto-grouping, and response-based cost extraction.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { runWithTask, setContext, clearContext } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import {
  registerDomainRate,
  clearDomainRates,
  trackHttp,
  untrackHttp,
  getRecordedEvents,
  clearRecordedEvents,
  getServiceCatalog,
  resetServiceCatalog,
} from "../src/adapters/http.js";
import { toAttributionEventV2 } from "../src/attribution/convert.js";

let tmpDir: string;
let buffer: EventBuffer;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-httpv2-test-"));
  buffer = new EventBuffer(join(tmpDir, "test.db"));
  clearDomainRates();
  clearRecordedEvents();
  untrackHttp();
  resetServiceCatalog();
  clearContext();
});

afterEach(() => {
  untrackHttp();
  clearDomainRates();
  clearRecordedEvents();
  resetServiceCatalog();
  clearContext();
  vi.unstubAllGlobals();
  buffer.close();
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("HTTP adapter v2 — catalog cost extraction", () => {
  it("emits OpenAI embedding tokens without synthetic cost evidence", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      model: "text-embedding-3-small",
      usage: { prompt_tokens: 17, total_tokens: 17 },
    }), { status: 200, headers: { "content-type": "application/json", "x-request-id": "req-17" } })));
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "embedding" });
    await runWithTask(task, async () => { await fetch("https://api.openai.com/v1/embeddings"); });

    const event = getRecordedEvents()[0];
    expect(event.costUsd.toString()).toBe("0");
    expect(event.costConfidence).toBe("unknown");
    expect(event.pricingVersion).toBeUndefined();
    const wire = toAttributionEventV2(event);
    expect(wire).toMatchObject({
      component: "external",
      provider: { name: "openai", service: "embeddings", record_id: "req-17" },
      resource: { type: "model", id: "text-embedding-3-small" },
      usage: [{ metric: "input_tokens", quantity: "17", unit: "Tokens" }],
    });
    expect(wire?.cost_evidence).toBeUndefined();
  });

  it("does not observe usage from failed provider responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      model: "text-embedding-3-small",
      usage: { total_tokens: 17 },
    }), { status: 500, headers: { "content-type": "application/json" } })));
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "embedding" });
    await runWithTask(task, async () => {
      const response = await fetch("https://api.openai.com/v1/embeddings");
      await response.text();
    });

    expect(getRecordedEvents().some(
      (event) => event.details["attribution_observer_service"] === "openai_embeddings",
    )).toBe(false);
  });

  it("carries the Cohere request model into attribution v2", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: "cohere-29",
      meta: { billed_units: { input_tokens: 29 } },
    }), { status: 200, headers: { "content-type": "application/json" } })));
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "embedding" });
    await runWithTask(task, async () => {
      await fetch("https://api.cohere.com/v2/embed", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: "embed-v4.0", texts: ["hello"] }),
      });
    });

    expect(toAttributionEventV2(getRecordedEvents()[0])).toMatchObject({
      provider: { name: "cohere", service: "embed", record_id: "cohere-29" },
      resource: { type: "model", id: "embed-v4.0" },
      usage: [{ metric: "input_tokens", quantity: "29", unit: "Tokens" }],
    });
  });

  it("does not block on unfinished Request streams for observer metadata", async () => {
    const baseFetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: "cohere-stream",
      meta: { billed_units: { input_tokens: 11 } },
    }), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", baseFetch);
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "embedding" });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"model":"embed-v4.0"'));
        // Deliberately never close: instrumentation must not wait for this
        // optional metadata stream before invoking the real fetch.
      },
    });
    const request = new Request("https://api.cohere.com/v2/embed", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
      duplex: "half",
    } as RequestInit & { duplex: "half" });

    await runWithTask(task, async () => { await fetch(request); });
    await request.body?.cancel();

    expect(baseFetch).toHaveBeenCalledOnce();
    const wire = toAttributionEventV2(getRecordedEvents()[0]);
    expect(wire).toMatchObject({
      provider: { name: "cohere", service: "embed", record_id: "cohere-stream" },
      usage: [{ metric: "input_tokens", quantity: "11", unit: "Tokens" }],
    });
    expect(wire?.resource).toBeUndefined();
    expect(wire?.cost_evidence).toBeUndefined();
  });

  it("emits Deepgram duration as speech-to-text seconds", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      metadata: { request_id: "dg-25", duration: 25.933313 },
    }), { status: 200, headers: { "content-type": "application/json" } })));
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "transcription" });
    await runWithTask(task, async () => { await fetch("https://api.deepgram.com/v1/listen"); });

    const wire = toAttributionEventV2(getRecordedEvents()[0]);
    expect(wire).toMatchObject({
      component: "speech_to_text",
      provider: { name: "deepgram", service: "speech_to_text_pre_recorded", record_id: "dg-25" },
      resource: { type: "sku", id: "base-general:monolingual" },
      usage: [{ metric: "audio_seconds", quantity: "25.933313", unit: "Seconds" }],
    });
    expect(wire?.usage_period?.end_at).toBeDefined();
    expect(wire?.cost_evidence).toBeUndefined();
  });

  it("emits completed AssemblyAI transcript usage from a bounded response", async () => {
    const providerBody = {
      id: "assembly-42",
      status: "completed",
      audio_duration: 42.5,
      audio_channels: 2,
      speech_model_used: "universal-3-pro",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(providerBody), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "transcription" });
    const response = await runWithTask(task, async () => fetch(
      "https://api.assemblyai.com/v2/transcript/assembly-42",
    ));

    expect(await response.json()).toEqual(providerBody);
    const event = getRecordedEvents().find(
      (candidate) => candidate.details["attribution_observer_service"] === "assemblyai_transcription",
    );
    expect(toAttributionEventV2(event!)).toMatchObject({
      component: "speech_to_text",
      provider: {
        name: "assemblyai",
        service: "speech_to_text_pre_recorded",
        record_id: "assembly-42",
      },
      resource: { type: "model", id: "universal-3-pro" },
      usage: [{ metric: "audio_seconds", quantity: "85", unit: "Seconds" }],
    });
  });

  it("skips AssemblyAI observer parsing when an undeclared body exceeds the limit", async () => {
    const providerBody = {
      id: "assembly-large",
      status: "completed",
      audio_duration: 120,
      audio_channels: 1,
      speech_model_used: "universal-3-pro",
      words: ["x".repeat(1_048_576)],
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(providerBody), {
      status: 200,
      // Deliberately omit Content-Length: the stream reader must enforce the
      // limit even when the provider does not declare a response size.
      headers: { "content-type": "application/json" },
    })));
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "transcription" });
    const response = await runWithTask(task, async () => fetch(
      "https://api.assemblyai.com/v2/transcript/assembly-large",
    ));

    expect((await response.json()).id).toBe("assembly-large");
    expect(getRecordedEvents().some(
      (event) => event.details["attribution_observer_service"] === "assemblyai_transcription",
    )).toBe(false);
  });

  it("emits OpenAI TTS characters without consuming the binary response", async () => {
    const audio = new Uint8Array([1, 2, 3, 4]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(audio, {
      status: 200,
      headers: { "content-type": "audio/mpeg", "x-request-id": "req-tts-4" },
    })));
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "speech" });
    const response = await runWithTask(task, async () => fetch(
      "https://api.openai.com/v1/audio/speech",
      { method: "POST", body: JSON.stringify({ model: "tts-1-hd", input: "Hi 🌍" }) },
    ));

    expect(Array.from(new Uint8Array(await response.arrayBuffer()))).toEqual([1, 2, 3, 4]);
    const wire = toAttributionEventV2(getRecordedEvents()[0]);
    expect(wire).toMatchObject({
      component: "text_to_speech",
      provider: { name: "openai", service: "text_to_speech", record_id: "req-tts-4" },
      resource: { type: "model", id: "tts-1-hd" },
      usage: [{ metric: "characters", quantity: "4", unit: "Characters" }],
    });
    expect(wire?.cost_evidence).toBeUndefined();
  });

  it("emits OpenAI TTS characters from a Request object body", async () => {
    const audio = new Uint8Array([5, 6, 7]);
    const baseFetch = vi.fn().mockResolvedValue(new Response(audio, {
      status: 200,
      headers: { "content-type": "audio/mpeg", "x-request-id": "req-tts-request" },
    }));
    vi.stubGlobal("fetch", baseFetch);
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "speech" });
    const request = new Request("https://api.openai.com/v1/audio/speech", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "tts-1", input: "Hello" }),
    });

    const response = await runWithTask(task, async () => fetch(request));

    expect(baseFetch).toHaveBeenCalledWith(request, undefined);
    expect(Array.from(new Uint8Array(await response.arrayBuffer()))).toEqual([5, 6, 7]);
    const wire = toAttributionEventV2(getRecordedEvents()[0]);
    expect(wire).toMatchObject({
      component: "text_to_speech",
      provider: { name: "openai", service: "text_to_speech", record_id: "req-tts-request" },
      resource: { type: "model", id: "tts-1" },
      usage: [{ metric: "characters", quantity: "5", unit: "Characters" }],
    });
    expect(wire?.cost_evidence).toBeUndefined();
  });

  it("does not attribute the embedded Request body when init.body overrides it", async () => {
    const audio = new Uint8Array([8, 9]);
    const baseFetch = vi.fn().mockResolvedValue(new Response(audio, {
      status: 200,
      headers: { "content-type": "audio/mpeg", "x-request-id": "req-tts-override" },
    }));
    vi.stubGlobal("fetch", baseFetch);
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "speech" });
    const request = new Request("https://api.openai.com/v1/audio/speech", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "tts-1", input: "Embedded body" }),
    });
    const override = new Blob([
      JSON.stringify({ model: "tts-1-hd", input: "Provider body" }),
    ], { type: "application/json" });

    const response = await runWithTask(task, async () => fetch(request, { body: override }));
    expect(Array.from(new Uint8Array(await response.arrayBuffer()))).toEqual([8, 9]);

    expect(baseFetch).toHaveBeenCalledWith(request, { body: override });
    expect(getRecordedEvents().filter(
      (event) => event.details.attribution_component === "text_to_speech",
    )).toHaveLength(0);
  });

  it("attributes the embedded Request body when init.body is null", async () => {
    const audio = new Uint8Array([10, 11]);
    const baseFetch = vi.fn().mockResolvedValue(new Response(audio, {
      status: 200,
      headers: { "content-type": "audio/mpeg", "x-request-id": "req-tts-null" },
    }));
    vi.stubGlobal("fetch", baseFetch);
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "speech" });
    const request = new Request("https://api.openai.com/v1/audio/speech", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "tts-1", input: "Retained" }),
    });

    const response = await runWithTask(task, async () => fetch(request, { body: null }));

    expect(baseFetch).toHaveBeenCalledWith(request, { body: null });
    expect(Array.from(new Uint8Array(await response.arrayBuffer()))).toEqual([10, 11]);
    expect(toAttributionEventV2(getRecordedEvents()[0])).toMatchObject({
      component: "text_to_speech",
      resource: { type: "model", id: "tts-1" },
      usage: [{ metric: "characters", quantity: "8", unit: "Characters" }],
    });
  });

  it("emits separate Deepgram base and add-on attribution lines", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      metadata: { request_id: "dg-addon", duration: 10, channels: 2 },
    }), { status: 200, headers: { "content-type": "application/json" } })));
    trackHttp(buffer);
    const task = createTask({ taskId: randomUUID(), taskType: "transcription" });
    const url = "https://api.deepgram.com/v1/listen?model=nova-3&language=multi" +
      "&multichannel=true&diarize_model=v2&redact=pci&keyterm=Acme";
    await runWithTask(task, async () => { await fetch(url, { method: "POST" }); });

    const wires = getRecordedEvents().map(toAttributionEventV2);
    expect(wires).toHaveLength(4);
    expect(wires.map((wire) => wire?.resource?.id)).toEqual([
      "nova-3:multilingual",
      "speaker_diarization",
      "redaction",
      "keyterm_prompting",
    ]);
    expect(wires.every((wire) => wire?.usage[0].quantity === "20")).toBe(true);
    expect(wires.every((wire) => wire?.cost_evidence === undefined)).toBe(true);
  });

  it("attributes a user catalog override as manual evidence", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ results: [] }))));
    trackHttp(buffer);
    getServiceCatalog()?.registerOverride("tavily_search", 0.05, "request");
    const task = createTask({ taskId: randomUUID(), taskType: "test" });
    await runWithTask(task, async () => { await fetch("https://api.tavily.com/search"); });
    const event = getRecordedEvents()[0];
    expect(event.pricingSource).toBe("manual");
    expect(event.pricingVersion).toBeUndefined();
    expect(toAttributionEventV2(event)?.cost_evidence).toMatchObject({ source: "manual", amount: "0.05" });
  });

  it("extracts cost from response body for known service", async () => {
    // Mock fetch returning Tavily-like response with credits used
    const responseBody = { results: [], usage: { credits: 2 } };
    const mockResponse = new Response(JSON.stringify(responseBody), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    trackHttp(buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fetch("https://api.tavily.com/search");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].serviceName).toBe("Tavily Search");
    // 2 credits * $0.008 = $0.016
    expect(events[0].costUsd.toNumber()).toBeCloseTo(0.016, 6);
    expect(events[0].costConfidence).toBe("exact");
  });

  it("extracts cost from response header for known service", async () => {
    // Mock fetch returning ScrapingBee-like response with header
    const mockResponse = new Response("{}", {
      status: 200,
      headers: {
        "content-type": "application/json",
        "Spb-cost": "3",
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    trackHttp(buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fetch("https://app.scrapingbee.com/api/v1/");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].serviceName).toBe("ScrapingBee");
    // 3 * $0.000327 = $0.000981
    expect(events[0].costUsd.toNumber()).toBeCloseTo(0.000981, 6);
    expect(events[0].costConfidence).toBe("exact");
  });

  it("uses endpoint_match pricing for matched endpoint", async () => {
    const mockResponse = new Response(JSON.stringify({ results: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    trackHttp(buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fetch("https://maps.googleapis.com/maps/api/geocode/json?address=NYC");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].serviceName).toBe("Google Maps Geocoding");
    expect(events[0].costUsd.toNumber()).toBe(0.005);
    expect(events[0].costConfidence).toBe("computed");
  });

  it("records unknown domain with confidence=unknown and cost=0", async () => {
    const mockResponse = new Response("{}", {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    trackHttp(buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fetch("https://api.unknown-service.com/v1/data");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].costUsd.toNumber()).toBe(0);
    expect(events[0].costConfidence).toBe("unknown");
    expect(events[0].serviceName).toBe("api.unknown-service.com");
  });
});

describe("HTTP adapter v2 — session auto-grouping", () => {
  it("auto-creates session task when no explicit task and buffer provided", async () => {
    const mockResponse = new Response("{}", {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    setContext({ customerId: "acme", agent: "test_bot" });
    trackHttp(buffer);

    // No explicit task — session manager should create one
    await fetch("https://api.unknown-service.com/v1/data");

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].taskId).toBeTruthy();
  });

  it("records via an auto-task when no buffer provided", async () => {
    const mockResponse = new Response("{}", {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    // Track without buffer — an auto-task is still created so the HTTP
    // cost is recorded (mirrors the Python adapter).
    trackHttp();

    await fetch("https://api.unknown-service.com/v1/data");

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].taskId).toBeTruthy();
  });
});

describe("HTTP adapter v2 — override precedence", () => {
  it("user-registered domain rate takes precedence over catalog", async () => {
    const mockResponse = new Response(
      JSON.stringify({ results: [], api_credits_used: 5 }),
      {
        status: 200,
        headers: { "content-type": "application/json" },
      }
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    // Register manual rate for Tavily domain
    registerDomainRate("api.tavily.com", 0.05, "request");
    trackHttp(buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      await fetch("https://api.tavily.com/search");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    // Should use the registered rate, NOT catalog extraction
    expect(events[0].costUsd.toNumber()).toBe(0.05);
    expect(events[0].pricingSource).toBe("manual");
    expect(events[0].serviceName).toBe("api.tavily.com");
  });
});

describe("HTTP adapter v2 — response handling edge cases", () => {
  it("handles non-JSON response body gracefully", async () => {
    const mockResponse = new Response("<html>Hello</html>", {
      status: 200,
      headers: { "content-type": "text/html" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    trackHttp(buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      // Exa is a fixed-price service, so non-JSON body is fine
      await fetch("https://api.exa.ai/search");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    // Should still get the fixed cost even without JSON body
    expect(events[0].costUsd.toNumber()).toBe(0.007);
    expect(events[0].serviceName).toBe("Exa Search");
  });

  it("handles large response body by skipping body parse", async () => {
    const mockResponse = new Response("{}", {
      status: 200,
      headers: {
        "content-type": "application/json",
        "content-length": "2000000", // 2MB — over limit
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    trackHttp(buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    await runWithTask(task, async () => {
      // Tavily: body-based extraction should fall back due to large body
      await fetch("https://api.tavily.com/search");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    // Should fall back to estimated cost (fallback_credits=1 * $0.008)
    expect(events[0].costUsd.toNumber()).toBeCloseTo(0.008, 6);
    expect(events[0].costConfidence).toBe("estimated");
  });

  it("returns original response unchanged", async () => {
    const originalBody = { results: [1, 2, 3], api_credits_used: 1 };
    const mockResponse = new Response(JSON.stringify(originalBody), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    trackHttp(buffer);

    const task = createTask({ taskId: randomUUID(), taskType: "test" });

    const resp = await runWithTask(task, async () => {
      return await fetch("https://api.tavily.com/search");
    });

    // The original response should still be consumable
    const body = await resp.json();
    expect(body.results).toEqual([1, 2, 3]);
  });
});
