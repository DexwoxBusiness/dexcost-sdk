/**
 * Cgroup v2 file readers (node-only, browser-safe).
 *
 * Fail-silent contract (convention §9): every read returns `null` on missing
 * or malformed input. Non-Linux hosts, cgroup-v1 kernels, browsers, and
 * containers without a cgroup mount all silently return `null` — the caller
 * decides the fallback.
 *
 * Backed file layouts (all under `/sys/fs/cgroup/`):
 *
 * - `cpu.stat`         — multi-line; `usage_usec <N>` is the cumulative CPU
 *                        time consumed (microseconds). Read at task start +
 *                        end to compute vcpu-seconds for long-running runtimes.
 * - `cpu.max`          — single line `<quota|"max"> <period>` (both in
 *                        microseconds). `quota/period` is the vCPU count
 *                        enforced on this cgroup; `"max"` means no limit
 *                        (fall back to `os.cpus().length`).
 * - `memory.peak`      — single integer (bytes); the high-water mark since
 *                        cgroup creation. Available on kernels >= 5.19;
 *                        absent otherwise.
 * - `memory.max`       — single integer (bytes) or `"max"` (unlimited).
 * - `memory.current`   — single integer (bytes); the current RSS.
 *
 * Mirrors `python/src/dexcost/cgroup_reader.py`.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { cpus } from "node:os";

export interface CpuStat {
  /** Cumulative CPU time consumed (microseconds). */
  usageUsec: number;
}

export interface CpuMax {
  /** Period quota in microseconds, or `null` when cgroup is unlimited. */
  quotaUs: number | null;
  /** Period length in microseconds. */
  periodUs: number;
  /** Computed `quotaUs / periodUs`; `os.cpus().length` when unlimited. */
  vcpuCount: number;
}

let _cgroupRoot = "/sys/fs/cgroup";

/** Test-only — override the cgroup root. Pass `null` to restore default. */
export function _setCgroupRootForTests(p: string | null): void {
  _cgroupRoot = p ?? "/sys/fs/cgroup";
}

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

function _readText(name: string): string | null {
  if (!_isNode()) return null;
  try {
    return readFileSync(join(_cgroupRoot, name), "utf-8");
  } catch {
    return null;
  }
}

function _readInt(name: string): number | null {
  const raw = _readText(name);
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (trimmed === "max") return null;
  const n = Number(trimmed);
  if (!Number.isFinite(n) || Number.isNaN(n)) return null;
  if (!/^-?\d+$/.test(trimmed)) return null;
  return n;
}

export function readCpuStat(): CpuStat | null {
  if (!_isNode()) return null;
  const raw = _readText("cpu.stat");
  if (raw === null) return null;
  for (const line of raw.split("\n")) {
    if (line.startsWith("usage_usec ")) {
      const parts = line.split(/\s+/);
      if (parts.length < 2) return null;
      const n = Number(parts[1]);
      if (!Number.isFinite(n) || Number.isNaN(n)) return null;
      if (!/^-?\d+$/.test(parts[1]!)) return null;
      return { usageUsec: n };
    }
  }
  return null;
}

export function readCpuMax(): CpuMax | null {
  if (!_isNode()) return null;
  const raw = _readText("cpu.max");
  if (raw === null) return null;
  const parts = raw.trim().split(/\s+/);
  if (parts.length !== 2) return null;
  const periodUs = Number(parts[1]);
  if (!Number.isFinite(periodUs) || Number.isNaN(periodUs)) return null;
  if (!/^-?\d+$/.test(parts[1]!)) return null;
  if (periodUs <= 0) return null;
  if (parts[0] === "max") {
    const n = cpus().length || 1;
    return { quotaUs: null, periodUs, vcpuCount: n };
  }
  const quotaUs = Number(parts[0]);
  if (!Number.isFinite(quotaUs) || Number.isNaN(quotaUs)) return null;
  if (!/^-?\d+$/.test(parts[0]!)) return null;
  return { quotaUs, periodUs, vcpuCount: quotaUs / periodUs };
}

export function readMemoryPeak(): number | null {
  if (!_isNode()) return null;
  return _readInt("memory.peak");
}

export function readMemoryMax(): number | null {
  if (!_isNode()) return null;
  return _readInt("memory.max");
}

export function readMemoryCurrent(): number | null {
  if (!_isNode()) return null;
  return _readInt("memory.current");
}
