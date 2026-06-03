/**
 * Decision #3 + convention §1 carve-out — gpu_utilization_signal is observability-only.
 * Mirrors python/tests/test_gpu_utilization_signal_observability.py.
 *
 * LOAD-BEARING TEST. The convention §1 carve-out says: signal events have
 * NO cost_usd / pricing_source / cost_confidence / pricing_version, and
 * the Control Layer must NEVER aggregate them into any cost field. This
 * test pins that contract as executable code.
 */

import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import * as dexcost from "../src/index.js";
import { GpuAccountant } from "../src/core/gpu-accountant.js";
import { GpuRuntimeKind } from "../src/core/gpu-runtime.js";
import {
  _resetCloudDetectForTests,
  _setResultForTests,
  type CloudEnv,
} from "../src/cloud-detect.js";

let tmpDir: string;
const SELF = process.pid;

function awsP5(): CloudEnv {
  return {
    provider: "aws",
    region: "us-east-1",
    source: "imds",
    instanceType: "p5.48xlarge",
  };
}

function stubHooks() {
  let call = 0;
  const samples = [
    {},
    {
      [SELF]: [{ pid: SELF, smUtil: 50, memUtil: 30, timeStamp: 30_000_000 }],
    },
  ];
  return {
    initNvml: () => true,
    getDeviceCount: () => 1,
    getProductName: () => "nvidia h100 80gb hbm3",
    getMigMode: () => false,
    getMemoryInfo: () => ({
      usedBytes: 2 * 1024 ** 3,
      totalBytes: 80 * 1024 ** 3,
    }),
    classifyScope: () => ({
      kind: "container" as const,
      path: "/docker/abc",
    }),
    enumeratePids: () => [SELF],
    getProcessUtilization: () => (samples[call++] as any) ?? {},
  };
}

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-gpu-signals-"));
  try {
    dexcost.close();
  } catch {
    // not initialized
  }
});

afterEach(() => {
  try {
    dexcost.close();
  } catch {
    // already closed
  }
  _resetCloudDetectForTests();
  rmSync(tmpDir, { recursive: true, force: true });
});

function emitAndFinalize(): { tracker: any; task: any } {
  _setResultForTests(awsP5());
  const tracker = dexcost.init({
    dbPath: join(tmpDir, "buf.db"),
    autoInstrument: [],
    storage: "local",
    trackHttp: false,
  });
  const accountant = new GpuAccountant(
    GpuRuntimeKind.AwsEc2Gpu,
    awsP5(),
    stubHooks(),
  );
  accountant.snapshotStart();
  const tt = tracker.startTask({ taskType: "x" });
  (tt.task as any)._gpu = accountant;
  tt.task.startedAt = new Date(Date.now() - 60_000);
  tt.end("success");
  return { tracker, task: tt.task };
}

describe("gpu_utilization_signal observability carve-out (Decision #3)", () => {
  test("signal events have cost_usd=0 after back-fill (NEVER priced)", () => {
    const { tracker, task } = emitAndFinalize();
    const events = tracker.buffer.queryEvents(task.taskId);
    const sigs = events.filter(
      (e: any) => e.eventType === "gpu_utilization_signal",
    );
    expect(sigs.length).toBeGreaterThanOrEqual(1);
    for (const sig of sigs) {
      expect(sig.costUsd.toNumber()).toBe(0);
      expect((sig.details as Record<string, unknown>).cost_pending).toBeUndefined();
    }
  });

  test("LOAD-BEARING: signal events NEVER aggregated into task.gpuCostUsd", () => {
    const { tracker, task } = emitAndFinalize();
    const events = tracker.buffer.queryEvents(task.taskId);
    const gpuCostSum = events
      .filter((e: any) => e.eventType === "gpu_cost")
      .reduce((acc: number, e: any) => acc + e.costUsd.toNumber(), 0);
    const signalCount = events.filter(
      (e: any) => e.eventType === "gpu_utilization_signal",
    ).length;
    expect(signalCount).toBeGreaterThanOrEqual(1);
    expect(task.gpuCostUsd.toNumber()).toBe(gpuCostSum);
  });

  test("signal events carry the load-bearing observability fields", () => {
    const { tracker, task } = emitAndFinalize();
    const events = tracker.buffer.queryEvents(task.taskId);
    const sigs = events.filter(
      (e: any) => e.eventType === "gpu_utilization_signal",
    );
    expect(sigs.length).toBeGreaterThanOrEqual(1);
    const details = sigs[0].details as Record<string, unknown>;
    for (const field of [
      "gpu_index",
      "gpu_sku",
      "sm_util_pct",
      "mem_util_pct",
      "vram_used_peak_bytes",
      "vram_total_bytes",
      "process_count",
      "sample_count",
      "task_duration_ms",
    ]) {
      expect(details).toHaveProperty(field);
    }
  });

  test("signal events have no pricing_source or pricing_version", () => {
    const { tracker, task } = emitAndFinalize();
    const events = tracker.buffer.queryEvents(task.taskId);
    const sigs = events.filter(
      (e: any) => e.eventType === "gpu_utilization_signal",
    );
    for (const sig of sigs) {
      expect(sig.pricingSource).toBeUndefined();
      expect(sig.pricingVersion).toBeUndefined();
    }
  });
});
