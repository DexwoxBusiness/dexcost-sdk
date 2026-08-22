import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import type { CostEvent } from "./models.js";
import { applyEventIdempotency } from "./idempotency.js";

export type CapabilityKind = "tool" | "skill" | "workflow" | "extension" | "other";
export type CapabilitySource =
  | "built_in"
  | "project"
  | "user"
  | "plugin"
  | "marketplace"
  | "remote"
  | "other";
export type CapabilityInvocation =
  | "explicit"
  | "automatic"
  | "nested"
  | "scheduled"
  | "remote"
  | "other";

export interface CapabilityIdentity {
  name: string;
  kind: CapabilityKind;
  namespace?: string;
  version?: string;
  source?: CapabilitySource;
  sourceId?: string;
  invocation?: CapabilityInvocation;
}

const CANONICAL = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const KINDS = new Set<CapabilityKind>(["tool", "skill", "workflow", "extension", "other"]);
const SOURCES = new Set<CapabilitySource>([
  "built_in", "project", "user", "plugin", "marketplace", "remote", "other",
]);
const INVOCATIONS = new Set<CapabilityInvocation>([
  "explicit", "automatic", "nested", "scheduled", "remote", "other",
]);
const capabilityStore = new AsyncLocalStorage<CapabilityIdentity | undefined>();

export function validateCapability(value: CapabilityIdentity): CapabilityIdentity {
  if (!CANONICAL.test(value.name)) {
    throw new Error("capability name must be a canonical lowercase identifier");
  }
  if (!KINDS.has(value.kind)) throw new Error(`unsupported capability kind ${value.kind}`);
  if (value.namespace !== undefined && !CANONICAL.test(value.namespace)) {
    throw new Error("capability namespace must be a canonical lowercase identifier");
  }
  if (value.version !== undefined && (value.version.length < 1 || value.version.length > 128)) {
    throw new Error("capability version must contain 1 to 128 characters");
  }
  if (value.source !== undefined && !SOURCES.has(value.source)) {
    throw new Error(`unsupported capability source ${value.source}`);
  }
  if (value.sourceId !== undefined) {
    if (value.source === undefined) throw new Error("capability sourceId requires source");
    if (value.sourceId.length < 1 || value.sourceId.length > 256) {
      throw new Error("capability sourceId must contain 1 to 256 characters");
    }
  }
  if (value.invocation !== undefined && !INVOCATIONS.has(value.invocation)) {
    throw new Error(`unsupported capability invocation ${value.invocation}`);
  }
  return Object.freeze({ ...value });
}

export function capabilityToDict(value: CapabilityIdentity): Record<string, string> {
  const valid = validateCapability(value);
  const result: Record<string, string> = { name: valid.name, kind: valid.kind };
  if (valid.namespace !== undefined) result["namespace"] = valid.namespace;
  if (valid.version !== undefined) result["version"] = valid.version;
  if (valid.source !== undefined) result["source"] = valid.source;
  if (valid.sourceId !== undefined) result["source_id"] = valid.sourceId;
  if (valid.invocation !== undefined) result["invocation"] = valid.invocation;
  return result;
}

export function getCapability(): CapabilityIdentity | undefined {
  return capabilityStore.getStore();
}

/** Set or clear capability attribution for the remaining async context. */
export function setCapability(capability?: CapabilityIdentity): CapabilityIdentity | undefined {
  const previous = capabilityStore.getStore();
  capabilityStore.enterWith(capability === undefined ? undefined : validateCapability(capability));
  return previous;
}

export function runWithCapability<T>(capability: CapabilityIdentity, fn: () => T): T {
  return capabilityStore.run(validateCapability(capability), fn);
}

export function canonicalToolCapabilityName(name: string): string {
  const normalized = name.trim().toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[._-]+/, "");
  if (normalized.length > 0 && normalized === name && normalized.length <= 128) return normalized;
  const digest = createHash("sha256").update(name, "utf8").digest("hex").slice(0, 12);
  const base = normalized.slice(0, 115).replace(/[._-]+$/, "") || "tool";
  return `${base}-${digest}`;
}

export function defaultToolCapability(name: string): CapabilityIdentity {
  return validateCapability({
    name: canonicalToolCapabilityName(name), kind: "tool", invocation: "explicit",
  });
}

export function applyEventCapability(
  event: CostEvent,
  capability: CapabilityIdentity | undefined = getCapability(),
): CostEvent {
  if (capability !== undefined) {
    event.details = { ...event.details, attribution_capability: capabilityToDict(capability) };
  }
  return event;
}

/** Apply both ambient capability and the privacy-safe idempotency hash. */
export function stampAmbientAttribution(event: CostEvent): CostEvent {
  applyEventCapability(event);
  applyEventIdempotency(event);
  return event;
}
