import type { DeliveryCounts, EventBuffer } from "./buffer.js";

export type DeliveryWorkerState = "local_only" | "idle" | "syncing" | "backoff" | "auth_failed" | "stopped";
export type DeliveryErrorOperation = "transport" | "authentication" | "conversion";

export interface DeliveryErrorEvent {
  occurredAt: Date;
  operation: DeliveryErrorOperation;
  errorType: string;
  message: string;
  retryable: boolean;
  consecutiveFailures: number;
}

export type DeliveryErrorCallback = (event: DeliveryErrorEvent) => void;

export interface DeliveryStatusOptions extends DeliveryCounts {
  enabled: boolean;
  workerState: DeliveryWorkerState;
  lastAttemptAt?: Date;
  lastSuccessAt?: Date;
  lastErrorAt?: Date;
  lastErrorType?: string;
  lastErrorMessage?: string;
  consecutiveFailures?: number;
  successfulBatches?: number;
  failedBatches?: number;
  deliveredRecords?: number;
  backoffSeconds?: number;
}

export class DeliveryStatus implements DeliveryStatusOptions {
  readonly enabled: boolean;
  readonly workerState: DeliveryWorkerState;
  readonly pendingEvents: number;
  readonly quarantinedEvents: number;
  readonly pendingTasks: number;
  readonly quarantinedTasks: number;
  readonly pendingOutcomes: number;
  readonly quarantinedOutcomes: number;
  readonly pendingRevenues: number;
  readonly quarantinedRevenues: number;
  readonly pendingProviderJobs: number;
  readonly quarantinedProviderJobs: number;
  readonly oldestPendingAt?: Date;
  readonly lastAttemptAt?: Date;
  readonly lastSuccessAt?: Date;
  readonly lastErrorAt?: Date;
  readonly lastErrorType?: string;
  readonly lastErrorMessage?: string;
  readonly consecutiveFailures: number;
  readonly successfulBatches: number;
  readonly failedBatches: number;
  readonly deliveredRecords: number;
  readonly backoffSeconds: number;

  constructor(options: DeliveryStatusOptions) {
    Object.assign(this, options);
    this.enabled = options.enabled; this.workerState = options.workerState;
    this.pendingEvents = options.pendingEvents; this.quarantinedEvents = options.quarantinedEvents;
    this.pendingTasks = options.pendingTasks; this.quarantinedTasks = options.quarantinedTasks;
    this.pendingOutcomes = options.pendingOutcomes;
    this.quarantinedOutcomes = options.quarantinedOutcomes; this.pendingRevenues = options.pendingRevenues;
    this.quarantinedRevenues = options.quarantinedRevenues;
    this.pendingProviderJobs = options.pendingProviderJobs;
    this.quarantinedProviderJobs = options.quarantinedProviderJobs;
    this.consecutiveFailures = options.consecutiveFailures ?? 0;
    this.successfulBatches = options.successfulBatches ?? 0;
    this.failedBatches = options.failedBatches ?? 0;
    this.deliveredRecords = options.deliveredRecords ?? 0;
    this.backoffSeconds = options.backoffSeconds ?? 0;
  }

  get pendingRecords(): number {
    return this.pendingEvents + this.pendingTasks + this.pendingOutcomes +
      this.pendingRevenues + this.pendingProviderJobs;
  }

  get quarantinedRecords(): number {
    return this.quarantinedEvents + this.quarantinedTasks + this.quarantinedOutcomes +
      this.quarantinedRevenues + this.quarantinedProviderJobs;
  }

  get healthy(): boolean {
    return this.workerState !== "auth_failed" && this.workerState !== "backoff" &&
      this.quarantinedRecords === 0;
  }
}

const callbacks = new Set<DeliveryErrorCallback>();

export function onDeliveryError(callback: DeliveryErrorCallback): DeliveryErrorCallback {
  if (typeof callback !== "function") throw new TypeError("delivery error callback must be callable");
  callbacks.add(callback);
  return callback;
}

export function removeDeliveryErrorCallback(callback: DeliveryErrorCallback): boolean {
  return callbacks.delete(callback);
}

export function emitDeliveryError(event: DeliveryErrorEvent): void {
  for (const callback of [...callbacks]) {
    try { callback(event); } catch { /* observability must not break delivery */ }
  }
}

const emptyCounts = (): DeliveryCounts => ({
  pendingEvents: 0, quarantinedEvents: 0, pendingTasks: 0, quarantinedTasks: 0,
  pendingOutcomes: 0, quarantinedOutcomes: 0, pendingRevenues: 0,
  quarantinedRevenues: 0, pendingProviderJobs: 0, quarantinedProviderJobs: 0,
});

export function localDeliveryStatus(buffer?: EventBuffer): DeliveryStatus {
  return new DeliveryStatus({
    ...(buffer?.deliveryCounts() ?? emptyCounts()), enabled: false, workerState: "local_only",
  });
}
