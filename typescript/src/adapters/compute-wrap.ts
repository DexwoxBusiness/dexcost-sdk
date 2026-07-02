/**
 * Serverless handler wraps for compute capture (node-only, browser-safe).
 *
 * Each wrap is a thin decorator that:
 *   1. Reads runtime-specific env vars (memory limit, init type, region).
 *   2. Creates a ComputeAccountant and attaches it to the active task.
 *   3. Times the handler with performance.now().
 *   4. Reads cgroup memory.peak at exit.
 *   5. Builds the per-invocation compute_cost event with cost_pending=true
 *      and persists it via the global tracker's buffer.
 *   6. TrackedTask.end()'s _finalizeCompute back-fills cost_usd at finalize.
 *
 * When no dexcost task is in context the wrap is a transparent pass-through —
 * anonymous compute never creates orphan events (capture spec §6 case 2).
 *
 * Mirrors python/src/dexcost/compute_wrap.py.
 */

import { randomUUID } from "node:crypto";
import { ComputeAccountant } from "../core/compute-accountant.js";
import { RuntimeKind } from "../core/compute-runtime.js";
import { getCurrentTask } from "../core/context.js";
import { getCloudEnv } from "../cloud-detect.js";
import { readMemoryPeak } from "../core/cgroup-reader.js";
import { getTracker, flushBeforeFreeze } from "../core/tracker.js";
import { createCostEvent } from "../core/models.js";
import type { Task } from "../core/models.js";

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

/** Insert the compute_cost event with cost_pending=true via the global tracker. */
function _persistComputeEvent(task: Task, details: Record<string, any>): void {
  let tracker;
  try {
    tracker = getTracker();
  } catch {
    // No global tracker → nothing to persist into.
    return;
  }
  const ev = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
    eventType: "compute_cost",
    costUsd: 0,
    costConfidence: "unknown",
    isRetry: false,
    details,
  });
  try {
    tracker.buffer.addEvent(ev);
  } catch {
    // Fail-silent per convention §9.
    // eslint-disable-next-line no-console
    console.warn("compute_wrap failed to persist event");
  }
}

/**
 * Run `handler` while measuring duration_ms and peak memory; persist a
 * serverless compute_cost event on exit. Exceptions from the handler are
 * re-raised after the event is persisted (the cost is still incurred —
 * capture spec §6 case 7).
 */
async function _timeAndCapture<R>(
  accountant: ComputeAccountant,
  handler: () => Promise<R> | R,
): Promise<R> {
  const t0 = performance.now();
  try {
    return await handler();
  } finally {
    const durationMs = Math.trunc(performance.now() - t0);
    const peak = readMemoryPeak() ?? 0;
    try {
      const details = accountant.buildServerlessEvent(durationMs, peak);
      const task = getCurrentTask();
      if (details !== null && task !== undefined) {
        _persistComputeEvent(task, details);
      }
    } catch {
      // eslint-disable-next-line no-console
      console.warn("compute_wrap event-build failed");
    }
    // Freeze-prone platforms give no background CPU after return — the
    // pusher interval may never fire. Push whatever is buffered now,
    // bounded so a slow endpoint can't hang the handler (and never
    // throwing over the handler's own result/error).
    await flushBeforeFreeze();
  }
}

// ─── Lambda ──────────────────────────────────────────────────────────────────

/**
 * Wrap an AWS Lambda handler — emits one compute_cost event per invocation
 * with the env-declared memory limit, the wall-clock duration, the
 * architecture detected at runtime, and the initialization type.
 */
export function wrapLambdaHandler<E, C, R>(
  handler: (event: E, context: C) => Promise<R> | R,
): (event: E, context: C) => Promise<R> {
  return async (event: E, context: C): Promise<R> => {
    const task = getCurrentTask();
    if (!task) {
      // No active dexcost task → pass-through (capture spec §6 case 2).
      return await handler(event, context);
    }
    if (!_isNode()) {
      return await handler(event, context);
    }
    let memMb = 128;
    const raw = process.env.AWS_LAMBDA_FUNCTION_MEMORY_SIZE;
    if (raw !== undefined) {
      const parsed = Number(raw);
      if (Number.isFinite(parsed) && parsed > 0) memMb = Math.trunc(parsed);
    }
    const initType = process.env.AWS_LAMBDA_INITIALIZATION_TYPE || "on-demand";
    const region = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || undefined;
    const accountant = new ComputeAccountant({
      runtime: RuntimeKind.Lambda,
      lambdaMemoryMb: memMb,
      initializationType: initType,
      region,
    });
    (task as any)._compute = accountant;
    return await _timeAndCapture(accountant, () => handler(event, context));
  };
}

// ─── Cloud Run ───────────────────────────────────────────────────────────────

/**
 * Wrap a Cloud Run HTTP handler. Default billing model is request-based
 * (estimated confidence); override via init(computeBillingOverrides=
 * {cloud_run: 'instance'}) for instance-based billing customers.
 */
export function wrapCloudRunHandler<A extends any[], R>(
  handler: (...args: A) => Promise<R> | R,
): (...args: A) => Promise<R> {
  return async (...args: A): Promise<R> => {
    const task = getCurrentTask();
    if (!task) return await handler(...args);
    if (!_isNode()) return await handler(...args);
    const accountant = new ComputeAccountant({
      runtime: RuntimeKind.CloudRun,
      region: getCloudEnv().region ?? undefined,
    });
    (task as any)._compute = accountant;
    return await _timeAndCapture(accountant, () => handler(...args));
  };
}

// ─── Cloud Functions Gen2 ────────────────────────────────────────────────────

export function wrapCloudFunctionsHandler<A extends any[], R>(
  handler: (...args: A) => Promise<R> | R,
): (...args: A) => Promise<R> {
  return async (...args: A): Promise<R> => {
    const task = getCurrentTask();
    if (!task) return await handler(...args);
    if (!_isNode()) return await handler(...args);
    const accountant = new ComputeAccountant({
      runtime: RuntimeKind.CloudFunctions,
      region: getCloudEnv().region ?? undefined,
    });
    (task as any)._compute = accountant;
    return await _timeAndCapture(accountant, () => handler(...args));
  };
}

// ─── Azure Functions ─────────────────────────────────────────────────────────

export function wrapAzureFunctionsHandler<A extends any[], R>(
  handler: (...args: A) => Promise<R> | R,
): (...args: A) => Promise<R> {
  return async (...args: A): Promise<R> => {
    const task = getCurrentTask();
    if (!task) return await handler(...args);
    if (!_isNode()) return await handler(...args);
    const accountant = new ComputeAccountant({
      runtime: RuntimeKind.AzureFunctions,
      region: process.env.REGION_NAME || undefined,
    });
    (task as any)._compute = accountant;
    return await _timeAndCapture(accountant, () => handler(...args));
  };
}

// ─── Vercel Fluid ────────────────────────────────────────────────────────────

export function wrapVercelHandler<A extends any[], R>(
  handler: (...args: A) => Promise<R> | R,
): (...args: A) => Promise<R> {
  return async (...args: A): Promise<R> => {
    const task = getCurrentTask();
    if (!task) return await handler(...args);
    if (!_isNode()) return await handler(...args);
    const accountant = new ComputeAccountant({
      runtime: RuntimeKind.Vercel,
      region: process.env.VERCEL_REGION || undefined,
    });
    (task as any)._compute = accountant;
    return await _timeAndCapture(accountant, () => handler(...args));
  };
}
