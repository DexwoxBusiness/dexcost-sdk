/**
 * Per-task GPU accountant — Phase 2 v1 capture.
 *
 * One per dexcost task. Lives on Task as `_gpu` (mirrors `_compute` /
 * `_network`). Holds the start cgroup snapshot, the per-device NVML
 * start-snapshot timestamps (Decision #8 persistent state), and the
 * device indexes.
 *
 * During the task and at finalization:
 *
 * 1. Periodically snapshots NVML utilization across all devices, retaining
 *    evidence from GPU processes that exit before task finalization.
 * 2. Walks the cgroup PIDs (Decision #1) and accumulates SM-time across
 *    them per device.
 * 3. Computes the window-averaged `sm_util_pct` per Decision #3 (NOT a
 *    point sample at finalize).
 * 4. Resolves a coarse GPU SKU hint from the productName.
 * 5. Emits ONE gpu_cost event (cost_pending=true; back-filled at task
 *    finalize) AND N gpu_utilization_signal events (one per device that
 *    the task's cgroup touched).
 *
 * Idempotent — second call to snapshotEndAndBuild returns
 * `{ costDetails: null, signalEvents: null }` per capture spec §5.3.
 *
 * **TS deviation**: accepts a `GpuAccountantHooks` object so tests can
 * inject deterministic stubs without monkeypatching ESM module imports
 * (matches the gpu-runtime pattern). Default hooks delegate to the
 * production nvml-reader + cgroup-walker modules.
 *
 * Browser safety: snapshotStart short-circuits off-Node; snapshotEndAndBuild
 * returns nulls.
 *
 * Mirrors python/src/dexcost/gpu_accountant.py.
 */

import { GpuRuntimeKind } from "./gpu-runtime.js";
import type { CloudEnv } from "../cloud-detect.js";
import {
  classifyScope as defaultClassifyScope,
  enumeratePids as defaultEnumeratePids,
  fallbackLabelFor,
  type CgroupScope,
} from "./cgroup-walker.js";
import {
  getDeviceCount as defaultGetDeviceCount,
  getMemoryInfo as defaultGetMemoryInfo,
  getMigMode as defaultGetMigMode,
  getProcessUtilization as defaultGetProcessUtilization,
  getProcessUtilizationAsync as defaultGetProcessUtilizationAsync,
  getProductName as defaultGetProductName,
  initNvml as defaultInitNvml,
  type MemInfo,
  type UtilSample,
} from "./nvml-reader.js";

// ─── Warn-once state ────────────────────────────────────────────────────────

const _warnedModes: Set<string> = new Set();

export function _resetWarningStateForTests(): void {
  _warnedModes.clear();
}

function _warnOnce(mode: string, message: string): void {
  if (_warnedModes.has(mode)) return;
  _warnedModes.add(mode);
  // eslint-disable-next-line no-console
  console.warn(message);
}

// ─── Hooks (dependency injection for tests) ─────────────────────────────────

export interface GpuAccountantHooks {
  initNvml: () => boolean;
  getDeviceCount: () => number | null;
  getProductName: (index: number) => string | null;
  getMigMode: (index: number) => boolean;
  getMemoryInfo: (index: number) => MemInfo | null;
  classifyScope: () => CgroupScope;
  enumeratePids: (scope: CgroupScope) => number[] | null;
  getProcessUtilization: (
    index: number,
    lastSeen: Record<number, number>,
  ) => Record<number, UtilSample[]> | null;
  getProcessUtilizationAsync?: (
    index: number,
  ) => Promise<Record<number, UtilSample[]> | null>;
  samplingIntervalMs?: number;
}

function defaultHooks(): GpuAccountantHooks {
  return {
    initNvml: defaultInitNvml,
    getDeviceCount: defaultGetDeviceCount,
    getProductName: defaultGetProductName,
    getMigMode: defaultGetMigMode,
    getMemoryInfo: defaultGetMemoryInfo,
    classifyScope: defaultClassifyScope,
    enumeratePids: defaultEnumeratePids,
    getProcessUtilization: defaultGetProcessUtilization,
    getProcessUtilizationAsync: defaultGetProcessUtilizationAsync,
  };
}

