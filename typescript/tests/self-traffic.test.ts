/**
 * Regression tests for the SDK self-traffic loop.
 *
 * Production finding (dexwox dashboard, 0.11.0): the event pusher,
 * pricing refresh, and catalog refresh all go through the PATCHED global
 * fetch. Each telemetry push resolved an ambient session task, which was
 * persisted, then pushed on the next cycle — which is itself a fetch —
 * creating an endless drip of empty agent_session tasks (Pending →
 * Success as the idle sweep caught them) plus egress cost for dexcost
 * pushing dexcost, on a completely idle application.
 *
 * Fix: SDK-internal hostnames bypass capture entirely — no events, no
 * session, no byte accounting — checked FIRST in both the fetch wrapper
 * and the node http/https wrapper. The tracker auto-registers its
 * resolved endpoint (and serviceCatalogUrl host); api.dexcost.io is
 * always internal.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { EventBuffer } from "../src/transport/buffer.js";
import { PricingEngine } from "../src/pricing/engine.js";
import { clearContext } from "../src/core/context.js";
import {
  trackHttp,
  untrackHttp,
  clearDomainRates,
  clearRecordedEvents,
  resetServiceCatalog,
  getSessionManager,
  getRecordedEvents,
  registerInternalHost,
  _resetInternalHostsForTests,
} from "../src/adapters/http.js";

let tmpDir: string;
let buffer: EventBuffer;
let pricing: PricingEngine;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-selftraffic-"));
  buffer = new EventBuffer(join(tmpDir, "test.db"));
  pricing = new PricingEngine();
  clearDomainRates();
  clearRecordedEvents();
  untrackHttp();
  resetServiceCatalog();
  clearContext();
  _resetInternalHostsForTests();
});

afterEach(() => {
  untrackHttp();
  clearDomainRates();
  clearRecordedEvents();
  resetServiceCatalog();
  clearContext();
  _resetInternalHostsForTests();
  vi.unstubAllGlobals();
  buffer.close();
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("SDK self-traffic bypass", () => {
  it("calls to api.dexcost.io produce NOTHING: no tasks, no sessions, no events, no bytes", async () => {
    const base = vi.fn(async () => new Response('{"ok":true}', { status: 200 }));
    vi.stubGlobal("fetch", base);
    trackHttp(buffer, pricing);

    // Simulate three pusher flush cycles on an otherwise idle app.
    for (let i = 0; i < 3; i++) {
      const res = await fetch("https://api.dexcost.io/v1/events", {
        method: "POST",
        body: JSON.stringify({ events: [] }),
      });
      await res.text();
    }

    // The underlying fetch WAS called (traffic flows normally)…
    expect(base).toHaveBeenCalledTimes(3);
    // …but capture saw none of it. Pre-fix: one agent_session task per
    // idle window, re-fed into the next push forever.
    expect(buffer.getAllTasks()).toHaveLength(0);
    expect(buffer.getAllEvents()).toHaveLength(0);
    expect(getRecordedEvents()).toHaveLength(0);
    expect(getSessionManager()!.activeSessionCount).toBe(0);
  });

  it("bypassed responses are returned raw (no byte-counting stream wrapper)", async () => {
    const original = new Response('{"ok":true}', { status: 200 });
    vi.stubGlobal("fetch", vi.fn(async () => original));
    trackHttp(buffer, pricing);

    const res = await fetch("https://api.dexcost.io/v1/tasks", { method: "POST", body: "{}" });
    // Identity preserved — internal calls pay zero capture overhead.
    expect(res).toBe(original);
  });

  it("registerInternalHost extends the bypass (self-hosted control layer)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    trackHttp(buffer, pricing);
    registerInternalHost("control.internal.example.com");

    const res = await fetch("https://control.internal.example.com/v1/events", {
      method: "POST",
      body: "{}",
    });
    await res.text();

    expect(buffer.getAllTasks()).toHaveLength(0);
    expect(getSessionManager()!.activeSessionCount).toBe(0);
  });

  it("the tracker auto-registers its resolved endpoint host", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    const { CostTracker } = await import("../src/core/tracker.js");
    const tracker = new CostTracker({
      dbPath: join(tmpDir, "tracker.db"),
      endpoint: "https://control.dexwox.example.com",
      autoInstrument: [],
      // trackHttp defaults ON — the constructor patches fetch synchronously.
    });
    try {
      const res = await fetch("https://control.dexwox.example.com/v1/events", {
        method: "POST",
        body: "{}",
      });
      await res.text();
      expect(tracker.buffer.getAllTasks()).toHaveLength(0);
      expect(tracker.buffer.getAllEvents()).toHaveLength(0);
    } finally {
      tracker.close();
    }
  });

  it("non-internal hosts are still fully captured (control case)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({ model: "kimi-k2", usage: { input_tokens: 10, output_tokens: 5 } }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    trackHttp(buffer, pricing);

    const res = await fetch("https://api.kimi.com/anthropic/v1/messages", {
      method: "POST",
      body: "{}",
    });
    await res.text();

    expect(buffer.getAllEvents().filter((e) => e.eventType === "llm_call")).toHaveLength(1);
    expect(getSessionManager()!.activeSessionCount).toBe(1);
  });
});
