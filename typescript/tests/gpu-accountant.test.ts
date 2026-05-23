/**
 * Per-task GPU accountant — cgroup walk + NVML snapshot pair + dual-event emission.
 * Mirrors python/tests/test_gpu_accountant.py.
 *
 * The TS deviation is dependency injection: the GpuAccountant constructor
 * accepts an Options object with nvml/cgroup hooks (matching the gpu-runtime
 * pattern) so tests inject deterministic stubs.
 */

import { describe, it, expect, beforeEach } from "vitest";
import {
  GpuAccountant,
  _resetWarningStateForTests,
  type GpuAccountantHooks,
} from "../src/core/gpu-accountant.js";
import { GpuRuntimeKind } from "../src/core/gpu-runtime.js";
import type { CloudEnv } from "../src/cloud-detect.js";
import type { CgroupScope } from "../src/core/cgroup-walker.js";
import type { UtilSample, MemInfo } from "../src/core/nvml-reader.js";

function cloud(
  provider: string | null,
  region: string | null,
  source: "env" | "imds" | "dmi" | "none",
  instanceType?: string | null,
): CloudEnv {
  return { provider, region, source, instanceType: instanceType ?? null };
}

const SELF = process.pid;

function defaultMemInfo(): MemInfo {
  return { usedBytes: 21474836480, totalBytes: 85899345920 };
}

function baseHooks(
  overrides: Partial<GpuAccountantHooks> = {},
): GpuAccountantHooks {
  return {
    initNvml: () => true,
    getDeviceCount: () => 1,
    getProductName: (_i) => "nvidia h100 80gb hbm3",
    getMigMode: (_i) => false,
    getMemoryInfo: (_i) => defaultMemInfo(),
    classifyScope: (): CgroupScope => ({ kind: "container", path: "/docker/abc" }),
    enumeratePids: (_scope) => [SELF],
    getProcessUtilization: (_i, _ts) => ({}),
    ...overrides,
  };
}

describe("GpuAccountant — Modal serverless emission", () => {
  beforeEach(() => _resetWarningStateForTests());

  it("emits one gpu_cost + one gpu_utilization_signal per device", () => {
    let call = 0;
    const samples: Array<Record<number, UtilSample>> = [
      {}, // baseline
      {
        [SELF]: {
          pid: SELF,
          smUtil: 80,
          memUtil: 30,
          timeStamp: 1_234_000,
        },
      },
    ];
    const hooks = baseHooks({
      getProcessUtilization: (_i, _ts) => samples[call++] ?? {},
    });
    const acc = new GpuAccountant(
      GpuRuntimeKind.Modal,
      cloud("modal", null, "env"),
      hooks,
    );
    acc.snapshotStart();
    const { costDetails, signalEvents } = acc.snapshotEndAndBuild(1234);
    expect(costDetails).not.toBeNull();
    expect(costDetails!.billing_model).toBe("per_gpu_second_active");
    expect(costDetails!.gpu_vendor).toBe("nvidia");
    expect(costDetails!.gpu_sku).not.toBeNull();
    expect(costDetails!.gpu_count).toBe(1);
    expect(costDetails!.duration_ms).toBe(1234);
    expect(costDetails!.cost_pending).toBe(true);
    expect(costDetails!.mig_profile).toBeNull();

    expect(signalEvents).not.toBeNull();
    expect(signalEvents!.length).toBe(1);
    const sig = signalEvents![0];
    expect(sig.gpu_index).toBe(0);
    expect(sig.gpu_sku).toBe(costDetails!.gpu_sku);
    expect(sig.sm_util_pct).not.toBeNull();
    expect(sig.vram_total_bytes).toBe(85899345920);
  });
});

describe("GpuAccountant — idempotency", () => {
  it("second snapshotEndAndBuild returns (null, null)", () => {
    _resetWarningStateForTests();
    const hooks = baseHooks({
      getDeviceCount: () => 0,
    });
    const acc = new GpuAccountant(
      GpuRuntimeKind.Modal,
      cloud("modal", null, "env"),
      hooks,
    );
    acc.snapshotStart();
    acc.snapshotEndAndBuild(1000);
    const second = acc.snapshotEndAndBuild(2000);
    expect(second.costDetails).toBeNull();
    expect(second.signalEvents).toBeNull();
  });
});

describe("GpuAccountant — no NVML / 0 devices → nothing", () => {
  it("init_nvml false → empty events", () => {
    _resetWarningStateForTests();
    const hooks = baseHooks({ initNvml: () => false });
    const acc = new GpuAccountant(
      GpuRuntimeKind.Modal,
      cloud("modal", null, "env"),
      hooks,
    );
    acc.snapshotStart();
    const r = acc.snapshotEndAndBuild(1000);
    expect(r.costDetails).toBeNull();
    expect(r.signalEvents).toBeNull();
  });

  it("0 devices → empty events", () => {
    _resetWarningStateForTests();
    const hooks = baseHooks({ getDeviceCount: () => 0 });
    const acc = new GpuAccountant(
      GpuRuntimeKind.AwsEc2Gpu,
      cloud("aws", "us-east-1", "imds", "p5.48xlarge"),
      hooks,
    );
    acc.snapshotStart();
    const r = acc.snapshotEndAndBuild(60_000);
    expect(r.costDetails).toBeNull();
    expect(r.signalEvents).toBeNull();
  });
});

