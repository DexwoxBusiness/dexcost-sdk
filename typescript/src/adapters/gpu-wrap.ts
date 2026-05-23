/**
 * Serverless GPU handler wraps — Modal / RunPod / Replicate.
 *
 * Phase 2 GPU foundation Task 7. Mirrors python/src/dexcost/gpu_wrap.py +
 * compute-wrap.ts's shape.
 *
 * Each wrap is a thin decorator that:
 *   1. Creates a GpuAccountant and attaches it to the active task as `_gpu`.
 *   2. Times the handler with performance.now().
 *   3. Calls accountant.snapshotEndAndBuild(durationMs) at exit.
 *   4. Persists the dual events (1 gpu_cost with cost_pending=true + N
 *      gpu_utilization_signal) via the global tracker's buffer.
 *   5. Handler exceptions are re-raised AFTER events are persisted (the
 *      GPU-seconds were consumed and Modal/RunPod/Replicate bill failed
 *      invocations identically to successful ones — capture spec §6 case 7).
 *
 * When no dexcost task is in context the wrap is transparent (capture spec
 * §6 case 2 — anonymous compute never creates orphan events).
 *
 * Browser-safe: short-circuits the accountant work off-Node.
 */

import { randomUUID } from "node:crypto";
import { GpuAccountant, type GpuAccountantHooks } from "../core/gpu-accountant.js";
import { GpuRuntimeKind } from "../core/gpu-runtime.js";
import { getCurrentTask } from "../core/context.js";
import { getCloudEnv } from "../cloud-detect.js";
import { getTracker } from "../core/tracker.js";
import { createCostEvent } from "../core/models.js";
import type { Task } from "../core/models.js";

function _isNode(): boolean {
  return typeof process !== "undefined" && !!process.versions?.node;
}

// ─── Test hook: override the GpuAccountant hooks injected by the wrap ───────

let _hooksOverride: Partial<GpuAccountantHooks> | null = null;

/**
 * Test-only — pre-set the hooks every wrap-created GpuAccountant should
 * receive (used to stub NVML / cgroup primitives). Pass null to restore.
 */
export function _setGpuAccountantHooksForTests(
  hooks: Partial<GpuAccountantHooks> | null,
): void {
  _hooksOverride = hooks;
}

// ─── Persist helpers ────────────────────────────────────────────────────────

function _persistGpuEvents(
  task: Task,
  costDetails: Record<string, any> | null,
  signalEvents: Array<Record<string, any>> | null,
): void {
  let tracker;
  try {
    tracker = getTracker();
  } catch {
    return; // No global tracker → nothing to persist
  }
  try {
    if (costDetails !== null) {
      const ev = createCostEvent({
        eventId: randomUUID(),
        taskId: task.taskId,
        eventType: "gpu_cost",
        costUsd: 0, // back-filled at task finalize
        costConfidence: "unknown",
        isRetry: false,
        details: costDetails,
      });
      tracker.buffer.addEvent(ev);
    }
    if (signalEvents) {
      for (const sig of signalEvents) {
        const ev = createCostEvent({
          eventId: randomUUID(),
          taskId: task.taskId,
          eventType: "gpu_utilization_signal",
          costUsd: 0, // Decision #3 observability-only
          costConfidence: "unknown",
          isRetry: false,
          details: sig,
        });
        tracker.buffer.addEvent(ev);
      }
    }
  } catch {
    // Fail-silent per convention §9.
    // eslint-disable-next-line no-console
    console.warn("gpu_wrap failed to persist events");
  }
}

async function _timeAndCapture<R>(
  accountant: GpuAccountant,
  handler: () => Promise<R> | R,
): Promise<R> {
  accountant.snapshotStart();
  const t0 = performance.now();
  try {
    return await handler();
  } finally {
    const durationMs = Math.trunc(performance.now() - t0);
    try {
      const { costDetails, signalEvents } = accountant.snapshotEndAndBuild(durationMs);
      const task = getCurrentTask();
      if (task !== undefined) {
        _persistGpuEvents(task, costDetails, signalEvents);
      }
    } catch {
      // eslint-disable-next-line no-console
      console.warn("gpu_wrap event-build failed");
    }
  }
}

function _makeAccountant(runtime: GpuRuntimeKind): GpuAccountant {
  return new GpuAccountant(
    runtime,
    getCloudEnv(),
    _hooksOverride ?? undefined,
  );
}

// ─── Modal ──────────────────────────────────────────────────────────────────

export function wrapModalHandler<A extends any[], R>(
  handler: (...args: A) => Promise<R> | R,
): (...args: A) => Promise<R> {
  return async (...args: A): Promise<R> => {
    const task = getCurrentTask();
    if (!task) return await handler(...args);
    if (!_isNode()) return await handler(...args);
    const accountant = _makeAccountant(GpuRuntimeKind.Modal);
    (task as any)._gpu = accountant;
    return await _timeAndCapture(accountant, () => handler(...args));
  };
}

// ─── RunPod ─────────────────────────────────────────────────────────────────

export function wrapRunpodHandler<A extends any[], R>(
  handler: (...args: A) => Promise<R> | R,
): (...args: A) => Promise<R> {
  return async (...args: A): Promise<R> => {
    const task = getCurrentTask();
    if (!task) return await handler(...args);
    if (!_isNode()) return await handler(...args);
    const accountant = _makeAccountant(GpuRuntimeKind.RunPod);
    (task as any)._gpu = accountant;
    return await _timeAndCapture(accountant, () => handler(...args));
  };
}

// ─── Replicate ──────────────────────────────────────────────────────────────

export function wrapReplicateHandler<A extends any[], R>(
  handler: (...args: A) => Promise<R> | R,
): (...args: A) => Promise<R> {
  return async (...args: A): Promise<R> => {
    const task = getCurrentTask();
    if (!task) return await handler(...args);
    if (!_isNode()) return await handler(...args);
    const accountant = _makeAccountant(GpuRuntimeKind.Replicate);
    (task as any)._gpu = accountant;
    return await _timeAndCapture(accountant, () => handler(...args));
  };
}
