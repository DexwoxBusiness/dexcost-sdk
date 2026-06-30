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
  return task;
}
