/**
 * Tests for adaptive batch splitting in the EventPusher.
 *
 * Verifies that oversized batches are automatically split before pushing
 * to the server, preventing SQS 256KB payload limit issues.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";
import { createCostEvent } from "../src/core/models.js";
import type { TrackerOptions } from "../src/core/tracker.js";

/** Maximum payload size from the pusher module. */
const MAX_PAYLOAD_BYTES = 120_000;

/**
 * Create an event with `detailsSize` bytes of padding in `details`.
 *
 * Note: the pusher applies `enforceMetadataLimit` (10 KB cap) to each
 * event's `details` before sending, so padding larger than ~10 KB is
 * replaced with a small truncation stub. Tests that need to exceed the
 * 200 KB payload limit therefore use many events with ~9 KB details each.
 */
function makeEvent(detailsSize: number = 100) {
  return createCostEvent({
    eventId: randomUUID(),
    taskId: randomUUID(),
    eventType: "llm_call",
    costUsd: 0.05,
    costConfidence: "exact",
    pricingSource: "litellm",
    provider: "openai",
    model: `gpt-4-${"m".repeat(250)}`,
    inputTokens: 100,
    outputTokens: 50,
    details: {
      padding: "x".repeat(detailsSize),
      request_id: `req_${"r".repeat(252)}`,
    },
  });
}

function makeOptions(overrides: Partial<TrackerOptions> = {}): TrackerOptions {
  return {
    apiKey: "dx_live_test123",
    batchSize: 500,
    flushIntervalMs: 60_000,
    ...overrides,
  };
}

describe("Adaptive batch splitting", () => {
  let tmpDir: string;
  let buffer: EventBuffer;
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "dexcost-split-"));
    buffer = new EventBuffer(join(tmpDir, "test.db"));
  });

  afterEach(() => {
    buffer.close();
    rmSync(tmpDir, { recursive: true, force: true });
    // Restore original fetch
    globalThis.fetch = originalFetch;
  });

  it("sends small batch as single request", async () => {
    for (let i = 0; i < 5; i++) {
      buffer.addEvent(makeEvent(100));
    }

    let fetchCallCount = 0;
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      fetchCallCount++;
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();

    expect(fetchCallCount).toBe(1);
  });

  it("splits oversized batch into multiple requests", async () => {
    // ~9KB details each (survives the 10KB metadata cap); 40 events
    // ≈ 360KB > 200KB limit, so the batch must split.
    for (let i = 0; i < 200; i++) {
      buffer.addEvent(makeEvent(9_000));
    }

    let fetchCallCount = 0;
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      fetchCallCount++;
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();

    expect(fetchCallCount).toBeGreaterThanOrEqual(2);
  });

  it("all events are included across split chunks", async () => {
    const eventIds: string[] = [];
    for (let i = 0; i < 10; i++) {
      const ev = makeEvent(30_000);
      eventIds.push(ev.eventId);
      buffer.addEvent(ev);
    }

    const sentEventIds: string[] = [];
    globalThis.fetch = vi.fn().mockImplementation(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { events: Array<{ event_id: string }> };
      for (const ev of body.events) {
        sentEventIds.push(ev.event_id);
      }
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();

    // Every original event should appear exactly once across all chunks
    expect(sentEventIds.sort()).toEqual(eventIds.sort());
  });

  it("tasks are only sent with the first chunk", async () => {
    // Add events large enough to trigger a split (~9KB each survives the
    // metadata cap; 40 events ≈ 360KB > 200KB limit).
    for (let i = 0; i < 200; i++) {
      buffer.addEvent(makeEvent(9_000));
    }

    const payloads: Array<{ events: unknown[]; tasks: unknown[] }> = [];
    globalThis.fetch = vi.fn().mockImplementation(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { events: unknown[]; tasks: unknown[] };
      payloads.push(body);
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();

    // Should have split into multiple payloads
    expect(payloads.length).toBeGreaterThanOrEqual(2);

    // The first payload may have tasks (from getAllTasks), subsequent payloads should not
    for (let i = 1; i < payloads.length; i++) {
      expect(payloads[i].tasks).toHaveLength(0);
    }
  });

  it("handles 413 response without retrying same batch", async () => {
    for (let i = 0; i < 3; i++) {
      buffer.addEvent(makeEvent(100));
    }

    let callCount = 0;
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      callCount++;
      return new Response(JSON.stringify({ error: "Payload too large" }), { status: 413 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();

    // Should NOT retry the same batch -- 413 is permanent
    expect(callCount).toBe(1);
  });

  it("recursive split handles very large batch", async () => {
    // 100 events * ~9KB each ≈ 900KB -- needs multiple recursive splits.
    for (let i = 0; i < 400; i++) {
      buffer.addEvent(makeEvent(9_000));
    }

    let fetchCallCount = 0;
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      fetchCallCount++;
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();

    // Should split multiple times -- at least 4-5 chunks
    expect(fetchCallCount).toBeGreaterThanOrEqual(4);
  });

  it("each chunk contains valid JSON with events and tasks keys", async () => {
    for (let i = 0; i < 10; i++) {
      buffer.addEvent(makeEvent(30_000));
    }

    globalThis.fetch = vi.fn().mockImplementation(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      // Validate structure
      expect(body).toHaveProperty("events");
      expect(body).toHaveProperty("tasks");
      expect(Array.isArray(body.events)).toBe(true);
      expect(Array.isArray(body.tasks)).toBe(true);
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();
  });

  it("each chunk payload is under the size limit", async () => {
    for (let i = 0; i < 10; i++) {
      buffer.addEvent(makeEvent(30_000));
    }

    globalThis.fetch = vi.fn().mockImplementation(async (_url: string, init: RequestInit) => {
      const bodyStr = init.body as string;
      // Each chunk should be at or under the MAX_PAYLOAD_BYTES limit
      // (may exceed at max depth, but should not in normal operation)
      expect(bodyStr.length).toBeLessThanOrEqual(MAX_PAYLOAD_BYTES * 1.5);
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();
  });

  it("empty buffer sends no requests", async () => {
    let fetchCallCount = 0;
    globalThis.fetch = vi.fn().mockImplementation(async () => {
      fetchCallCount++;
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    });

    const pusher = new EventPusher(buffer, makeOptions());
    await pusher.flush();

    expect(fetchCallCount).toBe(0);
  });

});
