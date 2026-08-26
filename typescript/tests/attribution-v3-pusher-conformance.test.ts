import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createCostEvent } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";

describe("attribution v3 pusher conformance", () => {
  let buffer: EventBuffer;
  let tempDir: string;
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), "dexcost-attribution-v3-"));
    buffer = new EventBuffer(join(tempDir, "buffer.db"));
  });

  afterEach(() => {
    buffer.close();
    rmSync(tempDir, { recursive: true, force: true });
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("quarantines a malformed head row and delivers the valid event behind it", async () => {
    const invalid = createCostEvent({
      eventId: randomUUID(),
      taskId: "task-123",
      eventType: "llm_call",
    });
    const valid = createCostEvent({
      eventId: randomUUID(),
      taskId: randomUUID(),
      eventType: "llm_call",
    });
    buffer.addEvent(invalid);
    buffer.addEvent(valid);

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, rejected: 0 }), { status: 202 }),
    );

    const pusher = new EventPusher(buffer, { apiKey: "dx_test", batchSize: 1 }, "https://api.dexcost.test");
    await expect(pusher.flush()).rejects.toThrow("were quarantined");

    expect(buffer.getPendingEvents()).toHaveLength(0);
    expect(buffer.getQuarantinedEvents().map((event) => event.eventId)).toEqual([invalid.eventId]);
    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const request = vi.mocked(globalThis.fetch).mock.calls[0]?.[1];
    const body = JSON.parse(String(request?.body)) as { events: Array<{ event_id: string }> };
    expect(body.events.map((event) => event.event_id)).toEqual([valid.eventId]);
  });

  it("uploads GPU utilization as an unpriced v3 observation", async () => {
    buffer.addEvent(createCostEvent({
      eventId: randomUUID(),
      taskId: randomUUID(),
      eventType: "gpu_utilization_signal",
      details: { gpu_index: 0, gpu_sku: "h100", sm_util_pct: 42 },
    }));
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, rejected: 0 }), { status: 202 }),
    );

    const pusher = new EventPusher(buffer, { apiKey: "dx_test", batchSize: 10 });
    await pusher.flush();

    expect(buffer.pendingCount).toBe(0);
    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const request = vi.mocked(globalThis.fetch).mock.calls[0]?.[1];
    const body = JSON.parse(String(request?.body)) as {
      events: Array<Record<string, unknown>>;
    };
    expect(body.events[0]).toMatchObject({
      schema_version: "3",
      component: "gpu",
      usage_snapshot: "full",
    });
    expect(body.events[0]).not.toHaveProperty("cost_evidence");
    expect(body.events[0]).not.toHaveProperty("details");
  });

  it("replays a retained converter quarantine once after an SDK upgrade", async () => {
    const event = createCostEvent({
      eventId: randomUUID(), taskId: randomUUID(), eventType: "llm_call",
      provider: "openai", model: "gpt-5", inputTokens: 2, outputTokens: 1,
    });
    buffer.addEvent(event);
    buffer.markQuarantined([event.eventId]);
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, rejected: 0 }), { status: 202 }),
    );

    const pusher = new EventPusher(buffer, { apiKey: "dx_test", batchSize: 10 });
    await pusher.flush();

    expect(buffer.getQuarantinedEvents()).toEqual([]);
    expect(buffer.getPendingEvents()).toEqual([]);
    expect(globalThis.fetch).toHaveBeenCalledOnce();
  });

  it("never invokes destructive pending cleanup from automatic delivery", async () => {
    buffer.addEvent(createCostEvent({
      eventId: randomUUID(), taskId: randomUUID(), eventType: "llm_call",
      provider: "openai", model: "gpt-5", inputTokens: 2, outputTokens: 1,
    }));
    const cleanup = vi.spyOn(buffer, "purgeOldPending");
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, rejected: 0 }), { status: 202 }),
    );
    const pusher = new EventPusher(buffer, { apiKey: "dx_test", batchSize: 10 });
    await pusher.flush();
    expect(cleanup).not.toHaveBeenCalled();
  });

  it("throttles duplicate background warnings by failing event-set fingerprint", () => {
    const pusher = new EventPusher(buffer, { apiKey: "dx_test", batchSize: 10 });
    const internal = pusher as unknown as {
      _handleConversionFailures(eventIds: string[], surface: boolean): void;
    };
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(Date, "now").mockReturnValue(1_000);

    internal._handleConversionFailures(["event-a"], false);
    internal._handleConversionFailures(["event-a"], false);
    expect(warn).toHaveBeenCalledTimes(1);

    internal._handleConversionFailures(["event-b"], false);
    expect(warn).toHaveBeenCalledTimes(2);
  });
});