describe("GpuAccountant — Decision #1 fallback labels", () => {
  it("bare_metal_user_slice → _cgroup_scope_fallback=no_container_scope", () => {
    _resetWarningStateForTests();
    let call = 0;
    const samples: Array<Record<number, UtilSample>> = [
      {},
      {
        [SELF]: { pid: SELF, smUtil: 50, memUtil: 20, timeStamp: 500_000 },
      },
    ];
    const hooks = baseHooks({
      classifyScope: () => ({ kind: "bare_metal_user_slice", path: null }),
      getMemoryInfo: () => ({ usedBytes: 0, totalBytes: 85899345920 }),
      getProcessUtilization: () => samples[call++] ?? {},
    });
    const acc = new GpuAccountant(
      GpuRuntimeKind.AwsEc2Gpu,
      cloud("aws", "us-east-1", "imds", "p5.48xlarge"),
      hooks,
    );
    acc.snapshotStart();
    const { costDetails } = acc.snapshotEndAndBuild(1000);
    expect(costDetails!._cgroup_scope_fallback).toBe("no_container_scope");
  });

  it("cgroup walk denied → _cgroup_scope_fallback=self_pid_only", () => {
    _resetWarningStateForTests();
    const hooks = baseHooks({
      getMemoryInfo: () => ({ usedBytes: 0, totalBytes: 80 * 1024 ** 3 }),
      enumeratePids: () => null,
      getProcessUtilization: () => ({}),
    });
    const acc = new GpuAccountant(
      GpuRuntimeKind.AwsEc2Gpu,
      cloud("aws", "us-east-1", "imds", "p5.48xlarge"),
      hooks,
    );
    acc.snapshotStart();
    const { costDetails } = acc.snapshotEndAndBuild(1000);
    expect(costDetails!._cgroup_scope_fallback).toBe("self_pid_only");
  });
});

describe("GpuAccountant — Decision #2 MIG transparency", () => {
  it("MIG detected → mig_profile populated, log fires", () => {
    _resetWarningStateForTests();
    const hooks = baseHooks({
      getProductName: () => "nvidia a100 80gb",
      getMigMode: () => true,
      getMemoryInfo: () => ({ usedBytes: 0, totalBytes: 80 * 1024 ** 3 }),
      getProcessUtilization: () => ({}),
    });
    const acc = new GpuAccountant(
      GpuRuntimeKind.AwsEc2Gpu,
      cloud("aws", "us-east-1", "imds", "p4d.24xlarge"),
      hooks,
    );
    acc.snapshotStart();
    const { costDetails } = acc.snapshotEndAndBuild(1000);
    expect(costDetails).not.toBeNull();
    expect(costDetails!.mig_profile).not.toBeNull();
  });
});

describe("GpuAccountant — Decision #3 window-averaged sm_util_pct", () => {
  it("4s@80% + 1s@0% over 5s → sm_util_pct ≈ 64%", () => {
    _resetWarningStateForTests();
    // Simulate accumulated active-GPU-microseconds = 3.2s (0.8 × 4_000_000)
    let call = 0;
    const samples: Array<Record<number, UtilSample>> = [
      {},
      {
        [SELF]: { pid: SELF, smUtil: 0, memUtil: 0, timeStamp: 3_200_000 },
      },
    ];
    const hooks = baseHooks({
      getMemoryInfo: () => ({ usedBytes: 0, totalBytes: 80 * 1024 ** 3 }),
      getProcessUtilization: () => samples[call++] ?? {},
    });
    const acc = new GpuAccountant(
      GpuRuntimeKind.Modal,
      cloud("modal", null, "env"),
      hooks,
    );
    acc.snapshotStart();
    const { costDetails, signalEvents } = acc.snapshotEndAndBuild(5000);
    expect(costDetails!.gpu_seconds_used).toBeGreaterThanOrEqual(3.0);
    expect(costDetails!.gpu_seconds_used).toBeLessThanOrEqual(3.4);
    expect(signalEvents![0].sm_util_pct).not.toBeNull();
    expect(signalEvents![0].sm_util_pct as number).toBeGreaterThanOrEqual(60);
    expect(signalEvents![0].sm_util_pct as number).toBeLessThanOrEqual(70);
  });
});

describe("GpuAccountant — sub-100ms degenerate", () => {
  it("duration_ms=0 → sm_util_pct = null (no div-by-zero)", () => {
    _resetWarningStateForTests();
    const hooks = baseHooks({
      getMemoryInfo: () => ({ usedBytes: 0, totalBytes: 80 * 1024 ** 3 }),
      getProcessUtilization: () => ({}),
    });
    const acc = new GpuAccountant(
      GpuRuntimeKind.Modal,
      cloud("modal", null, "env"),
      hooks,
    );
    acc.snapshotStart();
    const { signalEvents } = acc.snapshotEndAndBuild(0);
    expect(signalEvents).not.toBeNull();
    expect(signalEvents![0].sm_util_pct).toBeNull();
  });
});

describe("GpuAccountant — multi-device", () => {
  it("4 devices → 4 signal events with distinct gpu_index", () => {
    _resetWarningStateForTests();
    let utilCall = 0;
    const hooks = baseHooks({
      getDeviceCount: () => 4,
      getMemoryInfo: () => ({ usedBytes: 1 * 1024 ** 3, totalBytes: 80 * 1024 ** 3 }),
      getProcessUtilization: () => {
        utilCall++;
        if (utilCall <= 4) return {};
        return {
          [SELF]: { pid: SELF, smUtil: 50, memUtil: 20, timeStamp: 500_000 },
        };
      },
    });
    const acc = new GpuAccountant(
      GpuRuntimeKind.AwsEc2Gpu,
      cloud("aws", "us-east-1", "imds", "p5.48xlarge"),
      hooks,
    );
    acc.snapshotStart();
    const { costDetails, signalEvents } = acc.snapshotEndAndBuild(1000);
    expect(costDetails).not.toBeNull();
    expect(costDetails!.gpu_count).toBe(4);
    expect(signalEvents!.length).toBe(4);
    signalEvents!.forEach((sig, i) => {
      expect(sig.gpu_index).toBe(i);
    });
  });
});
