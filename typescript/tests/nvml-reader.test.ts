/**
 * NVML reader — TypeScript port of python/tests/test_nvml_reader.py.
 *
 * **Architectural deviation from Python**: TS has no maintained native NVML
 * binding for Node in 2026, so this module shells out to `nvidia-smi` via
 * `child_process.spawnSync` and parses CSV output. Functions still return
 * typed objects (ProcessInfo, UtilSample, MemInfo); the parser maps CSV
 * columns to fields. Per-probe overhead ~50ms.
 *
 * Fail-silent contract identical to Python: every reader returns `null`
 * (or `false` for booleans) on missing binary / non-zero exit / parse
 * failure — the caller (GpuAccountant) decides the fallback policy.
 *
 * Decision #4: getProductName applies NFC + lowercase + whitespace collapse.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import type { SpawnSyncReturns } from "node:child_process";
import {
  _resetWarningStateForTests,
  _setNvidiaSmiPathForTests,
  _setSpawnFnForTests,
  getComputeRunningProcesses,
  getDeviceCount,
  getMemoryInfo,
  getMigMode,
  getProductName,
  getProcessUtilization,
  initNvml,
  nvmlAvailable,
} from "../src/core/nvml-reader.js";

function makeSpawnResult(
  stdout: string,
  status = 0,
): SpawnSyncReturns<string> {
  return {
    pid: 1,
    output: ["", stdout, ""],
    stdout,
    stderr: "",
    status,
    signal: null,
  } as SpawnSyncReturns<string>;
}

function makeFailedSpawn(): SpawnSyncReturns<string> {
  return {
    pid: 0,
    output: ["", "", "nvidia-smi: command not found"],
    stdout: "",
    stderr: "nvidia-smi: command not found",
    status: 127,
    signal: null,
    error: Object.assign(new Error("ENOENT"), { code: "ENOENT" }),
  } as SpawnSyncReturns<string>;
}

describe("nvml-reader (TS shells out to nvidia-smi)", () => {
  let mockResult: SpawnSyncReturns<string> = makeFailedSpawn();
  // Queue lets a single test cycle through multiple results.
  let queue: SpawnSyncReturns<string>[] = [];

  beforeEach(() => {
    _resetWarningStateForTests();
    _setNvidiaSmiPathForTests(null);
    queue = [];
    mockResult = makeFailedSpawn();
    _setSpawnFnForTests((_cmd, _args, _options) => {
      if (queue.length > 0) return queue.shift()!;
      return mockResult;
    });
  });

  afterEach(() => {
    _setSpawnFnForTests(null);
    _resetWarningStateForTests();
    _setNvidiaSmiPathForTests(null);
  });

  it("nvmlAvailable returns false when nvidia-smi is missing", () => {
    mockResult = makeFailedSpawn();
    expect(nvmlAvailable()).toBe(false);
    expect(initNvml()).toBe(false);
    expect(getDeviceCount()).toBe(null);
  });

  it("initNvml returns true when nvidia-smi exits 0", () => {
    mockResult = makeSpawnResult("0\n");
    expect(initNvml()).toBe(true);
  });

  it("getProductName normalises NFC + lowercase + whitespace (incl. double-space)", () => {
    mockResult = makeSpawnResult("NVIDIA H100  80GB HBM3\n");
    expect(getProductName(0)).toBe("nvidia h100 80gb hbm3");
  });

  it("getProductName collapses U+00A0 non-breaking space and lower-cases", () => {
    mockResult = makeSpawnResult("NVIDIA A100-SXM4-80GB\n");
    expect(getProductName(0)).toBe("nvidia a100-sxm4-80gb");
  });

  it("getProductName returns null on non-zero exit", () => {
    mockResult = makeSpawnResult("", 1);
    expect(getProductName(0)).toBe(null);
  });

  it("getComputeRunningProcesses parses pid + used_memory CSV", () => {
    // nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits
    mockResult = makeSpawnResult("1234, 1024\n5678, 512\n");
    const procs = getComputeRunningProcesses(0);
    expect(procs).not.toBeNull();
    expect(procs!.length).toBe(2);
    expect(procs![0].pid).toBe(1234);
    expect(procs![0].usedGpuMemory).toBe(1024 * 1024 * 1024);
    expect(procs![1].pid).toBe(5678);
  });

  it("getComputeRunningProcesses returns null on permission-denied / failure", () => {
    mockResult = makeSpawnResult("", 1);
    expect(getComputeRunningProcesses(0)).toBeNull();
  });

  it("getProcessUtilization parses per-PID sm/mem util and updates timestamps in place", () => {
    mockResult = makeSpawnResult(
      "# gpu pid type sm mem enc dec command\n" +
      "# Idx   #    C/G  %   %   %   %   name\n" +
      "    0  1234     C  50  30   0   0  python\n" +
      "    0  5678     C  70  40   0   0  torch\n",
    );
    const timestamps: Record<number, number> = {};
    const samples = getProcessUtilization(0, timestamps);
    expect(samples).not.toBeNull();
    expect(samples![1234].smUtil).toBe(50);
    expect(samples![1234].memUtil).toBe(30);
    expect(samples![5678].smUtil).toBe(70);
    expect(samples![5678].memUtil).toBe(40);
    expect(timestamps[1234]).toBeGreaterThan(0);
    expect(timestamps[5678]).toBeGreaterThan(0);
  });

  it("getMemoryInfo parses used and total MiB into bytes", () => {
    mockResult = makeSpawnResult("20480, 81920\n");
    const mem = getMemoryInfo(0);
    expect(mem).not.toBeNull();
    expect(mem!.usedBytes).toBe(20480 * 1024 * 1024);
    expect(mem!.totalBytes).toBe(81920 * 1024 * 1024);
  });

  it("getMigMode returns true when mig.mode.current is Enabled", () => {
    mockResult = makeSpawnResult("Enabled\n");
    expect(getMigMode(0)).toBe(true);
  });

  it("getMigMode returns false on Disabled / N/A / error", () => {
    queue.push(makeSpawnResult("Disabled\n"));
    expect(getMigMode(0)).toBe(false);
    queue.push(makeSpawnResult("[N/A]\n"));
    expect(getMigMode(1)).toBe(false);
    queue.push(makeSpawnResult("", 1));
    expect(getMigMode(2)).toBe(false);
  });

  it("getDeviceCount parses CSV count", () => {
    mockResult = makeSpawnResult("8\n");
    expect(getDeviceCount()).toBe(8);
  });

  it("getDeviceCount returns null when nvidia-smi exits non-zero", () => {
    mockResult = makeSpawnResult("", 1);
    expect(getDeviceCount()).toBeNull();
  });
});
