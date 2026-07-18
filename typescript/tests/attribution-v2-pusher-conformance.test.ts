import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createCostEvent } from "../src/core/models.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";

describe("attribution v2 pusher conformance", () => {
  let buffer: EventBuffer;
  let tempDir: string;
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), "dexcost-attribution-v2-"));
    buffer = new EventBuffer(join(tempDir, "buffer.db"));
  });

  afterEach(() => {
    buffer.close();
    rmSync(tempDir, { recursive: true, force: true });
    globalThis.fetch = originalFetch;
  });

  it("keeps an unrepresentable event pending while delivering valid siblings", async () => {
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

    const pusher = new EventPusher(buffer, { apiKey: "dx_test", batchSize: 10 }, "https://api.dexcost.test");
    await expect(pusher.flush()).rejects.toThrow("remain pending");

    expect(buffer.getPendingEvents()).toHaveLength(1);
    expect(buffer.getPendingEvents()[0]?.eventId).toBe(invalid.eventId);
    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const request = vi.mocked(globalThis.fetch).mock.calls[0]?.[1];
    const body = JSON.parse(String(request?.body)) as { events: Array<{ event_id: string }> };
    expect(body.events.map((event) => event.event_id)).toEqual([valid.eventId]);
  });

  it("acknowledges an observability-only GPU signal without uploading it", async () => {
    buffer.addEvent(createCostEvent({
      eventId: randomUUID(),
      taskId: randomUUID(),
      eventType: "gpu_utilization_signal",
    }));
    globalThis.fetch = vi.fn();

    const pusher = new EventPusher(buffer, { apiKey: "dx_test", batchSize: 10 });
    await pusher.flush();

    expect(buffer.pendingCount).toBe(0);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});
