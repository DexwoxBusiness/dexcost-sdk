/**
 * Fargate ECS task metadata reader (node-only, browser-safe).
 *
 * Hits `${ECS_CONTAINER_METADATA_URI_V4}/task` (or v3) once per process and
 * caches the parsed result. Exposes vcpuCount (number) and
 * memoryBytesLimit (number — converted from MiB per Decision #7).
 *
 * Fail-silent contract (convention §9): unreachable endpoint, malformed JSON,
 * missing fields all return `null` and log once via convention §11.
 *
 * Mirrors python/src/dexcost/fargate_metadata.py.
 */

const PROBE_TIMEOUT_MS = 250;

export interface FargateTaskMetadata {
  vcpuCount: number;
  memoryBytesLimit: number;
}

let _cached: FargateTaskMetadata | null = null;
let _resolved = false;
let _warned = false;

/** Test-only — clear cached state. */
export function _resetForTests(): void {
  _cached = null;
  _resolved = false;
  _warned = false;
}

/** Hookable fetch — tests replace this to avoid real network calls. */
let _fetchImpl: typeof fetch = (...args) => fetch(...args);
export function _setFetchForTests(impl: typeof fetch | null): void {
  _fetchImpl = impl ?? ((...args) => fetch(...args));
}

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

function _endpoint(): string | null {
  if (!_isNode()) return null;
  const base =
    process.env.ECS_CONTAINER_METADATA_URI_V4 ||
    process.env.ECS_CONTAINER_METADATA_URI;
  if (!base) return null;
  return base.replace(/\/+$/, "") + "/task";
}

/**
 * Read + cache the ECS task metadata. Idempotent.
 *
 * Returns `null` when not on Fargate, when the endpoint is unreachable,
 * or when the Limits block is missing / malformed.
 */
export async function fetchFargateMetadata(): Promise<FargateTaskMetadata | null> {
  if (_resolved) return _cached;

  const url = _endpoint();
  if (url === null) {
    _resolved = true;
    return null;
  }

  let payload: unknown;
  try {
    const resp = await _fetchImpl(url, {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    payload = await resp.json();
  } catch (exc) {
    _resolved = true;
    if (!_warned) {
      _warned = true;
      // eslint-disable-next-line no-console
      console.warn(
        `fargate metadata unreachable (${(exc as Error)?.message ?? exc}); ` +
          "compute cost will fall through to default rates",
      );
    }
    return null;
  }

  const limits =
    typeof payload === "object" && payload !== null
      ? ((payload as Record<string, unknown>).Limits as Record<string, unknown> | undefined)
      : undefined;
  if (!limits) {
    _resolved = true;
    return null;
  }

  const cpuRaw = limits.CPU;
  const memRaw = limits.Memory;
  const vcpu = typeof cpuRaw === "number" ? cpuRaw : Number(cpuRaw);
  const memMib = typeof memRaw === "number" ? memRaw : Number(memRaw);
  if (
    !Number.isFinite(vcpu) ||
    Number.isNaN(vcpu) ||
    !Number.isFinite(memMib) ||
    Number.isNaN(memMib)
  ) {
    _resolved = true;
    return null;
  }

  // Decision #7 — Fargate memory is in MiB (binary), NOT MB. Convert to
  // bytes via the binary divisor.
  const memoryBytesLimit = Math.trunc(memMib) * 1024 * 1024;

  const result: FargateTaskMetadata = {
    vcpuCount: vcpu,
    memoryBytesLimit,
  };
  _cached = result;
  _resolved = true;
  return result;
}
