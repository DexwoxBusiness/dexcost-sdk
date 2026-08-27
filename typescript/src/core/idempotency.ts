import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { eventToDict, type CostEvent } from "./models.js";

interface IdempotencyScope {
  readonly key: string;
  nextOccurrence: number;
}

interface IdempotencyContext {
  readonly scope?: IdempotencyScope;
}

export interface CapturedIdempotencyKey {
  readonly key: string;
  readonly occurrence: number;
}

export type IdempotencyKey = string | CapturedIdempotencyKey;

/**
 * One-shot token returned by {@link setIdempotencyKey}.
 *
 * Call `reset()` to restore the exact previous scope, including its next
 * occurrence counter. Reset the token before yielding to another async
 * continuation. For scopes that cross `await`, use
 * {@link runWithIdempotencyKey}; Node's callback-scoped AsyncLocalStorage API
 * is what preserves already-created child contexts while restoring callers.
 */
export interface IdempotencyKeyToken {
  readonly previousKey: string | undefined;
  reset(): void;
}

interface IdempotencyTokenState {
  readonly previous: IdempotencyContext | undefined;
  readonly installed: IdempotencyContext;
  used: boolean;
}

const keyStore = new AsyncLocalStorage<IdempotencyContext | undefined>();
const tokenStates = new WeakMap<object, IdempotencyTokenState>();
const EVENT_NAMESPACE = "ee9858ce-fc4e-5c97-a803-2ea9df316d5c";

function validateKey(key: string): string {
  if (typeof key !== "string" || key.length < 1 || key.length > 255) {
    throw new Error("idempotency key must contain 1 to 255 characters");
  }
  for (const character of key) {
    const code = character.charCodeAt(0);
    if (code < 0x21 || code > 0x7e) {
      throw new Error("idempotency key must contain visible ASCII characters only");
    }
  }
  return key;
}

function activeContext(): IdempotencyContext | undefined {
  return keyStore.getStore();
}

export function getIdempotencyKey(): string | undefined {
  return activeContext()?.scope?.key;
}

/** Reserve one deterministic operation occurrence from the active scope. */
export function captureIdempotencyKey(): CapturedIdempotencyKey | undefined {
  const scope = activeContext()?.scope;
  if (scope === undefined) return undefined;
  const occurrence = scope.nextOccurrence;
  scope.nextOccurrence += 1;
  return { key: scope.key, occurrence };
}

function createToken(
  previous: IdempotencyContext | undefined,
  installed: IdempotencyContext,
): IdempotencyKeyToken {
  const token: IdempotencyKeyToken = {
    previousKey: previous?.scope?.key,
    reset(): void {
      restoreToken(token);
    },
  };
  tokenStates.set(token, { previous, installed, used: false });
  return Object.freeze(token);
}

function restoreToken(token: IdempotencyKeyToken): void {
  const state = tokenStates.get(token);
  if (state === undefined) throw new Error("invalid idempotency key token");
  if (state.used) throw new Error("idempotency key token has already been reset");
  if (activeContext() !== state.installed) {
    throw new Error("idempotency key tokens must be reset in LIFO order in their originating async context");
  }
  state.used = true;
  // Restore only this execution context. Async resources created while the
  // installed context was active have already captured it and must retain it.
  keyStore.enterWith(state.previous);
}

/**
 * Set or clear the caller key for the remaining async context.
 *
 * The returned token restores the exact prior scope and occurrence counter.
 * Tokens are single-use, synchronous, and must be restored in LIFO order.
 * Use {@link runWithIdempotencyKey} for a scope that crosses `await`.
 */
export function setIdempotencyKey(key?: string): IdempotencyKeyToken {
  const previous = activeContext();
  const installed: IdempotencyContext = {
    scope: key === undefined
      ? undefined
      : { key: validateKey(key), nextOccurrence: 0 },
  };
  keyStore.enterWith(installed);
  return createToken(previous, installed);
}

export function runWithIdempotencyKey<T>(key: string, fn: () => T): T {
  return keyStore.run({
    scope: { key: validateKey(key), nextOccurrence: 0 },
  }, fn);
}

export function idempotencyHash(key: string): string;
export function idempotencyHash(event: CostEvent): string | undefined;
export function idempotencyHash(value: string | CostEvent): string | undefined {
  if (typeof value === "string") {
    return createHash("sha256").update(validateKey(value), "ascii").digest("hex");
  }
  const hash = value.details["_dexcost_idempotency_sha256"];
  return typeof hash === "string" && /^[0-9a-f]{64}$/.test(hash) ? hash : undefined;
}

function uuidBytes(value: string): Buffer {
  return Buffer.from(value.replaceAll("-", ""), "hex");
}

function uuid5(namespace: string, input: string): string {
  const bytes = createHash("sha1").update(uuidBytes(namespace)).update(input, "utf8").digest().subarray(0, 16);
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x50;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const raw = bytes.toString("hex");
  return `${raw.slice(0, 8)}-${raw.slice(8, 12)}-${raw.slice(12, 16)}-${raw.slice(16, 20)}-${raw.slice(20)}`;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function applyEventIdempotency(
  event: CostEvent,
  key: IdempotencyKey | undefined = captureIdempotencyKey(),
): CostEvent {
  // The operation wrapper may already have reserved and stamped this event.
  // Defensive storage/capability stamping must not consume another occurrence.
  if (idempotencyHash(event) !== undefined || key === undefined) return event;
  const resolvedKey = typeof key === "string" ? key : key.key;
  const occurrence = typeof key === "string" ? undefined : key.occurrence;
  const hash = idempotencyHash(resolvedKey);
  const identity = stableJson({
    event_type: event.eventType,
    provider: event.provider ?? null,
    model: event.model ?? null,
    service_name: event.serviceName ?? null,
    operation_name: event.details["attribution_operation_name"] ?? null,
    resource_type: event.details["attribution_resource_type"] ?? null,
    resource_id: event.details["attribution_resource_id"] ?? null,
    capability: event.details["attribution_capability"] ?? null,
  });
  const oldId = event.eventId;
  const occurrencePart = occurrence === undefined ? "" : `${occurrence}\0`;
  event.eventId = uuid5(EVENT_NAMESPACE, `${event.taskId}\0${hash}\0${occurrencePart}${identity}`);
  event.details = {
    ...event.details,
    _dexcost_idempotency_sha256: hash,
    ...(occurrence === undefined ? {} : { _dexcost_idempotency_occurrence: occurrence }),
  };
  for (const field of ["attribution_operation_id", "attribution_attempt_id"]) {
    if (event.details[field] === oldId) event.details[field] = event.eventId;
  }
  return event;
}

export function equivalentIdempotentEvent(left: CostEvent, right: CostEvent): boolean {
  const leftHash = idempotencyHash(left);
  if (leftHash === undefined || leftHash !== idempotencyHash(right)) return false;
  const leftValue = eventToDict(left);
  const rightValue = eventToDict(right);
  delete leftValue["occurred_at"];
  delete rightValue["occurred_at"];
  return stableJson(leftValue) === stableJson(rightValue);
}
