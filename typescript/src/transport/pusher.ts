/**
 * Background HTTP event pusher for dexcost.
 *
 * Periodically reads pending events from the buffer and POSTs them
 * to a remote endpoint using the built-in `fetch` API (Node 18+).
 */

import type { CostEvent, Task } from "../core/models.js";
import type { AttributionEventV3 } from "../attribution/v3-types.js";
import type { TrackerOptions } from "../core/tracker.js";
import type { EventBuffer } from "./buffer.js";
import { toAttributionTaskIngestV1 } from "../attribution/convert.js";
import { toAttributionObservationV3 } from "../attribution/v3-convert.js";
import { redactDict, hashValue, enforceMetadataLimit } from "../security/redaction.js";
import { DEFAULT_ENDPOINT } from "../core/endpoint.js";
import { toBusinessIdentityRevision } from "../core/business-identity.js";
import { providerJobFromDict } from "../core/provider-jobs.js";
import {
  DeliveryStatus,
  emitDeliveryError,
  type DeliveryErrorOperation,
  type DeliveryWorkerState,
} from "./delivery.js";

interface ProviderJobKey { eventId: string; revision: number }

/** Maximum backoff in milliseconds (5 minutes). */
const MAX_BACKOFF_MS = 300_000;

/** Leave headroom below the control-plane's 128,000-byte queue contract. */
const MAX_PAYLOAD_BYTES = 120_000;

/** Minimum interval between purge runs in milliseconds (1 hour). */
const PURGE_INTERVAL_MS = 3_600_000;

/** Bound extra work while scanning past quarantined conversion failures. */
const MAX_CONVERSION_SCAN = 1_000;
const CONVERSION_SCAN_MULTIPLIER = 10;

/** Emit at most one background warning per failing event set per hour. */
const CONVERSION_WARN_INTERVAL_MS = 3_600_000;

function conversionFailureFingerprint(eventIds: string[]): string {
  let hash = 0x811c9dc5;
  for (const character of [...eventIds].sort().join("\u0000")) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function redactEventDetails(
  details: Record<string, unknown>,
  fields: string[],
): Record<string, unknown> {
  const redacted = redactDict(details, fields);
  const dimensions = redacted.attribution_dimensions;
  if (!Array.isArray(dimensions)) return redacted;

  const fieldSet = new Set(fields);
  return {
    ...redacted,
    attribution_dimensions: dimensions.filter((candidate) => {
      if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) {
        return true;
      }
      const key = (candidate as Record<string, unknown>).key;
      return typeof key !== "string" || !fieldSet.has(key);
    }),
  };
}

/**
 * Pushes buffered events to a remote endpoint on a periodic interval.
 *
 * Implements exponential backoff on failure, resetting on success.
 */
