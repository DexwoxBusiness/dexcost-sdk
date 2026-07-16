/**
 * Sprint 2 Theme D / §3.2.1 (B12) — pusher partial-success accounting.
 *
 * Pre-fix the outer pusher only called `markSynced` when BOTH halves of
 * the split returned `true`. If the first half POST succeeded server-
 * side but the second half failed, the first-half events stayed
 * pending and were re-sent on the next tick → duplicates at the
 * control plane.
 *
 * Post-fix `pushWithSplit` marks events synced at each leaf POST that
 * succeeds, decoupling the partial-success accounting from
 * sibling-half outcomes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";
import { createCostEvent } from "../src/core/models.js";

function makeEvent() {
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
    // Enough padding so a few hundred events exceed the 200 KB payload limit.
    details: {
      padding: "x".repeat(9000),
      request_id: `req_${"r".repeat(252)}`,
    },
  });
}

describe("Pusher partial-success accounting (B12)", () => {
  let tmpDir: string;
  let buffer: EventBuffer;
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), "dexcost-b12-"));
    buffer = new EventBuffer(join(tmpDir, "test.db"));
  });

  afterEach(() => {
    buffer.close();
    rmSync(tmpDir, { recursive: true, force: true });
    globalThis.fetch = originalFetch;
  });

  it("marks first-half events synced even when second half fails", async () => {
    // Seed enough events to force a split (200 events × ~9 KB each ≈ 1.8 MB).
    for (let i = 0; i < 200; i++) {
      buffer.addEvent(makeEvent());
    }
    const totalPending = buffer.pendingCount;
    expect(totalPending).toBe(200);

    // Fetch mock: first call 200, every subsequent call 500.
    let callCount = 0;
    globalThis.fetch = vi.fn(async () => {
      callCount += 1;
      if (callCount === 1) {
        return new Response(JSON.stringify({ queued: 100 }), { status: 200 });
      }
      return new Response("server error", { status: 500 });
    }) as typeof fetch;

    const pusher = new EventPusher(buffer, {
      apiKey: "dx_live_test",
      batchSize: 200,
      flushIntervalMs: 60_000,
    });

    await pusher.push();

    expect(callCount).toBeGreaterThanOrEqual(2);

    const stillPending = buffer.pendingCount;
    // Pre-fix: all 200 still pending. Post-fix: a meaningful subset
    // (the successful first-leaf POST) is marked synced.
    expect(stillPending).not.toBe(200);
    expect(stillPending).toBeGreaterThan(0);
  });
});
