/**
 * Serverless GPU handler wraps — Modal / RunPod / Replicate.
 * Mirrors python/tests/test_gpu_wrap.py.
 */

import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import * as dexcost from "../src/index.js";
import {
  wrapModalHandler,
  wrapRunpodHandler,
  wrapReplicateHandler,
  _setGpuAccountantHooksForTests,
} from "../src/adapters/gpu-wrap.js";
import { runWithTask } from "../src/core/context.js";
import type { GpuAccountantHooks } from "../src/core/gpu-accountant.js";
import { _resetWarningStateForTests as resetAccountantWarnings } from "../src/core/gpu-accountant.js";
import type { UtilSample } from "../src/core/nvml-reader.js";

let tmpDir: string;
let snapshot: Record<string, string | undefined> = {};
const ENV_KEYS = [
  "MODAL_TASK_ID",
  "RUNPOD_POD_ID",
  "REPLICATE_MODEL",
];

const SELF = process.pid;

function stubHooks(): GpuAccountantHooks {
  let utilCall = 0;
  const samples: Array<Record<number, UtilSample[]>> = [
    {},
    {
      [SELF]: [
        {
          pid: SELF,
          smUtil: 50,
          memUtil: 20,
          timeStamp: 500_000,
        },
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
    classifyScope: () => ({ kind: "container", path: "/docker/abc" }),
    enumeratePids: () => [SELF],
    getProcessUtilization: () => samples[utilCall++] ?? {},
  };
}

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-gpu-wrap-"));
  snapshot = {};
  for (const k of ENV_KEYS) {
    snapshot[k] = process.env[k];
    delete process.env[k];
  }
  try {
    dexcost.close();
  } catch {
    // not initialized
  }
  resetAccountantWarnings();
});

afterEach(() => {
  try {
    dexcost.close();
  } catch {
    // already closed
  }
  for (const [k, v] of Object.entries(snapshot)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  _setGpuAccountantHooksForTests(null);
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("wrapModalHandler", () => {
  test("emits 1 gpu_cost + 1 gpu_utilization_signal event", async () => {
    process.env.MODAL_TASK_ID = "task-abc";
    _setGpuAccountantHooksForTests(stubHooks());
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });
    const tt = tracker.startTask({ taskType: "modal" });
    const handler = wrapModalHandler(async (_payload: unknown) => ({
      result: "ok",
    }));
    const result = await runWithTask(tt.task, () => handler({ input: 42 }));
    expect(result).toEqual({ result: "ok" });

    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const cost = events.filter((e) => e.eventType === "gpu_cost");
    const sig = events.filter((e) => e.eventType === "gpu_utilization_signal");
    expect(cost.length).toBe(1);
    expect(sig.length).toBe(1);
    const det = cost[0].details as Record<string, any>;
    expect(det.billing_model).toBe("per_gpu_second_active");
    expect(det.cost_pending).toBe(true);
    expect(cost[0].costUsd.toNumber()).toBe(0); // back-filled at task finalize
    // gpu_utilization_signal has cost_usd=0 (Decision #3 observability carve-out)
    expect(sig[0].costUsd.toNumber()).toBe(0);
    expect(sig[0].pricingSource).toBeUndefined();
    const sigDet = sig[0].details as Record<string, any>;
    expect(sigDet).toHaveProperty("sm_util_pct");
    expect(sigDet).toHaveProperty("vram_used_peak_bytes");
  });
});

describe("wrapRunpodHandler", () => {
  test("billing_model=per_gpu_second_active", async () => {
    process.env.RUNPOD_POD_ID = "pod-xyz";
    _setGpuAccountantHooksForTests(stubHooks());
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });
    const tt = tracker.startTask({ taskType: "runpod" });
    const handler = wrapRunpodHandler(async () => "ok");
    await runWithTask(tt.task, () => handler());
    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const cost = events.filter((e) => e.eventType === "gpu_cost");
    expect(cost.length).toBe(1);
    expect((cost[0].details as Record<string, any>).billing_model).toBe(
      "per_gpu_second_active",
    );
  });
});

describe("wrapReplicateHandler", () => {
  test("billing_model=per_gpu_second_active", async () => {
    process.env.REPLICATE_MODEL = "owner/model";
    _setGpuAccountantHooksForTests(stubHooks());
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });
    const tt = tracker.startTask({ taskType: "replicate" });
    const handler = wrapReplicateHandler(async (_payload: unknown) => ({}));
    await runWithTask(tt.task, () => handler({ x: 1 }));
    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const cost = events.filter((e) => e.eventType === "gpu_cost");
    expect(cost.length).toBe(1);
    expect((cost[0].details as Record<string, any>).billing_model).toBe(
      "per_gpu_second_active",
    );
  });
});

describe("no active task → transparent pass-through", () => {
  test("Modal wrap returns handler result without persisting events", async () => {
    process.env.MODAL_TASK_ID = "task-abc";
    _setGpuAccountantHooksForTests(stubHooks());
    const handler = wrapModalHandler(async (x: number) => x * 2);
    // No tracker initialized; no task in context.
    const result = await handler(21);
    expect(result).toBe(42);
  });
});

describe("handler exception → event still emitted, exception re-raised", () => {
  test("ValueError from handler → cost event persisted + exception bubbles", async () => {
    process.env.MODAL_TASK_ID = "task-abc";
    _setGpuAccountantHooksForTests(stubHooks());
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });
    const tt = tracker.startTask({ taskType: "modal" });
    const handler = wrapModalHandler(async () => {
      throw new Error("simulated handler failure");
    });
    await expect(
      runWithTask(tt.task, () => handler({})),
    ).rejects.toThrow(/simulated/);

    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const cost = events.filter((e) => e.eventType === "gpu_cost");
    expect(cost.length).toBe(1);
  });
});

describe("exports", () => {
  test("all three wraps exported from top-level", () => {
    expect(typeof (dexcost as any).wrapModalHandler).toBe("function");
    expect(typeof (dexcost as any).wrapRunpodHandler).toBe("function");
    expect(typeof (dexcost as any).wrapReplicateHandler).toBe("function");
  });
});
