/**
 * End-to-end: long-running EC2 GPU task emits dual events + back-fills cost.
 * Mirrors python/tests/test_gpu_auto_emission_and_back_fill.py.
 */

import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { SpawnSyncReturns } from "node:child_process";

import * as dexcost from "../src/index.js";
import { Decimal } from "../src/core/models.js";
import { GpuAccountant } from "../src/core/gpu-accountant.js";
import { GpuRuntimeKind } from "../src/core/gpu-runtime.js";
import {
  _resetCloudDetectForTests,
  _setResultForTests,
  type CloudEnv,
} from "../src/cloud-detect.js";
import { _setSpawnFnForTests } from "../src/core/nvml-reader.js";

let tmpDir: string;
const SELF = process.pid;

function awsP5CloudEnv(): CloudEnv {
  return {
    provider: "aws",
    region: "us-east-1",
    source: "imds",
    instanceType: "p5.48xlarge",
  };
}

function stubHooks(timeStampMicros: number) {
  let call = 0;
  const samples = [
    {},
    {
      [SELF]: [
        { pid: SELF, smUtil: 50, memUtil: 30, timeStamp: timeStampMicros },
      ],
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
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-gpu-emission-"));
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
  _setSpawnFnForTests(null);
  rmSync(tmpDir, { recursive: true, force: true });
});

function smiResult(stdout: string): SpawnSyncReturns<string> {
  return {
    pid: 1,
    output: ["", stdout, ""],
    stdout,
    stderr: "",
    status: 0,
    signal: null,
  } as SpawnSyncReturns<string>;
}

function installLocalSmi(): void {
  _setSpawnFnForTests((_cmd, args) => {
    const joined = args.join(" ");
    if (joined.includes("--query-gpu=count")) return smiResult("1\n");
    if (joined.includes("--query-gpu=name")) return smiResult("NVIDIA L4 24GB\n");
    if (joined.includes("--query-gpu=mig.mode.current")) return smiResult("Disabled\n");
    if (joined.includes("--query-gpu=memory.used,memory.total")) return smiResult("1024, 24576\n");
    if (args[0] === "pmon") return smiResult(`0 ${SELF} C 50 20 - - whisper\n`);
    return smiResult("");
  });
}

describe("EC2 GPU task emits dual events and back-fills cost", () => {
  test("long-running EC2 p5: gpu_cost back-filled; gpu_utilization_signal stays cost_usd=0", async () => {
    _setResultForTests(awsP5CloudEnv());
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });
    // 30s active GPU-microseconds = 30_000_000
    const hooks = stubHooks(30_000_000);
    const accountant = new GpuAccountant(
      GpuRuntimeKind.AwsEc2Gpu,
      awsP5CloudEnv(),
      hooks,
    );
    accountant.snapshotStart();

    const tt = tracker.startTask({ taskType: "ec2-gpu" });
    (tt.task as any)._gpu = accountant;
    // Simulate a 60-second window by back-dating startedAt.
    tt.task.startedAt = new Date(Date.now() - 60_000);
    tt.end("success");

    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const costEvs = events.filter((e) => e.eventType === "gpu_cost");
    const sigEvs = events.filter(
      (e) => e.eventType === "gpu_utilization_signal",
    );
    expect(costEvs.length).toBe(1);
    const ev = costEvs[0];
    expect(ev.costUsd.toNumber()).toBeGreaterThan(0);
    expect(ev.pricingSource).toContain("gpu_catalog:aws:ec2_gpu:");
    expect(ev.costConfidence).toBe("computed");
    expect(ev.pricingVersion).toMatch(/^gpu:/);
    expect((ev.details as Record<string, unknown>).cost_pending).toBeUndefined();

    expect(sigEvs.length).toBeGreaterThanOrEqual(1);
    for (const sig of sigEvs) {
      expect(sig.costUsd.toNumber()).toBe(0); // NEVER back-filled per Decision #3
    }

    expect(tt.task.gpuCostUsd.toString()).toBe(ev.costUsd.toString());
    const expectedTotal = tt.task.llmCostUsd
      .plus(tt.task.externalCostUsd)
      .plus(tt.task.computeCostUsd)
      .plus(tt.task.networkCostUsd)
      .plus(tt.task.gpuCostUsd);
    // Decimal arithmetic — exact equality of the rolled-up total.
    expect(tt.task.totalCostUsd.minus(expectedTotal).abs().toNumber()).toBeLessThan(1e-9);
  });
});

