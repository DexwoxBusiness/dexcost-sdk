/**
 * NestJS interceptor for automatic request cost tracking.
 *
 * Dependency-free at build time: the ExecutionContext / CallHandler
 * surfaces are duck-typed, and `rxjs` — a hard dependency of every NestJS
 * app — is loaded lazily from the HOST application at first use, so
 * neither `@nestjs/common` nor `rxjs` enters this SDK's dependency tree.
 *
 * Usage (global interceptor, recommended):
 *
 *   import { APP_INTERCEPTOR } from "@nestjs/core";
 *   import { init, DexcostInterceptor } from "@dexcost/sdk";
 *
 *   init({ apiKey: process.env.DEXCOST_API_KEY });
 *
 *   @Module({
 *     providers: [
 *       {
 *         provide: APP_INTERCEPTOR,
 *         useValue: new DexcostInterceptor({
 *           customerIdFrom: "headers.x-customer-id",
 *           skip: (req) => req.url === "/health",
 *         }),
 *       },
 *     ],
 *   })
 *   export class AppModule {}
 *
 * Each request runs inside a tracked task (`request.dexcostTask`), so
 * auto-instrumented LLM/HTTP calls in controllers/services are attributed
 * to it. The downstream chain is SUBSCRIBED inside the task's
 * AsyncLocalStorage scope (the nestjs-cls pattern) — returning
 * `next.handle()` directly would run the route handler outside the scope.
 *
 * Works on both HTTP platforms (Express and Fastify request shapes are
 * duck-typed). Non-HTTP contexts (RPC, WebSockets, GraphQL) pass through
 * untracked — wrap those boundaries with `wrapJobHandler`/`track` instead.
 */

import { createRequire } from "node:module";
import type { CostTracker, TrackedTask } from "../core/tracker.js";
import { runWithTask } from "../core/context.js";
import { scrubUrl } from "../security/redaction.js";
import { debugLog } from "../core/debug.js";
import { getNestedValue, resolveMiddlewareTracker } from "./shared.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Options for {@link DexcostInterceptor}. */
export interface NestInterceptorOptions {
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

/** Minimal structural rxjs surface the interceptor needs. */
interface RxLike {
  Observable: new (subscribe: (subscriber: any) => any) => any;
}

let _rx: RxLike | null | undefined;

/** Test-only: inject a fake rxjs implementation. */
export function _setRxForTests(rx: RxLike | null | undefined): void {
  _rx = rx;
}

const _require = createRequire(import.meta.url);

/**
 * Resolve rxjs from the host application. Cached; `null` means resolution
 * failed (surfaced loudly once — a Nest app without rxjs cannot exist, so
 * this indicates a broken install).
 */
function _resolveRx(): RxLike | null {
  if (_rx !== undefined) return _rx;
  try {
    _rx = _require("rxjs") as RxLike;
  } catch {
    _rx = null;
    // eslint-disable-next-line no-console
    console.warn(
      "[dexcost] DexcostInterceptor could not resolve 'rxjs' from the host " +
        "application — requests are passing through untracked. (rxjs ships " +
        "with every NestJS app; is this interceptor mounted outside Nest?)",
    );
  }
  return _rx;
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
 * NestJS interceptor (structurally implements `NestInterceptor`).
 *
 * Failure posture: dexcost failures degrade to pass-through (loud once);
 * handler errors are never swallowed — the task is marked "failed" and
 * the error continues to Nest's exception filters.
 */
export class DexcostInterceptor {
  private readonly _options: NestInterceptorOptions;

  constructor(options: NestInterceptorOptions = {}) {
    this._options = options;
  }

  intercept(context: any, next: { handle: () => any }): any {
    let tracked: TrackedTask | undefined;
    let response: any;
    try {
      // Only HTTP contexts carry a request we can attribute.
      if (typeof context?.switchToHttp !== "function") {
        return next.handle();
      }
      const http = context.switchToHttp();
      const request = http?.getRequest?.();
      response = http?.getResponse?.();
      if (!request || this._options.skip?.(request)) {
        return next.handle();
      }
      const tracker = resolveMiddlewareTracker("nestjs", this._options.tracker);
      const rx = _resolveRx();
      if (!tracker || !rx) {
        return next.handle();
      }

      const method = typeof request.method === "string" ? request.method : "UNKNOWN";
      // Express exposes originalUrl, Fastify exposes url.
      const rawUrl =
        typeof request.originalUrl === "string"
          ? request.originalUrl
          : typeof request.url === "string"
            ? request.url
            : "/";
      tracked = tracker.startTask({
        taskType: this._options.taskType?.(request) ?? `${method} ${scrubUrl(rawUrl)}`,
        customerId: this._options.customerIdFrom
          ? getNestedValue(request, this._options.customerIdFrom)
          : undefined,
        projectId: this._options.projectIdFrom
          ? getNestedValue(request, this._options.projectIdFrom)
          : undefined,
      });
      request.dexcostTask = tracked;

      const task = tracked;
      const { Observable } = rx;
      // Subscribe INSIDE the task's ALS scope so the controller/service
      // chain (which runs on subscription) inherits the task.
      return new Observable((subscriber: any) => {
        const subscription = runWithTask(task.task, () =>
          next.handle().subscribe({
            next: (value: unknown) => subscriber.next(value),
            error: (err: unknown) => {
              _endOnce(task, "failed");
              subscriber.error(err);
            },
            complete: () => {
              const statusCode =
                typeof response?.statusCode === "number"
                  ? response.statusCode
                  : // Fastify reply exposes statusCode too; raw fallback 200.
                    200;
              _endOnce(task, statusCode >= 400 ? "failed" : "success");
              subscriber.complete();
            },
          }),
        );
        return () => {
          // Unsubscription without completion (client disconnect, timeout
          // operator) — the task must not leak as pending.
          _endOnce(task, "success");
          subscription.unsubscribe();
        };
      });
    } catch (err) {
      // dexcost failure must never block the request pipeline.
      debugLog("nestjs", `interceptor degraded to pass-through: ${String(err)}`);
      if (tracked) _endOnce(tracked, "success");
      return next.handle();
    }
  }
}