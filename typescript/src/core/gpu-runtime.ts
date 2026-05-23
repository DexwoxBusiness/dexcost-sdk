/**
 * Active GPU runtime resolver — Phase 2 Task 3.
 *
 * Sibling of `compute-runtime.ts`. Coexists without modification —
 * compute-runtime answers "which compute billing model" and gpu-runtime
 * answers "which GPU billing model (if any)".
 *
 * Cascade priority (capture spec §5.5):
 *
 * 1. Serverless GPU env vars (MODAL_TASK_ID / RUNPOD_POD_ID /
 *    REPLICATE_MODEL) — win immediately when NVML is available.
 * 2. IaaS GPU via cloud_detect — provider AWS/GCP/Azure AND instanceType
 *    matches a GPU-family regex AND NVML reports ≥1 device. Classifies as
 *    AWS_EC2_GPU / GCP_GCE_BUNDLED / GCP_GCE_N1_ATTACHED (Decision #9) /
 *    AZURE_VM_GPU / AZURE_VM_VGPU (Decision #10).
 * 3. Reserved-GPU providers — Lambda Labs / CoreWeave from cloud_detect
 *    AND NVML reports ≥1 device.
 * 4. NONE when NVML unavailable, 0 devices, or unmatched runtime.
 *
 * **String values match Python EXACTLY** — cross-SDK event portability.
 *
 * **TS deviation**: takes an `Options` object with hook functions. This
 * lets tests inject deterministic stubs without monkeypatching module-
 * level imports (ESM bindings are immutable). The default options use the
 * production nvml-reader + cloud-detect modules.
 *
 * Mirrors python/src/dexcost/gpu_runtime.py.
 */

import { getCloudEnv as defaultGetCloudEnv, type CloudEnv } from "../cloud-detect.js";
import {
  getDeviceCount as defaultGetDeviceCount,
  nvmlAvailable as defaultNvmlAvailable,
} from "./nvml-reader.js";

/** GPU runtime kind discriminator. String values pinned to Python. */
export const GpuRuntimeKind = {
  Modal: "modal",
  RunPod: "runpod",
  Replicate: "replicate",
  LambdaLabs: "lambda_labs",
  CoreWeave: "coreweave",
  AwsEc2Gpu: "aws_ec2_gpu",
  GcpGceBundled: "gcp_gce_bundled",
  GcpGceN1Attached: "gcp_gce_n1_attached",
  AzureVmGpu: "azure_vm_gpu",
  AzureVmVgpu: "azure_vm_vgpu",
  None: "none",
} as const;

export type GpuRuntimeKind =
  (typeof GpuRuntimeKind)[keyof typeof GpuRuntimeKind];

export interface ResolveGpuRuntimeOptions {
  nvmlAvailable?: () => boolean;
  getDeviceCount?: () => number | null;
  getCloudEnv?: () => CloudEnv;
}

// ─── GPU instance-family matchers ───────────────────────────────────────────

// AWS GPU EC2 families: g4/g4dn/g5/g5g/g6/g6e/p3/p4d/p4de/p5/p5e/p5en
const AWS_GPU_FAMILY_RE = /^(g4|g4dn|g5|g5g|g6|g6e|p3|p4d|p4de|p5|p5e|p5en)\./i;

// GCP A2/A3/A4 (bundled GPU) + G2 (L4-bundled)
const GCP_BUNDLED_GPU_FAMILY_RE = /^(a2|a3|a4|g2)-/i;

// GCP N1 — attached-accelerator path (Decision #9 — accelerator type not
// exposed by metadata; rely on NVML fallback).
const GCP_N1_FAMILY_RE = /^n1-/i;

// Azure ND/NC series — bundled GPU instances.
const AZURE_GPU_FAMILY_RE = /^Standard_(ND|NC)/i;

// Azure NVadsA10 v5 — fractional vGPU (Decision #10). More specific; matched first.
const AZURE_VGPU_FAMILY_RE = /^Standard_NV\d+ads_A10_v5/i;

function isAwsGpuInstance(it: string | null | undefined): boolean {
  return !!it && AWS_GPU_FAMILY_RE.test(it);
}
function isGcpBundledGpuInstance(it: string | null | undefined): boolean {
  return !!it && GCP_BUNDLED_GPU_FAMILY_RE.test(it);
}
function isGcpN1Instance(it: string | null | undefined): boolean {
  return !!it && GCP_N1_FAMILY_RE.test(it);
}
function isAzureVgpuInstance(it: string | null | undefined): boolean {
  return !!it && AZURE_VGPU_FAMILY_RE.test(it);
}
function isAzureGpuInstance(it: string | null | undefined): boolean {
  return !!it && AZURE_GPU_FAMILY_RE.test(it);
}

// ─── Resolver ───────────────────────────────────────────────────────────────

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

/**
 * Return the active GPU runtime, or NONE when there's no GPU.
 *
 * The cascade short-circuits on the FIRST positive match. If NVML can't
 * initialize or reports 0 devices, returns NONE regardless of env-var
 * signals — a Modal task on a CPU-only Modal function emits no GPU events.
 */
export function resolveGpuRuntime(
  options?: ResolveGpuRuntimeOptions,
): GpuRuntimeKind {
  const nvmlAvailable = options?.nvmlAvailable ?? defaultNvmlAvailable;
  const getDeviceCount = options?.getDeviceCount ?? defaultGetDeviceCount;
  const getCloudEnv = options?.getCloudEnv ?? defaultGetCloudEnv;

  // Browser → no GPU.
  if (!_isNode()) return GpuRuntimeKind.None;

  // NVML must be available AND see ≥1 device for any GPU event emission.
  if (!nvmlAvailable()) return GpuRuntimeKind.None;
  const count = getDeviceCount() ?? 0;
  if (count <= 0) return GpuRuntimeKind.None;

  // 1. Serverless GPU env vars — fastest path, decisive when set.
  const env = process.env;
  if (env.MODAL_TASK_ID || env.MODAL_IMAGE_ID) return GpuRuntimeKind.Modal;
  if (env.RUNPOD_POD_ID || env.RUNPOD_POD_HOSTNAME) return GpuRuntimeKind.RunPod;
  if (env.REPLICATE_MODEL || env.REPLICATE_PREDICTION_ID) {
    return GpuRuntimeKind.Replicate;
  }

  // 2/3. Cloud-detect IaaS / reserved-GPU.
  const cloud = getCloudEnv();
  const provider = cloud.provider;
  const it = cloud.instanceType ?? null;

  if (provider === "lambda_labs") return GpuRuntimeKind.LambdaLabs;
  if (provider === "coreweave") return GpuRuntimeKind.CoreWeave;

  if (provider === "aws") {
    if (isAwsGpuInstance(it)) return GpuRuntimeKind.AwsEc2Gpu;
  }
  if (provider === "gcp") {
    if (isGcpBundledGpuInstance(it)) return GpuRuntimeKind.GcpGceBundled;
    if (isGcpN1Instance(it)) return GpuRuntimeKind.GcpGceN1Attached;
  }
  if (provider === "azure") {
    if (isAzureVgpuInstance(it)) return GpuRuntimeKind.AzureVmVgpu;
    if (isAzureGpuInstance(it)) return GpuRuntimeKind.AzureVmGpu;
  }

  return GpuRuntimeKind.None;
}