// Map GpuRuntimeKind → details.billing_model for pricing dispatch.
const BILLING_MODEL_FOR_RUNTIME: Record<string, string> = {
  [GpuRuntimeKind.Modal]: "per_gpu_second_active",
  [GpuRuntimeKind.RunPod]: "per_gpu_second_active",
  [GpuRuntimeKind.Replicate]: "per_gpu_second_active",
  [GpuRuntimeKind.LambdaLabs]: "per_gpu_hour_reserved",
  [GpuRuntimeKind.CoreWeave]: "per_gpu_hour_reserved",
  [GpuRuntimeKind.GcpGceN1Attached]: "per_gpu_hour_reserved",
  [GpuRuntimeKind.AwsEc2Gpu]: "per_instance_hour",
  [GpuRuntimeKind.GcpGceBundled]: "per_instance_hour",
  [GpuRuntimeKind.AzureVmGpu]: "per_instance_hour",
  [GpuRuntimeKind.AzureVmVgpu]: "per_vgpu_hour",
  [GpuRuntimeKind.LocalGpu]: "local_gpu_usage_only",
};

// ─── Public types ───────────────────────────────────────────────────────────

export interface GpuCostDetails {
  billing_model: string;
  runtime_kind: string;
  cloud_provider: string | null;
  gpu_vendor: string;
  gpu_sku: string | null;
  gpu_count: number;
  region: string | null;
  duration_ms: number;
  gpu_seconds_used: number;
  instance_type: string | null;
  vgpu_profile: string | null;
  mig_profile: string | null;
  cost_pending: true;
  _nvml_product_name_lower?: string;
  _cgroup_scope_fallback?: string;
}

export interface GpuUtilizationSignal {
  billing_model: string;
  runtime_kind: string;
  cloud_provider: string | null;
  gpu_index: number;
  gpu_sku: string | null;
  sm_util_pct: number | null;
  mem_util_pct: number | null;
  vram_used_peak_bytes: number;
  vram_total_bytes: number;
  process_count: number;
  sample_count: number;
  task_duration_ms: number;
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

function resolveSkuFromProductName(name: string | null | undefined): string | null {
  if (!name) return null;
  if (name.includes("h100")) return "h100-80gb-sxm5";
  if (name.includes("h200")) return "h200-141gb-sxm5";
  if (name.includes("a100")) {
    if (name.includes("40gb")) return "a100-40gb-sxm4";
    return "a100-80gb-sxm4";
  }
  if (name.includes("a10g")) return "a10g-24gb";
  if (name.includes("a10-4q")) return "a10-vgpu-1of6";
  if (name.includes("a10-8q")) return "a10-vgpu-1of3";
  if (name.includes("a10-12q")) return "a10-vgpu-1of2";
  if (name.includes("a10-24q") || name.includes("a10")) return "a10";
  if (name.includes("l40s")) return "l40s-48gb";
  if (name.includes("l4")) return "l4-24gb";
  if (name.includes("tesla t4") || name.includes("nvidia t4")) return "t4-16gb";
  if (name.includes("rtx 6000")) return "rtx-6000-24gb";
  return null;
}

const AZURE_VGPU_PROFILE_BY_INSTANCE: Record<string, string> = {
  Standard_NV6ads_A10_v5: "1/6 A10",
  Standard_NV12ads_A10_v5: "1/3 A10",
  Standard_NV18ads_A10_v5: "1/2 A10",
  Standard_NV36ads_A10_v5: "full A10",
  Standard_NV72ads_A10_v5: "2x A10",
};

// ═════════════════════════════════════════════════════════════════════════════
// GpuAccountant
// ═════════════════════════════════════════════════════════════════════════════

export class GpuAccountant {
  readonly runtime: GpuRuntimeKind;
  readonly cloudEnv: CloudEnv;
  private readonly hooks: GpuAccountantHooks;

