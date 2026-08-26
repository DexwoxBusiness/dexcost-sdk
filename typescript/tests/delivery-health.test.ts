import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createCostEvent, createTask } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";
import {
  localDeliveryStatus, onDeliveryError, removeDeliveryErrorCallback,
} from "../src/transport/delivery.js";

const paths: string[] = [];
const buffers: EventBuffer[] = [];
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  for (const buffer of buffers.splice(0)) buffer.close();
  for (const path of paths.splice(0)) rmSync(path, { recursive: true, force: true });
});
function storage(): EventBuffer {
  const path = mkdtempSync(join(tmpdir(), "dexcost-delivery-")); paths.push(path);
  const value = new EventBuffer(join(path, "events.db")); buffers.push(value); return value;
}
function populate(buffer: EventBuffer): void {
  const task = createTask({ taskId: "11111111-1111-4111-8111-111111111111", taskType: "agent" });
  buffer.upsertTask(task);
  buffer.addEvent(createCostEvent({
    eventId: "55555555-5555-4555-8555-555555555555", taskId: task.taskId,
    eventType: "llm_call", provider: "openai", model: "gpt-5",
    inputTokens: 1, outputTokens: 1, costUsd: "0.01", costConfidence: "computed",
    pricingSource: "litellm", details: {},
  }));
}

describe("delivery health", () => {
  it("reports local-only queue depth before cloud delivery", () => {
    const buffer = storage(); populate(buffer);
    const status = localDeliveryStatus(buffer);
    expect(status.enabled).toBe(false);
    expect(status.workerState).toBe("local_only");
    expect(status.pendingRecords).toBe(2);
    expect(status.healthy).toBe(true);
    expect(status.oldestPendingAt).toBeInstanceOf(Date);
  });

  it("joins successful worker counters with durable depth", async () => {
    const buffer = storage(); populate(buffer);
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"accepted":2,"rejected":0}', { status: 202 })));
    const pusher = new EventPusher(buffer, { apiKey: "dx_test_delivery", batchSize: 10 }, "https://api.dexcost.io");
    await pusher.flush();
    const status = pusher.status();
    expect(status.workerState).toBe("idle");
    expect(status.pendingRecords).toBe(0);
    expect(status.successfulBatches).toBe(1);
    expect(status.deliveredRecords).toBe(2);
    expect(status.lastAttemptAt).toBeInstanceOf(Date);
    expect(status.lastSuccessAt).toBeInstanceOf(Date);
  });

  it("makes 401 auth failure visible, redacted, and callback-safe", async () => {
    const buffer = storage(); populate(buffer);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("denied", { status: 401 })));
    const pusher = new EventPusher(buffer, { apiKey: "dx_test_delivery", batchSize: 10 }, "https://api.dexcost.io");
    const received: unknown[] = [];
    const callback = onDeliveryError((event) => received.push(event));
    const broken = onDeliveryError(() => { throw new Error("callback bug"); });
    try {
      await pusher.flush();
      const status = pusher.status();
      expect(status.workerState).toBe("auth_failed");
      expect(status.pendingRecords).toBe(2);
      expect(status.failedBatches).toBe(1);
      expect(status.lastErrorMessage).not.toContain("dx_test_delivery");
      expect(received).toHaveLength(1);
      expect(received[0]).toMatchObject({ operation: "authentication", retryable: false });
    } finally {
      removeDeliveryErrorCallback(callback); removeDeliveryErrorCallback(broken); pusher.stop();
    }
  });

  it("treats 403 as retryable transport policy rather than a revoked key", async () => {
    const buffer = storage(); populate(buffer);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("forbidden", { status: 403 })));
    const pusher = new EventPusher(buffer, { apiKey: "dx_test_delivery" }, "https://api.dexcost.io");
    await pusher.flush();
    expect(pusher.authFailed).toBe(false);
    expect(pusher.status()).toMatchObject({ workerState: "backoff", consecutiveFailures: 1 });
  });

  it("uses delivery backoff as the actual next-attempt delay", async () => {
    vi.useFakeTimers();
    const buffer = storage(); populate(buffer);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response("temporary", { status: 503 }))
      .mockResolvedValueOnce(new Response('{"accepted":2,"rejected":0}', { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    const pusher = new EventPusher(
      buffer,
      { apiKey: "dx_test_delivery", batchSize: 10, flushIntervalMs: 100 },
      "https://api.dexcost.io",
    );
    try {
      pusher.start();
      await vi.advanceTimersByTimeAsync(100);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(pusher.status()).toMatchObject({ workerState: "backoff", backoffSeconds: 2 });

      await vi.advanceTimersByTimeAsync(1_999);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(pusher.status()).toMatchObject({ workerState: "idle", consecutiveFailures: 0 });
      expect(buffer.deliveryCounts().pendingEvents).toBe(0);
    } finally {
      pusher.stop();
    }
  });
});
