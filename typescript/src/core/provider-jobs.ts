import { createHash } from "node:crypto";
import { Decimal, canonicalDecimal, isoCanonical } from "./models.js";
import type { CapabilityIdentity } from "./capabilities.js";
import { capabilityToDict } from "./capabilities.js";

export type ProviderJobStatus =
  | "submitted"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "unknown";
export type ProviderJobCostSource = "provider_reported" | "sdk_catalog" | "sdk_rate_registry" | "manual";
export type ProviderJobCostConfidence = "exact" | "computed" | "estimated" | "unknown";

export interface ProviderJobUsageLine {
  metric: string;
  quantity: Decimal;
  unit: string;
}

export interface ProviderJobRevisionOptions {
  eventId?: string;
  revision?: number;
  taskId: string;
  provider: string;
  service: string;
  providerRecordId: string;
  operation: string;
  component: string;
  eventType: "llm_call" | "external_cost" | "compute_cost";
  resourceType: "model" | "sku" | "instance" | "endpoint" | "session" | "other" | "tool";
  resourceId: string;
  status: ProviderJobStatus;
  submittedAt?: Date;
  observedAt?: Date;
  ownsTask?: boolean;
  billingDimensions?: ReadonlyArray<readonly [string, string]>;
  usage?: ReadonlyArray<ProviderJobUsageLine>;
  costAmount?: Decimal;
  costSource?: ProviderJobCostSource;
  costConfidence?: ProviderJobCostConfidence;
  pricingVersion?: string;
  latencyMs?: number;
  errorType?: string;
  errorCode?: string;
  taskInputTokens?: number;
  taskOutputTokens?: number;
  taskCachedTokens?: number;
  capability?: CapabilityIdentity;
}

