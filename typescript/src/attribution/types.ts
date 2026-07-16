export const ATTRIBUTION_V2_CONTRACT_VERSION = "2.0.0";

export const ATTRIBUTION_COMPONENTS = [
  "llm",
  "telephony",
  "voice_platform",
  "speech_to_text",
  "text_to_speech",
  "realtime_transport",
  "recording",
  "post_call_analysis",
  "compute",
  "gpu",
  "network",
  "storage",
  "external",
] as const;

export type AttributionComponent = (typeof ATTRIBUTION_COMPONENTS)[number];

export const ATTRIBUTION_USAGE_METRICS = [
  "input_tokens",
  "output_tokens",
  "cache_read_input_tokens",
  "cache_write_input_tokens",
  "reasoning_output_tokens",
  "characters",
  "audio_seconds",
  "connected_seconds",
  "recording_seconds",
  "agent_seconds",
  "compute_seconds",
  "vcpu_seconds",
  "memory_gib_seconds",
  "gpu_seconds",
  "request_count",
  "call_count",
  "bytes_in",
  "bytes_out",
  "image_count",
  "page_count",
  "credit_count",
] as const;

export type AttributionUsageMetric = (typeof ATTRIBUTION_USAGE_METRICS)[number];

export const ATTRIBUTION_USAGE_UNITS = [
  "Tokens",
  "Characters",
  "Seconds",
  "vCPU-Seconds",
  "GiB-Seconds",
  "GPU-Seconds",
  "Requests",
  "Calls",
  "Bytes",
  "Images",
  "Pages",
  "Credits",
] as const;

export type AttributionUsageUnit = (typeof ATTRIBUTION_USAGE_UNITS)[number];

export const ATTRIBUTION_UNIT_BY_METRIC: Readonly<
  Record<AttributionUsageMetric, AttributionUsageUnit>
> = Object.freeze({
  input_tokens: "Tokens",
  output_tokens: "Tokens",
  cache_read_input_tokens: "Tokens",
  cache_write_input_tokens: "Tokens",
  reasoning_output_tokens: "Tokens",
  characters: "Characters",
  audio_seconds: "Seconds",
  connected_seconds: "Seconds",
  recording_seconds: "Seconds",
  agent_seconds: "Seconds",
  compute_seconds: "Seconds",
  vcpu_seconds: "vCPU-Seconds",
  memory_gib_seconds: "GiB-Seconds",
  gpu_seconds: "GPU-Seconds",
  request_count: "Requests",
  call_count: "Calls",
  bytes_in: "Bytes",
  bytes_out: "Bytes",
  image_count: "Images",
  page_count: "Pages",
  credit_count: "Credits",
});

export type AttributionConfidence = "exact" | "computed" | "estimated" | "unknown";
export type AttributionLifecycleState = "pending" | "provisional" | "final" | "voided";
export type AttributionCostEvidenceSource =
  | "provider_reported"
  | "sdk_catalog"
  | "sdk_rate_registry"
  | "manual";

export interface AttributionUsageLineV2 {
  metric: AttributionUsageMetric;
  quantity: string;
  unit: AttributionUsageUnit;
}

export interface AttributionProviderIdentityV2 {
  name: string;
  service: string;
  record_id?: string;
  region?: string;
}

export interface AttributionResourceV2 {
  type: "model" | "sku" | "instance" | "endpoint" | "session" | "other";
  id: string;
}

export interface AttributionCostEvidenceV2 {
  amount: string;
  currency: string;
  source: AttributionCostEvidenceSource;
  confidence: AttributionConfidence;
  pricing_version?: string;
}

export interface AttributionLifecycleV2 {
  state: AttributionLifecycleState;
  revision: number;
}

export interface AttributionUsagePeriodV2 {
  start_at: string;
  end_at?: string;
}

export interface AttributionEventV2 {
  schema_version: "2";
  event_id: string;
  task_id: string;
  occurred_at: string;
  observed_at: string;
  component: AttributionComponent;
  provider: AttributionProviderIdentityV2;
  resource?: AttributionResourceV2;
  lifecycle: AttributionLifecycleV2;
  usage_period?: AttributionUsagePeriodV2;
  usage: AttributionUsageLineV2[];
  cost_evidence?: AttributionCostEvidenceV2;
  retry_of?: string;
}

export interface AttributionTaskIngestV1 {
  task_id: string;
  task_type: string;
  status: "pending" | "running" | "success" | "failed";
  started_at: string;
  ended_at: string | null;
  metadata: Record<string, unknown>;
  customer_id: string | null;
  project_id: string | null;
  parent_task_id: string | null;
  experiment_id: string | null;
  variant: string | null;
  schema_version: "1";
}
