/**
 * Auto-task creation for auto-instrumented calls without an explicit task.
 *
 * Mirrors the Python SDK's `auto_task.py`. When an LLM/HTTP call happens
 * outside an explicit `track()` task, an auto-task is created so the cost
 * is never silently lost. Attribution (customerId/projectId/metadata) is
 * pulled from the ambient `DexcostContext` when available.
 */

import { randomUUID } from "node:crypto";
import { createTask } from "./models.js";
import type { Task } from "./models.js";
import { getCurrentTask, getContext } from "./context.js";
import {
  NetworkAccountant,
  registerAccountant,
} from "../adapters/network-accountant.js";
import { finalizeTaskNetwork } from "./network-finalize.js";
import type { EventBuffer } from "../transport/buffer.js";

/** Return true if there is no active explicit task. */
export function needsAutoTask(): boolean {
  return getCurrentTask() === undefined;
}

/**
 * Create an auto-task with attribution from the current DexcostContext.
 *
 * Reads `customerId`, `projectId`, `metadata`, and `agent` from the
 * ambient context (set via `setContext`). When `agent` is set in the
 * context it overrides the provided `taskType`.
 *
 * The returned task is NOT bound to AsyncLocalStorage. Callers must
 * either pass it explicitly or scope it with `runWithTask()`. The
 * previous design used `setCurrentTask(task)` (enterWith), which
 * leaked the completed task into subsequent calls in the same async
 * chain — a second unwrapped LLM call would inherit the stale task
 * instead of creating its own auto-task.
 */
export function createAutoTask(taskType: string): Task {
  const ctx = getContext();
  const effectiveTaskType = ctx?.agent ? ctx.agent : taskType;
  const task = createTask({
    taskId: randomUUID(),
    taskType: effectiveTaskType,
    customerId: ctx?.customerId,
    projectId: ctx?.projectId,
    metadata: ctx?.metadata ? { ...ctx.metadata } : {},
  });
  // Register a NetworkAccountant so the patched fetch attributes the
  // call's bytes to this task (same as TrackedTask does for explicit
  // tasks). Drained + priced by finalizeAutoTask — every creator of an
  // auto-task MUST finalize it through that helper or the accountant
  // registry entry leaks.
  registerAccountant(task.taskId, new NetworkAccountant());
  return task;
}

/**
 * Finalize an auto-task: set terminal status, drain its NetworkAccountant
 * into byte aggregates + egress dollars, and persist.
 *
 * Counterpart of Python's `finalize_auto_task`, extended with the network
 * finalize step so auto-tasks get the same egress pricing as explicit
 * `tracker.track()` tasks (whose TrackedTask.end() runs the same shared
 * path).
 *
 * Idempotent enough for the instrument call sites: the accountant drain
 * unregisters on first call, and later calls only re-stamp status.
 */
export function finalizeAutoTask(
  task: Task,
  status: "success" | "failed",
  buffer?: EventBuffer | null,
): void {
  task.status = status;
  task.endedAt = new Date();
  if (status === "failed") {
    task.failureCount += 1;
  }
  try {
    finalizeTaskNetwork(task, buffer ?? undefined);
  } catch {
    // Tier-5 fail-silent: a pricing/catalog bug must never break user
    // code paths that finalize auto-tasks (instrument hot paths).
  }
  buffer?.upsertTask(task);
}
