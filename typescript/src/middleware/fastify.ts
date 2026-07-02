/**
 * Fastify plugin for automatic request cost tracking.
 *
 * Dependency-free: typed structurally against the Fastify v4/v5 plugin and
 * hook shapes, so `fastify` stays out of the dependency tree.
 *
 * Usage:
 *
 *   import Fastify from "fastify";
 *   import { init, dexcostFastifyPlugin } from "@dexcost/sdk";
 *
 *   init({ apiKey: process.env.DEXCOST_API_KEY });
 *   const app = Fastify();
 *   await app.register(dexcostFastifyPlugin, {
 *     customerIdFrom: "headers.x-customer-id",
 *     skip: (req) => req.url === "/health",
 *   });
 *
 * Each request runs inside a tracked task (available as
 * `request.dexcostTask`), so auto-instrumented LLM/HTTP calls made while
 * handling the request are attributed to it. Task status comes from the
 * response status code (>= 400 → failed) or the error hook.
 *
 * Context propagation: the onRequest hook re-enters the rest of the
 * request lifecycle through `runWithTask(task, done)` — the callback-style
 * hook contract means everything downstream (later hooks, the route
 * handler) executes inside the task's AsyncLocalStorage scope, with no
 * `enterWith` leak across keep-alive requests.
 */

import type { CostTracker, TrackedTask } from "../core/tracker.js";
import { runWithTask } from "../core/context.js";
import { scrubUrl } from "../security/redaction.js";
import { getNestedValue, resolveMiddlewareTracker } from "./shared.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Options for {@link dexcostFastifyPlugin}. */
export interface FastifyPluginOptions {
  /** Tracker to record into. Defaults to the `init()` singleton (lazy). */
  tracker?: CostTracker;
  /** Dot-path into the request for a customer ID, e.g. "headers.x-customer-id". */
  customerIdFrom?: string;
  /** Dot-path into the request for a project ID. */
  projectIdFrom?: string;
  /** Derive the task type from the request. Defaults to `"METHOD /url"`. */
  taskType?: (request: unknown) => string;
  /** Return true to skip tracking (health checks, static assets). */
  skip?: (request: unknown) => boolean;
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
 * The plugin function. Register with `app.register(dexcostFastifyPlugin, opts)`.
 */
export function dexcostFastifyPlugin(
  instance: any,
  opts: FastifyPluginOptions,
  done: (err?: Error) => void,
): void {
  try {
    instance.addHook(
      "onRequest",
      (request: any, _reply: any, hookDone: () => void) => {
        try {
          if (opts.skip?.(request)) {
            hookDone();
            return;
          }
          const tracker = resolveMiddlewareTracker("fastify", opts.tracker);
          if (!tracker) {
            hookDone();
            return;
          }

          const method = typeof request?.method === "string" ? request.method : "UNKNOWN";
          const url = scrubUrl(typeof request?.url === "string" ? request.url : "/");
          const tracked = tracker.startTask({
            taskType: opts.taskType?.(request) ?? `${method} ${url}`,
            customerId: opts.customerIdFrom
              ? getNestedValue(request, opts.customerIdFrom)
              : undefined,
            projectId: opts.projectIdFrom
              ? getNestedValue(request, opts.projectIdFrom)
              : undefined,
          });
          request.dexcostTask = tracked;
          // Continue the lifecycle INSIDE the task's async scope.
          runWithTask(tracked.task, hookDone);
        } catch {
          // dexcost failure must never block the request pipeline.
          hookDone();
        }
      },
    );

    instance.addHook(
      "onResponse",
      (request: any, reply: any, hookDone: () => void) => {
        const tracked: TrackedTask | undefined = request?.dexcostTask;
        if (tracked) {
          const statusCode = typeof reply?.statusCode === "number" ? reply.statusCode : 200;
          _endOnce(tracked, statusCode >= 400 ? "failed" : "success");
        }
        hookDone();
      },
    );

    instance.addHook(
      "onError",
      (request: any, _reply: any, _error: unknown, hookDone: () => void) => {
        const tracked: TrackedTask | undefined = request?.dexcostTask;
        if (tracked) {
          _endOnce(tracked, "failed");
        }
        hookDone();
      },
    );

    done();
  } catch (err) {
    done(err instanceof Error ? err : new Error(String(err)));
  }
}

// Break Fastify's plugin encapsulation (what `fastify-plugin` does) so the
// hooks apply to the whole application when registered at the root — the
// behaviour every user of a tracking plugin expects.
(dexcostFastifyPlugin as any)[Symbol.for("skip-override")] = true;
(dexcostFastifyPlugin as any)[Symbol.for("fastify.display-name")] = "dexcost";
