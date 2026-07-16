/**
 * Background HTTP event pusher for dexcost.
 *
 * Periodically reads pending events from the buffer and POSTs them
 * to a remote endpoint using the built-in `fetch` API (Node 18+).
 */

import type { CostEvent, Task } from "../core/models.js";
import type { AttributionEventV2 } from "../attribution/types.js";
import type { TrackerOptions } from "../core/tracker.js";
import type { EventBuffer } from "./buffer.js";
import { toAttributionEventV2, toAttributionTaskIngestV1 } from "../attribution/convert.js";
import { redactDict, hashValue, enforceMetadataLimit } from "../security/redaction.js";
import { DEFAULT_ENDPOINT } from "../core/endpoint.js";

/** Maximum backoff in milliseconds (5 minutes). */
const MAX_BACKOFF_MS = 300_000;

/** Leave headroom below the control-plane's 128,000-byte queue contract. */
const MAX_PAYLOAD_BYTES = 120_000;

/** Minimum interval between purge runs in milliseconds (1 hour). */
const PURGE_INTERVAL_MS = 3_600_000;

/**
 * Pushes buffered events to a remote endpoint on a periodic interval.
 *
 * Implements exponential backoff on failure, resetting on success.
 */
export class EventPusher {
  private _interval: ReturnType<typeof setInterval> | null = null;
  private _purgeInterval: ReturnType<typeof setInterval> | null = null;
  private _backoffMs = 1000;
  private _buffer: EventBuffer;
  private _options: TrackerOptions;
  /**
   * Control Layer endpoint, resolved by the tracker from explicit in-code
   * config and passed in here. The pusher never reads the env or calls
   * `resolveEndpoint()` itself — the endpoint is fully determined upstream so a
   * hostile env cannot redirect the ingest POST (and the Bearer API key).
   * Defaults to the production endpoint when not supplied (the production
   * tracker always passes the resolved value explicitly).
   */
  private _endpoint: string;
  private _pushing = false;
  private _lastPurgeMs = 0;
  /** Set permanently when the API key is rejected (HTTP 401/403). */
  private _authFailed = false;

  constructor(
    buffer: EventBuffer,
    options: TrackerOptions,
    endpoint: string = DEFAULT_ENDPOINT,
  ) {
    this._buffer = buffer;
    this._options = options;
    this._endpoint = endpoint;
  }

  /**
   * Update the API key and clear any auth-failed state so the push
   * loop can resume. Sprint 2 Theme D / §3.2.3 (B14). When the
   * Control Layer returns 401/403 the pusher sets `_authFailed=true`
   * and calls `stop()`. Without this method the only recovery is
   * restarting the customer's process.
   */
  setApiKey(newKey: string): void {
    this._options = { ...this._options, apiKey: newKey };
    this._authFailed = false;
    // If the loop was torn down by the prior auth failure, restart it.
    if (!this._interval) {
      this.start();
    }
  }

  /**
   * Start the periodic background push loop.
   */
  start(): void {
    if (this._interval) {
      return; // Already running
    }
    const intervalMs = this._options.flushIntervalMs ?? 30000;
    this._interval = setInterval(() => {
      void this.push();
    }, intervalMs);

    // Allow the process to exit even if the interval is running
    if (this._interval.unref) {
      this._interval.unref();
    }

    // Independent purge interval (runs even when pushes are failing).
    // Purges old synced events AND stale pending events so a permanently
    // failing sync can't grow the local buffer without bound.
    this._purgeInterval = setInterval(() => {
      try {
        this._buffer.purgeSynced();
        this._buffer.purgeOldPending();
      } catch {
        // Non-fatal — purge will be retried next cycle
      }
    }, 60 * 60 * 1000);
    if (this._purgeInterval.unref) {
      this._purgeInterval.unref();
    }
  }

  /**
   * Stop the periodic background push loop.
   */
  stop(): void {
    if (this._interval) {
      clearInterval(this._interval);
      this._interval = null;
    }
    if (this._purgeInterval) {
      clearInterval(this._purgeInterval);
      this._purgeInterval = null;
    }
  }

  /**
   * Force an immediate flush of all pending events.
   */
  async flush(): Promise<void> {
    await this.push();
  }

