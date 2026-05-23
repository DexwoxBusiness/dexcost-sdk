/**
 * NVML reader — Phase 2 GPU foundation.
 *
 * **TypeScript architectural deviation from Python**: there is no maintained
 * native NVML binding for Node in 2026. This module shells out to the
 * `nvidia-smi` CLI via `child_process.spawnSync` and parses CSV output.
 * Functions still return typed objects (ProcessInfo, UtilSample, MemInfo)
 * so downstream callers see the same shape as the Python `pynvml`
 * wrapper. Per-probe overhead is ~50ms (acceptable for finalize-time use;
 * not suitable for hot-path sampling — but neither is NVML).
 *
 * Fail-silent contract (convention §9) identical to Python:
 * every reader returns `null` (or `false` for booleans) on missing binary,
 * non-zero exit, or parse failure — the caller (GpuAccountant) decides
 * the fallback policy per Decision #1's classification table.
 *
 * **Browser safety**: nvmlAvailable() short-circuits to false off-Node.
 *
 * Decision #4: getProductName applies NFC + lowercase + whitespace collapse
 * on the raw nvidia-smi product-name output before returning — catalog
 * alias matching depends on byte-level equality after normalization.
 *
 * Decision #8: getProcessUtilization takes a mutable `lastSeenTimestamps`
 * dict and updates it in place. The TS implementation uses
 * `process.hrtime.bigint() → ms` for the per-PID timestamp (nvidia-smi pmon
 * does not expose NVML's sample-buffer timestamp). The accountant uses
 * (endTs - startTs) ms as the per-PID active window proxy.
 */

import { spawnSync as nodeSpawnSync } from "node:child_process";
import type { SpawnSyncReturns } from "node:child_process";

// Indirection so tests can swap the spawn implementation. The TS ESM bound
// import would otherwise be immutable across module instances.
type SpawnFn = (
  cmd: string,
  args: string[],
  options: { encoding: "utf-8"; timeout: number },
) => SpawnSyncReturns<string>;

let _spawnFn: SpawnFn = (cmd, args, options) =>
  nodeSpawnSync(cmd, args, options) as SpawnSyncReturns<string>;

/** Test-only — override the spawnSync implementation. Pass `null` to restore. */
export function _setSpawnFnForTests(fn: SpawnFn | null): void {
  _spawnFn =
    fn ??
    ((cmd, args, options) =>
      nodeSpawnSync(cmd, args, options) as SpawnSyncReturns<string>);
}

// ─── Warn-once tracking (single-threaded JS → no lock) ──────────────────────

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

// ─── Browser-safety guard ───────────────────────────────────────────────────

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

// ─── Test-only nvidia-smi path override ─────────────────────────────────────

let _nvidiaSmiPath: string = "nvidia-smi";

/** Test-only — override the nvidia-smi binary path. Pass `null` to restore. */
export function _setNvidiaSmiPathForTests(p: string | null): void {
  _nvidiaSmiPath = p ?? "nvidia-smi";
}

// ─── Typed return values ────────────────────────────────────────────────────

export interface ProcessInfo {
  /** PID of the process holding the GPU. */
  pid: number;
  /** VRAM used by this process in bytes (0 when nvidia-smi reports N/A). */
  usedGpuMemory: number;
}

export interface UtilSample {
  pid: number;
  /** 0–100; percent of time SMs had ≥1 kernel running during the sample window. */
  smUtil: number;
  /** 0–100; percent of time memory subsystem was busy. */
  memUtil: number;
  /** Per-PID timestamp (milliseconds since process start) for Decision #8 state. */
  timeStamp: number;
}

