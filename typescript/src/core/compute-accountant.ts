/**
 * Per-task compute accountant.
 *
 * Holds start cgroup snapshot + runtime context for one dexcost task. At
 * task finalize, emits exactly one `compute_cost` event with
 * `cost_pending: true` — the pricing engine back-fills `cost_usd` via the
 * deferred-cost pattern inherited from network v2 §6.4.
 *
 * Capture §5.3 invariant: at most one event per task per runtime. Idempotent —
 * second call to snapshotEndAndBuild / buildServerlessEvent returns null.
 *
 * TS is single-threaded → freeze flag is sufficient, no mutex needed.
 *
 * Mirrors python/src/dexcost/compute_accountant.py.
 */

import { cpus } from "node:os";
import {
  readCpuMax,
  readCpuStat,
  readMemoryCurrent,
  readMemoryMax,
  readMemoryPeak,
} from "./cgroup-reader.js";
import { RuntimeKind } from "./compute-runtime.js";

/** Map a RuntimeKind to the details.billing_model discriminator. */
function billingModelFor(runtime: RuntimeKind): string {
  switch (runtime) {
    case RuntimeKind.Lambda:
      return "lambda";
    case RuntimeKind.Fargate:
      return "fargate";
    case RuntimeKind.Ec2:
      return "ec2";
    case RuntimeKind.Gce:
      return "gce";
    case RuntimeKind.AzureVm:
      return "azure_vm";
    case RuntimeKind.CloudRun:
      return "cloud_run_request";
    case RuntimeKind.CloudFunctions:
      return "cloud_functions";
    case RuntimeKind.AzureFunctions:
      return "azure_functions";
    case RuntimeKind.Vercel:
      return "vercel_fluid";
    case RuntimeKind.K8sPod:
      return "k8s_pod";
    default:
      return "unknown";
  }
}

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

function detectArch(): string {
  if (!_isNode()) return "x86_64";
  const a = process.arch;
  if (a === "arm64" || a === "arm") return "arm64";
  return "x86_64";
}

export interface ComputeAccountantOptions {
  runtime: RuntimeKind;
  lambdaMemoryMb?: number;
  fargateVcpu?: number;
  fargateMemoryMib?: number;
  architecture?: string;
  initializationType?: string;
  region?: string;
}

export class ComputeAccountant {
  private _frozen = false;
  private _startCpuUsec: number | null = null;

  readonly runtime: RuntimeKind;
  readonly lambdaMemoryMb: number | undefined;
  readonly fargateVcpu: number | undefined;
  readonly fargateMemoryMib: number | undefined;
  readonly architecture: string;
  readonly initializationType: string | undefined;
  readonly region: string | undefined;

  constructor(opts: ComputeAccountantOptions) {
    this.runtime = opts.runtime;
    this.lambdaMemoryMb = opts.lambdaMemoryMb;
    this.fargateVcpu = opts.fargateVcpu;
    this.fargateMemoryMib = opts.fargateMemoryMib;
    this.architecture = opts.architecture ?? detectArch();
    this.initializationType = opts.initializationType;
    this.region = opts.region;
  }

  // ------------------------------------------------------------------
  // Long-running runtimes (Fargate / EC2 / GCE / Azure VM / K8s pod /
  // Cloud Run instance-based)
  // ------------------------------------------------------------------

  /** Capture the cgroup CPU counter at task start. Idempotent. */
  snapshotStart(): void {
    if (this._startCpuUsec !== null) return;
    const s = readCpuStat();
    this._startCpuUsec = s ? s.usageUsec : 0;
  }

  /**
   * Capture cgroup CPU/memory at task end and build the event details.
   *
   * Returns null if already frozen (second call) or runtime is unknown.
   */
  snapshotEndAndBuild(durationMs: number): Record<string, any> | null {
    if (this._frozen) return null;
    this._frozen = true;
    const startCpu = this._startCpuUsec ?? 0;

    const end = readCpuStat();
    const cpuMax = readCpuMax();
    // Capture §6 case 6 — memory.peak missing → fall back to memory.current.
    let memPeak = readMemoryPeak();
    if (memPeak === null) {
      memPeak = readMemoryCurrent() ?? 0;
    }
    const memLimit = readMemoryMax() ?? 0;

    let vcpuSecondsUsed = 0;
    if (end !== null && end.usageUsec >= startCpu) {
      vcpuSecondsUsed = (end.usageUsec - startCpu) / 1_000_000;
    }

    const vcpuCount = cpuMax ? cpuMax.vcpuCount : _nproc();

    return {
      billing_model: billingModelFor(this.runtime),
      duration_ms: durationMs,
      memory_bytes_peak: Math.trunc(memPeak),
      memory_bytes_limit: Math.trunc(memLimit),
      vcpu_count: vcpuCount,
      vcpu_seconds_used: vcpuSecondsUsed,
      invocation_count: 0,
      region: this.region ?? null,
      architecture: this.architecture,
      initialization_type: null,
      cost_pending: true,
    };
  }

  // ------------------------------------------------------------------
  // Serverless runtimes
  // ------------------------------------------------------------------

  /**
   * Build a per-invocation event for Lambda / Cloud Run / Cloud Functions /
   * Azure Functions / Vercel.
   */
  buildServerlessEvent(
    durationMs: number,
    memoryBytesPeak: number,
  ): Record<string, any> | null {
    if (this._frozen) return null;
    this._frozen = true;

    let memLimit: number;
    let vcpuCount: number;
    if (this.runtime === RuntimeKind.Lambda) {
      // Lambda's AWS_LAMBDA_FUNCTION_MEMORY_SIZE is DECIMAL MB.
      memLimit = (this.lambdaMemoryMb ?? 128) * 1_000_000;
      vcpuCount = vcpuCountFromCgroup();
    } else if (this.runtime === RuntimeKind.Fargate) {
      memLimit = (this.fargateMemoryMib ?? 0) * 1024 * 1024;
      vcpuCount =
        this.fargateVcpu !== undefined ? this.fargateVcpu : vcpuCountFromCgroup();
    } else {
      // Cloud Run / Cloud Functions / Azure Functions / Vercel —
      // cgroup memory.max is the declared limit.
      memLimit = readMemoryMax() ?? memoryBytesPeak;
      vcpuCount = vcpuCountFromCgroup();
    }

    return {
      billing_model: billingModelFor(this.runtime),
      duration_ms: durationMs,
      memory_bytes_peak: memoryBytesPeak,
      memory_bytes_limit: memLimit,
      vcpu_count: vcpuCount,
      vcpu_seconds_used: 0,
      invocation_count: 1,
      region: this.region ?? null,
      architecture: this.architecture,
      initialization_type: this.initializationType ?? null,
      cost_pending: true,
    };
  }
}

function _nproc(): number {
  if (!_isNode()) return 1;
  return cpus().length || 1;
}

function vcpuCountFromCgroup(): number {
  const cpuMax = readCpuMax();
  if (cpuMax !== null) return cpuMax.vcpuCount;
  return _nproc();
}