describe("Unknown GPU runtime emits nothing", () => {
  test("Task without _gpu accountant → no GPU events, gpuCostUsd=0", async () => {
    _setResultForTests({
      provider: null,
      region: null,
      source: "none",
      instanceType: null,
    });
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });
    const tt = tracker.startTask({ taskType: "no-gpu" });
    tt.end("success");
    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const gpuEvs = events.filter(
      (e) =>
        e.eventType === "gpu_cost" ||
        e.eventType === "gpu_utilization_signal",
    );
    expect(gpuEvs.length).toBe(0);
    expect(tt.task.gpuCostUsd.toNumber()).toBe(0);
  });
});

describe("local GPU usage-only opt in", () => {
  test("measures the owning leaf task without inventing hardware cost", async () => {
    _setResultForTests({ provider: null, region: null, source: "none", instanceType: null });
    installLocalSmi();
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"), autoInstrument: [], storage: "local", trackHttp: false,
    });
    const root = tracker.startTask({ taskType: "campaign-root" });
    expect((root.task as any)._gpu).toBeUndefined();
    const whisper = tracker.startTask({ taskType: "local-whisper", trackGpu: true });
    expect((whisper.task as any)._gpu?.runtime).toBe(GpuRuntimeKind.LocalGpu);
    await new Promise((resolve) => setTimeout(resolve, 5));
    whisper.end("success");
    root.end("success");

    const events = tracker.buffer.queryEvents(whisper.task.taskId);
    const cost = events.find((event) => event.eventType === "gpu_cost");
    expect(cost).toBeDefined();
    expect(cost?.costUsd.toString()).toBe("0");
    expect(cost?.pricingSource).toMatch(/^unpriced:local_gpu/);
    expect(cost?.costConfidence).toBe("unknown");
    expect(cost?.pricingVersion).toBeUndefined();
    expect(cost?.details).toMatchObject({
      billing_model: "local_gpu_usage_only",
      runtime_kind: "local_gpu",
      cloud_provider: null,
    });
    expect(tracker.buffer.queryEvents(root.task.taskId).filter(
      (event) => event.eventType === "gpu_cost",
    )).toHaveLength(0);
  });
});

describe("Decision #3 carve-out: signal events NEVER aggregated into gpuCostUsd", () => {
  test("LOAD-BEARING — signal events stay at cost_usd=0 and don't bump task.gpuCostUsd", async () => {
    _setResultForTests(awsP5CloudEnv());
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });
    const hooks = stubHooks(10_000_000);
    const accountant = new GpuAccountant(
      GpuRuntimeKind.AwsEc2Gpu,
      awsP5CloudEnv(),
      hooks,
    );
    accountant.snapshotStart();
    const tt = tracker.startTask({ taskType: "ec2-gpu" });
    (tt.task as any)._gpu = accountant;
    tt.task.startedAt = new Date(Date.now() - 60_000);
    tt.end("success");

    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const gpuCostSum = events
      .filter((e) => e.eventType === "gpu_cost")
      .reduce((acc, e) => acc.plus(e.costUsd), new Decimal(0));
    const signalCount = events.filter(
      (e) => e.eventType === "gpu_utilization_signal",
    ).length;
    expect(signalCount).toBeGreaterThanOrEqual(1);
    // The Decision #3 convention §1 carve-out: task.gpuCostUsd is the sum
    // of gpu_cost events ONLY; signals contribute zero.
    expect(tt.task.gpuCostUsd.toString()).toBe(gpuCostSum.toString());
  });
});