export interface MemInfo {
  usedBytes: number;
  totalBytes: number;
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function _runSmi(args: string[]): { stdout: string; ok: boolean } {
  if (!_isNode()) return { stdout: "", ok: false };
  try {
    const r = _spawnFn(_nvidiaSmiPath, args, {
      encoding: "utf-8",
      timeout: 5000,
    });
    if (r.error || r.status !== 0) {
      return { stdout: "", ok: false };
    }
    return { stdout: r.stdout ?? "", ok: true };
  } catch {
    return { stdout: "", ok: false };
  }
}

function _normalizeProductName(raw: string): string {
  // NFC → lowercase → whitespace collapse (incl. NBSP / NNBSP / zero-width).
  const nfc = raw.normalize("NFC");
  // String.split(/\s+/) treats Unicode whitespace incl. U+00A0 NBSP as a delimiter.
  return nfc.toLowerCase().split(/\s+/).filter(Boolean).join(" ");
}

// ─── Availability + init ────────────────────────────────────────────────────

/**
 * True when `nvidia-smi` is invokable and exits 0 on the count query.
 *
 * The TS-SDK definition of "available" is "binary works"; in Python this
 * was "pynvml importable AND nvmlInit succeeds". Same end behaviour:
 * downstream code checks this predicate before calling readers.
 */
export function nvmlAvailable(): boolean {
  if (!_isNode()) return false;
  const r = _runSmi(["--query-gpu=count", "--format=csv,noheader"]);
  if (!r.ok) {
    _warnOnce(
      "gpu_nvidia_smi_not_found",
      "nvidia-smi not on PATH or exited non-zero; GPU capture disabled. " +
        "Install NVIDIA driver + container toolkit for GPU cost attribution.",
    );
    return false;
  }
  return true;
}

/** Call `nvidia-smi --query-gpu=count` once. True on success, false on any failure. */
export function initNvml(): boolean {
  return nvmlAvailable();
}

/** No-op on TS; the CLI has no persistent handle. Provided for API parity. */
export function shutdownNvml(): void {
  /* no-op */
}

// ─── Device enumeration ─────────────────────────────────────────────────────

/** Number of NVIDIA devices visible to nvidia-smi. `null` on failure. */
export function getDeviceCount(): number | null {
  if (!_isNode()) return null;
  const r = _runSmi(["--query-gpu=count", "--format=csv,noheader"]);
  if (!r.ok) return null;
  // nvidia-smi --query-gpu=count emits one line per GPU, all with the same count value.
  // Take the first non-empty line.
  const first = r.stdout.split("\n").map((s) => s.trim()).find((s) => s);
  if (!first) return null;
  const n = parseInt(first, 10);
  if (Number.isNaN(n)) {
    _warnOnce("gpu_device_count_parse", `nvidia-smi count unparseable: ${first}`);
    return null;
  }
  return n;
}

// ─── Decision #4: NFC-normalized productName ────────────────────────────────

/**
 * Return the nvidia-smi product-name for `index`, NFC-normalized + lowercased.
 *
 * Decision #4 — alias matching against the catalog depends on byte-level
 * equality post-normalization.
 */
export function getProductName(index: number): string | null {
  if (!_isNode()) return null;
  const r = _runSmi([
    `--query-gpu=name`,
    `--format=csv,noheader`,
    `-i`,
    String(index),
  ]);
  if (!r.ok) {
    _warnOnce(
      "gpu_product_name_failed",
      `nvidia-smi --query-gpu=name failed for device ${index}`,
    );
    return null;
  }
  const line = r.stdout.split("\n").map((s) => s.trim()).find((s) => s);
  if (!line) return null;
  return _normalizeProductName(line);
}

// ─── Per-PID compute processes (Decision #1 measurement-side primitive) ────

/**
 * List of PIDs currently holding GPU `index` + their VRAM usage in bytes.
 *
 * Returns `null` on permission denied / failure — the Decision #1
 * load-bearing case for non-root containers. The accountant's cgroup walk
 * then degrades to self-PID-only.
 */
export function getComputeRunningProcesses(index: number): ProcessInfo[] | null {
  if (!_isNode()) return null;
  const r = _runSmi([
    `--query-compute-apps=pid,used_memory`,
    `--format=csv,noheader,nounits`,
    `-i`,
    String(index),
  ]);
  if (!r.ok) {
    _warnOnce(
      "gpu_nvml_permission_denied",
      `nvidia-smi --query-compute-apps failed for device ${index}; ` +
        `GpuAccountant will degrade to self-PID-only`,
    );
    return null;
  }
  const out: ProcessInfo[] = [];
  for (const line of r.stdout.split("\n")) {
    const s = line.trim();
    if (!s) continue;
    const parts = s.split(",").map((p) => p.trim());
    if (parts.length < 2) continue;
    const pid = parseInt(parts[0], 10);
    if (Number.isNaN(pid)) continue;
    let memMib = parseInt(parts[1], 10);
    if (Number.isNaN(memMib)) memMib = 0;
    // nounits ⇒ used_memory is MiB.
    out.push({ pid, usedGpuMemory: memMib * 1024 * 1024 });
  }
  return out;
}

// ─── Per-PID utilization (Decision #8 persistent timestamps) ────────────────

/**
 * Per-PID utilization sampled from `nvidia-smi pmon -c 1 -s u`.
 *
 * Decision #8 — TS uses `Date.now()` as the per-PID timestamp proxy
 * because nvidia-smi pmon does not expose the NVML sample-buffer epoch.
 * The accountant uses (endTs - startTs) ms as the active-GPU-time
 * approximation between snapshots.
 *
 * Updates `lastSeenTimestamps` in place — callers persist across snapshots.
 */
export function getProcessUtilization(
  index: number,
  lastSeenTimestamps: Record<number, number>,
): Record<number, UtilSample> | null {
  if (!_isNode()) return null;
  const r = _runSmi([
    `pmon`,
    `-c`,
    `1`,
    `-s`,
    `u`,
    `-o`,
    `T`,
    `-i`,
    String(index),
  ]);
  if (!r.ok) {
    _warnOnce(
      "gpu_process_utilization_failed",
      `nvidia-smi pmon failed for device ${index}`,
    );
    return null;
  }
  const out: Record<number, UtilSample> = {};
  const nowMs = Date.now();
  for (const rawLine of r.stdout.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    // Skip header lines and N/A rows.
    if (line.startsWith("#")) continue;
    // Whitespace-separated columns: gpu pid type sm mem enc dec command
    const parts = line.split(/\s+/);
    if (parts.length < 5) continue;
    const pid = parseInt(parts[1], 10);
    if (Number.isNaN(pid)) continue;
    const sm = parseInt(parts[3], 10);
    const mem = parseInt(parts[4], 10);
    if (Number.isNaN(sm) || Number.isNaN(mem)) continue;
    out[pid] = {
      pid,
      smUtil: sm,
      memUtil: mem,
      timeStamp: nowMs,
    };
    lastSeenTimestamps[pid] = nowMs;
  }
  return out;
}

// ─── Memory + MIG ───────────────────────────────────────────────────────────

/** Device-level used / total VRAM (bytes). `null` on failure. */
export function getMemoryInfo(index: number): MemInfo | null {
  if (!_isNode()) return null;
  const r = _runSmi([
    `--query-gpu=memory.used,memory.total`,
    `--format=csv,noheader,nounits`,
    `-i`,
    String(index),
  ]);
  if (!r.ok) {
    _warnOnce(
      "gpu_memory_info_failed",
      `nvidia-smi --query-gpu=memory.used,memory.total failed for device ${index}`,
    );
    return null;
  }
  const line = r.stdout.split("\n").map((s) => s.trim()).find((s) => s);
  if (!line) return null;
  const parts = line.split(",").map((s) => s.trim());
  if (parts.length < 2) return null;
  const usedMib = parseInt(parts[0], 10);
  const totalMib = parseInt(parts[1], 10);
  if (Number.isNaN(usedMib) || Number.isNaN(totalMib)) return null;
  return {
    usedBytes: usedMib * 1024 * 1024,
    totalBytes: totalMib * 1024 * 1024,
  };
}

/** True when MIG is enabled on this device (Decision #2 detection). */
export function getMigMode(index: number): boolean {
  if (!_isNode()) return false;
  const r = _runSmi([
    `--query-gpu=mig.mode.current`,
    `--format=csv,noheader`,
    `-i`,
    String(index),
  ]);
  if (!r.ok) {
    // Older GPUs without MIG support → fail-silent (NOT an error).
    return false;
  }
  const line = r.stdout.split("\n").map((s) => s.trim()).find((s) => s);
  if (!line) return false;
  return line.toLowerCase() === "enabled";
}