  private _frozen = false;
  private _scope: CgroupScope | null = null;
  private _initialPids: Set<number> = new Set();
  // Decision #8: per-device-per-PID lastSeenTimeStamp persisted across calls.
  private _initialTimestamps: Record<number, Record<number, number>> = {};
  private _baselineTimestamps: Record<number, Record<number, number>> = {};
  private _acceptedTimestamps: Record<number, Record<number, number>> = {};
  private _utilizationSamples: Record<number, Record<number, UtilSample[]>> = {};
  private _observedPids: Set<number> = new Set();
  private _deviceIndexes: number[] = [];
  private _deviceProductNames: (string | null)[] = [];
  private _deviceMigModes: boolean[] = [];
  // Per-device peak VRAM tracker.
  private _vramTotal: Record<number, number> = {};
  private _vramUsedPeak: Record<number, number> = {};
  // Per-device PID set observed across the task.
  private _pidsTouchedPerDevice: Record<number, Set<number>> = {};
  private _sampleTimer: ReturnType<typeof setInterval> | null = null;
  private _sampleInFlight = false;
  private readonly _samplingIntervalMs: number;

  constructor(
    runtime: GpuRuntimeKind,
    cloudEnv: CloudEnv,
    hooks?: Partial<GpuAccountantHooks>,
  ) {
    this.runtime = runtime;
    this.cloudEnv = cloudEnv;
    this.hooks = { ...defaultHooks(), ...(hooks ?? {}) };
    this._samplingIntervalMs = Math.max(1, hooks?.samplingIntervalMs ?? 1_000);
  }

  /** Initialize NVML, snapshot cgroup PIDs, capture baseline NVML timestamps. */
  snapshotStart(): void {
    if (!_isNode()) return;
    if (!this.hooks.initNvml()) return;
    const count = this.hooks.getDeviceCount() ?? 0;
    if (count <= 0) return;
    this._deviceIndexes = Array.from({ length: count }, (_, i) => i);
    for (const i of this._deviceIndexes) {
      const name = this.hooks.getProductName(i);
      this._deviceProductNames.push(name);
      const mig = this.hooks.getMigMode(i);
      this._deviceMigModes.push(mig);
      if (mig) {
        _warnOnce(
          `gpu_mig_detected_full_billing_applied:device${i}`,
          `NVML reports MIG enabled on device ${i} (productName=${String(name)}); ` +
            `Decision #2 — full-GPU rate applied. details.mig_profile populated ` +
            `for v1.1 forward-compat.`,
        );
      }
      const mem = this.hooks.getMemoryInfo(i);
      if (mem) {
        this._vramTotal[i] = mem.totalBytes;
        this._vramUsedPeak[i] = mem.usedBytes;
      }
      this._initialTimestamps[i] = {};
      this._baselineTimestamps[i] = {};
      this._acceptedTimestamps[i] = {};
      this._utilizationSamples[i] = {};
      this._pidsTouchedPerDevice[i] = new Set<number>();
      // Baseline NVML sample — captures the per-PID lastSeenTimeStamp.
      const baseline = this.hooks.getProcessUtilization(
        i,
        this._initialTimestamps[i],
      );
      if (baseline) {
        for (const pidKey of Object.keys(baseline)) {
          this._pidsTouchedPerDevice[i].add(Number(pidKey));
        }
      }
      this._baselineTimestamps[i] = { ...this._initialTimestamps[i] };
      this._acceptedTimestamps[i] = { ...this._initialTimestamps[i] };
    }

    // Snapshot cgroup PIDs (Decision #1 scope classification).
    this._scope = this.hooks.classifyScope();
    const pids = this.hooks.enumeratePids(this._scope);
    this._initialPids = new Set(pids ?? [process.pid]);
    this._observedPids = new Set(this._initialPids);
    if (this.hooks.getProcessUtilizationAsync) {
      this._sampleTimer = setInterval(() => {
        void this.capturePeriodicSample();
      }, this._samplingIntervalMs);
      const timer = this._sampleTimer as unknown as { unref?: () => void };
      timer.unref?.();
    }
  }

