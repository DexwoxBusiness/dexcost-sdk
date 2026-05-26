/**
 * B2 regression — Sprint 2 Theme C / plan §3.1.1 (TS port of Python d37b6b5
 * and Go bbe1133).
 *
 * Pre-fix the TS GPU accountant treated NVML's per-PID `timeStamp` as if
 * it were "accumulated SM-microseconds" and emitted
 * `gpu_seconds_used = max_ts - base_ts` per device — reporting wall
 * time × 100% utilization instead of integrated SM utilization.
 *
 * Post-fix the accountant integrates `sm_util × dt` across the sample
 * sequence NVML returns, matching the Python canonical implementation.
 */

import { describe, it, expect } from "vitest";

import { GpuAccountant } from "../src/core/gpu-accountant.js";
import { GpuRuntimeKind } from "../src/core/gpu-runtime.js";
import type { UtilSample } from "../src/core/nvml-reader.js";

const PID = 12345;

function cloud() {
  return { provider: "modal" as const, region: null, source: "env" as const };
}

describe("GPU SM-time integration (B2 TS port)", () => {
  it("gpu_seconds_used integrates sm_util × dt across samples", () => {
    let call = 0;
    const samples: Array<Record<number, UtilSample[]>> = [
      {}, // baseline — no PIDs at start
      {
        // PID at t=20s sm=80% → covers 0..20s → 16 sm-sec
        // PID at t=60s sm=40% → covers 20..60s → 16 sm-sec
        // Canonical total: 32 sm-seconds.
        [PID]: [
          { pid: PID, smUtil: 80, memUtil: 10, timeStamp: 20_000_000 },
          { pid: PID, smUtil: 40, memUtil: 10, timeStamp: 60_000_000 },
        ],
      },
    ];
    const acc = new GpuAccountant(GpuRuntimeKind.Modal, cloud(), {
      initNvml: () => true,
      shutdownNvml: () => {},
      getDeviceCount: () => 1,
      getDeviceHandle: (i) => `h${i}`,
      getProductName: () => "NVIDIA H100 80GB HBM3",
      getMigMode: () => false,
      getMemoryInfo: () => ({ usedBytes: 21474836480, totalBytes: 85899345920 }),
      getProcessUtilization: () => samples[call++] ?? {},
      classifyScope: () => ({ kind: "container", path: "/docker/abc" }),
      enumeratePids: () => [PID],
    });
    acc.snapshotStart();
    const { costDetails } = acc.snapshotEndAndBuild(60_000);

    expect(costDetails).not.toBeNull();
    const gpuSeconds = costDetails!.gpu_seconds_used;
    expect(gpuSeconds).toBeGreaterThanOrEqual(31.9);
    expect(gpuSeconds).toBeLessThanOrEqual(32.1);
  });
});
