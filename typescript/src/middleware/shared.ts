/**
 * Shared plumbing for the HTTP-framework middlewares (Express, Fastify,
 * Hono). Framework adapters stay dependency-free (structural typing only);
 * everything cross-cutting lives here.
 */

import type { CostTracker } from "../core/tracker.js";
import { getTracker } from "../core/tracker.js";
import { debugLog } from "../core/debug.js";

/**
 * Resolve a dot-separated path on an object.
 *
 * @example getNestedValue({ user: { orgId: "acme" } }, "user.orgId") // "acme"
 */
export function getNestedValue(obj: unknown, path: string): string | undefined {
  const parts = path.split(".");
  let current: unknown = obj;
  for (const part of parts) {
    if (current == null || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return typeof current === "string" ? current : undefined;
}

const _warned = new Set<string>();

/** Test-only: reset the warn-once latches. */
export function _resetMiddlewareWarningsForTests(): void {
  _warned.clear();
}

/**
 * Resolve the tracker for a middleware, defaulting to the `init()`
 * singleton, resolved lazily PER REQUEST — so middleware can be
 * constructed at module scope before `init()` has run.
 *
 * Loud (once per framework) when the SDK was never initialized: a silent
 * pass-through would be an invisible tracking gap.
 */
export function resolveMiddlewareTracker(
  framework: string,
  explicit?: CostTracker,
): CostTracker | null {
  if (explicit) return explicit;
  try {
    return getTracker();
  } catch {
    if (!_warned.has(framework)) {
      _warned.add(framework);
      // eslint-disable-next-line no-console
      console.warn(
        `[dexcost] ${framework} middleware is mounted but dexcost is not ` +
          "initialized (init() was never called) — requests are passing " +
          "through untracked.",
      );
    }
    debugLog("middleware", `${framework}: request untracked (tracker not initialized)`);
    return null;
  }
}
