import { Decimal, canonicalDecimal } from "../core/models.js";
import type { CostEvent } from "../core/models.js";

export type PricingExplanationStatus = "provider_reported" | "provisional" | "unpriced";

export interface PricingProvenanceOptions {
  catalogSource: string;
  stale: boolean;
  releaseId?: string;
  releaseSequence?: number;
  artifactKind?: string;
  artifactSha256?: string;
  artifactSchemaVersion?: string;
  observerRulesSha256?: string;
  safetyPolicyVersion?: string;
  workspaceOverlay?: boolean;
}

export class PricingProvenance {
  readonly catalogSource: string;
  readonly stale: boolean;
  readonly releaseId?: string;
  readonly releaseSequence?: number;
  readonly artifactKind?: string;
  readonly artifactSha256?: string;
  readonly artifactSchemaVersion?: string;
  readonly observerRulesSha256?: string;
  readonly safetyPolicyVersion?: string;
  readonly workspaceOverlay: boolean;

  constructor(options: PricingProvenanceOptions) {
    if (typeof options.catalogSource !== "string" || options.catalogSource.length === 0) {
      throw new TypeError("catalogSource must be a non-empty string");
    }
    if (typeof options.stale !== "boolean" ||
        (options.workspaceOverlay !== undefined && typeof options.workspaceOverlay !== "boolean")) {
      throw new TypeError("pricing provenance flags must be booleans");
    }
    if (options.releaseSequence !== undefined &&
        (!Number.isSafeInteger(options.releaseSequence) || options.releaseSequence < 1)) {
      throw new TypeError("releaseSequence must be a positive integer");
    }
    for (const [name, value] of Object.entries({
      releaseId: options.releaseId,
      artifactKind: options.artifactKind,
      artifactSchemaVersion: options.artifactSchemaVersion,
      safetyPolicyVersion: options.safetyPolicyVersion,
    })) if (value !== undefined && (typeof value !== "string" || value.length === 0)) {
      throw new TypeError(`${name} must be a non-empty string`);
    }
    for (const [name, value] of Object.entries({
      artifactSha256: options.artifactSha256, observerRulesSha256: options.observerRulesSha256,
    })) if (value !== undefined && !/^[0-9a-f]{64}$/.test(value)) {
      throw new TypeError(`${name} must be a lowercase SHA-256 digest`);
    }
    Object.assign(this, options);
    this.catalogSource = options.catalogSource;
    this.stale = options.stale;
    this.workspaceOverlay = options.workspaceOverlay ?? false;
  }

  toDict(): Record<string, unknown> {
    return {
      catalog_source: this.catalogSource,
      stale: this.stale,
      workspace_overlay: this.workspaceOverlay,
      ...(this.releaseId === undefined ? {} : { release_id: this.releaseId }),
      ...(this.releaseSequence === undefined ? {} : { release_sequence: this.releaseSequence }),
      ...(this.artifactKind === undefined ? {} : { artifact_kind: this.artifactKind }),
      ...(this.artifactSha256 === undefined ? {} : { artifact_sha256: this.artifactSha256 }),
      ...(this.artifactSchemaVersion === undefined ? {} : { artifact_schema_version: this.artifactSchemaVersion }),
      ...(this.observerRulesSha256 === undefined ? {} : { observer_rules_sha256: this.observerRulesSha256 }),
      ...(this.safetyPolicyVersion === undefined ? {} : { safety_policy_version: this.safetyPolicyVersion }),
    };
  }

  static fromDict(value: unknown): PricingProvenance {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new TypeError("pricing provenance must be an object");
    }
    const row = value as Record<string, unknown>;
    const allowed = new Set([
      "catalog_source", "stale", "workspace_overlay", "release_id", "release_sequence",
      "artifact_kind", "artifact_sha256", "artifact_schema_version", "observer_rules_sha256",
      "safety_policy_version",
    ]);
    const unknown = Object.keys(row).filter((key) => !allowed.has(key));
    if (unknown.length > 0) throw new TypeError(`unknown pricing provenance fields: ${unknown.sort().join(", ")}`);
    return new PricingProvenance({
      catalogSource: row.catalog_source as string,
      stale: row.stale as boolean,
      workspaceOverlay: (row.workspace_overlay ?? false) as boolean,
      releaseId: row.release_id as string | undefined,
      releaseSequence: row.release_sequence as number | undefined,
      artifactKind: row.artifact_kind as string | undefined,
      artifactSha256: row.artifact_sha256 as string | undefined,
      artifactSchemaVersion: row.artifact_schema_version as string | undefined,
      observerRulesSha256: row.observer_rules_sha256 as string | undefined,
      safetyPolicyVersion: row.safety_policy_version as string | undefined,
    });
  }
}

export interface PricingExplanationOptions {
  eventId: string;
  taskId: string;
  eventType: string;
  component: string;
  status: PricingExplanationStatus;
  amountUsd: Decimal;
  confidence: string;
  pricingSource?: string;
  pricingVersion?: string;
  selectedRule?: string;
  inputs?: ReadonlyArray<readonly [string, string]>;
  provenance?: PricingProvenance;
}

export class PricingExplanation {
  readonly authority = "sdk_evidence" as const;
  readonly eventId: string;
  readonly taskId: string;
  readonly eventType: string;
  readonly component: string;
  readonly status: PricingExplanationStatus;
  readonly amountUsd: Decimal;
  readonly confidence: string;
  readonly pricingSource?: string;
  readonly pricingVersion?: string;
  readonly selectedRule?: string;
  readonly inputs: ReadonlyArray<readonly [string, string]>;
  readonly provenance?: PricingProvenance;