export class EventPusher {
  private _interval: ReturnType<typeof setTimeout> | null = null;
  private _purgeInterval: ReturnType<typeof setInterval> | null = null;
  private _running = false;
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
  private _lastConversionWarnMs = 0;
  private _lastConversionWarnFingerprint = "";
  private _quarantineRecoveryAttempted = false;
  /** Set permanently when the ingestion API rejects the key with HTTP 401. */
  private _authFailed = false;
  private _workerState: DeliveryWorkerState = "stopped";
  private _lastAttemptAt?: Date;
  private _lastSuccessAt?: Date;
  private _lastErrorAt?: Date;
  private _lastErrorType?: string;
  private _lastErrorMessage?: string;
  private _consecutiveFailures = 0;
  private _successfulBatches = 0;
  private _failedBatches = 0;
  private _deliveredRecords = 0;
  private _activePushStats?: { deliveredRecords: number; quarantinedRecords: number };

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
    this._consecutiveFailures = 0;
    this._workerState = "idle";
    // If the loop was torn down by the prior auth failure, restart it.
    if (!this._interval) {
      this.start();
    }
  }

  /**
   * Start the periodic background push loop.
   */
  start(): void {
    if (this._running) {
      return; // Already running
    }
    this._running = true;
    this._workerState = "idle";
    const intervalMs = this._options.flushIntervalMs ?? 30000;
    this._schedulePush(intervalMs);

    // Independent acknowledged-data cleanup. Unsent financial attribution is
    // never deleted automatically, including while authentication is broken.
    this._purgeInterval = setInterval(() => {
      try {
        this._buffer.purgeSynced();
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
    this._running = false;
    if (this._interval) {
      clearTimeout(this._interval);
      this._interval = null;
    }
    if (this._purgeInterval) {
      clearInterval(this._purgeInterval);
      this._purgeInterval = null;
    }
    if (!this._authFailed) this._workerState = "stopped";
  }

  /** Schedule exactly one background cycle, then choose the next delay from its result. */
  private _schedulePush(delayMs: number): void {
    if (!this._running || this._authFailed) return;
    if (this._interval !== null) clearTimeout(this._interval);
    this._interval = setTimeout(() => {
      this._interval = null;
      void this.push(false).finally(() => {
        if (!this._running || this._authFailed) return;
        const regular = this._options.flushIntervalMs ?? 30000;
        this._schedulePush(this._workerState === "backoff" ? this._backoffMs : regular);
      });
    }, Math.max(0, delayMs));
    this._interval.unref?.();
  }

  private _recordAttempt(): void {
    this._lastAttemptAt = new Date();
    this._workerState = "syncing";
  }

  private _recordSuccess(deliveredRecords: number): void {
    this._consecutiveFailures = 0;
    if (deliveredRecords > 0) {
      this._lastSuccessAt = new Date();
      this._successfulBatches += 1;
      this._deliveredRecords += deliveredRecords;
    }
    this._workerState = "idle";
  }

  private _recordError(
    error: unknown,
    operation: DeliveryErrorOperation,
    retryable: boolean,
    state: DeliveryWorkerState,
  ): void {
    const occurredAt = new Date();
    const errorType = error instanceof Error ? error.name : typeof error;
    let message = error instanceof Error ? error.message : String(error);
    if (this._options.apiKey) message = message.replaceAll(this._options.apiKey, "[REDACTED]");
    message = message.slice(0, 1024);
    this._lastErrorAt = occurredAt;
    this._lastErrorType = errorType;
    this._lastErrorMessage = message;
    this._consecutiveFailures += 1;
    this._failedBatches += 1;
    this._workerState = state;
    emitDeliveryError({
      occurredAt, operation, errorType, message, retryable,
      consecutiveFailures: this._consecutiveFailures,
    });
  }

  status(): DeliveryStatus {
    return new DeliveryStatus({
      ...this._buffer.deliveryCounts(), enabled: true, workerState: this._workerState,
      lastAttemptAt: this._lastAttemptAt, lastSuccessAt: this._lastSuccessAt,
      lastErrorAt: this._lastErrorAt, lastErrorType: this._lastErrorType,
      lastErrorMessage: this._lastErrorMessage, consecutiveFailures: this._consecutiveFailures,
      successfulBatches: this._successfulBatches, failedBatches: this._failedBatches,
      deliveredRecords: this._deliveredRecords,
      backoffSeconds: this._workerState === "backoff" ? this._backoffMs / 1000 : 0,
    });
  }

  /**
   * Force an immediate flush of all pending events.
   */
  async flush(): Promise<void> {
    await this.push(true);
  }

  /**
   * Push pending events to the remote endpoint.
   *
   * Uses exponential backoff on failure, capping at MAX_BACKOFF_MS.
   * Resets backoff on success.
   */
  private async push(surfaceConversionErrors: boolean): Promise<void> {
    if (this._pushing) {
      return; // Avoid concurrent pushes
    }
    if (this._authFailed) {
      // API key was rejected — sync is permanently disabled.
      return;
    }

    // Converter failures are retained durably. Requeue them once for each
    // pusher lifetime so a corrected SDK can deliver old rows without users
    // editing SQLite; still-invalid rows return to quarantine in this flush.
    if (!this._quarantineRecoveryAttempted) {
      this._quarantineRecoveryAttempted = true;
      try { this._buffer.requeueQuarantinedEvents(); } catch { /* retry requires restart */ }
    }

    const batchSize = Math.max(1, this._options.batchSize ?? 100);
    const taskDeliveries = this._buffer.getPendingTaskDeliveries();
    const tasks = taskDeliveries.map((delivery) => delivery.task);
    const taskSyncVersions = new Map(
      taskDeliveries.map((delivery) => [delivery.task.taskId, delivery.syncVersion]),
    );
    const wireEvents: AttributionEventV3[] = [];
    const eventSyncVersions = new Map<string, number>();
    const failedEventIds: string[] = [];
    const seenEventIds = new Set<string>();
    const scanLimit = Math.max(
      batchSize,
      Math.min(MAX_CONVERSION_SCAN, batchSize * CONVERSION_SCAN_MULTIPLIER),
    );
    let scanned = 0;

    // Quarantine failed pages as we go, then fetch the next oldest pending
    // page. This lets one flush reach valid events behind a malformed prefix.
    while (wireEvents.length < batchSize && scanned < scanLimit) {
      const pageLimit = Math.min(batchSize - wireEvents.length, scanLimit - scanned);
      const pending = this._buffer.getPendingEventDeliveries(pageLimit);
      if (pending.length === 0) break;

      const pageFailedEventIds: string[] = [];
      let newlyScanned = 0;
      for (const delivery of pending) {
        const { event, syncVersion } = delivery;
        if (seenEventIds.has(event.eventId)) continue;
        seenEventIds.add(event.eventId);
        eventSyncVersions.set(event.eventId, syncVersion);
        newlyScanned++;
        scanned++;
        const converted = this._serializeEvent(event);
        if (converted === null) pageFailedEventIds.push(event.eventId);
        else wireEvents.push(converted);
      }

      if (pageFailedEventIds.length > 0) {
        this._buffer.markQuarantined(pageFailedEventIds, eventSyncVersions);
        failedEventIds.push(...pageFailedEventIds);
      }

      // A storage failure may leave the same rows pending. Do not spin inside
      // one flush; the throttled background path can retry later.
      if (newlyScanned === 0 || pending.length < pageLimit) break;
    }

    const outcomes = this._buffer.getPendingLedger("outcome", batchSize);
    const revenueRevisions = this._buffer.getPendingLedger("revenue", batchSize);
    const providerJobRecords = this._buffer.getPendingLedger(
      "provider_job", Math.max(1, batchSize - wireEvents.length),
    );
    const providerJobKeys: ProviderJobKey[] = [];
    const failedProviderJobKeys: ProviderJobKey[] = [];
    for (const raw of providerJobRecords) {
      try {
        const job = providerJobFromDict(raw);
        wireEvents.push(job.toAttributionObservation(this._options.environment) as unknown as AttributionEventV3);
        providerJobKeys.push({ eventId: job.eventId, revision: job.revision });
      } catch {
        const eventId = raw["event_id"];
        const revision = raw["revision"];
        if (typeof eventId === "string" && Number.isSafeInteger(revision)) {
          failedProviderJobKeys.push({ eventId, revision: Number(revision) });
        }
      }
    }
    this._buffer.markLedgerQuarantined(
      "provider_job", failedProviderJobKeys.map((item) => [item.eventId, item.revision] as const),
    );
    const businessIdentities = tasks.flatMap((task) => {
      if (task.endedAt === undefined) return [];
      const identity = toBusinessIdentityRevision(task);
      return identity === undefined ? [] : [this._serializeBusinessIdentity(identity)];
    });

    if (wireEvents.length === 0 && tasks.length === 0 && outcomes.length === 0 && revenueRevisions.length === 0) {
      this._handleConversionFailures(failedEventIds, surfaceConversionErrors);
      return;
    }

    this._pushing = true;
    this._recordAttempt();

    try {
      const pushStats = { deliveredRecords: 0, quarantinedRecords: 0 };
      this._activePushStats = pushStats;
      const ok = await this.pushWithSplit(
        wireEvents, tasks, businessIdentities, outcomes, revenueRevisions, providerJobKeys,
        eventSyncVersions, taskSyncVersions,
      );
      if (ok) {
        this._backoffMs = 1000; // Reset backoff on success
        // Only a successful leaf POST may acknowledge a record. Terminally
        // oversized records are quarantined and intentionally excluded here.
        this._recordSuccess(pushStats.deliveredRecords);

        // Purge acknowledged events only (throttled to once per hour).
        const now = Date.now();
        if (now - this._lastPurgeMs >= PURGE_INTERVAL_MS) {
          try {
            this._buffer.purgeSynced();
          } catch {
            // Non-fatal — purge will be retried next cycle
          }
          this._lastPurgeMs = now;
        }
      } else {
        this._backoffMs = Math.min(this._backoffMs * 2, MAX_BACKOFF_MS);
        if (!this._authFailed) {
          this._recordError(
            new Error("control plane did not accept the complete attribution batch"),
            "transport", true, "backoff",
          );
        }
      }
    } catch (error) {
      this._backoffMs = Math.min(this._backoffMs * 2, MAX_BACKOFF_MS);
      this._recordError(error, "transport", true, "backoff");
    } finally {
      this._activePushStats = undefined;
      this._pushing = false;
    }

    this._handleConversionFailures(failedEventIds, surfaceConversionErrors);
  }

  private _handleConversionFailures(eventIds: string[], surface: boolean): void {
    if (eventIds.length === 0) {
      this._lastConversionWarnFingerprint = "";
      return;
    }
    const preview = eventIds.slice(0, 3).join(", ");
    const error = new Error(
      `${eventIds.length} event(s) were quarantined because they cannot be represented by attribution v3 (event IDs: ${preview})`,
    );
    this._recordError(error, "conversion", false, "idle");
    if (surface) throw error;

    const now = Date.now();
    const fingerprint = conversionFailureFingerprint(eventIds);
    if (
      fingerprint !== this._lastConversionWarnFingerprint ||
      now - this._lastConversionWarnMs >= CONVERSION_WARN_INTERVAL_MS
    ) {
      console.warn(`[dexcost] ${error.message}`);
      this._lastConversionWarnFingerprint = fingerprint;
      this._lastConversionWarnMs = now;
    }
  }

  /** Convert durable capture into the strict, details-free v3 wire observation. */
  private _serializeEvent(event: CostEvent): AttributionEventV3 | null {
    // The converter promotes selected detail fields into typed provider and
    // resource fields. Redact before conversion so configured identifiers
    // cannot bypass the field-level policy. Keep durable capture untouched.
    const redactFields = this._options.redactFields;
    const sanitized = redactFields && redactFields.length > 0
      ? { ...event, details: redactEventDetails(event.details, redactFields) }
      : event;
    return toAttributionObservationV3(sanitized, this._options.environment);
  }

  private _serializeBusinessIdentity(identity: Record<string, unknown>): Record<string, unknown> {
    const result = structuredClone(identity);
    const assignment = result["assignment"] as Record<string, unknown>;
    for (const field of this._options.redactFields ?? []) delete assignment[field];
    if (assignment["experiment_id"] === undefined) delete assignment["variant"];
    if (this._options.hashCustomerId) {
      for (const key of ["customer_id", "project_id"]) {
        const value = assignment[key];
        if (typeof value === "string") assignment[key] = hashValue(value);
      }
    }
    return result;
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
    events: AttributionEventV3[],
    tasks: Task[],
    businessIdentities: Array<Record<string, unknown>> = [],
    outcomes: Array<Record<string, unknown>> = [],
    revenueRevisions: Array<Record<string, unknown>> = [],
    providerJobKeys: ProviderJobKey[] = [],
    eventSyncVersions: ReadonlyMap<string, number> = new Map(),
    taskSyncVersions: ReadonlyMap<string, number> = new Map(),
  ): Promise<boolean> {
    let payload: string;
    try {
      payload = JSON.stringify({
        events,
        tasks: tasks.map((t) => this._serializeTask(t)),
        business_identities: businessIdentities,
        outcomes,
        revenue_revisions: revenueRevisions,
        cost_pools: [],
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
        this._buffer.markSynced(events.map((e) => e.event_id), eventSyncVersions);
        if (tasks.length > 0) {
          this._buffer.markTasksSynced(tasks.map((t) => t.taskId), taskSyncVersions);
        }
        this._buffer.markLedgerSynced("outcome", outcomes.map((item) => [
          String(item["outcome_id"]),
          Number((item["lifecycle"] as Record<string, unknown>)["revision"]),
        ] as const));
        this._buffer.markLedgerSynced("revenue", revenueRevisions.map((item) => [
          String(item["revenue_id"]),
          Number((item["lifecycle"] as Record<string, unknown>)["revision"]),
        ] as const));
        this._buffer.markLedgerSynced(
          "provider_job", providerJobKeys.map((item) => [item.eventId, item.revision] as const),
        );
        if (this._activePushStats !== undefined) {
          this._activePushStats.deliveredRecords += events.length + tasks.length +
            businessIdentities.length + outcomes.length + revenueRevisions.length;
        }
      }
      return ok;
    }

    if (events.length > 1) {
      const mid = Math.floor(events.length / 2);
      const firstIds = new Set(events.slice(0, mid).map((event) => event.event_id));
      const firstJobs = providerJobKeys.filter((job) => firstIds.has(job.eventId));
      const secondJobs = providerJobKeys.filter((job) => !firstIds.has(job.eventId));
      const firstOk = await this.pushWithSplit(
        events.slice(0, mid), tasks, businessIdentities, outcomes, revenueRevisions, firstJobs,
        eventSyncVersions, taskSyncVersions,
      );
      if (!firstOk) return false;
      return this.pushWithSplit(
        events.slice(mid), [], [], [], [], secondJobs, eventSyncVersions, taskSyncVersions,
      );
    }

    if (tasks.length > 1) {
      const mid = Math.floor(tasks.length / 2);
      const firstTaskIds = new Set(tasks.slice(0, mid).map((task) => task.taskId));
      const firstIdentities = businessIdentities.filter((item) => firstTaskIds.has(String(item["task_id"])));
      const secondIdentities = businessIdentities.filter((item) => !firstTaskIds.has(String(item["task_id"])));
      const firstOk = await this.pushWithSplit(
        [], tasks.slice(0, mid), firstIdentities, outcomes, revenueRevisions, [],
        eventSyncVersions, taskSyncVersions,
      );
      if (!firstOk) return false;
      return this.pushWithSplit(
        events, tasks.slice(mid), secondIdentities, [], [], providerJobKeys,
        eventSyncVersions, taskSyncVersions,
      );
    }

    if (outcomes.length > 1) {
      const mid = Math.floor(outcomes.length / 2);
      const firstOk = await this.pushWithSplit(
        events, tasks, businessIdentities, outcomes.slice(0, mid), revenueRevisions, providerJobKeys,
        eventSyncVersions, taskSyncVersions,
      );
      if (!firstOk) return false;
      return this.pushWithSplit(
        [], [], [], outcomes.slice(mid), [], [], eventSyncVersions, taskSyncVersions,
      );
    }

    if (revenueRevisions.length > 1) {
      const mid = Math.floor(revenueRevisions.length / 2);
      const firstOk = await this.pushWithSplit(
        events, tasks, businessIdentities, outcomes, revenueRevisions.slice(0, mid), providerJobKeys,
        eventSyncVersions, taskSyncVersions,
      );
      if (!firstOk) return false;
      return this.pushWithSplit(
        [], [], [], [], revenueRevisions.slice(mid), [], eventSyncVersions, taskSyncVersions,
      );
    }

    if (events.length === 1 && tasks.length === 1) {
      const taskOk = await this.pushWithSplit(
        [], tasks, businessIdentities, outcomes, revenueRevisions, [],
        eventSyncVersions, taskSyncVersions,
      );
      if (!taskOk) return false;
      return this.pushWithSplit(
        events, [], [], [], [], providerJobKeys, eventSyncVersions, taskSyncVersions,
      );
    }

    if (events.length === 1) {
      console.warn(
        `[dexcost] Single event exceeds payload limit (${payloadBytes} bytes), quarantining`,
      );
      if (providerJobKeys.length > 0) {
        this._buffer.markLedgerQuarantined(
          "provider_job", providerJobKeys.map((item) => [item.eventId, item.revision] as const),
        );
      } else this._buffer.markQuarantined([events[0].event_id], eventSyncVersions);
      if (this._activePushStats !== undefined) this._activePushStats.quarantinedRecords += 1;
      return true;
    }

    if (tasks.length === 1) {
      console.warn(
        `[dexcost] Single task exceeds payload limit (${payloadBytes} bytes), quarantining`,
      );
      this._buffer.markTasksQuarantined([tasks[0].taskId], taskSyncVersions);
      if (this._activePushStats !== undefined) this._activePushStats.quarantinedRecords += 1;
      return true;
    }

    if (outcomes.length === 1) {
      const item = outcomes[0];
      this._buffer.markLedgerQuarantined("outcome", [[
        String(item["outcome_id"]), Number((item["lifecycle"] as Record<string, unknown>)["revision"]),
      ]]);
      console.warn(`[dexcost] Single outcome exceeds payload limit (${payloadBytes} bytes), quarantining`);
      if (this._activePushStats !== undefined) this._activePushStats.quarantinedRecords += 1;
      return true;
    }
    if (revenueRevisions.length === 1) {
      const item = revenueRevisions[0];
      this._buffer.markLedgerQuarantined("revenue", [[
        String(item["revenue_id"]), Number((item["lifecycle"] as Record<string, unknown>)["revision"]),
      ]]);
      console.warn(`[dexcost] Single revenue revision exceeds payload limit (${payloadBytes} bytes), quarantining`);
      if (this._activePushStats !== undefined) this._activePushStats.quarantinedRecords += 1;
      return true;
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

    if (response.status === 401) {
      // The ingestion contract uses 401 for invalid/revoked keys.
      console.error(
        `[dexcost] API key rejected (HTTP ${response.status}) — disabling sync`,
      );
      this._authFailed = true;
      const error = new Error(`API key rejected (HTTP ${response.status})`);
      error.name = "HTTPError";
      this._recordError(error, "authentication", false, "auth_failed");
      this.stop();
      return false;
    }

    if (response.status === 403) {
      throw new Error("control plane request was forbidden (HTTP 403)");
    }

    return false;
  }

  /** Whether sync has been permanently disabled due to a rejected API key. */
  get authFailed(): boolean {
    return this._authFailed;
  }
}
