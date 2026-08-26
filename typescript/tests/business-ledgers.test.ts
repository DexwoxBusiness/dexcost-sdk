import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  amendOutcome,
  close,
  getOutcomeHistory,
  init,
  recordOutcome,
} from "../src/index.js";
import {
  OutcomeRevision,
  RevenueRevision,
  outcomeValue,
  revenueAmount,
} from "../src/core/business.js";
import { Decimal } from "../src/core/models.js";
import { CostTracker } from "../src/core/tracker.js";
import { EventBuffer } from "../src/transport/buffer.js";
import { EventPusher } from "../src/transport/pusher.js";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  try { close(); } catch { /* singleton was not initialized */ }
  vi.restoreAllMocks();
});

describe("business revision wire models", () => {
  it("preserves typed outcomes and exact revenue on the canonical wire", () => {
    const taskId = randomUUID();
    const outcomeId = "4b2b84d8-972f-4ca2-8919-e2f21c41d17c";
    const outcome = new OutcomeRevision({
      outcomeId,
      taskId,
      name: "campaign_exported",
      state: "achieved",
      value: true,
      effectiveAt: new Date("2026-08-16T12:00:00.000Z"),
      observedAt: new Date("2026-08-16T12:00:01.000Z"),
    });
    expect(outcome.toDict()).toEqual({
      schema_version: "1",
      outcome_id: outcomeId,
      task_id: taskId,
      name: "campaign_exported",
      effective_at: "2026-08-16T12:00:00.000000Z",
      observed_at: "2026-08-16T12:00:01.000000Z",
      lifecycle: { state: "achieved", revision: 1 },
      value: { type: "boolean", value: true },
    });
    expect(OutcomeRevision.fromDict(outcome.toDict()).toDict()).toEqual(outcome.toDict());
    expect(outcomeValue(3n)).toEqual({ type: "integer", value: "3" });
    expect(outcomeValue(new Decimal("1.2500"))).toEqual({ type: "decimal", value: "1.25" });

    const revenue = new RevenueRevision({
      revenueId: "6f889ec1-005e-4b6d-8ab8-a0dc7e147c91",
      taskId,
      outcomeId,
      state: "recognized",
      amount: revenueAmount("12.3400", "USD"),
      source: { type: "sdk", recordId: "invoice-42" },
      effectiveAt: new Date("2026-08-20T12:00:00.000Z"),
      observedAt: new Date("2026-08-20T12:00:01.000Z"),
    });
    expect(revenue.toDict()).toEqual({
      schema_version: "1",
      revenue_id: revenue.revenueId,
      task_id: taskId,
      outcome_id: outcomeId,
      effective_at: "2026-08-20T12:00:00.000000Z",
      observed_at: "2026-08-20T12:00:01.000000Z",
      lifecycle: { state: "recognized", revision: 1 },
      amount: { value: "12.34", currency: "USD" },
      source: { type: "sdk", record_id: "invoice-42" },
    });
    expect(RevenueRevision.fromDict(revenue.toDict()).toDict()).toEqual(revenue.toDict());
  });

  it("rejects runtime attempts to bypass typed values, exact money, and lifecycle rules", () => {
    const taskId = randomUUID();
    expect(() => new OutcomeRevision({
      taskId, name: "bad", value: { type: "integer", value: "01" } as never,
    })).toThrow("invalid integer outcome value");
    expect(() => new OutcomeRevision({
      taskId, name: "pending", state: "pending", value: true,
    })).toThrow("pending outcomes cannot assert a value");
    expect(() => revenueAmount(1.1, "USD")).toThrow("safe integer");
    expect(() => revenueAmount("1", "usd")).toThrow("uppercase");
    expect(() => new RevenueRevision({ taskId, state: "recognized" })).toThrow("requires an amount");
    expect(() => new RevenueRevision({ taskId, state: "voided" })).toThrow("supersede");
  });
});