  constructor(options: PricingExplanationOptions) {
    Object.assign(this, options);
    this.eventId = options.eventId;
    this.taskId = options.taskId;
    this.eventType = options.eventType;
    this.component = options.component;
    this.status = options.status;
    this.amountUsd = options.amountUsd;
    this.confidence = options.confidence;
    this.inputs = options.inputs ?? [];
  }

  toDict(): Record<string, unknown> {
    return {
      event_id: this.eventId, task_id: this.taskId, event_type: this.eventType,
      component: this.component, status: this.status, authority: this.authority,
      amount_usd: canonicalDecimal(this.amountUsd), confidence: this.confidence,
      pricing_source: this.pricingSource ?? null, pricing_version: this.pricingVersion ?? null,
      selected_rule: this.selectedRule ?? null, inputs: Object.fromEntries(this.inputs),
      provenance: this.provenance?.toDict() ?? null,
    };
  }
}

const registry = new Map<string, PricingProvenance>();
const REGISTRY_LIMIT = 128;
const RELEASE_VERSION = /^(?:(compute|gpu|egress):)?catalog-release:([1-9][0-9]*):([0-9a-f]{12})(:stale)?/;

export function registerPricingProvenance(version: string, provenance: PricingProvenance): void {
  if (typeof version !== "string" || version.length === 0) throw new TypeError("pricing version must be a non-empty string");
  registry.delete(version);
  registry.set(version, provenance);
  while (registry.size > REGISTRY_LIMIT) registry.delete(registry.keys().next().value!);
}

function inferredProvenance(event: CostEvent): PricingProvenance | undefined {
  if (event.pricingVersion === undefined) return undefined;
  const match = RELEASE_VERSION.exec(event.pricingVersion);
  if (match === null) return new PricingProvenance({
    catalogSource: "local_or_bootstrap", stale: false,
    artifactKind: event.eventType === "llm_call" ? "llm_prices"
      : event.pricingSource === "service_catalog" ? "service_prices" : undefined,
    workspaceOverlay: event.pricingSource?.startsWith("workspace_overlay") ?? false,
  });
  const kind = match[1];
  return new PricingProvenance({
    catalogSource: "catalog_release", stale: match[4] !== undefined,
    releaseSequence: Number(match[2]),
    artifactKind: kind === "compute" ? "compute_prices" : kind === "gpu" ? "gpu_prices"
      : kind === "egress" ? "egress_prices" : "llm_prices",
    workspaceOverlay: event.pricingSource?.startsWith("workspace_overlay") ?? false,
  });
}

export function pricingProvenanceForEvent(event: CostEvent): PricingProvenance | undefined {
  const durable = event.details.pricing_provenance;
  if (durable !== undefined) return PricingProvenance.fromDict(durable);
  if (event.pricingVersion !== undefined && registry.has(event.pricingVersion)) return registry.get(event.pricingVersion);
  return inferredProvenance(event);
}

export function applyEventPricingProvenance(event: CostEvent): CostEvent {
  const provenance = pricingProvenanceForEvent(event);
  if (provenance !== undefined) event.details = { ...event.details, pricing_provenance: provenance.toDict() };
  return event;
}

function component(event: CostEvent): string {
  const explicit = event.details.attribution_component;
  if (typeof explicit === "string" && explicit.length > 0) return explicit;
  return event.eventType === "llm_call" ? "llm" : event.eventType === "compute_cost" ? "compute"
    : event.eventType === "gpu_cost" ? "gpu" : event.eventType === "network" ? "network" : "external";
}

function pricingInputs(event: CostEvent): Array<readonly [string, string]> {
  const values: Record<string, string> = {};
  if (event.model !== undefined) values.model = event.model;
  if (event.serviceName !== undefined) values.service = event.serviceName;
  for (const [key, value] of Object.entries({
    input_tokens: event.inputTokens, output_tokens: event.outputTokens,
    cached_tokens: event.cachedTokens, latency_ms: event.latencyMs,
  })) if (value !== undefined) values[key] = String(value);
  for (const key of [
    "attribution_usage_metric", "attribution_usage_quantity", "attribution_usage_unit",
    "attribution_usage_duration_seconds", "cache_creation_input_tokens", "billing_model",
    "cloud_provider", "region", "gpu_sku", "runtime",
  ]) {
    const value = event.details[key];
    if (typeof value === "string" || typeof value === "number" || typeof value === "bigint" || value instanceof Decimal) {
      values[key] = value instanceof Decimal ? canonicalDecimal(value) : String(value);
    }
  }
  return Object.entries(values).sort(([left], [right]) => left.localeCompare(right));
}

export function explainEventPricing(event: CostEvent): PricingExplanation {
  const source = event.pricingSource;
  const sourceText = source as string | undefined;
  const status: PricingExplanationStatus = sourceText === "provider_response" || sourceText === "provider_reported"
    ? "provider_reported"
    : event.costConfidence === "unknown" || sourceText === undefined || sourceText === "unknown" || sourceText.startsWith("unpriced:")
      ? "unpriced" : "provisional";
  return new PricingExplanation({
    eventId: event.eventId, taskId: event.taskId, eventType: event.eventType,
    component: component(event), status, amountUsd: event.costUsd,
    confidence: event.costConfidence, pricingSource: source, pricingVersion: event.pricingVersion,
    selectedRule: source, inputs: pricingInputs(event), provenance: pricingProvenanceForEvent(event),
  });
}
