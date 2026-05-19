/**
 * Tests for the HTTP fetch adapter.
 *
 * Patches globalThis.fetch to auto-record external_cost events when HTTP
 * requests target domains with registered cost rates.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { CostTracker } from "../src/core/tracker.js";
import { runWithTask } from "../src/core/context.js";
import { createTask } from "../src/core/models.js";
import {
  registerDomainRate,
  getDomainRates,
  clearDomainRates,
  trackHttp,
  untrackHttp,
  getRecordedEvents,
  clearRecordedEvents,
} from "../src/adapters/http.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-http-test-"));
  clearDomainRates();
  clearRecordedEvents();
  untrackHttp();
});

afterEach(() => {
  untrackHttp();
  clearDomainRates();
  clearRecordedEvents();
  vi.unstubAllGlobals();
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("registerDomainRate / getDomainRates", () => {
  it("registers and retrieves domain rates", () => {
    registerDomainRate("api.example.com", 0.005);
    registerDomainRate("api.other.com", 0.01, "call");

    const rates = getDomainRates();

    expect(rates["api.example.com"]).toBeDefined();
    expect(rates["api.example.com"].costUsd).toBe(0.005);
    expect(rates["api.example.com"].per).toBe("request");

    expect(rates["api.other.com"]).toBeDefined();
    expect(rates["api.other.com"].costUsd).toBe(0.01);
    expect(rates["api.other.com"].per).toBe("call");
  });

  it("clears domain rates", () => {
    registerDomainRate("api.example.com", 0.005);
    expect(Object.keys(getDomainRates())).toHaveLength(1);

    clearDomainRates();
    expect(Object.keys(getDomainRates())).toHaveLength(0);
  });
});

describe("trackHttp / fetch interception", () => {
  it("records event when fetch hits registered domain inside a task", async () => {
    const mockResponse = new Response(JSON.stringify({ ok: true }), {
      status: 200,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    registerDomainRate("api.example.com", 0.005, "request");
    trackHttp();

    const task = createTask({ taskId: randomUUID(), taskType: "test-fetch" });

    await runWithTask(task, async () => {
      await fetch("https://api.example.com/v1/data");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);

    const event = events[0];
    expect(event.eventType).toBe("external_cost");
    expect(event.serviceName).toBe("api.example.com");
    expect(event.costUsd).toBe(0.005);
    expect(event.costConfidence).toBe("exact");
    expect(event.pricingSource).toBe("rate_registry");
    expect(event.taskId).toBe(task.taskId);
    expect(event.details["url"]).toBe("https://api.example.com/v1/data");
    expect(event.details["per"]).toBe("request");
    expect(event.eventId).toBeTruthy();
  });

  it("records event with an auto-task when no active task", async () => {
    const mockResponse = new Response(JSON.stringify({ ok: true }), {
      status: 200,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    registerDomainRate("api.example.com", 0.005);
    trackHttp();

    // Call fetch outside any task context — an auto-task is created so
    // the HTTP cost is never silently lost.
    await fetch("https://api.example.com/v1/data");

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].taskId).toBeTruthy();
  });

  it("records unknown domain with confidence=unknown and cost=0", async () => {
    const mockResponse = new Response(JSON.stringify({ ok: true }), {
      status: 200,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    // Register a DIFFERENT domain
    registerDomainRate("api.other.com", 0.005);
    trackHttp();

    const task = createTask({ taskId: randomUUID(), taskType: "test-fetch" });

    await runWithTask(task, async () => {
      await fetch("https://api.example.com/v1/data");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].costUsd).toBe(0);
    expect(events[0].costConfidence).toBe("unknown");
    expect(events[0].serviceName).toBe("api.example.com");
  });

  it("strips port from domain for matching", async () => {
    const mockResponse = new Response(JSON.stringify({ ok: true }), {
      status: 200,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    // Register without port
    registerDomainRate("api.example.com", 0.01, "request");
    trackHttp();

    const task = createTask({ taskId: randomUUID(), taskType: "test-port" });

    await runWithTask(task, async () => {
      // URL includes port — should still match
      await fetch("https://api.example.com:443/v1/data");
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].serviceName).toBe("api.example.com");
  });

  it("restores original fetch on untrackHttp", async () => {
    const originalFetch = vi.fn().mockResolvedValue(
      new Response("{}", { status: 200 })
    );
    vi.stubGlobal("fetch", originalFetch);

    trackHttp();

    // fetch is now wrapped — it should not be the exact same reference
    const wrappedFetch = globalThis.fetch;
    expect(wrappedFetch).not.toBe(originalFetch);

    untrackHttp();

    // After untrack, fetch is restored to original
    expect(globalThis.fetch).toBe(originalFetch);
  });

  it("also accepts a Request object as first fetch argument", async () => {
    const mockResponse = new Response(JSON.stringify({ ok: true }), {
      status: 200,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(mockResponse));

    registerDomainRate("api.example.com", 0.005, "request");
    trackHttp();

    const task = createTask({ taskId: randomUUID(), taskType: "test-request-obj" });

    await runWithTask(task, async () => {
      const req = new Request("https://api.example.com/v1/resource");
      await fetch(req);
    });

    const events = getRecordedEvents();
    expect(events).toHaveLength(1);
    expect(events[0].serviceName).toBe("api.example.com");
    expect(events[0].details["url"]).toBe("https://api.example.com/v1/resource");
  });
});