describe("durable outcome and revenue ledgers", () => {
  it("enforces idempotent contiguous revisions, immutable identity, lifecycle, and currency", () => {
    const buffer = new EventBuffer(":memory:");
    try {
      const taskId = randomUUID();
      const outcomeId = randomUUID();
      const first = new OutcomeRevision({ outcomeId, taskId, name: "lead_converted", state: "pending" });
      buffer.insertOutcomeRevision(first);
      buffer.insertOutcomeRevision(first);
      const achieved = new OutcomeRevision({
        outcomeId, taskId, name: "lead_converted", state: "achieved", revision: 2, value: true,
      });
      buffer.insertOutcomeRevision(achieved);
      expect(buffer.getOutcomeHistory(outcomeId).map((item) =>
        (item.lifecycle as Record<string, unknown>).revision)).toEqual([1, 2]);
      expect(() => buffer.insertOutcomeRevision(new OutcomeRevision({
        outcomeId, taskId, name: "lead_converted", state: "missed", revision: 2,
      }))).toThrow("already exists with different content");
      expect(() => buffer.insertOutcomeRevision(new OutcomeRevision({
        outcomeId, taskId, name: "lead_converted", state: "achieved", revision: 4,
      }))).toThrow("expected revision 3");
      expect(() => buffer.insertOutcomeRevision(new OutcomeRevision({
        outcomeId, taskId: randomUUID(), name: "lead_converted", state: "achieved", revision: 3,
      }))).toThrow("cannot change taskId or name");

      const revenueId = randomUUID();
      const source = { type: "sdk" as const, recordId: "invoice-42" };
      buffer.insertRevenueRevision(new RevenueRevision({
        revenueId, taskId, state: "pending", source,
      }));
      buffer.insertRevenueRevision(new RevenueRevision({
        revenueId, taskId, state: "recognized", revision: 2,
        source, amount: revenueAmount("12.34", "USD"),
      }));
      expect(() => buffer.insertRevenueRevision(new RevenueRevision({
        revenueId, taskId: randomUUID(), state: "recognized", revision: 3,
        source, amount: revenueAmount("12.34", "USD"),
      }))).toThrow("cannot change taskId");
      expect(() => buffer.insertRevenueRevision(new RevenueRevision({
        revenueId, taskId, state: "provisional", revision: 3,
        source, amount: revenueAmount("12.34", "USD"),
      }))).toThrow("recognized -> provisional");
      expect(() => buffer.insertRevenueRevision(new RevenueRevision({
        revenueId, taskId, state: "recognized", revision: 3,
        source, amount: revenueAmount("12.34", "EUR"),
      }))).toThrow("currency cannot change");
    } finally {
      buffer.close();
    }
  });

  it("persists complete correction history across a SQLite reopen", () => {
    const directory = mkdtempSync(join(tmpdir(), "dexcost-business-ledger-"));
    const path = join(directory, "ledger.db");
    const taskId = randomUUID();
    const outcomeId = randomUUID();
    try {
      const first = new EventBuffer(path);
      first.insertOutcomeRevision(new OutcomeRevision({
        outcomeId, taskId, name: "campaign_exported", state: "pending",
      }));
      first.insertOutcomeRevision(new OutcomeRevision({
        outcomeId, taskId, name: "campaign_exported", state: "achieved", revision: 2, value: true,
      }));
      first.close();

      const reopened = new EventBuffer(path);
      expect(reopened.getOutcomeHistory(outcomeId)).toHaveLength(2);
      expect(reopened.deliveryCounts().pendingOutcomes).toBe(2);
      reopened.close();
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
});

describe("outcome amendment APIs", () => {
  it("appends the next typed revision and enforces optimistic and task ownership guards", () => {
    const tracker = new CostTracker({
      dbPath: ":memory:", autoInstrument: [], trackHttp: false, trackNetwork: false,
      catalogReleases: false,
    });
    try {
      const taskId = randomUUID();
      const first = tracker.recordOutcome("lead_converted", { taskId, state: "pending" });
      const amended = tracker.amendOutcome(first.outcomeId, {
        state: "achieved", value: new Decimal("125.50"), expectedRevision: 1,
      });
      expect(amended).toMatchObject({
        taskId, name: "lead_converted", revision: 2,
        value: { type: "decimal", value: "125.5" },
      });
      expect(tracker.getOutcomeHistory(first.outcomeId).map((item) => item.revision)).toEqual([1, 2]);
      expect(() => tracker.amendOutcome(first.outcomeId, {
        state: "missed", expectedRevision: 1,
      })).toThrow("revision conflict: expected 1, found 2");

      const otherTask = tracker.startTask({ taskType: "other" });
      expect(() => otherTask.amendOutcome(first.outcomeId, { state: "missed" }))
        .toThrow("different task");
      otherTask.end("success");
    } finally {
      tracker.close();
    }
  });

  it("exports the singleton amend/history surface", () => {
    init({
      dbPath: ":memory:", autoInstrument: [], trackHttp: false, trackNetwork: false,
      catalogReleases: false,
    });
    const first = recordOutcome("campaign_exported", {
      taskId: randomUUID(), state: "achieved", value: true,
    });
    const amended = amendOutcome(first.outcomeId, {
      state: "missed", value: false, expectedRevision: 1,
    });
    expect(amended.revision).toBe(2);
    expect(getOutcomeHistory(first.outcomeId).map((item) => item.state)).toEqual(["achieved", "missed"]);
  });
});

describe("business-ledger delivery", () => {
  it("uploads and acknowledges outcome/revenue-only batches", async () => {
    const buffer = new EventBuffer(":memory:");
    const taskId = randomUUID();
    const outcome = new OutcomeRevision({
      taskId, name: "campaign_exported", state: "achieved", value: true,
    });
    const revenue = new RevenueRevision({
      taskId, outcomeId: outcome.outcomeId, state: "recognized",
      amount: revenueAmount("24.50", "USD"), source: { type: "sdk", recordId: "invoice-99" },
    });
    buffer.insertOutcomeRevision(outcome);
    buffer.insertRevenueRevision(revenue);
    let payload: Record<string, unknown> | undefined;
    globalThis.fetch = vi.fn(async (_url, init) => {
      payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return new Response(JSON.stringify({ accepted: 2, rejected: 0 }), { status: 202 });
    }) as typeof fetch;

    try {
      const pusher = new EventPusher(buffer, {
        apiKey: "dx_live_business", batchSize: 100, flushIntervalMs: 60_000,
      });
      await pusher.push();
      expect(payload?.outcomes).toEqual([outcome.toDict()]);
      expect(payload?.revenue_revisions).toEqual([revenue.toDict()]);
      expect(buffer.getPendingLedger("outcome")).toEqual([]);
      expect(buffer.getPendingLedger("revenue")).toEqual([]);
    } finally {
      buffer.close();
    }
  });
});
