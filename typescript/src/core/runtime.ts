/**
 * Runtime detection — Node, Bun, Deno.
 *
 * The SDK runs on Bun and Deno through their Node-compat layers, but two
 * behaviours must branch on the actual runtime:
 *
 * - better-sqlite3 is a V8-ABI native addon. Bun rejects it with a
 *   CATCHABLE error at construction; Deno's dlopen of a non-NAPI addon is
 *   a FATAL process-level symbol-lookup error that no try/catch can stop —
 *   so on Deno the SDK must not even attempt to load it.
 * - `dexcost doctor` reports the runtime and adjusts its probes.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

/** True when running under Bun. */
export function isBun(): boolean {
  return typeof (globalThis as any).Bun !== "undefined";
}

/** True when running under Deno. */
export function isDeno(): boolean {
  return typeof (globalThis as any).Deno !== "undefined";
}

/** Human-readable runtime descriptor, e.g. "deno 2.2.7 (node-compat 22.x)". */
export function runtimeDescription(): string {
  const g: any = globalThis;
  if (isBun()) {
    return `bun ${g.Bun?.version ?? "unknown"} (node-compat ${process.versions.node})`;
  }
  if (isDeno()) {
    return `deno ${g.Deno?.version?.deno ?? "unknown"} (node-compat ${process.versions.node})`;
  }
  return `node ${process.versions.node}`;
}