  /**
   * Push pending events to the remote endpoint.
   *
   * Uses exponential backoff on failure, capping at MAX_BACKOFF_MS.
   * Resets backoff on success.
   */
  private async push(): Promise<void> {
    if (this._pushing) {
      return; // Avoid concurrent pushes
    }
    if (this._authFailed) {
      // API key was rejected — sync is permanently disabled.
      return;
    }

    const batchSize = this._options.batchSize ?? 100;
    const pending = this._buffer.getPendingEvents(batchSize);
    const tasks = this._buffer.getPendingTasks();
    const wireEvents: AttributionEventV2[] = [];
    const skippedEventIds: string[] = [];
    for (const event of pending) {
      const converted = this._serializeEvent(event);
      if (converted === null) skippedEventIds.push(event.eventId);
      else wireEvents.push(converted);
    }
    // Observability-only signals and permanently invalid legacy rows must not
    // poison the durable pending queue forever.
    if (skippedEventIds.length > 0) this._buffer.markSynced(skippedEventIds);
    if (wireEvents.length === 0 && tasks.length === 0) return;

    this._pushing = true;

    try {
      const ok = await this.pushWithSplit(wireEvents, tasks);
      if (ok) {
        // §3.2.1 (B12): pushWithSplit now marks synced at each leaf
        // POST; these calls are kept as a defensive idempotent safety
        // net for any future path that returns true without splitting.
        this._buffer.markSynced(wireEvents.map((e) => e.event_id));
        this._buffer.markTasksSynced(tasks.map((t) => t.taskId));
        this._backoffMs = 1000; // Reset backoff on success

        // Purge old synced events + stale pending events (throttled to
        // once per hour). purgeOldPending is the safety net for events
        // that can never be delivered (mirrors Python sync.py).
        const now = Date.now();
        if (now - this._lastPurgeMs >= PURGE_INTERVAL_MS) {
          try {
            this._buffer.purgeSynced();
            this._buffer.purgeOldPending();
          } catch {
            // Non-fatal — purge will be retried next cycle
          }
          this._lastPurgeMs = now;
        }
      } else {
        this._backoffMs = Math.min(this._backoffMs * 2, MAX_BACKOFF_MS);
      }
    } catch {
      this._backoffMs = Math.min(this._backoffMs * 2, MAX_BACKOFF_MS);
    } finally {
      this._pushing = false;
    }
  }

  /** Convert durable capture into the strict, details-free v2 wire event. */
  private _serializeEvent(event: CostEvent): AttributionEventV2 | null {
    // Attribution v2 has no arbitrary details carrier. The converter reads
    // only an accounting allow-list, so event metadata/PII cannot leak.
    return toAttributionEventV2(event);
  }

  /**
   * Serialise a single task to its wire dict, applying the same PII
   * protections as `_serializeEvent` does for events: `redactFields` are
   * stripped from `metadata`, `customer_id`/`project_id` are SHA-256
   * hashed when `hashCustomerId` is set, and oversized metadata is
   * replaced with a stub.
   *
   * Without this, task `metadata` (which can carry user PII) and the raw
   * `customer_id`/`project_id` would be POSTed unredacted — a leak the
   * event path already guards against. Mirrors the Python SyncWorker.
   */
  private _serializeTask(task: Task): Record<string, unknown> {
    const dict = toAttributionTaskIngestV1(task) as unknown as Record<string, unknown>;

    let metadata = dict["metadata"] as Record<string, unknown> | undefined | null;
    if (metadata && typeof metadata === "object") {
      // Strip configured PII fields from task metadata.
      const redactFields = this._options.redactFields;
      if (redactFields && redactFields.length > 0) {
        metadata = redactDict(metadata, redactFields);
      }
      // Enforce the metadata size limit.
      metadata = enforceMetadataLimit(metadata);
      dict["metadata"] = metadata;
    }

    // Hash customer/project identifiers when configured.
    if (this._options.hashCustomerId) {
      for (const key of ["customer_id", "project_id"]) {
        const val = dict[key];
        if (typeof val === "string") {
          dict[key] = hashValue(val);
        }
      }
    }

    return dict;
  }

