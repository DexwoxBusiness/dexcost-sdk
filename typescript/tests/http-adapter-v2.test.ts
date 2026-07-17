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
