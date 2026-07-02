/**
 * Hono middleware for automatic request cost tracking.
 *
 * Hono is the idiomatic HTTP server on Bun and Deno (and runs on Node),
 * so this adapter is the SDK's request-tracking answer on those runtimes.
 * Dependency-free: typed structurally against Hono's `(c, next)`
 * middleware contract.
 *
 * Usage:
 *
 *   import { Hono } from "hono";
 *   import { init, createHonoMiddleware } from "@dexcost/sdk";
 *
 *   init({ apiKey: process.env.DEXCOST_API_KEY });
 *   const app = new Hono();
 *   app.use("*", createHonoMiddleware({
 *     customerId: (c) => c.req.header("x-customer-id"),
 *     skip: (c) => c.req.path === "/health",
 *   }));
 *
 * The tracked task is stored on the context (`c.get("dexcostTask")`), and
 * the downstream chain runs inside its AsyncLocalStorage scope so
 * auto-instrumented LLM/HTTP calls are attributed to the request.
 *
 * Runtime note: requires AsyncLocalStorage — available on Node and Bun
 * natively, on Deno via Node-compat, and on Cloudflare Workers only with
 * the `nodejs_compat` flag.
 */

import type { CostTracker, TrackedTask } from "../core/tracker.js";
import { runWithTask } from "../core/context.js";
import { scrubUrl } from "../security/redaction.js";
import { resolveMiddlewareTracker } from "./shared.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Options for {@link createHonoMiddleware}. */
export interface HonoMiddlewareOptions {
  /** Tracker to record into. Defaults to the `init()` singleton (lazy). */
  tracker?: CostTracker;
  /** Extract a customer ID from the context, e.g. `(c) => c.req.header("x-customer-id")`. */
  customerId?: (c: any) => string | undefined;
  /** Extract a project ID from the context. */
  projectId?: (c: any) => string | undefined;
  /** Derive the task type. Defaults to `"METHOD /path"`. */
  taskType?: (c: any) => string;
  /** Return true to skip tracking (health checks, static assets). */
  skip?: (c: any) => boolean;
}

function _endOnce(tracked: TrackedTask, status: "success" | "failed"): void {
  try {
    if (tracked.task.status === "pending") {
      tracked.end(status);
    }
  } catch {
    // dexcost errors must never break request handling
  }
}

/**
 * Create a Hono middleware handler.
 *
 * Failure posture: dexcost errors are contained (the request proceeds
 * untracked); handler errors are NEVER swallowed — they propagate to
 * Hono's error handling after the task is marked "failed".
 */
export function createHonoMiddleware(
  options: HonoMiddlewareOptions = {},
): (c: any, next: () => Promise<void>) => Promise<void> {
  return async (c: any, next: () => Promise<void>): Promise<void> => {
    let tracked: TrackedTask | undefined;
    try {
      if (options.skip?.(c)) {
        return await next();
      }
      const tracker = resolveMiddlewareTracker("hono", options.tracker);
      if (!tracker) {
        return await next();
      }

      const method = typeof c?.req?.method === "string" ? c.req.method : "UNKNOWN";
      const path = scrubUrl(typeof c?.req?.path === "string" ? c.req.path : "/");
      tracked = tracker.startTask({
        taskType: options.taskType?.(c) ?? `${method} ${path}`,
        customerId: options.customerId?.(c),
        projectId: options.projectId?.(c),
      });
      if (typeof c?.set === "function") {
        c.set("dexcostTask", tracked);
      }
    } catch {
      // dexcost failure must never block the request — proceed untracked.
      tracked = undefined;
    }

    if (!tracked) {
      return next();
    }

    const task = tracked;
    try {
      await runWithTask(task.task, () => next());
      const statusCode = typeof c?.res?.status === "number" ? c.res.status : 200;
      _endOnce(task, statusCode >= 400 ? "failed" : "success");
    } catch (err) {
      _endOnce(task, "failed");
      throw err;
    }
  };
}
