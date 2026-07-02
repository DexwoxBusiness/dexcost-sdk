/**
 * NetworkAccountant — per-task in-process accumulator of HTTP byte usage.
 *
 * Ports python/src/dexcost/network_accountant.py to TypeScript and mirrors
 * the Go (go/adapters/network_accountant.go) and Rust (rust/src/adapters/
 * network_accountant.rs) ports. Single-threaded event loop means no
 * mutex is needed — concurrent record() can't happen without an await.
 *
 * isInternal follows the v1 §4.2 three-valued classification (same as
 * `classifyDestination` in `_netbytes.ts`):
 *
 *   - `true`  → bytes are intra-VPC / loopback → 0 external bytes.
 *   - `false` → confirmed public IP → all of bytesOut are external.
 *   - `null`  → unresolved named host → treated as external (conservative —
 *               over-attribute rather than undercount).
 */

/** Number of host entries kept in by_host after finalize (plus `_other`). */
export const FINALIZE_CAP = 20;
/** Max distinct hosts tracked live before overflow folds into `_other`. */
export const LIVE_CAP = 500;

interface HostEntry {
  /** [calls, bytes_in, bytes_out, external_bytes_out] */
  values: [number, number, number, number];
}

export interface NetworkSnapshot {
  bytesIn: number;
  bytesOut: number;
  /** canonical scalar — basis for v2 network_cost_usd */
  externalBytesOut: number;
  callCount: number;
  /** `{ hosts: [...] }` shape, JSON-friendly */
  byHost: { hosts: Array<Record<string, unknown>> };
}

export class NetworkAccountant {
  private bytesIn = 0;
  private bytesOut = 0;
  private externalBytesOut = 0;
  private callCount = 0;
  private hosts = new Map<string, HostEntry>();
  // Overflow bucket once LIVE_CAP distinct hosts are tracked.
  private other: [number, number, number, number] = [0, 0, 0, 0];
  private frozen = false;

  /** Add one HTTP call's bytes. No-op once finalize() has been called. */
  record(
    host: string,
    bytesIn: number,
    bytesOut: number,
    isInternal: boolean | null = null,
  ): void {
    // Clamp negatives — bytes can never be negative.
    bytesIn = Math.max(0, bytesIn);
    bytesOut = Math.max(0, bytesOut);
    const externalOut = isInternal === true ? 0 : bytesOut;

    if (this.frozen) return;

    this.bytesIn += bytesIn;
    this.bytesOut += bytesOut;
    this.externalBytesOut += externalOut;
    this.callCount += 1;

    const key = host || "_unknown";
    const entry = this.hosts.get(key);
    if (entry) {
      entry.values[0] += 1;
      entry.values[1] += bytesIn;
      entry.values[2] += bytesOut;
      entry.values[3] += externalOut;
    } else if (this.hosts.size < LIVE_CAP) {
      this.hosts.set(key, { values: [1, bytesIn, bytesOut, externalOut] });
    } else {
      this.other[0] += 1;
      this.other[1] += bytesIn;
      this.other[2] += bytesOut;
      this.other[3] += externalOut;
    }
  }

  /** Number of distinct hosts currently tracked (excludes `_other`). */
  liveHostCount(): number {
    return this.hosts.size;
  }

  /**
   * Freezes the accountant and returns the snapshot for the task fields.
   *
   * Returns the top FINALIZE_CAP hosts by total bytes (bytes_in + bytes_out)
   * plus an `_other` bucket summing the rest. Each host entry carries
   * `external_bytes_out` so v2 per-host egress cost survives the cap.
   * If a real host is literally named `_other` it is folded into the
   * synthetic overflow bucket — the output never has duplicate names.
   */
  finalize(): NetworkSnapshot {
    this.frozen = true;

    const ranked: Array<[string, HostEntry]> = Array.from(this.hosts.entries());
    ranked.sort((a, b) => {
      const totalA = a[1].values[1] + a[1].values[2];
      const totalB = b[1].values[1] + b[1].values[2];
      return totalB - totalA;
    });

    const other: [number, number, number, number] = [...this.other];
    const top: Array<[string, HostEntry]> = [];

    for (let i = 0; i < ranked.length; i++) {
      const [host, entry] = ranked[i];
      if (i < FINALIZE_CAP) {
        if (host === "_other") {
          // Fold real-host-named-_other into the synthetic bucket so the
          // output never contains duplicates.
          for (let j = 0; j < 4; j++) other[j] += entry.values[j];
        } else {
          top.push([host, entry]);
        }
      } else {
        for (let j = 0; j < 4; j++) other[j] += entry.values[j];
      }
    }

    const hosts: Array<Record<string, unknown>> = top.map(([host, entry]) => ({
      host,
      calls: entry.values[0],
      bytes_in: entry.values[1],
      bytes_out: entry.values[2],
      external_bytes_out: entry.values[3],
    }));
    if (other[0] > 0) {
      hosts.push({
        host: "_other",
        calls: other[0],
        bytes_in: other[1],
        bytes_out: other[2],
        external_bytes_out: other[3],
      });
    }

    return {
      bytesIn: this.bytesIn,
      bytesOut: this.bytesOut,
      externalBytesOut: this.externalBytesOut,
      callCount: this.callCount,
      byHost: { hosts },
    };
  }
}

// ---------------------------------------------------------------------------
// Registry — taskId → NetworkAccountant
// ---------------------------------------------------------------------------
//
// The HTTP adapter (patched globalThis.fetch) resolves a task via context
// AsyncLocalStorage but the registered accountant is keyed by taskId so
// the tracker can register/unregister independently of context state.
// Mirrors the Rust + Go registry pattern.

const _registry = new Map<string, NetworkAccountant>();

export function registerAccountant(
  taskId: string,
  accountant: NetworkAccountant,
): void {
  _registry.set(taskId, accountant);
}

export function getAccountant(taskId: string): NetworkAccountant | undefined {
  return _registry.get(taskId);
}

export function unregisterAccountant(
  taskId: string,
): NetworkAccountant | undefined {
  const a = _registry.get(taskId);
  _registry.delete(taskId);
  return a;
}

/** Test-only: number of live accountant registry entries (leak checks). */
export function _accountantRegistrySize(): number {
  return _registry.size;
}

/**
 * Test-only: clear the entire registry. Use between tests that exercise
 * the registry to avoid cross-test contamination.
 */
export function _resetAccountantRegistryForTests(): void {
  _registry.clear();
}