  private appendSamples(
    index: number,
    batch: Record<number, UtilSample[]> | null,
  ): void {
    if (!batch) return;
    for (const [pidKey, samples] of Object.entries(batch)) {
      const pid = Number(pidKey);
      this._pidsTouchedPerDevice[index].add(pid);
      let lastAccepted = this._acceptedTimestamps[index][pid] ?? 0;
      for (const sample of [...samples].sort((a, b) => a.timeStamp - b.timeStamp)) {
        if (sample.timeStamp <= lastAccepted) continue;
        (this._utilizationSamples[index][pid] ??= []).push(sample);
        lastAccepted = sample.timeStamp;
      }
      this._acceptedTimestamps[index][pid] = lastAccepted;
      this._initialTimestamps[index][pid] = Math.max(
        this._initialTimestamps[index][pid] ?? 0,
        lastAccepted,
      );
    }
  }

  private captureCurrentPids(): void {
    if (!this._scope) return;
    const pids = this.hooks.enumeratePids(this._scope);
    for (const pid of pids ?? [process.pid]) this._observedPids.add(pid);
  }

  private async capturePeriodicSample(): Promise<void> {
    const read = this.hooks.getProcessUtilizationAsync;
    if (this._frozen || this._sampleInFlight || !read) return;
    this._sampleInFlight = true;
    this.captureCurrentPids();
    try {
      const batches = await Promise.all(
        this._deviceIndexes.map(async (index) => ({
          index,
          batch: await read(index),
        })),
      );
      if (this._frozen) return;
      for (const { index, batch } of batches) {
        this.appendSamples(index, batch);
        const mem = this.hooks.getMemoryInfo(index);
        if (mem) {
          this._vramUsedPeak[index] = Math.max(
            this._vramUsedPeak[index] ?? 0,
            mem.usedBytes,
          );
        }
      }
    } finally {
      this._sampleInFlight = false;
    }
  }