const CANONICAL = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const UNIT = /^[A-Za-z0-9][A-Za-z0-9._{}/*^+-]{0,63}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function deterministicUuid(...parts: string[]): string {
  const chars = createHash("sha256").update(parts.join("\0"), "utf8").digest("hex").slice(0, 32).split("");
  chars[12] = "5";
  chars[16] = ((Number.parseInt(chars[16] ?? "0", 16) & 0x3) | 0x8).toString(16);
  const raw = chars.join("");
  return `${raw.slice(0, 8)}-${raw.slice(8, 12)}-${raw.slice(12, 16)}-${raw.slice(16, 20)}-${raw.slice(20)}`;
}

export function providerJobEventId(provider: string, service: string, recordId: string): string {
  return deterministicUuid("dexcost:provider-job:v1", provider, service, recordId);
}

export class ProviderJobRevision {
  readonly schemaVersion = "1";
  readonly eventId: string;
  readonly revision: number;
  readonly taskId: string;
  readonly provider: string;
  readonly service: string;
  readonly providerRecordId: string;
  readonly operation: string;
  readonly component: string;
  readonly eventType: "llm_call" | "external_cost" | "compute_cost";
  readonly resourceType: ProviderJobRevisionOptions["resourceType"];
  readonly resourceId: string;
  readonly status: ProviderJobStatus;
  readonly submittedAt: Date;
  readonly observedAt: Date;
  readonly ownsTask: boolean;
  readonly billingDimensions: ReadonlyArray<readonly [string, string]>;
  readonly usage: ReadonlyArray<ProviderJobUsageLine>;
  readonly costAmount?: Decimal;
  readonly costSource?: ProviderJobCostSource;
  readonly costConfidence?: ProviderJobCostConfidence;
  readonly pricingVersion?: string;
  readonly latencyMs?: number;
  readonly errorType?: string;
  readonly errorCode?: string;
  readonly taskInputTokens?: number;
  readonly taskOutputTokens?: number;
  readonly taskCachedTokens?: number;
  readonly capability?: CapabilityIdentity;

  constructor(options: ProviderJobRevisionOptions) {
    for (const [name, value] of [["provider", options.provider], ["service", options.service], ["operation", options.operation], ["component", options.component]] as const) {
      if (!CANONICAL.test(value)) throw new Error(`${name} must be canonical`);
    }
    if (!UUID.test(options.taskId)) throw new Error("taskId must be a UUID");
    if (options.providerRecordId.length < 1 || options.providerRecordId.length > 256) {
      throw new Error("providerRecordId must contain 1 to 256 characters");
    }
    this.provider = options.provider;
    this.service = options.service;
    this.providerRecordId = options.providerRecordId;
    this.eventId = options.eventId ?? providerJobEventId(this.provider, this.service, this.providerRecordId);
    if (!UUID.test(this.eventId)) throw new Error("provider job eventId must be a UUID");
    this.revision = options.revision ?? 1;
    if (!Number.isInteger(this.revision) || this.revision < 1 || this.revision > 2_147_483_647) {
      throw new Error("provider job revision must be between 1 and 2147483647");
    }
    this.taskId = options.taskId;
    this.operation = options.operation;
    this.component = options.component;
    this.eventType = options.eventType;
    this.resourceType = options.resourceType;
    if (!["model", "sku", "instance", "endpoint", "session", "other", "tool"].includes(this.resourceType)) {
      throw new Error("unsupported provider job resourceType");
    }
    this.resourceId = options.resourceId;
    if (this.resourceId.length < 1 || this.resourceId.length > 256) {
      throw new Error("resourceId must contain 1 to 256 characters");
    }
    this.status = options.status;
    if (!["submitted", "running", "succeeded", "failed", "cancelled", "unknown"].includes(this.status)) {
      throw new Error("unsupported provider job status");
    }
    this.submittedAt = options.submittedAt ?? new Date();
    this.observedAt = options.observedAt ?? new Date();
    if (!Number.isFinite(this.submittedAt.getTime()) || !Number.isFinite(this.observedAt.getTime())) {
      throw new Error("provider job timestamps must be valid dates");
    }
    if (this.observedAt.getTime() < this.submittedAt.getTime()) {
      throw new Error("provider job observedAt cannot precede submittedAt");
    }
    this.ownsTask = options.ownsTask ?? false;
    this.billingDimensions = options.billingDimensions ?? [];
    if (this.billingDimensions.length > 24) throw new Error("provider job supports at most 24 billing dimensions");
    const dimensionKeys = new Set<string>();
    for (const [key, value] of this.billingDimensions) {
      if (!CANONICAL.test(key)) throw new Error("provider job billing dimension key must be canonical");
      if (value.length < 1 || value.length > 256) throw new Error("provider job billing dimension value must contain 1 to 256 characters");
      if (dimensionKeys.has(key)) throw new Error(`duplicate provider job billing dimension ${key}`);
      dimensionKeys.add(key);
    }
    this.usage = options.usage ?? [];
    const usageIdentities = new Set<string>();
    for (const line of this.usage) {
      if (!CANONICAL.test(line.metric) || !UNIT.test(line.unit)) throw new Error("provider job usage line is invalid");
      if (!line.quantity.isFinite() || !line.quantity.gt(0)) throw new Error("provider job usage quantities must be positive");
      const identity = `${line.metric}\0${line.unit}`;
      if (usageIdentities.has(identity)) throw new Error(`duplicate provider job usage line ${line.metric}/${line.unit}`);
      usageIdentities.add(identity);
    }
    this.costAmount = options.costAmount;
    this.costSource = options.costSource;
    this.costConfidence = options.costConfidence;
    this.pricingVersion = options.pricingVersion;
    this.latencyMs = options.latencyMs;
    this.errorType = options.errorType;
    this.errorCode = options.errorCode;
    this.taskInputTokens = options.taskInputTokens;
    this.taskOutputTokens = options.taskOutputTokens;
    this.taskCachedTokens = options.taskCachedTokens;
    this.capability = options.capability;
    const pending = this.status === "submitted" || this.status === "running";
    if (pending && this.usage.length > 0) throw new Error("pending provider jobs cannot assert usage");
    if (pending && [this.costAmount, this.costSource, this.costConfidence, this.pricingVersion].some((value) => value !== undefined)) {
      throw new Error("pending provider jobs cannot assert cost evidence");
    }
    if (this.status === "succeeded" && this.usage.length === 0) {
      throw new Error("successful provider jobs require provider-observed usage");
    }
    const costFields = [this.costAmount, this.costSource, this.costConfidence];
    if (costFields.some((value) => value !== undefined) && !costFields.every((value) => value !== undefined)) {
      throw new Error("provider job cost amount, source, and confidence are atomic");
    }
    if (this.costAmount !== undefined) {
      if (!this.costAmount.isFinite() || !this.costAmount.gt(0)) throw new Error("provider job cost evidence must be positive");
      if (this.costSource === "provider_reported" && !["exact", "estimated"].includes(this.costConfidence!)) {
        throw new Error("provider-reported cost must be exact or estimated");
      }
      if (["sdk_catalog", "sdk_rate_registry"].includes(this.costSource!) &&
          (this.costConfidence === "exact" || this.pricingVersion === undefined)) {
        throw new Error("SDK provider-job cost requires non-exact confidence and pricingVersion");
      }
    } else if (this.pricingVersion !== undefined) {
      throw new Error("pricingVersion requires cost evidence");
    }
    if (this.latencyMs !== undefined && (!Number.isSafeInteger(this.latencyMs) || this.latencyMs < 0 || this.latencyMs > 86_400_000)) {
      throw new Error("latencyMs must be between 0 and 86400000");
    }
    if (this.errorType !== undefined && !CANONICAL.test(this.errorType)) throw new Error("errorType must be canonical");
    if (this.errorCode !== undefined && (this.errorCode.length < 1 || this.errorCode.length > 64)) {
      throw new Error("errorCode must contain 1 to 64 characters");
    }
    for (const [name, value] of [["taskInputTokens", this.taskInputTokens], ["taskOutputTokens", this.taskOutputTokens], ["taskCachedTokens", this.taskCachedTokens]] as const) {
      if (value !== undefined && (!Number.isSafeInteger(value) || value < 0)) throw new Error(`${name} must be a non-negative integer`);
    }
  }

  get terminal(): boolean {
    return ["succeeded", "failed", "cancelled", "unknown"].includes(this.status);
  }

  toDict(): Record<string, unknown> {
    const result: Record<string, unknown> = {
      schema_version: "1",
      event_id: this.eventId,
      revision: this.revision,
      task_id: this.taskId,
      provider: this.provider,
      service: this.service,
      provider_record_id: this.providerRecordId,
      operation: this.operation,
      component: this.component,
      event_type: this.eventType,
      resource_type: this.resourceType,
      resource_id: this.resourceId,
      status: this.status,
      submitted_at: isoCanonical(this.submittedAt),
      observed_at: isoCanonical(this.observedAt),
      owns_task: this.ownsTask,
      billing_dimensions: this.billingDimensions.map(([key, value]) => ({ key, value })),
      usage: this.usage.map((line) => ({
        metric: line.metric, quantity: canonicalDecimal(line.quantity), unit: line.unit,
      })),
    };
    if (this.costAmount !== undefined) result["cost_amount"] = canonicalDecimal(this.costAmount);
    if (this.costSource !== undefined) result["cost_source"] = this.costSource;
    if (this.costConfidence !== undefined) result["cost_confidence"] = this.costConfidence;
    if (this.pricingVersion !== undefined) result["pricing_version"] = this.pricingVersion;
    if (this.latencyMs !== undefined) result["latency_ms"] = this.latencyMs;
    if (this.errorType !== undefined) result["error_type"] = this.errorType;
    if (this.errorCode !== undefined) result["error_code"] = this.errorCode;
    if (this.taskInputTokens !== undefined) result["task_input_tokens"] = this.taskInputTokens;
    if (this.taskOutputTokens !== undefined) result["task_output_tokens"] = this.taskOutputTokens;
    if (this.taskCachedTokens !== undefined) result["task_cached_tokens"] = this.taskCachedTokens;
    if (this.capability !== undefined) result["capability"] = capabilityToDict(this.capability);
    return result;
  }

  toAttributionObservation(environment?: string): Record<string, unknown> {
    const operationStatus = this.status === "submitted" || this.status === "running"
      ? "in_progress"
      : this.status;
    const dimensions = this.billingDimensions.map(([key, value]) => ({
      key, value: { type: "string", value },
    }));
    const result: Record<string, unknown> = {
      schema_version: "3",
      event_id: this.eventId,
      task_id: this.taskId,
      occurred_at: isoCanonical(this.submittedAt),
      observed_at: isoCanonical(this.observedAt),
      component: this.component,
      provider: { name: this.provider, service: this.service, record_id: this.providerRecordId },
      resource: { type: this.resourceType, id: this.resourceId },
      operation: {
        id: this.eventId,
        name: this.operation,
        status: operationStatus,
        attempt: { id: this.eventId, number: 1 },
        ...(this.latencyMs === undefined ? {} : { latency_ms: this.latencyMs }),
        ...(this.errorType === undefined ? {} : {
          error: { type: this.errorType, ...(this.errorCode === undefined ? {} : { code: this.errorCode }) },
        }),
      },
      lifecycle: { state: this.terminal ? "final" : "provisional", revision: this.revision },
      usage_snapshot: "full",
      usage_period: {
        start_at: isoCanonical(this.submittedAt),
        ...(this.terminal ? { end_at: isoCanonical(this.observedAt) } : {}),
      },
      usage: this.usage.map((line) => ({
        line_id: deterministicUuid(this.eventId, line.metric, line.unit, JSON.stringify(dimensions)),
        metric: line.metric,
        quantity: canonicalDecimal(line.quantity),
        unit: line.unit,
        dimensions,
      })),
    };
    if (environment !== undefined) result["environment"] = environment;
    if (this.capability !== undefined) result["capability"] = capabilityToDict(this.capability);
    if (this.costAmount !== undefined) {
      result["cost_evidence"] = {
        amount: canonicalDecimal(this.costAmount), currency: "USD",
        source: this.costSource, confidence: this.costConfidence,
        ...(this.pricingVersion === undefined ? {} : { pricing_version: this.pricingVersion }),
      };
    }
    return result;
  }
}

export function providerJobFromDict(value: Record<string, unknown>): ProviderJobRevision {
  const lifecycle = value["lifecycle"] as Record<string, unknown> | undefined;
  const usage = Array.isArray(value["usage"]) ? value["usage"] : [];
  const dimensions = Array.isArray(value["billing_dimensions"]) ? value["billing_dimensions"] : [];
  const capability = value["capability"] as Record<string, unknown> | undefined;
  return new ProviderJobRevision({
    eventId: String(value["event_id"]),
    revision: Number(value["revision"] ?? lifecycle?.["revision"] ?? 1),
    taskId: String(value["task_id"]),
    provider: String(value["provider"]),
    service: String(value["service"]),
    providerRecordId: String(value["provider_record_id"]),
    operation: String(value["operation"]),
    component: String(value["component"]),
    eventType: value["event_type"] as ProviderJobRevisionOptions["eventType"],
    resourceType: value["resource_type"] as ProviderJobRevisionOptions["resourceType"],
    resourceId: String(value["resource_id"]),
    status: value["status"] as ProviderJobStatus,
    submittedAt: new Date(String(value["submitted_at"])),
    observedAt: new Date(String(value["observed_at"])),
    ownsTask: value["owns_task"] === true,
    billingDimensions: dimensions.map((item) => {
      const row = item as Record<string, unknown>;
      return [String(row["key"]), String(row["value"])] as const;
    }),
    usage: usage.map((item) => {
      const row = item as Record<string, unknown>;
      return { metric: String(row["metric"]), quantity: new Decimal(String(row["quantity"])), unit: String(row["unit"]) };
    }),
    costAmount: value["cost_amount"] === undefined ? undefined : new Decimal(String(value["cost_amount"])),
    costSource: value["cost_source"] as ProviderJobCostSource | undefined,
    costConfidence: value["cost_confidence"] as ProviderJobCostConfidence | undefined,
    pricingVersion: value["pricing_version"] as string | undefined,
    latencyMs: value["latency_ms"] as number | undefined,
    errorType: value["error_type"] as string | undefined,
    errorCode: value["error_code"] as string | undefined,
    taskInputTokens: value["task_input_tokens"] as number | undefined,
    taskOutputTokens: value["task_output_tokens"] as number | undefined,
    taskCachedTokens: value["task_cached_tokens"] as number | undefined,
    capability: capability === undefined ? undefined : {
      name: String(capability["name"]),
      kind: capability["kind"] as CapabilityIdentity["kind"],
      namespace: capability["namespace"] as string | undefined,
      version: capability["version"] as string | undefined,
      source: capability["source"] as CapabilityIdentity["source"],
      sourceId: capability["source_id"] as string | undefined,
      invocation: capability["invocation"] as CapabilityIdentity["invocation"],
    },
  });
}
