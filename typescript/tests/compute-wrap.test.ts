/**
 * Handler wraps emit compute_cost events per invocation and pass through
 * when no dexcost task is in context (capture spec §6 case 2).
 *
 * Ports python/tests/test_compute_wrap.py (6 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import * as dexcost from "../src/index.js";
import { CostTracker } from "../src/core/tracker.js";
import { wrapLambdaHandler } from "../src/adapters/compute-wrap.js";
import { runWithTask } from "../src/core/context.js";
import * as cgroup from "../src/core/cgroup-reader.js";

let tmpDir: string;
let snapshot: Record<string, string | undefined> = {};

const ENV_KEYS = [
  "AWS_LAMBDA_FUNCTION_NAME",
  "AWS_LAMBDA_FUNCTION_MEMORY_SIZE",
  "AWS_LAMBDA_INITIALIZATION_TYPE",
  "AWS_REGION",
  "AWS_DEFAULT_REGION",
];

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "dexcost-cw-"));
  snapshot = {};
  for (const k of ENV_KEYS) {
    snapshot[k] = process.env[k];
    delete process.env[k];
  }
  // Reset the global tracker singleton if present.
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
  vi.restoreAllMocks();
  for (const [k, v] of Object.entries(snapshot)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("wrapLambdaHandler", () => {
  test("emits a compute_cost event with cost_pending=true", async () => {
    process.env.AWS_LAMBDA_FUNCTION_NAME = "fn";
    process.env.AWS_LAMBDA_FUNCTION_MEMORY_SIZE = "1024";
    process.env.AWS_LAMBDA_INITIALIZATION_TYPE = "on-demand";
    process.env.AWS_REGION = "us-east-1";

    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });

    vi.spyOn(cgroup, "readMemoryPeak").mockReturnValue(256 * 1024 * 1024);

    const tt = tracker.startTask({ taskType: "lambda" });

    const handler = wrapLambdaHandler(async (_event: unknown, _ctx: unknown) => {
      return { statusCode: 200 };
    });

    const result = await runWithTask(tt.task, () => handler({}, {}));
    expect(result).toEqual({ statusCode: 200 });

    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const compute = events.filter((e) => e.eventType === "compute_cost");
    expect(compute.length).toBe(1);
    const details = compute[0].details as Record<string, any>;
    expect(details.billing_model).toBe("lambda");
    expect(details.invocation_count).toBe(1);
    // 1024 MB → DECIMAL bytes (AWS env var convention).
    expect(details.memory_bytes_limit).toBe(1024 * 1_000_000);
    expect(details.memory_bytes_peak).toBe(256 * 1024 * 1024);
    expect(details.initialization_type).toBe("on-demand");
    expect(details.region).toBe("us-east-1");
    expect(["x86_64", "arm64"]).toContain(details.architecture);
    expect(details.cost_pending).toBe(true);
    expect(compute[0].costUsd.toNumber()).toBe(0); // back-fills at task finalize
  });

  test("no active task → pass-through (capture spec §6 case 2)", async () => {
    const handler = wrapLambdaHandler(async () => "ok");
    const result = await handler({}, {});
    expect(result).toBe("ok");
  });

  test("handler exception still emits event (capture spec §6 case 7)", async () => {
    process.env.AWS_LAMBDA_FUNCTION_NAME = "fn";
    process.env.AWS_LAMBDA_FUNCTION_MEMORY_SIZE = "512";
    process.env.AWS_REGION = "us-east-1";

    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });

    vi.spyOn(cgroup, "readMemoryPeak").mockReturnValue(0);

    const tt = tracker.startTask({ taskType: "lambda" });

    const handler = wrapLambdaHandler(async (_event: unknown, _ctx: unknown) => {
      throw new Error("simulated handler failure");
    });

    await expect(
      runWithTask(tt.task, () => handler({}, {})),
    ).rejects.toThrow(/simulated/);

    const events = tracker.buffer.queryEvents(tt.task.taskId);
    const compute = events.filter((e) => e.eventType === "compute_cost");
    expect(compute.length).toBe(1);
    const details = compute[0].details as Record<string, any>;
    expect(details.billing_model).toBe("lambda");
  });

  test("computeBillingOverrides threaded through init()", () => {
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
      computeBillingOverrides: { cloud_run: "instance" },
      k8sNodeAware: true,
    });
    expect(tracker.computeBillingOverrides).toEqual({ cloud_run: "instance" });
    expect(tracker.k8sNodeAware).toBe(true);
  });

  test("computeBillingOverrides defaults to empty", () => {
    const tracker = dexcost.init({
      dbPath: join(tmpDir, "buf.db"),
      autoInstrument: [],
      storage: "local",
      trackHttp: false,
    });
    expect(tracker.computeBillingOverrides).toEqual({});
    expect(tracker.k8sNodeAware).toBe(false);
  });

  test("all five wraps exported from top-level", async () => {
    const idx = await import("../src/index.js");
    expect(typeof idx.wrapLambdaHandler).toBe("function");
    expect(typeof idx.wrapCloudRunHandler).toBe("function");
    expect(typeof idx.wrapCloudFunctionsHandler).toBe("function");
    expect(typeof idx.wrapAzureFunctionsHandler).toBe("function");
    expect(typeof idx.wrapVercelHandler).toBe("function");
  });
});