  /**
   * POST events with automatic batch splitting if payload exceeds size limit.
   *
   * Recursively splits events and tasks until every queue message fits the
   * published control-plane limit. Task chunks land before dependent events.
   */
  private async pushWithSplit(
    events: AttributionEventV2[],
    tasks: Task[],
  ): Promise<boolean> {
    let payload: string;
    try {
      payload = JSON.stringify({
        events,
        tasks: tasks.map((t) => this._serializeTask(t)),
      });
    } catch {
      return false; // Unserializable payload — skip this batch
    }

    const payloadBytes = new TextEncoder().encode(payload).byteLength;
    if (payloadBytes <= MAX_PAYLOAD_BYTES) {
      const ok = await this.postRaw(payload);
      if (ok) {
        // Sprint 2 Theme D / §3.2.1 (B12): mark synced at the leaf so
        // a sibling-half failure does not unwind work that succeeded.
        // Pre-fix the outer caller marked synced ONLY when both halves
        // returned true; first-half-OK + second-half-fail re-sent the
        // first half on the next tick → duplicates at the control plane.
        this._buffer.markSynced(events.map((e) => e.event_id));
        if (tasks.length > 0) {
          this._buffer.markTasksSynced(tasks.map((t) => t.taskId));
        }
      }
      return ok;
    }

    if (events.length > 1) {
      const mid = Math.floor(events.length / 2);
      const firstOk = await this.pushWithSplit(events.slice(0, mid), tasks);
      if (!firstOk) return false;
      return this.pushWithSplit(events.slice(mid), []);
    }

    if (tasks.length > 1) {
      const mid = Math.floor(tasks.length / 2);
      const firstOk = await this.pushWithSplit([], tasks.slice(0, mid));
      if (!firstOk) return false;
      return this.pushWithSplit(events, tasks.slice(mid));
    }

    if (events.length === 1 && tasks.length === 1) {
      const taskOk = await this.pushWithSplit([], tasks);
      if (!taskOk) return false;
      return this.pushWithSplit(events, []);
    }

    if (events.length === 1) {
      // Single event too large — skip it with warning
      console.warn(
        `[dexcost] Single event exceeds payload limit (${payloadBytes} bytes), skipping`,
      );
      this._buffer.markSynced([events[0].event_id]);
      return true;
    }

    if (tasks.length === 1) {
      console.warn(
        `[dexcost] Single task exceeds payload limit (${payloadBytes} bytes), skipping`,
      );
      this._buffer.markTasksSynced([tasks[0].taskId]);
    }
    return true;
  }

  /**
   * POST a pre-serialised JSON payload to the cloud ingest endpoint.
   *
   * Returns `true` on 2xx, `false` otherwise.
   */
  private async postRaw(body: string): Promise<boolean> {
    // Endpoint is the one the tracker resolved from explicit in-code config and
    // passed to the constructor — never the env. A hostile env cannot redirect
    // this POST (and the Bearer API key) to an attacker host.
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (this._options.apiKey) {
      headers["Authorization"] = `Bearer ${this._options.apiKey}`;
    }

    const url = `${this._endpoint}/v1/ingest`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 30_000);
    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers,
        body,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timeoutId);
    }

    if (response.ok) {
      try {
        const result = await response.json() as { rejected?: number };
        if ((result.rejected ?? 0) > 0) {
          console.warn(
            `[dexcost] Control plane rejected ${result.rejected} item(s) from an attribution-v2 batch`,
          );
          return false;
        }
      } catch {
        // Some compatible/private endpoints return an empty 2xx body.
      }
      return true;
    }

    if (response.status === 413) {
      // Permanent error — batch too large, don't retry
      // This shouldn't happen with pre-split but handle gracefully
      console.warn("[dexcost] Server returned 413 despite pre-split check");
      return false;
    }

    if (response.status === 401 || response.status === 403) {
      // API key rejected — stop sync permanently rather than retrying
      // a rejected key forever (mirrors Python sync.py).
      console.error(
        `[dexcost] API key rejected (HTTP ${response.status}) — disabling sync`,
      );
      this._authFailed = true;
      this.stop();
      return false;
    }

    return false;
  }

  /** Whether sync has been permanently disabled due to a rejected API key. */
  get authFailed(): boolean {
    return this._authFailed;
  }
}
