/**
 * Fix 3 — task metadata must be redacted/hashed before POST (PII leak).
 *
 * Before the fix, the pusher applied redaction/hashing/metadata-limit only
 * to event `details`; the task path sent `task.metadata` raw and never
 * hashed `customerId`/`projectId`. These tests inspect the synced payload
 * and assert the same protections are applied to tasks as to events.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";
import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";
import { createTask, createCostEvent } from "../src/core/models.js";
import { hashValue } from "../src/security/redaction.js";

let tmpDir: string;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-task-redact-"));
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

interface SentTask {
  metadata: Record<string, unknown>;
  customer_id: unknown;
  project_id: unknown;
}

interface SentEvent {
  provider?: { record_id?: string };
}

describe("Fix 3 — task metadata redaction on push", () => {
  it("redacts provider identifiers before attribution conversion", async () => {
    const buffer = new EventBuffer(join(tmpDir, "event.db"));
    const taskId = randomUUID();
    buffer.upsertTask(createTask({ taskId, taskType: "embedding" }));
    buffer.addEvent(createCostEvent({
      eventId: randomUUID(),
      taskId,
      eventType: "external_cost",
      provider: "openai",
      serviceName: "embeddings",
      details: {
        provider_record_id: "req-sensitive",
        attribution_usage_metric: "input_tokens",
        attribution_usage_quantity: "17",
      },
    }));

    let sentEvent: SentEvent | undefined;
    globalThis.fetch = vi.fn().mockImplementation(async (_url, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { events: SentEvent[] };
      sentEvent = body.events[0];
      return new Response("{}", { status: 202 });
    });

    const pusher = new EventPusher(buffer, {
      apiKey: "dx_live_x",
      redactFields: ["provider_record_id"],
    });
    await pusher.flush();

    expect(sentEvent).toBeDefined();
    expect(sentEvent!.provider?.record_id).toBeUndefined();

    pusher.stop();
    buffer.close();
  });

  it("redacts configured fields from task metadata and hashes customer/project ids", async () => {
    const buffer = new EventBuffer(join(tmpDir, "t.db"));
    const taskId = randomUUID();

    buffer.upsertTask(
      createTask({
        taskId,
        taskType: "resolve",
        customerId: "cust-acme",
        projectId: "proj-42",
        metadata: { ssn: "123-45-6789", tier: "enterprise" },
      }),
    );
    // An event is required so the pusher has a batch to push.
    buffer.addEvent(
      createCostEvent({ eventId: randomUUID(), taskId, eventType: "external_cost" }),
    );

    let sentTask: SentTask | undefined;
    globalThis.fetch = vi.fn().mockImplementation(async (_url, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { tasks: SentTask[] };
      sentTask = body.tasks[0];
      return new Response("{}", { status: 202 });
    });

    const pusher = new EventPusher(buffer, {
      apiKey: "dx_live_x",
      redactFields: ["ssn"],
      hashCustomerId: true,
    });
    await pusher.flush();

    expect(sentTask).toBeDefined();
    // PII field stripped from metadata, non-PII field kept.
    expect(sentTask!.metadata).not.toHaveProperty("ssn");
    expect(sentTask!.metadata["tier"]).toBe("enterprise");
    // customer_id / project_id hashed (SHA-256 hex), not raw.
    expect(sentTask!.customer_id).not.toBe("cust-acme");
    expect(sentTask!.customer_id).toBe(hashValue("cust-acme"));
    expect(sentTask!.project_id).not.toBe("proj-42");
    expect(sentTask!.project_id).toBe(hashValue("proj-42"));

    pusher.stop();
    buffer.close();
  });

  it("leaves customer/project ids raw when hashCustomerId is not set", async () => {
    const buffer = new EventBuffer(join(tmpDir, "n.db"));
    const taskId = randomUUID();
    buffer.upsertTask(
      createTask({ taskId, taskType: "resolve", customerId: "cust-acme" }),
    );
    buffer.addEvent(
      createCostEvent({ eventId: randomUUID(), taskId, eventType: "external_cost" }),
    );

    let sentTask: SentTask | undefined;
    globalThis.fetch = vi.fn().mockImplementation(async (_url, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { tasks: SentTask[] };
      sentTask = body.tasks[0];
      return new Response("{}", { status: 202 });
    });

    const pusher = new EventPusher(buffer, { apiKey: "dx_live_x" });
    await pusher.flush();

    expect(sentTask!.customer_id).toBe("cust-acme");

    pusher.stop();
    buffer.close();
  });

  it("replaces oversized task metadata with a truncation stub", async () => {
    const buffer = new EventBuffer(join(tmpDir, "big.db"));
    const taskId = randomUUID();
    buffer.upsertTask(
      createTask({
        taskId,
        taskType: "resolve",
        // > 10 KB serialised — must be truncated by enforceMetadataLimit.
        metadata: { blob: "x".repeat(20_000) },
      }),
    );
    buffer.addEvent(
      createCostEvent({ eventId: randomUUID(), taskId, eventType: "external_cost" }),
    );

    let sentTask: SentTask | undefined;
    globalThis.fetch = vi.fn().mockImplementation(async (_url, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { tasks: SentTask[] };
      sentTask = body.tasks[0];
      return new Response("{}", { status: 202 });
    });

    const pusher = new EventPusher(buffer, { apiKey: "dx_live_x" });
    await pusher.flush();

    expect(sentTask!.metadata).not.toHaveProperty("blob");
    expect(sentTask!.metadata["_truncated"]).toBe(true);

    pusher.stop();
    buffer.close();
  });
});