  /**
   * Build (gpu_cost_event, [gpu_utilization_signal_events]) at task finalize.
   *
   * Returns nulls when frozen, NVML unavailable, or 0 devices touched.
   */
  snapshotEndAndBuild(
    durationMs: number,
  ): { costDetails: GpuCostDetails | null; signalEvents: GpuUtilizationSignal[] | null } {
    if (this._frozen) return { costDetails: null, signalEvents: null };
    this._frozen = true;
    if (this._sampleTimer !== null) {
      clearInterval(this._sampleTimer);
      this._sampleTimer = null;
    }
    if (this._deviceIndexes.length === 0) {
      return { costDetails: null, signalEvents: null };
    }

    // End-snapshot cgroup walk + Decision #1 fallback label.
    const scope = this._scope ?? this.hooks.classifyScope();
    const endPidsList = this.hooks.enumeratePids(scope);
    let fallbackLabel: string | null;
    let currentPids: Set<number>;
    if (endPidsList === null) {
      fallbackLabel = "self_pid_only";
      currentPids = new Set([process.pid]);
    } else {
      fallbackLabel = fallbackLabelFor(scope);
      currentPids = new Set(endPidsList);
    }

    // Union of start + end PIDs.
    const cgroupPidUnion = new Set<number>([
      ...this._observedPids,
      ...currentPids,
    ]);

    // Canonical SKU from first non-null productName (homogeneous-device assumption).
    const canonicalProductName =
      this._deviceProductNames.find((n) => n !== null && n !== undefined) ?? null;
    let gpuSku: string | null;
    if (this.runtime === GpuRuntimeKind.LocalGpu && canonicalProductName) {
      // The normalized NVML name is authoritative for owned hardware. Coarse
      // aliases collapse PCIe/NVL/SXM and workstation variants.
      gpuSku = [...canonicalProductName].slice(0, 256).join("");
    } else {
      gpuSku = resolveSkuFromProductName(canonicalProductName);
    }

    // Decision #2 transparency: MIG presence surfaced regardless of cgroup-touch.
    let migProfile: string | null = null;
    if (this._deviceMigModes.some((m) => m)) {
      migProfile = "mig_detected";
    }

    const degenerateWindow = durationMs <= 0;

    const perDeviceGpuSeconds: Record<number, number> = {};
    const signalEvents: GpuUtilizationSignal[] = [];
    let anyPidTouched = false;

    for (const i of this._deviceIndexes) {
      // Sprint 2 Theme C / §3.1.1 (B2 TS port) — snapshot per-PID
      // baseline timestamps BEFORE the end NVML call mutates
      // `_initialTimestamps[i]` in place. Reading the mutated map
      // afterwards would zero every PID's first-sample dt.
      const baselineTsPerPid: Record<number, number> = {
        ...this._baselineTimestamps[i],
      };

      const finalSamples =
        this.hooks.getProcessUtilization(i, this._initialTimestamps[i]) ?? {};
      this.appendSamples(i, finalSamples);

      const mem = this.hooks.getMemoryInfo(i);
      if (mem) {
        this._vramUsedPeak[i] = Math.max(
          this._vramUsedPeak[i] ?? 0,
          mem.usedBytes,
        );
      }

      // Filter to cgroup-PID union. Each value is a list of samples.
      const relevantByPid: Record<number, UtilSample[]> = {};
      for (const [pidKey, samples] of Object.entries(this._utilizationSamples[i])) {
        const pid = Number(pidKey);
        if (cgroupPidUnion.has(pid) && samples.length > 0) {
          relevantByPid[pid] = samples;
        }
      }
      const relevantPidCount = Object.keys(relevantByPid).length;

      if (relevantPidCount > 0) {
        anyPidTouched = true;

        // B2: integrate sm_util × dt across the sample sequence.
        // Two semantics for "first sample of a PID with no baseline":
        //  - Device had ZERO PIDs at start → first sample's window
        //    extends back to the derived task_start_ts.
        //  - Other PIDs were active but this one wasn't → PID
        //    joined mid-task; first-sample dt is 0.
        const deviceHadBaselinePids = Object.keys(baselineTsPerPid).length > 0;
        let maxSampleTs = 0;
        for (const samples of Object.values(relevantByPid)) {
          for (const s of samples) {
            if (s.timeStamp > maxSampleTs) maxSampleTs = s.timeStamp;
          }
        }
        const taskStartTs = Math.max(0, maxSampleTs - durationMs * 1000);

        let gpuSecondsForDevice = 0;
        let memUtilSum = 0;
        let memUtilN = 0;
        for (const [pidKey, samples] of Object.entries(relevantByPid)) {
          const pid = Number(pidKey);
          let baselineForPid: number;
          if (pid in baselineTsPerPid) {
            baselineForPid = baselineTsPerPid[pid];
          } else if (deviceHadBaselinePids) {
            baselineForPid = samples[0].timeStamp;
          } else {
            baselineForPid = taskStartTs;
          }
          let prevTs = baselineForPid;
          for (const s of samples) {
            const dtUs = Math.max(0, s.timeStamp - prevTs);
            gpuSecondsForDevice +=
              (s.smUtil / 100.0) * (dtUs / 1_000_000.0);
            prevTs = s.timeStamp;
            memUtilSum += s.memUtil;
            memUtilN += 1;
          }
        }
        perDeviceGpuSeconds[i] = gpuSecondsForDevice;

        let smUtilPct: number | null;
        if (durationMs > 0) {
          const windowS = durationMs / 1000.0;
          smUtilPct = Math.min(100.0, (gpuSecondsForDevice / windowS) * 100.0);
        } else {
          smUtilPct = null;
        }
        const memUtilAvg = memUtilN > 0 ? memUtilSum / memUtilN : 0;

        signalEvents.push({
          billing_model: BILLING_MODEL_FOR_RUNTIME[this.runtime] ?? "local_gpu_usage_only",
          runtime_kind: this.runtime,
          cloud_provider: this.cloudEnv.provider,
          gpu_index: i,
          gpu_sku: gpuSku,
          sm_util_pct: smUtilPct,
          mem_util_pct: memUtilAvg,
          vram_used_peak_bytes: this._vramUsedPeak[i] ?? 0,
          vram_total_bytes: this._vramTotal[i] ?? 0,
          process_count: this._pidsTouchedPerDevice[i].size,
          sample_count: memUtilN,
          task_duration_ms: durationMs,
        });
      } else if (degenerateWindow) {
        signalEvents.push({
          billing_model: BILLING_MODEL_FOR_RUNTIME[this.runtime] ?? "local_gpu_usage_only",
          runtime_kind: this.runtime,
          cloud_provider: this.cloudEnv.provider,
          gpu_index: i,
          gpu_sku: gpuSku,
          sm_util_pct: null,
          mem_util_pct: null,
          vram_used_peak_bytes: this._vramUsedPeak[i] ?? 0,
          vram_total_bytes: this._vramTotal[i] ?? 0,
          process_count: this._pidsTouchedPerDevice[i].size,
          sample_count: 0,
          task_duration_ms: durationMs,
        });
      }
    }

    const shouldEmitCost =
      anyPidTouched ||
      fallbackLabel !== null ||
      degenerateWindow ||
      this._deviceMigModes.some((m) => m);
    if (!shouldEmitCost) {
      return { costDetails: null, signalEvents: null };
    }

    const totalGpuSeconds = Object.values(perDeviceGpuSeconds).reduce(
      (a, b) => a + b,
      0,
    );
    const costDetails = this.buildCostEvent({
      durationMs,
      gpuSku,
      gpuCount: this._deviceIndexes.length,
      gpuSecondsUsed: totalGpuSeconds,
      migProfile,
      fallbackLabel,
    });
    return {
      costDetails,
      signalEvents: signalEvents.length > 0 ? signalEvents : null,
    };
  }

