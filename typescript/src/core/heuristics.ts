/**
 * Retry Heuristic Engine for the dexcost TypeScript SDK.
 *
 * Analyses a sliding window of recent events per task and auto-detects
 * probable retries by identifying LLM calls that follow a failed call
 * (transient error) to the same model within the configured time window.
 */

import type { CostEvent } from "./models.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Error types that indicate a transient failure likely to be retried. */
export const TRANSIENT_ERRORS = new Set([
  "rate_limit",
  "timeout",
  "5xx",
  "server_error",
  "connection_error",
]);

/**
 * Base likelihood (0–1) that each transient error type leads to a retry.
 * Used as the starting multiplier before time-decay is applied.
 */
export const ERROR_LIKELIHOODS: Record<string, number> = {
  rate_limit: 1.0,
  timeout: 0.9,
  "5xx": 0.85,
  server_error: 0.85,
  connection_error: 0.8,
};

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

/** Result returned by `RetryHeuristicEngine.check()`. */
export interface HeuristicMatch {
  /** Whether the event is likely a retry. */
  isRetry: boolean;
  /** Confidence score from 0.0 to 1.0. */
  confidence: number;
  /** Event ID of the failed predecessor, if matched. */
  matchedEventId: string | undefined;
  /** "heuristic" when a match is found, "" otherwise. */
  reason: string;
}

// ---------------------------------------------------------------------------
// Engine
// ---------------------------------------------------------------------------

/** Canonical no-match result. */
const NO_MATCH: HeuristicMatch = {
  isRetry: false,
  confidence: 0,
  matchedEventId: undefined,
  reason: "",
};

/**
 * Sliding-window heuristic engine that detects probable retry events.
 *
 * Call `record(event)` for every event you process.
 * Call `check(event)` *before* recording to determine whether the
 * incoming event looks like a retry of a recently failed call.
 */
export class RetryHeuristicEngine {
  private readonly _windowSeconds: number;
  private readonly _threshold: number;

  /** Map from taskId → ordered list of recorded events. */
  private readonly _windows: Map<string, CostEvent[]> = new Map();

  /**
   * @param windowSeconds - How far back (in seconds) to look for a matching
   *   failed predecessor. Defaults to 30.
   * @param threshold - Minimum confidence score required to flag an event as
   *   a retry. Defaults to 0.8.
   */
  constructor(windowSeconds: number = 30, threshold: number = 0.8) {
    if (windowSeconds <= 0) {
      throw new Error(`windowSeconds must be positive, got ${windowSeconds}`);
    }
    if (threshold <= 0 || threshold > 1) {
      throw new Error(`threshold must be in (0, 1], got ${threshold}`);
    }
    this._windowSeconds = windowSeconds;
    this._threshold = threshold;
  }

  /** The configured sliding-window size in seconds. */
  get windowSeconds(): number {
    return this._windowSeconds;
  }

  /** The minimum confidence threshold for flagging a retry. */
  get threshold(): number {
    return this._threshold;
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /**
   * Record an event into the sliding window for its task.
   *
   * Events older than `windowSeconds` relative to the new event are pruned
   * before appending.
   */
  record(event: CostEvent): void {
    const list = this._getOrCreate(event.taskId);
    this._prune(list, event.occurredAt);
    if (list.length === 0) {
      this._windows.delete(event.taskId);
      // Re-create the entry for the incoming event
      const fresh: CostEvent[] = [event];
      this._windows.set(event.taskId, fresh);
    } else {
      list.push(event);
    }
  }

  /**
   * Check whether an incoming event looks like a retry.
   *
   * Walks the recorded events for the same task backwards (most-recent
   * first) looking for an `llm_call` event with the same model that ended
   * with a transient error within the time window.
   *
   * Does NOT modify the window — call `record()` separately.
   */
  check(event: CostEvent): HeuristicMatch {
    const list = this._windows.get(event.taskId);
    if (!list || list.length === 0) {
      return NO_MATCH;
    }

    const eventMs = event.occurredAt.getTime();

    // Walk backwards through the window list (most-recent first)
    for (let i = list.length - 1; i >= 0; i--) {
      const candidate = list[i];

      // Skip self (same eventId)
      if (candidate.eventId === event.eventId) {
        continue;
      }

      // Only consider llm_call events
      if (candidate.eventType !== "llm_call") {
        continue;
      }

      // Only consider the same model
      if (candidate.model !== event.model) {
        continue;
      }

      // We found an llm_call for the same model — inspect its outcome
      const errorType = candidate.details["error_type"];

      if (typeof errorType !== "string" || !TRANSIENT_ERRORS.has(errorType)) {
        // Same model succeeded (or had a non-transient error) — not a retry chain
        return NO_MATCH;
      }

      // Transient error found — compute time gap
      const candidateMs = candidate.occurredAt.getTime();
      const gapSeconds = (eventMs - candidateMs) / 1000;

      if (gapSeconds < 0 || gapSeconds > this._windowSeconds) {
        return NO_MATCH;
      }

      const baseLikelihood = ERROR_LIKELIHOODS[errorType] ?? 0.8;
      const confidence =
        baseLikelihood * Math.max(0, 1 - gapSeconds / this._windowSeconds);

      if (confidence >= this._threshold) {
        return {
          isRetry: true,
          confidence,
          matchedEventId: candidate.eventId,
          reason: "heuristic",
        };
      }

      // Confidence too low — still not a confirmed retry
      return NO_MATCH;
    }

    // No matching candidate found
    return NO_MATCH;
  }

  // -------------------------------------------------------------------------
  // Private helpers
  // -------------------------------------------------------------------------

  private _getOrCreate(taskId: string): CostEvent[] {
    let list = this._windows.get(taskId);
    if (!list) {
      list = [];
      this._windows.set(taskId, list);
    }
    return list;
  }

  /**
   * Remove events from the front of the list that are older than
   * `windowSeconds` relative to `referenceTime`.
   */
  private _prune(list: CostEvent[], referenceTime: Date): void {
    const cutoffMs =
      referenceTime.getTime() - this._windowSeconds * 1000;
    let i = 0;
    while (i < list.length && list[i].occurredAt.getTime() < cutoffMs) {
      i++;
    }
    if (i > 0) {
      list.splice(0, i);
    }
  }
}
