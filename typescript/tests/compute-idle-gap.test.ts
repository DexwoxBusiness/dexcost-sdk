/**
 * Decisions #9 + #10 — idle compute is invisible to dexcost. THE GAP IS THE
 * DESIGN.
 *
 * These tests fail fast if a future refactor ever adds synthetic "idle
 * pseudo-tasks" or otherwise pushes dexcost_compute_total toward the cloud
 * invoice on long-running runtimes. The under-attribution is the customer-
 * facing signal for "unaccounted capacity"; surfacing it as a feature
 * (README, dashboard, marketing) is mandatory per the decisions log.
 *
 * Ports python/tests/test_compute_idle_gap.py (2 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import Decimal from "decimal.js";

import { CostTracker } from "../src/core/tracker.js";
import { ComputeAccountant } from "../src/core/compute-accountant.js";
import { RuntimeKind } from "../src/core/compute-runtime.js";
import { _setResultForTests, _resetCloudDetectForTests } from "../src/cloud-detect.js";
import * as cgroup from "../src/core/cgroup-reader.js";

let tmpDir: string;
let tracker: CostTracker;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-idle-"));
  _setResultForTests({
    provider: "aws",
    region: "us-east-1",
    source: "imds",
    instanceType: "c7g.xlarge",
  });
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

/**
 * Run a single task end-to-end with mocked cgroup reads. Returns the final
 * task.computeCostUsd.
 */
function runTask(opts: {
  startOffsetSec: number;
  durationSec: number;
  cpuUsedSeconds: number;
  runtime?: RuntimeKind;
}): number {
  const runtime = opts.runtime ?? RuntimeKind.Ec2;

  // Restore + re-mock so each task gets fresh start/end behavior.
  vi.restoreAllMocks();
  const startSpy = vi.spyOn(cgroup, "readCpuStat").mockReturnValue({ usageUsec: 0 });

  const tt = tracker.startTask({ taskType: "x" });
  const accountant = new ComputeAccountant({
    runtime,
    region: "us-east-1",
    architecture: "x86_64",
    fargateVcpu: 4.0,
    fargateMemoryMib: 8192,
  });
  accountant.snapshotStart();
  (tt.task as any)._compute = accountant;
  // Backdate started_at so duration_ms = durationSec * 1000.
  // tt.end() overwrites endedAt with now(); set startedAt accordingly.
  tt.task.startedAt = new Date(Date.now() - opts.durationSec * 1000);

  // Now swap to END behavior — same spy.
  const usageUsec = Math.trunc(opts.cpuUsedSeconds * 1_000_000);
  startSpy.mockReturnValue({ usageUsec });
  vi.spyOn(cgroup, "readCpuMax").mockReturnValue({
    quotaUs: 400000,
    periodUs: 100000,
    vcpuCount: 4.0,
  });
  vi.spyOn(cgroup, "readMemoryPeak").mockReturnValue(512 * 1024 * 1024);
  vi.spyOn(cgroup, "readMemoryMax").mockReturnValue(8 * 1024 * 1024 * 1024);

  tt.end("success");
  return tt.task.computeCostUsd;
}

describe("Decision #9 + #10 — idle is invisible", () => {
  test("EC2 idle between tasks is invisible (Decision #9)", () => {
    // Two 60s tasks with 600s idle between them on a 4 vCPU c7g.xlarge
    // ($0.1450/hr). The cloud bill for the FULL 720s window = 720/3600 *
    // 0.1450 = $0.029. dexcost MUST report STRICTLY LESS — the 600s idle
    // gap is excluded by design.
    const a = runTask({ startOffsetSec: 0, durationSec: 60, cpuUsedSeconds: 10 });
    const b = runTask({ startOffsetSec: 660, durationSec: 60, cpuUsedSeconds: 10 });
    const total = new Decimal(a).plus(b);

    const fullWindowCloudShare = new Decimal(720).dividedBy(3600).times("0.1450");
    expect(
      total.lt(fullWindowCloudShare),
      `dexcost total ${total} must be < cloud share ${fullWindowCloudShare} ` +
        `on long-running runtimes — the 600s idle gap is by design (Decision #9). ` +
        `If this test starts failing because total grew, check whether a refactor ` +
        `added synthetic idle pseudo-tasks.`,
    ).toBe(true);
    expect(total.gt(0)).toBe(true);
  });

  test("Fargate container idle tail is invisible (Decision #10)", () => {
    // 3 back-to-back Fargate tasks, then 3000s container idle tail.
    const a = runTask({
      startOffsetSec: 0,
      durationSec: 10,
      cpuUsedSeconds: 2,
      runtime: RuntimeKind.Fargate,
    });
    const b = runTask({
      startOffsetSec: 10,
      durationSec: 10,
      cpuUsedSeconds: 2,
      runtime: RuntimeKind.Fargate,
    });
    const c = runTask({
      startOffsetSec: 20,
      durationSec: 10,
      cpuUsedSeconds: 2,
      runtime: RuntimeKind.Fargate,
    });
    const total = new Decimal(a).plus(b).plus(c);

    // Trivial upper bound: full-container-lifetime cost.
    const containerLifetimeS = new Decimal(3030);
    const fullWindowCloudShare = new Decimal("4.0")
      .times(containerLifetimeS)
      .times("0.0000111111");
    expect(
      total.lt(fullWindowCloudShare),
      `dexcost total ${total} must be < full-container-lifetime cost ` +
        `${fullWindowCloudShare} (Decision #10). The 50-minute idle tail is ` +
        `invisible by design.`,
    ).toBe(true);
    expect(total.gt(0)).toBe(true);
  });
});
