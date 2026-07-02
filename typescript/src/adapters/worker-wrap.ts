/**
 * Queue-worker wrapper — one tracked task per consumed job.
 *
 * Queue consumers (BullMQ, RabbitMQ, SQS, Kafka, cron ticks) are where
 * agent workloads actually run, and the classic integration mistake is
 * ambient-only capture there: consecutive jobs on the same async chain
 * blur together and attribution is lost. `wrapJobHandler` formalizes the
 * boundary: each invocation runs inside its own explicit tracked task with
 * per-job attribution extracted from the job payload.
 *
 *   // BullMQ
 *   new Worker("reviews", wrapJobHandler(
 *     async (job) => runReview(job.data),
 *     {
 *       taskType: "code_review",
 *       customerId: (job) => job.data.orgId,
 *       metadata: (job) => ({ pr_number: job.data.prNumber }),
 *     },
 *   ));
 *
 * Failure posture: dexcost failures (uninitialized SDK, extractor throws)
 * degrade to running the handler untracked — loudly, once. Handler errors
 * are NEVER swallowed: the task is marked "failed" and the error
 * propagates so the queue's retry semantics stay intact.
 */

import type { CostTracker } from "../core/tracker.js";
import { resolveMiddlewareTracker } from "../middleware/shared.js";
import { debugLog } from "../core/debug.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Options for {@link wrapJobHandler}. `TArgs` are the handler's arguments
 *  (extractors receive them all, so multi-arg consumers like RabbitMQ's
 *  `(msg, channel)` work unchanged). */
export interface WrapJobHandlerOptions<TArgs extends any[]> {
  /** Tracker to record into. Defaults to the `init()` singleton (lazy). */
  tracker?: CostTracker;
  /** Task type: a fixed string or derived per job. Defaults to the
   *  handler's function name, else "job". */
  taskType?: string | ((...args: TArgs) => string);
  /** Extract a customer ID from the job. */
  customerId?: (...args: TArgs) => string | undefined;
  /** Extract a project ID from the job. */
  projectId?: (...args: TArgs) => string | undefined;
  /** Extract metadata from the job (stamped on the task). */
  metadata?: (...args: TArgs) => Record<string, unknown> | undefined;
}

/**
 * Wrap a queue-consumer handler so every job runs inside its own tracked
 * task. All LLM/HTTP calls made while processing the job are attributed
 * to it automatically (AsyncLocalStorage scope).
 */
export function wrapJobHandler<TArgs extends any[], R>(
  handler: (...args: TArgs) => Promise<R> | R,
  options: WrapJobHandlerOptions<TArgs> = {},
): (...args: TArgs) => Promise<R> {
  return async (...args: TArgs): Promise<R> => {
    const tracker = resolveMiddlewareTracker("worker", options.tracker);
    if (!tracker) {
      return handler(...args);
    }

    // Extractors run user code over an arbitrary payload — a throw there
    // must degrade attribution, not kill the job.
    let taskType = "job";
    let customerId: string | undefined;
    let projectId: string | undefined;
    let metadata: Record<string, unknown> | undefined;
    try {
      taskType =
        typeof options.taskType === "function"
          ? options.taskType(...args)
          : options.taskType ?? (handler.name || "job");
      customerId = options.customerId?.(...args);
      projectId = options.projectId?.(...args);
      metadata = options.metadata?.(...args);
    } catch (err) {
      debugLog("worker", `attribution extractor threw (job tracked without it): ${String(err)}`);
    }

    // tracker.track marks the task failed and rethrows on handler error,
    // preserving the queue's retry semantics.
    return tracker.track(
      { taskType, customerId, projectId, metadata },
      () => Promise.resolve(handler(...args)),
    );
  };
}
