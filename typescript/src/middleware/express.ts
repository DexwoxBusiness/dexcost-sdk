/**
 * Express/Connect middleware for automatic HTTP request cost tracking.
 *
 * Wraps each incoming request in a tracked task, attaching the TrackedTask
 * instance to `req.dexcostTask` so downstream handlers can record costs.
 */

import type { CostTracker } from "../core/tracker.js";
import { scrubUrl } from "../security/redaction.js";

/** Options for configuring the Express middleware. */
export interface ExpressMiddlewareOptions {
  /**
   * Dot-path into the request object to extract a customer ID.
   * For example, "user.orgId" reads `req.user.orgId`.
   */
  customerIdFrom?: string;

  /**
   * Dot-path into the request object to extract a project ID.
   * For example, "headers.x-project-id" reads `req.headers["x-project-id"]`.
   */
  projectIdFrom?: string;

  /**
   * Custom function to derive the task type from the request.
   * Defaults to `"METHOD /path"`.
   */
  taskType?: (req: unknown) => string;

  /**
   * Predicate to skip tracking for certain requests (e.g. health checks).
   * Return `true` to skip.
   */
  skip?: (req: unknown) => boolean;
}

/**
 * Resolve a dot-separated path on an object.
 *
 * @example getNestedValue({ user: { orgId: "acme" } }, "user.orgId") // "acme"
 */
function getNestedValue(obj: unknown, path: string): string | undefined {
  const parts = path.split(".");
  let current: unknown = obj;
  for (const part of parts) {
    if (current == null || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return typeof current === "string" ? current : undefined;
}

/**
 * Create an Express/Connect middleware that wraps each request in a
 * dexcost tracked task.
 *
 * The middleware attaches the `TrackedTask` instance to `req.dexcostTask`
 * so that downstream route handlers can call `req.dexcostTask.recordLlmCall()`
 * and similar methods.
 *
 * @example
 * ```typescript
 * import express from "express";
 * import { CostTracker } from "dexcost";
 * import { createExpressMiddleware } from "dexcost/middleware/express";
 *
 * const app = express();
 * const tracker = new CostTracker();
 *
 * app.use(createExpressMiddleware(tracker, {
 *   customerIdFrom: "user.orgId",
 *   skip: (req) => req.path === "/health",
 * }));
 * ```
 */
export function createExpressMiddleware(
  tracker: CostTracker,
  options: ExpressMiddlewareOptions = {},
): (req: unknown, res: unknown, next: () => void) => void {
  return (req: unknown, res: unknown, next: () => void): void => {
    const reqObj = req as Record<string, unknown>;
    const resObj = res as Record<string, unknown>;

    if (options.skip?.(req)) {
      next();
      return;
    }

    const method = typeof reqObj["method"] === "string" ? reqObj["method"] : "UNKNOWN";
    // Express keeps the raw query string on originalUrl even when the
    // body is streamed (B11 correlation); a literal scrubUrl call here
    // strips userinfo and sensitive query params before this lands in
    // Task.taskType, which ships to the control plane.
    const rawUrl = typeof reqObj["originalUrl"] === "string"
      ? reqObj["originalUrl"]
      : typeof reqObj["path"] === "string"
        ? reqObj["path"]
        : "/";
    // scrubUrl is a no-op on relative paths (no scheme), but kicks in
    // when callers pass absolute URLs through originalUrl.
    const url = scrubUrl(rawUrl);

    const taskType = options.taskType?.(req) ?? `${method} ${url}`;

    const customerId = options.customerIdFrom
      ? getNestedValue(req, options.customerIdFrom)
      : undefined;

    const projectId = options.projectIdFrom
      ? getNestedValue(req, options.projectIdFrom)
      : undefined;

    const onFn = typeof resObj["on"] === "function" ? resObj["on"] as (event: string, cb: () => void) => void : null;

    if (onFn) {
      void tracker.track(
        { taskType, customerId, projectId },
        async (task) => {
          reqObj["dexcostTask"] = task;
          next();
          await new Promise<void>((resolve) => {
            onFn.call(resObj, "finish", () => {
              const statusCode = typeof resObj["statusCode"] === "number" ? resObj["statusCode"] : 200;
              const status = statusCode >= 400 ? "failed" : "success";
              task.end(status);
              resolve();
            });
          });
        },
      );
    } else {
      // Can't detect response end — finalize immediately after next()
      void tracker.track(
        { taskType, customerId, projectId },
        async (task) => {
          reqObj["dexcostTask"] = task;
          next();
          const statusCode = typeof resObj["statusCode"] === "number" ? resObj["statusCode"] : 200;
          const status = statusCode >= 400 ? "failed" : "success";
          task.end(status);
        },
      );
    }
  };
}
