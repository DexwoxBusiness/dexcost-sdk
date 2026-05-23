/**
 * GPU runtime cascade — Phase 2 Task 3.
 * Mirrors python/tests/test_gpu_runtime.py.
 *
 * The TS deviation is the dependency-injection pattern: instead of
 * monkeypatching module attributes, the resolver accepts an Options object
 * exposing `nvmlAvailable`, `getDeviceCount`, and `getCloudEnv` hooks so
 * tests can pass deterministic stubs without import-cycle gymnastics.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  GpuRuntimeKind,
  resolveGpuRuntime,
  type ResolveGpuRuntimeOptions,
} from "../src/core/gpu-runtime.js";

const SERVERLESS_VARS = [
  "MODAL_TASK_ID",
  "MODAL_IMAGE_ID",
  "RUNPOD_POD_ID",
  "RUNPOD_POD_HOSTNAME",
  "REPLICATE_MODEL",
  "REPLICATE_PREDICTION_ID",
];

function scrubEnv() {
  for (const v of SERVERLESS_VARS) delete process.env[v];
}

function opts(
  nvml: boolean,
  deviceCount: number,
  provider: string | null,
  instanceType?: string,
  region?: string,
): ResolveGpuRuntimeOptions {
  return {
    nvmlAvailable: () => nvml,
    getDeviceCount: () => deviceCount,
    getCloudEnv: () => ({
      provider,
      region: region ?? null,
      source: "env",
      instanceType: instanceType ?? null,
    }),
  };
}

describe("Serverless GPU clouds — env-var detection wins", () => {
  beforeEach(() => scrubEnv());
  afterEach(() => scrubEnv());

  it("MODAL_TASK_ID → MODAL", () => {
    process.env.MODAL_TASK_ID = "task-abc";
    expect(resolveGpuRuntime(opts(true, 1, "modal"))).toBe(
      GpuRuntimeKind.Modal,
    );
  });

  it("RUNPOD_POD_ID → RUNPOD", () => {
    process.env.RUNPOD_POD_ID = "pod-abc";
    expect(resolveGpuRuntime(opts(true, 1, "runpod"))).toBe(
      GpuRuntimeKind.RunPod,
    );
  });

  it("REPLICATE_MODEL → REPLICATE", () => {
    process.env.REPLICATE_MODEL = "owner/model";
    expect(resolveGpuRuntime(opts(true, 1, "replicate"))).toBe(
      GpuRuntimeKind.Replicate,
    );
  });
});

describe("AWS GPU family detection", () => {
  beforeEach(() => scrubEnv());

  const gpuCases: string[] = [
    "p5.48xlarge",
    "p4d.24xlarge",
    "p4de.24xlarge",
    "p5e.48xlarge",
    "p5en.48xlarge",
    "p3.2xlarge",
    "g4dn.xlarge",
    "g4dn.metal",
    "g5.xlarge",
    "g5g.xlarge",
    "g6.xlarge",
    "g6e.xlarge",
  ];

  it.each(gpuCases)("%s → AWS_EC2_GPU", (it_) => {
    expect(resolveGpuRuntime(opts(true, 1, "aws", it_, "us-east-1"))).toBe(
      GpuRuntimeKind.AwsEc2Gpu,
    );
  });

  const nonGpu = ["c7g.xlarge", "t3.medium", "m7i.large"];
  it.each(nonGpu)("%s → NONE (CPU family)", (it_) => {
    expect(resolveGpuRuntime(opts(true, 0, "aws", it_, "us-east-1"))).toBe(
      GpuRuntimeKind.None,
    );
  });
});

describe("GCP GPU family detection", () => {
  beforeEach(() => scrubEnv());

  const bundled = [
    "a2-highgpu-1g",
    "a2-ultragpu-1g",
    "a3-highgpu-8g",
    "a3-edgegpu-8g",
    "g2-standard-4",
  ];
  it.each(bundled)("%s → GCP_GCE_BUNDLED", (it_) => {
    expect(resolveGpuRuntime(opts(true, 1, "gcp", it_, "us-central1"))).toBe(
      GpuRuntimeKind.GcpGceBundled,
    );
  });

  const n1Cases = ["n1-standard-8", "n1-highmem-4"];
  it.each(n1Cases)("%s → GCP_GCE_N1_ATTACHED (Decision #9)", (it_) => {
    expect(resolveGpuRuntime(opts(true, 1, "gcp", it_, "us-central1"))).toBe(
      GpuRuntimeKind.GcpGceN1Attached,
    );
  });

  it("e2-standard-4 (CPU-only) + 0 devices → NONE", () => {
    expect(
      resolveGpuRuntime(opts(true, 0, "gcp", "e2-standard-4", "us-central1")),
    ).toBe(GpuRuntimeKind.None);
  });
});

describe("Azure GPU family detection", () => {
  beforeEach(() => scrubEnv());

  const azureGpu = [
    "Standard_ND96isr_H100_v5",
    "Standard_ND96amsr_A100_v4",
    "Standard_ND96asr_v4",
    "Standard_NC24ads_A100_v4",
    "Standard_NC6s_v3",
  ];
  it.each(azureGpu)("%s → AZURE_VM_GPU", (it_) => {
    expect(resolveGpuRuntime(opts(true, 1, "azure", it_, "eastus"))).toBe(
      GpuRuntimeKind.AzureVmGpu,
    );
  });

  const azureVgpu = [
    "Standard_NV6ads_A10_v5",
    "Standard_NV12ads_A10_v5",
    "Standard_NV36ads_A10_v5",
    "Standard_NV72ads_A10_v5",
  ];
  it.each(azureVgpu)("%s → AZURE_VM_VGPU (Decision #10)", (it_) => {
    expect(resolveGpuRuntime(opts(true, 1, "azure", it_, "eastus"))).toBe(
      GpuRuntimeKind.AzureVmVgpu,
    );
  });

  const nonGpu = ["Standard_D2s_v3", "Standard_B2ms"];
  it.each(nonGpu)("%s → NONE", (it_) => {
    expect(resolveGpuRuntime(opts(true, 0, "azure", it_, "eastus"))).toBe(
      GpuRuntimeKind.None,
    );
  });
});

describe("Reserved-GPU providers (Lambda Labs / CoreWeave)", () => {
  beforeEach(() => scrubEnv());

  it("lambda_labs via cloud_detect → LAMBDA_LABS", () => {
    expect(resolveGpuRuntime(opts(true, 8, "lambda_labs"))).toBe(
      GpuRuntimeKind.LambdaLabs,
    );
  });

  it("coreweave via cloud_detect → COREWEAVE", () => {
    expect(resolveGpuRuntime(opts(true, 8, "coreweave"))).toBe(
      GpuRuntimeKind.CoreWeave,
    );
  });
});

describe("No NVML / no GPU → NONE", () => {
  beforeEach(() => scrubEnv());

  it("NVML unavailable → NONE (even on a Modal task)", () => {
    process.env.MODAL_TASK_ID = "x";
    expect(resolveGpuRuntime(opts(false, 0, "modal"))).toBe(
      GpuRuntimeKind.None,
    );
  });

  it("NVML reports 0 devices → NONE (even on p5)", () => {
    expect(
      resolveGpuRuntime(opts(true, 0, "aws", "p5.48xlarge", "us-east-1")),
    ).toBe(GpuRuntimeKind.None);
  });

  it("no cloud provider resolved + no env vars → NONE", () => {
    expect(resolveGpuRuntime(opts(true, 0, null))).toBe(GpuRuntimeKind.None);
  });
});
