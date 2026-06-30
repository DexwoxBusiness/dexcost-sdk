/**
 * Session-based auto-grouping.
 *
 * Groups LLM + HTTP calls into one task per execution context
 * without requiring explicit tracker.track() wrappers.
 */

import { randomUUID } from "node:crypto";
import type { Task } from "./models.js";
import { createTask } from "./models.js";
import { getCurrentTask, runWithTask, setCurrentTask, getContext } from "./context.js";
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
 * SessionManager — creates and reuses session tasks automatically.
 *
 * When no explicit task is set via `tracker.track()`, the session manager
 * creates a session task and binds it to the current AsyncLocalStorage context.
 * Subsequent calls in the same context reuse the same session task.
 */
export class SessionManager {
  private _sessions: Map<string, SessionInfo> = new Map();

  /**
   * Get the currently-active task, or create a new session task if none exists.
   *
   * If a task is already active in the AsyncLocalStorage context (from an
   * explicit `tracker.track()` call), it is returned as-is.
   *
   * @param callType  Label for the type of call (e.g. "http", "llm")
   * @param buffer    EventBuffer to persist the new task
   * @returns The active or newly-created Task
   */
  getOrCreateSession(callType: string, buffer: EventBuffer): Task {
    const existing = getCurrentTask();
    if (existing !== undefined) {
      // Update last activity for existing session if we are tracking it
      const sessionInfo = this._sessions.get(existing.taskId);
      if (sessionInfo) {
        sessionInfo.lastActivityAt = Date.now();
      }
      return existing;
    }

    // Create a new session task
    const ctx = getContext();
    const taskType = ctx?.agent ?? "agent_session";

    const task = createTask({
      taskId: randomUUID(),
      taskType,
      customerId: ctx?.customerId,
      projectId: ctx?.projectId,
      metadata: { session: true, initiatedBy: callType },
    });

    buffer.upsertTask(task);

    this._sessions.set(task.taskId, {
      task,
      lastActivityAt: Date.now(),
    });

    // Bind the task to the current async context so subsequent calls
    // to getCurrentTask() within this async chain return this task.
    setCurrentTask(task);

    return task;
  }

  /**
   * Run a function within a session context.
   *
   * Creates a session task if none is active, then runs `fn` within that
   * task's AsyncLocalStorage context.
   */
  runInSession<T>(callType: string, buffer: EventBuffer, fn: () => T): T {
    const existing = getCurrentTask();
    if (existing !== undefined) {
      const sessionInfo = this._sessions.get(existing.taskId);
      if (sessionInfo) {
        sessionInfo.lastActivityAt = Date.now();
      }
      return fn();
    }

    const ctx = getContext();
    const taskType = ctx?.agent ?? "agent_session";

    const task = createTask({
      taskId: randomUUID(),
      taskType,
      customerId: ctx?.customerId,
      projectId: ctx?.projectId,
      metadata: { session: true, initiatedBy: callType },
    });

    buffer.upsertTask(task);

    this._sessions.set(task.taskId, {
      task,
      lastActivityAt: Date.now(),
    });

    return runWithTask(task, fn);
  }

  /**
   * Finalize sessions that have been idle for longer than 30 seconds.
   *
   * Sets their status to "success" and endedAt timestamp. Removes them
   * from the active session map. When a `buffer` is provided, re-persists
   * each finalized task so the updated status reaches the push cycle.
   */
  finalizeIdleSessions(buffer?: EventBuffer): void {
    const now = Date.now();
    const toRemove: string[] = [];

    for (const [taskId, session] of this._sessions) {
      if (now - session.lastActivityAt > IDLE_THRESHOLD_MS) {
        if (session.task.status === "pending") {
          session.task.status = "success";
          session.task.endedAt = new Date();
          if (buffer) {
            buffer.upsertTask(session.task);
          }
        }
        toRemove.push(taskId);
      }
    }

    for (const taskId of toRemove) {
      this._sessions.delete(taskId);
    }
  }

  /** Number of active sessions. */
  get activeSessionCount(): number {
    return this._sessions.size;
  }

  /** Get a session's task by ID (for testing). */
  getSession(taskId: string): Task | undefined {
    return this._sessions.get(taskId)?.task;
  }
}
