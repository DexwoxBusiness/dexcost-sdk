/**
 * Cgroup-scope classifier — Phase 2 GPU foundation Decision #1.
 *
 * Reads `/proc/self/cgroup` and classifies the cgroup scope by prefix into
 * one of:
 *
 * - "container"            — kubepods.slice / docker / containerd / crio /
 *                            system.slice/{docker,containerd,crio}-.
 *                            cgroup.procs enumerates the container's PIDs.
 * - "bare_metal_user_slice" — `/user.slice/...` (systemd user session).
 *                            Walking would capture every PID in the SSH
 *                            session, not just dexcost's task. Degrade to
 *                            self-PID-only at `estimated` confidence with
 *                            pricing_source suffix :no_container_scope.
 * - "root_cgroup"          — `/` (privileged single-tenant host). Ambiguous;
 *                            degrade to self-PID-only.
 * - "cgroup_v1"            — multi-line file (multiple v1 controllers).
 *                            v1.1 will walk; v1 degrades to self-PID-only.
 * - "unknown"              — anything else.
 *
 * **Browser safety**: returns "unknown" off-Node.
 *
 * Mirrors python/src/dexcost/cgroup_walker.py.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

// ─── Module state (testable) ────────────────────────────────────────────────

let _procSelfCgroupPath = "/proc/self/cgroup";
let _cgroupRoot = "/sys/fs/cgroup";

/** Test-only — override /proc/self/cgroup path. Pass null to restore. */
export function _setProcSelfCgroupPathForTests(p: string | null): void {
  _procSelfCgroupPath = p ?? "/proc/self/cgroup";
}

/** Test-only — override the cgroup root. Pass null to restore. */
export function _setCgroupRootForTests(p: string | null): void {
  _cgroupRoot = p ?? "/sys/fs/cgroup";
}

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

// ─── Browser-safety guard ───────────────────────────────────────────────────

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

// ─── Decision #1 prefix table (priority order) ──────────────────────────────

const CONTAINER_PREFIXES: readonly string[] = [
  "/kubepods.slice/",
  "/kubepods/",
  "/docker/",
  "/system.slice/docker-",
  "/containerd/",
  "/system.slice/containerd-",
  "/crio/",
  "/system.slice/crio-",
];

const BARE_METAL_PREFIXES: readonly string[] = [
  "/user.slice/",
];

// ─── Public types ───────────────────────────────────────────────────────────

export type CgroupScopeKind =
  | "container"
  | "bare_metal_user_slice"
  | "root_cgroup"
  | "cgroup_v1"
  | "unknown";

export interface CgroupScope {
  kind: CgroupScopeKind;
  /** Unified cgroup-v2 path for `container` scope; null for every other kind. */
  path: string | null;
}

// ─── Classification ─────────────────────────────────────────────────────────

export function classifyScope(): CgroupScope {
  if (!_isNode()) {
    return { kind: "unknown", path: null };
  }
  let raw: string;
  try {
    raw = readFileSync(_procSelfCgroupPath, "utf-8");
  } catch {
    return { kind: "unknown", path: null };
  }

  const lines = raw
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  if (lines.length === 0) {
    return { kind: "unknown", path: null };
  }

  // cgroup v1 → multiple controller lines; cgroup v2 → single "0::/path".
  if (lines.length > 1 || !lines[0].startsWith("0::")) {
    return { kind: "cgroup_v1", path: null };
  }

  const path = lines[0].substring(3); // strip "0::" prefix
  if (path === "/" || path === "") {
    return { kind: "root_cgroup", path: null };
  }

  for (const prefix of CONTAINER_PREFIXES) {
    if (path.startsWith(prefix)) {
      return { kind: "container", path };
    }
  }
  for (const prefix of BARE_METAL_PREFIXES) {
    if (path.startsWith(prefix)) {
      return { kind: "bare_metal_user_slice", path: null };
    }
  }
  return { kind: "unknown", path: null };
}

// ─── PID enumeration ────────────────────────────────────────────────────────

/**
 * Return the PID set to attribute GPU usage to.
 *
 * - container scope → walk `<cgroupRoot><scope.path>/cgroup.procs`. Returns
 *   `null` (NOT empty list) on read failure — signals the caller to log-
 *   once `gpu_cgroup_walk_forbidden` and fall back.
 * - every other scope → return `[process.pid]`. Bare-metal-no-container
 *   deliberately does NOT walk the systemd user slice (which would capture
 *   unrelated user PIDs — the silent-overcount case).
 *
 * Off-Node, returns `[1]` as a non-failing placeholder (matches Python
 * semantics where `os.getpid()` is always available in CPython; in TS the
 * browser doesn't have a PID so we surface a benign sentinel).
 */
export function enumeratePids(scope: CgroupScope): number[] | null {
  if (!_isNode()) {
    return [1];
  }
  if (scope.kind !== "container" || scope.path === null) {
    return [process.pid];
  }

  const cgroupProcsPath = join(_cgroupRoot + scope.path, "cgroup.procs");
  let raw: string;
  try {
    raw = readFileSync(cgroupProcsPath, "utf-8");
  } catch (err) {
    _warnOnce(
      "gpu_cgroup_walk_forbidden",
      `Could not read ${cgroupProcsPath} (${String(err)}); ` +
        `GpuAccountant will degrade to self-PID-only`,
    );
    return null;
  }

  const pids: number[] = [];
  for (const line of raw.split("\n")) {
    const s = line.trim();
    if (!s) continue;
    const n = parseInt(s, 10);
    if (!Number.isNaN(n)) pids.push(n);
  }
  return pids;
}

// ─── Decision #1 confidence labelling ───────────────────────────────────────

/**
 * Map scope kind to the pricing_source suffix per Decision #1.
 *
 * - container → null (full-fidelity attribution; no suffix)
 * - bare_metal_user_slice / root_cgroup → "no_container_scope"
 * - cgroup_v1 / unknown → "self_pid_only"
 */
export function fallbackLabelFor(scope: CgroupScope): string | null {
  if (scope.kind === "container") return null;
  if (scope.kind === "bare_metal_user_slice" || scope.kind === "root_cgroup") {
    return "no_container_scope";
  }
  return "self_pid_only";
}
