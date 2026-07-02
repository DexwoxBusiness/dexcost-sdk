/**
 * Session-based auto-grouping.
 *
 * Groups LLM + HTTP calls into one task per execution context
 * without requiring explicit tracker.track() wrappers.
 */

import { randomUUID } from "node:crypto";
import type { Task } from "./models.js";
import { createTask } from "./models.js";
import { getCurrentTask, runWithTask, getContext } from "./context.js";
import {
  NetworkAccountant,
  registerAccountant,
} from "../adapters/network-accountant.js";
import { finalizeTaskNetwork } from "./network-finalize.js";
import type { EventBuffer } from "../transport/buffer.js";

// ---------------------------------------------------------------------------
// Session tracking
// ---------------------------------------------------------------------------

interface SessionInfo {
  task: Task;
  lastActivityAt: number;
}

/** Idle threshold in milliseconds (30 seconds). */
const IDLE_THRESHOLD_MS = 30_000;

/**
 * Grouping key for calls made with no ambient DexcostContext set.
 *
 * The Python SDK groups sessions by `threading.get_ident()` — one session
 * per thread, reused across calls until 30s idle. Node is single-threaded,
 * so the equivalent is one process-wide session. When the app DOES set an
 * ambient context (`setContext(...)` per request/job), the context object's
 * identity is the grouping key instead, giving per-request sessions.
 */
const GLOBAL_SESSION_KEY: unknown = Symbol("dexcost.global_session");

/**
 * SessionManager — creates and reuses session tasks automatically.
 *
 * When no explicit task is set via `tracker.track()`, the session manager
 * creates a session task keyed on the ambient DexcostContext (or a global
 * key when none is set) and REUSES it for subsequent calls until the
 * session goes idle. Pre-fix, sessions were keyed by their own taskId and
 * never re-bound, so every un-wrapped HTTP/LLM call produced a brand-new
 * single-call "agent_session" task — no grouping ever happened (the
 * attribution scatter seen as many isolated 1-call tasks).
 */
export class SessionManager {
  /** Grouping key (context object | GLOBAL_SESSION_KEY) → session. */
  private _sessions: Map<unknown, SessionInfo> = new Map();
  /** taskId → session, for activity updates and test lookups. */
  private _byTaskId: Map<string, SessionInfo> = new Map();

  /** Resolve the grouping key for the current async context. */
  private _sessionKey(): unknown {
    return getContext() ?? GLOBAL_SESSION_KEY;
  }

  /** Create a session task for the current ambient context and index it. */
  private _createSession(callType: string, buffer: EventBuffer, key: unknown): SessionInfo {
    const ctx = getContext();
    const taskType = ctx?.agent ?? "agent_session";

    const task = createTask({
      taskId: randomUUID(),
      taskType,
      customerId: ctx?.customerId,
      projectId: ctx?.projectId,
      metadata: { session: true, initiatedBy: callType },
    });

    // Register a NetworkAccountant so the patched fetch attributes the
    // session's bytes to this task; drained + egress-priced when the
    // session is finalized (idle sweep or shutdown).
    registerAccountant(task.taskId, new NetworkAccountant());

    buffer.upsertTask(task);

    const info: SessionInfo = { task, lastActivityAt: Date.now() };
    this._sessions.set(key, info);
    this._byTaskId.set(task.taskId, info);
    return info;
  }

  /**
   * Get the currently-active task, or create a new session task if none exists.
   *
   * If a task is already active in the AsyncLocalStorage context (from an
   * explicit `tracker.track()` call), it is returned as-is. Otherwise the
   * session for the current grouping key is reused (creating it on first
   * call).
   *
   * @param callType  Label for the type of call (e.g. "http", "llm")
   * @param buffer    EventBuffer to persist the new task
   * @returns The active or newly-created Task
   */
  getOrCreateSession(callType: string, buffer: EventBuffer): Task {
    const existing = getCurrentTask();
    if (existing !== undefined) {
      // Update last activity for existing session if we are tracking it
      const sessionInfo = this._byTaskId.get(existing.taskId);
      if (sessionInfo) {
        sessionInfo.lastActivityAt = Date.now();
      }
      return existing;
    }

    const key = this._sessionKey();
    const info = this._sessions.get(key);
    if (info) {
      info.lastActivityAt = Date.now();
      return info.task;
    }

    // NOTE: intentionally NOT calling setCurrentTask(task) here.
    // enterWith() leaks the task into the remainder of the async chain,
    // causing subsequent unwrapped calls to inherit a stale session task.
    // Callers should use runInSession() (which scopes via runWithTask);
    // cross-call reuse instead comes from the grouping-key map above.
    return this._createSession(callType, buffer, key).task;
  }

  /**
   * Run a function within a session context.
   *
   * Creates (or reuses) a session task if none is active, then runs `fn`
   * within that task's AsyncLocalStorage context.
   */
  runInSession<T>(callType: string, buffer: EventBuffer, fn: () => T): T {
    const existing = getCurrentTask();
    if (existing !== undefined) {
      const sessionInfo = this._byTaskId.get(existing.taskId);
      if (sessionInfo) {
        sessionInfo.lastActivityAt = Date.now();
      }
      return fn();
    }

    const key = this._sessionKey();
    let info = this._sessions.get(key);
    if (info) {
      info.lastActivityAt = Date.now();
    } else {
      info = this._createSession(callType, buffer, key);
    }

    return runWithTask(info.task, fn);
  }

  /**
   * Finalize one session: drain its NetworkAccountant into byte aggregates
   * + egress dollars, mark it terminal, and persist.
   */
  private _finalizeSession(info: SessionInfo, buffer?: EventBuffer): void {
    if (info.task.status === "pending") {
      info.task.status = "success";
      info.task.endedAt = new Date();
    }
    try {
      // Drain regardless of prior status so the accountant registry entry
      // never leaks; stamps networkCostUsd for the pending→success case.
      finalizeTaskNetwork(info.task, buffer);
    } catch {
      // Fail-silent: pricing problems must never break the sweep.
    }
    if (buffer) {
      buffer.upsertTask(info.task);
    }
  }

  /**
   * Finalize sessions that have been idle for longer than 30 seconds.
   *
   * Sets their status to "success", drains network bytes into egress cost,
   * and stamps endedAt. Removes them from the active session map. When a
   * `buffer` is provided, re-persists each finalized task so the updated
   * status reaches the push cycle.
   */
  finalizeIdleSessions(buffer?: EventBuffer): void {
    const now = Date.now();
    const toRemove: unknown[] = [];

    for (const [key, session] of this._sessions) {
      if (now - session.lastActivityAt > IDLE_THRESHOLD_MS) {
        this._finalizeSession(session, buffer);
        toRemove.push(key);
      }
    }

    for (const key of toRemove) {
      const info = this._sessions.get(key);
      this._sessions.delete(key);
      if (info) this._byTaskId.delete(info.task.taskId);
    }
  }

  /**
   * Finalize ALL active sessions regardless of idle time.
   *
   * Called during shutdown (`close` / `closeAsync`) to ensure no session
   * tasks are left in "pending" status.
   */
  finalizeAllSessions(buffer?: EventBuffer): void {
    for (const [, session] of this._sessions) {
      this._finalizeSession(session, buffer);
    }
    this._sessions.clear();
    this._byTaskId.clear();
  }

  /** Number of active sessions. */
  get activeSessionCount(): number {
    return this._sessions.size;
  }

  /** Get a session's task by ID (for testing). */
  getSession(taskId: string): Task | undefined {
    return this._byTaskId.get(taskId)?.task;
  }
}
