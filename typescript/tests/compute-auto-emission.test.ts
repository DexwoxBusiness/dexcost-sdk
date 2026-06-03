/**
 * End-to-end: long-running EC2 task auto-emits a compute_cost event with
 * cost_pending=true at task finalize, then the pricing engine back-fills it.
 *
 * Pins the v1+v2 deferred-cost contract for the compute layer (analog of the
 * network v2 §6.4 pattern).
 *
 * Ports python/tests/test_compute_auto_emission_long_running.py (2 cases) to
 * vitest.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { join } from "node:path";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";

import { CostTracker } from "../src/core/tracker.js";
import { ComputeAccountant } from "../src/core/compute-accountant.js";
import { RuntimeKind } from "../src/core/compute-runtime.js";
import { _setResultForTests, _resetCloudDetectForTests } from "../src/cloud-detect.js";
import * as cgroup from "../src/core/cgroup-reader.js";

let tmpDir: string;
let tracker: CostTracker;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-ce-"));
  // Provide an explicit dbPath so each test gets its own isolated buffer.
  tracker = new CostTracker({
    dbPath: join(tmpDir, "buf.db"),
    autoInstrument: [],
    storage: "local",
    trackHttp: false,
  });
});

afterEach(() => {
  tracker.close();
  vi.restoreAllMocks();
  _resetCloudDetectForTests();
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("compute auto-emission (long-running)", () => {
  test("EC2 task emits and prices a compute_cost event", async () => {
    _setResultForTests({
      provider: "aws",
      region: "us-east-1",
      source: "imds",
      instanceType: "c7g.xlarge",
    });

    // snapshot_start reads usage_usec=0
    vi.spyOn(cgroup, "readCpuStat").mockReturnValue({ usageUsec: 0 });

    const trackedTask = tracker.startTask({ taskType: "x" });
    const accountant = new ComputeAccountant({
      runtime: RuntimeKind.Ec2,
      region: "us-east-1",
      architecture: "x86_64",
    });
    accountant.snapshotStart();
    // Attach to the task.
    (trackedTask.task as any)._compute = accountant;
    // Backdate started_at to 60s ago so duration_ms is 60_000.
    trackedTask.task.startedAt = new Date(Date.now() - 60_000);

    // Now mock the end-snapshot reads: 1_000_000 usec = 1 vCPU-second used.
    vi.spyOn(cgroup, "readCpuStat").mockReturnValue({ usageUsec: 1_000_000 });
    vi.spyOn(cgroup, "readCpuMax").mockReturnValue({
      quotaUs: 400000,
      periodUs: 100000,
      vcpuCount: 4.0,
    });
    vi.spyOn(cgroup, "readMemoryPeak").mockReturnValue(512 * 1024 * 1024);
    vi.spyOn(cgroup, "readMemoryMax").mockReturnValue(8 * 1024 * 1024 * 1024);

    trackedTask.end("success");

    const events = tracker.buffer.queryEvents(trackedTask.task.taskId);
    const computeEvents = events.filter((e) => e.eventType === "compute_cost");
    expect(computeEvents.length).toBe(1);
    const ev = computeEvents[0];
    expect(ev.costUsd.toNumber()).toBeGreaterThan(0);
    expect(ev.pricingSource).toMatch(/^compute_catalog:aws:ec2:/);
    expect(ev.costConfidence).toBe("computed");
    expect((ev.details as Record<string, unknown>).cost_pending).toBeUndefined();
    expect(trackedTask.task.computeCostUsd.toNumber()).toBeCloseTo(ev.costUsd.toNumber(), 10);
  });

  test("unknown runtime (no _compute) emits no event", async () => {
    _setResultForTests({ provider: null, region: null, source: "none" });

    const trackedTask = tracker.startTask({ taskType: "x" });
    // NO accountant assigned.
    trackedTask.end("success");

    const events = tracker.buffer.queryEvents(trackedTask.task.taskId);
    const compute = events.filter((e) => e.eventType === "compute_cost");
    expect(compute.length).toBe(0);
    expect(trackedTask.task.computeCostUsd.toNumber()).toBe(0);
  });
});
