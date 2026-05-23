/**
 * ComputeAccountant — start/end cgroup snapshots, single event per task,
 * fail-silent. Capture §5.3: at most one compute_cost event per task per runtime.
 *
 * Ports python/tests/test_compute_accountant.py (8 cases) to vitest.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ComputeAccountant } from "../src/core/compute-accountant.js";
import { RuntimeKind } from "../src/core/compute-runtime.js";
import * as cgroup from "../src/core/cgroup-reader.js";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ComputeAccountant", () => {
  test("long-running runtime emits one event with diff (EC2)", () => {
    // start=1M, end=4M → 3 vcpu-seconds.
    const snapshots = [{ usageUsec: 1_000_000 }, { usageUsec: 4_000_000 }];
    let i = 0;
    vi.spyOn(cgroup, "readCpuStat").mockImplementation(() => snapshots[i++] ?? null);
    vi.spyOn(cgroup, "readCpuMax").mockReturnValue({
      quotaUs: 100000,
      periodUs: 100000,
      vcpuCount: 1.0,
    });
    vi.spyOn(cgroup, "readMemoryPeak").mockReturnValue(512 * 1024 * 1024);
    vi.spyOn(cgroup, "readMemoryMax").mockReturnValue(1024 * 1024 * 1024);

    const a = new ComputeAccountant({ runtime: RuntimeKind.Ec2 });
    a.snapshotStart();
    const details = a.snapshotEndAndBuild(60_000);

    expect(details).not.toBeNull();
    expect(details!.billing_model).toBe("ec2");
    expect(details!.vcpu_seconds_used).toBeCloseTo(3.0);
    expect(details!.memory_bytes_peak).toBe(512 * 1024 * 1024);
    expect(details!.memory_bytes_limit).toBe(1024 * 1024 * 1024);
    expect(details!.vcpu_count).toBe(1.0);
    expect(details!.cost_pending).toBe(true);
  });

  test("serverless Lambda emits invocation event", () => {
    const a = new ComputeAccountant({
      runtime: RuntimeKind.Lambda,
      lambdaMemoryMb: 512,
      architecture: "x86_64",
      initializationType: "on-demand",
      region: "us-east-1",
    });
    const details = a.buildServerlessEvent(200, 400 * 1024 * 1024);
    expect(details).not.toBeNull();
    expect(details!.billing_model).toBe("lambda");
    expect(details!.duration_ms).toBe(200);
    expect(details!.invocation_count).toBe(1);
    // AWS_LAMBDA_FUNCTION_MEMORY_SIZE is DECIMAL MB.
    expect(details!.memory_bytes_limit).toBe(512 * 1_000_000);
    expect(details!.architecture).toBe("x86_64");
    expect(details!.initialization_type).toBe("on-demand");
    expect(details!.region).toBe("us-east-1");
    expect(details!.cost_pending).toBe(true);
  });

  test("second call per task no-ops (Capture §5.3)", () => {
    const a = new ComputeAccountant({
      runtime: RuntimeKind.Lambda,
      lambdaMemoryMb: 128,
      architecture: "x86_64",
    });
    const first = a.buildServerlessEvent(10, 0);
    const second = a.buildServerlessEvent(20, 0);
    expect(first).not.toBeNull();
    expect(second).toBeNull();
  });

  test("Fargate passes explicit vcpu and memory", () => {
    const a = new ComputeAccountant({
      runtime: RuntimeKind.Fargate,
      fargateVcpu: 0.5,
      fargateMemoryMib: 1024,
      architecture: "arm64",
      region: "us-east-1",
    });
    const details = a.buildServerlessEvent(60_000, 600 * 1024 * 1024);
    expect(details).not.toBeNull();
    expect(details!.billing_model).toBe("fargate");
    expect(details!.vcpu_count).toBe(0.5);
    expect(details!.memory_bytes_limit).toBe(1024 * 1024 * 1024);
    expect(details!.architecture).toBe("arm64");
  });

  test("non-linux fallback emits with zero vcpu_seconds", () => {
    vi.spyOn(cgroup, "readCpuStat").mockReturnValue(null);
    vi.spyOn(cgroup, "readCpuMax").mockReturnValue(null);
    vi.spyOn(cgroup, "readMemoryPeak").mockReturnValue(null);
    vi.spyOn(cgroup, "readMemoryMax").mockReturnValue(null);
    vi.spyOn(cgroup, "readMemoryCurrent").mockReturnValue(null);

    const a = new ComputeAccountant({ runtime: RuntimeKind.Ec2 });
    a.snapshotStart();
    const details = a.snapshotEndAndBuild(60_000);
    expect(details).not.toBeNull();
    expect(details!.vcpu_seconds_used).toBe(0);
    expect(details!.vcpu_count).toBeGreaterThan(0); // nproc fallback
  });

  test("memory.peak falls back to memory.current when missing (capture §6 case 6)", () => {
    vi.spyOn(cgroup, "readCpuStat").mockReturnValue({ usageUsec: 0 });
    vi.spyOn(cgroup, "readCpuMax").mockReturnValue({
      quotaUs: 100000,
      periodUs: 100000,
      vcpuCount: 1.0,
    });
    vi.spyOn(cgroup, "readMemoryPeak").mockReturnValue(null);
    vi.spyOn(cgroup, "readMemoryCurrent").mockReturnValue(256 * 1024 * 1024);
    vi.spyOn(cgroup, "readMemoryMax").mockReturnValue(1024 * 1024 * 1024);

    const a = new ComputeAccountant({ runtime: RuntimeKind.Ec2 });
    a.snapshotStart();
    const details = a.snapshotEndAndBuild(60_000);
    expect(details!.memory_bytes_peak).toBe(256 * 1024 * 1024);
  });

  test("architecture auto-detected from process.arch", () => {
    const a = new ComputeAccountant({
      runtime: RuntimeKind.Lambda,
      lambdaMemoryMb: 128,
    });
    expect(["x86_64", "arm64"]).toContain(a.architecture);
  });

  test("long-running snapshot freezes after finalize", () => {
    vi.spyOn(cgroup, "readCpuStat").mockReturnValue({ usageUsec: 100 });
    vi.spyOn(cgroup, "readCpuMax").mockReturnValue({
      quotaUs: 100000,
      periodUs: 100000,
      vcpuCount: 1.0,
    });
    vi.spyOn(cgroup, "readMemoryPeak").mockReturnValue(0);
    vi.spyOn(cgroup, "readMemoryMax").mockReturnValue(0);
    const a = new ComputeAccountant({ runtime: RuntimeKind.Ec2 });
    a.snapshotStart();
    const first = a.snapshotEndAndBuild(1000);
    const second = a.snapshotEndAndBuild(2000);
    expect(first).not.toBeNull();
    expect(second).toBeNull();
  });
});
