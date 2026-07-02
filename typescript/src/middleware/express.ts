/**
 * Express/Connect middleware for automatic HTTP request cost tracking.
 *
 * Wraps each incoming request in a tracked task, attaching the TrackedTask
 * instance to `req.dexcostTask` so downstream handlers can record costs.
 */

import type { CostTracker } from "../core/tracker.js";
import { scrubUrl } from "../security/redaction.js";
import { getNestedValue, resolveMiddlewareTracker } from "./shared.js";

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
 * Create an Express/Connect middleware that wraps each request in a
 * dexcost tracked task.
 *
 * The middleware attaches the `TrackedTask` instance to `req.dexcostTask`
 * so that downstream route handlers can call `req.dexcostTask.recordLlmCall()`
 * and similar methods.
 *
 * The tracker argument is optional — it defaults to the singleton created
 * by `init()`, resolved lazily per request so the middleware can be mounted
 * before `init()` runs.
 *
 * @example
 * ```typescript
 * import express from "express";
 * import { init, createExpressMiddleware } from "@dexcost/sdk";
 *
 * init({ apiKey: process.env.DEXCOST_API_KEY });
 * const app = express();
 *
 * app.use(createExpressMiddleware({
 *   customerIdFrom: "user.orgId",
 *   skip: (req) => req.path === "/health",
 * }));
 * ```
 */
export function createExpressMiddleware(
  options?: ExpressMiddlewareOptions,
): (req: unknown, res: unknown, next: () => void) => void;
export function createExpressMiddleware(
  tracker: CostTracker,
  options?: ExpressMiddlewareOptions,
): (req: unknown, res: unknown, next: () => void) => void;
export function createExpressMiddleware(
  trackerOrOptions?: CostTracker | ExpressMiddlewareOptions,
  maybeOptions?: ExpressMiddlewareOptions,
): (req: unknown, res: unknown, next: () => void) => void {
  // Overload discrimination: a CostTracker instance has a `track` method;
  // an options bag never does.
  const explicitTracker =
    trackerOrOptions && typeof (trackerOrOptions as CostTracker).track === "function"
      ? (trackerOrOptions as CostTracker)
      : undefined;
  const options: ExpressMiddlewareOptions =
    (explicitTracker ? maybeOptions : (trackerOrOptions as ExpressMiddlewareOptions)) ?? {};

  return (req: unknown, res: unknown, next: () => void): void => {
    const reqObj = req as Record<string, unknown>;
    const resObj = res as Record<string, unknown>;

    if (options.skip?.(req)) {
      next();
      return;
    }

    const tracker = resolveMiddlewareTracker("express", explicitTracker);
    if (!tracker) {
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
