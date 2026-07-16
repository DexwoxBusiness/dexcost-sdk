/**
 * Phase D Task 10 — task-finalize egress pricing tests.
 *
 * Mirrors python/tests/test_network_cost_finalize.py +
 * test_network_cost_dual_invoice.py + test_network_cost_invariants.py
 * (parametrized property invariants).
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { CostTracker, TrackedTask } from "../src/core/tracker.js";
import {
  _resetAccountantRegistryForTests,
  getAccountant,
} from "../src/adapters/network-accountant.js";
import { _setResultForTests, _resetCloudDetectForTests } from "../src/cloud-detect.js";
import { createCostEvent, Decimal } from "../src/core/models.js";
import { randomUUID } from "node:crypto";

let tracker: CostTracker;

function pinCloudEnv(provider: string | null, region: string | null) {
  _setResultForTests({
    provider,
    region,
    source: provider ? "env" : "none",
  });
}

beforeEach(() => {
  _resetAccountantRegistryForTests();
  _resetCloudDetectForTests();
  tracker = new CostTracker({ dbPath: ":memory:" });
});

afterEach(() => {
  _resetCloudDetectForTests();
  _resetAccountantRegistryForTests();
  tracker.close();
});

function startTask(): TrackedTask {
  return tracker.startTask({ taskType: "test" });
}

describe("Phase D Task 10 — task finalize", () => {
  it("computes network_cost_usd from canonical scalar", () => {
    pinCloudEnv("aws", "us-east-1");
    const tt = startTask();
    const acct = getAccountant(tt.task.taskId);
    expect(acct).toBeDefined();
    // 1 GB external = $0.09 at aws/us-east-1.
    acct!.record("api.example.com", 0, 1_000_000_000, false);
    tt.end("success");
    expect(tt.task.networkCostUsd.toNumber()).toBeCloseTo(0.09, 6);
    expect(tt.task.networkBytesOut).toBe(1_000_000_000);
    expect(tt.task.networkCallCount).toBe(1);
  });

  it("per-host egress_cost_usd in network_by_host", () => {
    pinCloudEnv("aws", "us-east-1");
    const tt = startTask();
    const acct = getAccountant(tt.task.taskId)!;
    acct.record("api.example.com", 0, 500_000_000, false);
    tt.end("success");

    const hosts = (tt.task.networkByHost as { hosts: Array<Record<string, unknown>> })
      .hosts;
    const host = hosts.find((h) => h.host === "api.example.com")!;
    expect(host).toBeDefined();
    const hostCost = parseFloat(host.egress_cost_usd as string);
    // 0.5 GB * 0.09 = 0.045
    expect(hostCost).toBeCloseTo(0.045, 6);
  });

  it("internal host has zero egress cost", () => {
    pinCloudEnv("aws", "us-east-1");
    const tt = startTask();
    const acct = getAccountant(tt.task.taskId)!;
    // 999 MB to private IP → 0 external bytes → $0 cost.
    acct.record("10.0.0.5", 0, 999_999_999, true);
    tt.end("success");
    expect(tt.task.networkCostUsd.toNumber()).toBe(0);
  });

  it("back-fills cost_pending network events", () => {
    pinCloudEnv("aws", "us-east-1");
    const tt = startTask();

    // Pre-insert a cost_pending network event (mirrors what the patched
    // fetch would have emitted at body-completion time).
    const ev = createCostEvent({
      eventId: randomUUID(),
      taskId: tt.task.taskId,
      eventType: "network",
      costUsd: 0,
      costConfidence: "unknown",
      serviceName: "api.example.com",
      details: {
        url: "https://api.example.com/x",
        request_bytes: 1_000_000_000,
        response_bytes: 0,
        is_internal_traffic: false,
        cost_pending: true,
      },
    });
    tracker.buffer.addEvent(ev);

    // Drive the accountant so the per-task scalar matches.
    const acct = getAccountant(tt.task.taskId)!;
    acct.record("api.example.com", 0, 1_000_000_000, false);

    tt.end("success");

    const stored = tracker.buffer.queryEvents(tt.task.taskId);
    const net = stored.find((e) => e.eventType === "network")!;
    expect(net).toBeDefined();
    expect(net.costUsd.toNumber()).toBeCloseTo(0.09, 6);
    expect(net.details?.cost_pending).toBeUndefined();
    expect(net.details?.egress_pricing_source).toBe(
      "egress_catalog:aws:us-east-1",
    );
    expect(net.pricingSource).toBe("egress_catalog:aws:us-east-1");
    expect(net.pricingVersion).toBe("egress:1.0.0");
  });

  it("does not price inbound response bytes as cloud egress", () => {
    pinCloudEnv("aws", "us-east-1");
    const tt = startTask();
    const ev = createCostEvent({
      eventId: randomUUID(),
      taskId: tt.task.taskId,
      eventType: "network",
      details: {
        request_bytes: 0,
        response_bytes: 1_000_000_000,
        is_internal_traffic: false,
        cost_pending: true,
      },
    });
    tracker.buffer.addEvent(ev);
    getAccountant(tt.task.taskId)!.record("api.example.com", 1_000_000_000, 0, false);

    tt.end("success");

    const net = tracker.buffer.queryEvents(tt.task.taskId)
      .find((event) => event.eventType === "network")!;
    expect(net.costUsd.toString()).toBe("0");
    expect(net.pricingSource).toBe("egress_catalog:aws:us-east-1");
  });

  it("no cloud detected falls to meta default rate (Tier 3)", () => {
    pinCloudEnv(null, null);
    const tt = startTask();
    const acct = getAccountant(tt.task.taskId)!;
    acct.record("api.example.com", 0, 1_000_000_000, false);
    tt.end("success");
    // Universal default $0.09/GB → 1 GB → $0.09.
    expect(tt.task.networkCostUsd.toNumber()).toBeCloseTo(0.09, 6);
  });

  it("zero bytes yields zero network_cost_usd", () => {
    pinCloudEnv("aws", "us-east-1");
    const tt = startTask();
    tt.end("success");
    expect(tt.task.networkCostUsd.toNumber()).toBe(0);
    expect(tt.task.networkCallCount).toBe(0);
  });

  /**
   * Decision #7 — dual-invoice attribution (MANDATORY per Decisions Log).
   *
   * A cataloged-vendor call must produce exactly ONE event (external_cost
   * with the vendor charge) AND populate both:
   *   - task.externalCostUsd  (vendor's per-request invoice)
   *   - task.networkCostUsd   (cloud's egress invoice on the SAME bytes)
   *
   * The external_cost event's own costUsd stays unchanged at the vendor
   * charge — no egress dollars stamped on it. v2 §3.3 + §10.2.
   */
  it("Decision #7 — dual invoice: external + network both populated", () => {
    pinCloudEnv("aws", "us-east-1");
    const tt = startTask();

    // Pre-record the vendor invoice (HTTP adapter emits this at fetch
    // return time for cataloged calls).
    const vendorEv = createCostEvent({
      eventId: randomUUID(),
      taskId: tt.task.taskId,
      eventType: "external_cost",
      costUsd: 0.01,
      costConfidence: "exact",
      serviceName: "api.vendor.com",
      details: {
        url: "https://api.vendor.com/x",
        request_bytes: 500_000_000,
        response_bytes: 0,
        is_internal_traffic: false,
      },
    });
    tracker.buffer.addEvent(vendorEv);
    // Aggregate the vendor charge into externalCostUsd (mirrors what
    // tt.recordCost would have done; this synthetic test focuses on
    // the egress half).
    tt.task.externalCostUsd = new Decimal("0.01");
    tt.task.totalCostUsd = tt.task.totalCostUsd.plus("0.01");

    // Same bytes → accountant → external_bytes_out.
    const acct = getAccountant(tt.task.taskId)!;
    acct.record("api.vendor.com", 0, 500_000_000, false);

    tt.end("success");

    // (1) Exactly ONE event for this call — the vendor's external_cost.
    const stored = tracker.buffer.queryEvents(tt.task.taskId);
    expect(stored.length).toBe(1);
    expect(stored[0].eventType).toBe("external_cost");

    // (2) Vendor's per-request invoice is intact.
    expect(tt.task.externalCostUsd.toString()).toBe("0.01");

    // (3) Cloud's egress invoice on those same bytes is captured IN ADDITION.
    //     0.5 GB * 0.09 = 0.045
    expect(tt.task.networkCostUsd.toNumber()).toBeCloseTo(0.045, 6);

    // (4) Total = vendor + egress, no double-count, no silent drop.
    expect(tt.task.totalCostUsd.toNumber()).toBeCloseTo(0.055, 6);

    // (5) The external_cost event's own costUsd is UNCHANGED — no egress
    //     dollars stamped onto it. Events carry measurement; task carries
    //     derived attribution (v2 §3.3).
    expect(stored[0].costUsd.toString()).toBe("0.01");
  });

  /**
   * Property invariant (v2 §10.3 #2): sum(per-host egress_cost_usd) ==
   * task.networkCostUsd. Parametrized over host_count × is_internal.
   * Uses toBeCloseTo because TS uses plain `number` for cost fields.
   */
  describe.each([1, 5, 20, 100, 1000])("invariants — n=%d hosts", (nHosts) => {
    it.each<[boolean | null, string]>([
      [true, "internal"],
      [false, "external"],
      [null, "nil"],
    ])(`is_internal=%s (%s)`, (isInternal) => {
      pinCloudEnv("aws", "us-east-1");
      const tt = startTask();
      const acct = getAccountant(tt.task.taskId)!;
      for (let i = 0; i < nHosts; i++) {
        const bytesIn = (i * 37) % 5000;
        const bytesOut = (i * 53) % 5000;
        acct.record(`h${i}.com`, bytesIn, bytesOut, isInternal);
      }
      tt.end("success");

      const hosts = (tt.task.networkByHost as { hosts: Array<Record<string, unknown>> })
        .hosts;
      let sum = 0;
      for (const h of hosts) {
        sum += parseFloat((h.egress_cost_usd as string) ?? "0");
      }
      // Per-host egress sums to the task-level network cost (now exact
      // Decimal; compare via toNumber within float tolerance).
      expect(sum).toBeCloseTo(tt.task.networkCostUsd.toNumber(), 6);
    });
  });
});
