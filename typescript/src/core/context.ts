/**
 * Task context propagation using AsyncLocalStorage.
 *
 * Allows automatic association of cost events with the currently-active
 * task without explicit parameter passing.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import type { Task } from "./models.js";

const taskStore = new AsyncLocalStorage<Task>();

/**
 * Return the currently-active Task, or undefined if no task context is set.
 */
export function getCurrentTask(): Task | undefined {
  return taskStore.getStore();
}

/**
 * Execute `fn` with `task` as the current task context.
 *
 * Any code running inside `fn` (including async continuations) can call
 * `getCurrentTask()` to retrieve the task.
 */
export function runWithTask<T>(task: Task, fn: () => T): T {
  return taskStore.run(task, fn);
}

/**
 * Set the current task for the remaining async execution context.
 *
 * Uses `AsyncLocalStorage.enterWith()` (Node 18+) so the task is visible
 * to all subsequent code in the current async chain without wrapping in
 * a callback.
 */
export function setCurrentTask(task: Task): void {
  taskStore.enterWith(task);
}

// ---------------------------------------------------------------------------
// DexcostContext — dynamic customer/project attribution
// ---------------------------------------------------------------------------

/**
 * Ambient context for automatic cost attribution.
 *
 * Set once at request/job start (e.g., in Express middleware) and any
 * auto-instrumented LLM call without an explicit task will inherit
 * customerId, projectId, and metadata from this context.
 */
export interface DexcostContext {
  customerId?: string;
  projectId?: string;
  metadata?: Record<string, unknown>;
  agent?: string;
}

const contextStore = new AsyncLocalStorage<DexcostContext>();

/**
 * Set the ambient DexcostContext for the current async execution context.
 *
 * Uses `AsyncLocalStorage.enterWith()` (Node 18+) so the context is visible
 * to all code in the remaining async chain without wrapping in a callback.
 *
 * For concurrent request isolation (web servers), call this inside a
 * per-request middleware or use `runWithTask()` which isolates its own
 * async context.
 */
export function setContext(ctx: {
  customerId?: string;
  projectId?: string;
  metadata?: Record<string, unknown>;
  agent?: string;
}): void {
  contextStore.enterWith({
    customerId: ctx.customerId,
    projectId: ctx.projectId,
    metadata: ctx.metadata ?? {},
    agent: ctx.agent,
  });
}

/**
 * Return the current DexcostContext, or undefined if none is set.
 */
export function getContext(): DexcostContext | undefined {
  return contextStore.getStore();
}

/**
 * Run `fn` with `ctx` as the ambient DexcostContext, scoped to the call.
 *
 * Prefer this over `setContext()` in queue consumers, cron jobs, and any
 * long-lived worker loop: `setContext()` uses `enterWith()`, which binds the
 * context to the REMAINDER of the current async chain — consecutive jobs
 * processed on the same chain silently inherit the previous job's
 * customer/project attribution. `runWithContext()` scopes the context to
 * `fn` exactly (including all async continuations started inside it) and
 * restores the outer context afterwards.
 *
 * The context object also serves as the session grouping key, so each
 * `runWithContext()` scope gets its own ambient session task.
 */
export function runWithContext<T>(
  ctx: {
    customerId?: string;
    projectId?: string;
    metadata?: Record<string, unknown>;
    agent?: string;
  },
  fn: () => T,
): T {
  return contextStore.run(
    {
      customerId: ctx.customerId,
      projectId: ctx.projectId,
      metadata: ctx.metadata ?? {},
      agent: ctx.agent,
    },
    fn,
  );
}

/**
 * Clear the ambient DexcostContext for the current async execution context.
 */
export function clearContext(): void {
  contextStore.enterWith({});
}

// ---------------------------------------------------------------------------
// Per-call network-event suppression flag (v1 §5.3 invariant)
// ---------------------------------------------------------------------------
//
// When set, the HTTP adapter still records bytes into the NetworkAccountant
// but does NOT emit a standalone `network` event. LLM instruments wrap
// their outbound fetch call inside suppressNetworkEvent(...) so each call
// produces at most one of {llm_call, external_cost, network} — the v1
// §5.3 "≤ 1 event per HTTP call" invariant.

const suppressStore = new AsyncLocalStorage<boolean>();

/**
 * Returns `true` when the current async scope has suppressed `network`
 * event emission. Bytes are still counted into the accountant; only the
 * standalone event is withheld.
 */
export function isNetworkEventSuppressed(): boolean {
  return suppressStore.getStore() === true;
}

/**
 * Run `fn` (sync or async) inside a scope where `network`-event emission
 * is suppressed. Used by LLM instruments around their fetch call so the
 * call doesn't double-emit (llm_call + network).
 */
export function suppressNetworkEvent<T>(fn: () => T): T {
  return suppressStore.run(true, fn);
}