  // ------------------------------------------------------------------
  // Event builders
  // ------------------------------------------------------------------

  private buildCostEvent(args: {
    durationMs: number;
    gpuSku: string | null;
    gpuCount: number;
    gpuSecondsUsed: number;
    migProfile: string | null;
    fallbackLabel: string | null;
  }): GpuCostDetails {
    const details: GpuCostDetails = {
      billing_model:
        BILLING_MODEL_FOR_RUNTIME[this.runtime] ?? "local_gpu_usage_only",
      runtime_kind: this.runtime,
      cloud_provider: this.cloudEnv.provider,
      gpu_vendor: "nvidia", // Decision #5
      gpu_sku: args.gpuSku,
      gpu_count: args.gpuCount,
      region: this.cloudEnv.region,
      duration_ms: args.durationMs,
      gpu_seconds_used: args.gpuSecondsUsed,
      instance_type: this.cloudEnv.instanceType ?? null,
      vgpu_profile: this.resolveVgpuProfile(),
      mig_profile: args.migProfile,
      cost_pending: true,
    };
    const productName = this._deviceProductNames.find(
      (n) => n !== null && n !== undefined,
    );
    if (productName) {
      details._nvml_product_name_lower = productName;
    }
    if (args.fallbackLabel) {
      details._cgroup_scope_fallback = args.fallbackLabel;
    }
    return details;
  }

  private resolveVgpuProfile(): string | null {
    if (this.runtime !== GpuRuntimeKind.AzureVmVgpu) return null;
    const it = this.cloudEnv.instanceType ?? null;
    if (!it) return null;
    return AZURE_VGPU_PROFILE_BY_INSTANCE[it] ?? null;
  }
}
