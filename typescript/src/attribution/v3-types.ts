import type {
  AttributionComponent,
  AttributionConfidence,
  AttributionCostEvidenceSource,
  AttributionLifecycleState,
  AttributionResourceV2,
} from "./types.js";

export const ATTRIBUTION_V3_CONTRACT_VERSION = "3.2.0";

export type AttributionBillingDimensionValue =
  | { type: "string"; value: string }
  | { type: "boolean"; value: boolean }
  | { type: "integer"; value: string }
  | { type: "decimal"; value: string };

export interface AttributionBillingDimension {
  key: string;
  value: AttributionBillingDimensionValue;
}

/**
 * V3 meters are intentionally extensible. The control plane prices known
 * canonical meters and retains future provider meters as visibly unpriced.
 */
export type AttributionUsageMetricV3 = string;
export type AttributionUsageUnitV3 = string;

export interface AttributionUsageLineV3 {
  /** Stable across full-snapshot revisions for this metric/dimension stream. */
  line_id: string;
  metric: AttributionUsageMetricV3;
  quantity: string;
  unit: AttributionUsageUnitV3;
  dimensions: AttributionBillingDimension[];
}

export interface AttributionProviderIdentityV3 {
  name: string;
  service: string;
  record_id?: string;
  region?: string;
}

export interface AttributionResourceV3 {
  type: AttributionResourceV2["type"] | "tool";
  id: string;
}

export interface AttributionTraceIdentityV3 {
  trace_id: string;
  span_id: string;
}

export interface AttributionAttemptIdentityV3 {
  id: string;
  number: number;
  retry_of?: string;
}

export type AttributionOperationStatusV3 =
  | "in_progress"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "unknown";

export interface AttributionOperationErrorV3 {
  type: string;
  code?: string;
}

export interface AttributionOperationIdentityV3 {
  id: string;
  name: string;
  status: AttributionOperationStatusV3;
  attempt: AttributionAttemptIdentityV3;
  trace?: AttributionTraceIdentityV3;
  latency_ms?: number;
  error?: AttributionOperationErrorV3;
}

export type AttributionCapabilityKindV3 =
  | "tool" | "skill" | "workflow" | "extension" | "other";
export type AttributionCapabilitySourceV3 =
  | "built_in" | "project" | "user" | "plugin" | "marketplace" | "remote" | "other";
export type AttributionCapabilityInvocationV3 =
  | "explicit" | "automatic" | "nested" | "scheduled" | "remote" | "other";

export interface AttributionCapabilityIdentityV3 {
  name: string;
  kind: AttributionCapabilityKindV3;
  namespace?: string;
  version?: string;
  source?: AttributionCapabilitySourceV3;
  source_id?: string;
  invocation?: AttributionCapabilityInvocationV3;
}

export interface AttributionCostEvidenceV3 {
  amount: string;
  currency: string;
  source: AttributionCostEvidenceSource;
  confidence: AttributionConfidence;
  pricing_version?: string;
}

export interface AttributionLifecycleV3 {
  state: AttributionLifecycleState;
  revision: number;
}

export interface AttributionUsagePeriodV3 {
  start_at: string;
  end_at?: string;
}

/** Canonical attribution observation revision accepted by `/v1/ingest`. */
export interface AttributionObservationV3 {
  schema_version: "3";
  event_id: string;
  task_id: string;
  occurred_at: string;
  observed_at: string;
  environment?: string;
  component: AttributionComponent;
  provider: AttributionProviderIdentityV3;
  resource?: AttributionResourceV3;
  capability?: AttributionCapabilityIdentityV3;
  operation: AttributionOperationIdentityV3;
  lifecycle: AttributionLifecycleV3;
  usage_snapshot: "full";
  usage_period?: AttributionUsagePeriodV3;
  usage: AttributionUsageLineV3[];
  cost_evidence?: AttributionCostEvidenceV3;
}

/** Compatibility-free v3 wire name used by the transport. */
export type AttributionEventV3 = AttributionObservationV3;
