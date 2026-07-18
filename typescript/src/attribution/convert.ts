import {
  Decimal,
  canonicalDecimal,
  isoCanonical,
  type CostEvent,
  type Task,
} from "../core/models.js";
import {
  ATTRIBUTION_UNIT_BY_METRIC,
  type AttributionComponent,
  type AttributionCostEvidenceV2,
  type AttributionEventV2,
  type AttributionProviderIdentityV2,
  type AttributionResourceV2,
  type AttributionTaskIngestV1,
  type AttributionUsageLineV2,
  type AttributionUsageMetric,
} from "./types.js";
import { validateAttributionEventV2 } from "./validate.js";

const GIB = new Decimal(1024).pow(3);

function numberDetail(details: Record<string, unknown>, ...keys: string[]): number | undefined {
  for (const key of keys) {
    const value = details[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return undefined;
}

function stringDetail(details: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = details[key];
    if (typeof value === "string" && value.trim() !== "") return value;
  }
  return undefined;
}

function canonicalName(value: string | undefined, fallback: string): string {
  const normalized = (value ?? "")
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/[^a-z0-9._-]+/g, "_")
    .replace(/^[_\-.]+|[_\-.]+$/g, "")
    .slice(0, 128);
  return normalized || fallback;
}

function positiveQuantity(value: number | string | Decimal | undefined): string | undefined {
  if (value === undefined) return undefined;
  try {
    const decimal = value instanceof Decimal ? value : new Decimal(String(value));
    if (!decimal.isFinite() || !decimal.gt(0)) return undefined;
    const rounded = decimal.toDecimalPlaces(12);
    if (!rounded.gt(0)) return undefined;
    return canonicalDecimal(rounded);
  } catch {
    return undefined;
  }
}

function usageLine(metric: AttributionUsageMetric, quantity: number | string | Decimal | undefined): AttributionUsageLineV2 | undefined {
  const normalized = positiveQuantity(quantity);
  return normalized === undefined
    ? undefined
    : { metric, quantity: normalized, unit: ATTRIBUTION_UNIT_BY_METRIC[metric] };
}

function compactUsage(lines: Array<AttributionUsageLineV2 | undefined>): AttributionUsageLineV2[] {
  return lines.filter((line): line is AttributionUsageLineV2 => line !== undefined);
}

function providerFor(event: CostEvent): AttributionProviderIdentityV2 {
  const raw = (event.provider ?? "").toLowerCase();
  let name = canonicalName(event.provider, "unknown");
  let service = "api";
  if (raw.includes("openai")) [name, service] = ["openai", "responses"];
  else if (raw.includes("anthropic")) [name, service] = ["anthropic", "messages"];
  else if (raw.includes("bedrock")) [name, service] = ["aws", "bedrock"];
  else if (raw.includes("gemini") || raw === "google") [name, service] = ["google", "generate_content"];
  else if (raw.includes("cohere")) [name, service] = ["cohere", "chat"];
  else if (raw.includes("vercel")) [name, service] = ["vercel", "ai_sdk"];
  else if (raw.includes("langchain")) [name, service] = ["langchain", "chat"];

  if (event.eventType !== "llm_call") {
    const billingModel = stringDetail(event.details, "billing_model");
    const serviceName = event.serviceName;
    if (event.eventType === "compute_cost") {
      if (billingModel?.startsWith("azure")) name = "azure";
      else if (billingModel === "gce" || billingModel?.startsWith("cloud_") || billingModel === "cloud_functions") name = "google_cloud";
      else if (billingModel === "vercel_fluid") name = "vercel";
      else if (billingModel === "k8s_pod") name = "kubernetes";
      else if (billingModel === "lambda" || billingModel === "fargate" || billingModel === "ec2") name = "aws";
      else name = canonicalName(event.provider, "runtime");
      service = canonicalName(billingModel ?? serviceName, "compute");
    } else if (event.eventType === "gpu_cost") {
      name = canonicalName(stringDetail(event.details, "cloud_provider") ?? event.provider, "runtime");
      service = canonicalName(billingModel, "gpu");
    } else if (event.eventType === "network") {
      name = canonicalName(stringDetail(event.details, "cloud_provider") ?? event.provider, "internet");
      service = "egress";
    } else if (event.eventType === "retry_marker") {
      name = "dexcost";
      service = "retry";
    } else {
      const rawService = serviceName ?? "external";
      if (rawService.startsWith("mcp:")) {
        name = "mcp";
        service = canonicalName(rawService.slice(4), "tool");
      } else if (rawService.includes(".")) {
        name = canonicalName(rawService, "external");
        service = "http_api";
      } else {
        name = canonicalName(event.provider, canonicalName(rawService, "external"));
        service = canonicalName(rawService, "api");
      }
    }
  }

  const provider: AttributionProviderIdentityV2 = { name, service };
  const recordId = stringDetail(event.details, "provider_record_id", "request_id", "call_sid");
  const region = stringDetail(event.details, "region", "cloud_region");
  if (recordId !== undefined && recordId.length <= 256) provider.record_id = recordId;
  if (region !== undefined) provider.region = canonicalName(region, "unknown");
  return provider;
}

function resourceFor(event: CostEvent): AttributionResourceV2 | undefined {
  const explicitType = stringDetail(event.details, "attribution_resource_type");
  const explicitId = stringDetail(event.details, "attribution_resource_id");
  if (
    explicitId !== undefined &&
    explicitType !== undefined &&
    ["model", "sku", "instance", "endpoint", "session", "other"].includes(explicitType)
  ) {
    return { type: explicitType as AttributionResourceV2["type"], id: explicitId.slice(0, 256) };
  }
  if (event.model) return { type: "model", id: event.model.slice(0, 256) };
  if (event.eventType === "gpu_cost") {
    const sku = stringDetail(event.details, "gpu_sku", "instance_type");
    if (sku) return { type: "sku", id: sku.slice(0, 256) };
  }
  if (event.eventType === "compute_cost") {
    const instance = stringDetail(event.details, "instance_type", "architecture");
    if (instance) return { type: "instance", id: instance.slice(0, 256) };
  }
  if (event.eventType === "retry_marker") {
    const reason = event.retryReason?.trim();
    if (reason) return { type: "other", id: reason.slice(0, 256) };
  }
  return undefined;
}

function evidenceFor(event: CostEvent): AttributionCostEvidenceV2 | undefined {
  const amount = positiveQuantity(event.costUsd);
  if (amount === undefined) return undefined;
  if (event.eventType === "retry_marker") {
    return { amount, currency: "USD", source: "manual", confidence: "exact" };
  }
  const source = event.pricingSource;
  if (source === "provider_response") {
    return {
      amount,
      currency: "USD",
      source: "provider_reported",
      confidence: event.costConfidence === "exact" ? "exact" : "estimated",
    };
  }
  if (source === "manual" || source === "custom") {
    return { amount, currency: "USD", source: "manual", confidence: event.costConfidence };
  }
  const isSdkCatalog = source === "service_catalog"
    || source === "litellm"
    || source === "tokencost"
    || source?.startsWith("compute_catalog:") === true
    || source?.startsWith("gpu_catalog:") === true
    || source?.startsWith("egress_catalog:") === true;
  const mapped = source === "rate_registry" ? "sdk_rate_registry"
    : isSdkCatalog ? "sdk_catalog"
      : undefined;
  if (mapped === undefined || !event.pricingVersion) return undefined;
  return {
    amount,
    currency: "USD",
    source: mapped,
    confidence: event.costConfidence === "exact" ? "computed" : event.costConfidence,
    pricing_version: event.pricingVersion,
  };
}

function componentAndUsage(event: CostEvent): {
  component: AttributionComponent;
  usage: AttributionUsageLineV2[];
  durationSeconds?: number;
} | null {
  const details = event.details;
  switch (event.eventType) {
    case "gpu_utilization_signal":
      return null;
    case "retry_marker":
      return {
        component: "external",
        usage: [usageLine("request_count", 1)!],
      };
    case "llm_call": {
      const cached = event.cachedTokens ?? 0;
      const provider = (event.provider ?? "").toLowerCase();
      const cacheCountersAreDisjoint = provider.includes("anthropic")
        || provider.includes("bedrock")
        || provider === "aws";
      const input = cacheCountersAreDisjoint
        ? event.inputTokens
        : Math.max(0, (event.inputTokens ?? 0) - cached);
      const cacheWrite = numberDetail(details, "cache_creation_input_tokens");
      const reasoning = numberDetail(details, "reasoning_output_tokens", "reasoning_tokens");
      const output = reasoning === undefined ? event.outputTokens : Math.max(0, (event.outputTokens ?? 0) - reasoning);
      const usage = compactUsage([
        usageLine("input_tokens", input),
        usageLine("cache_read_input_tokens", cached),
        usageLine("cache_write_input_tokens", cacheWrite),
        usageLine("output_tokens", output),
        usageLine("reasoning_output_tokens", reasoning),
      ]);
      if (usage.length === 0) usage.push(usageLine("request_count", 1)!);
      return { component: "llm", usage };
    }
    case "compute_cost": {
      const durationSeconds = (numberDetail(details, "duration_ms") ?? 0) / 1000
        || numberDetail(details, "wall_clock_seconds") || 0;
      const memoryBytes = numberDetail(details, "memory_bytes_limit")
        ?? numberDetail(details, "memory_bytes_peak");
      return {
        component: "compute",
        durationSeconds,
        usage: compactUsage([
          usageLine("compute_seconds", durationSeconds),
          usageLine("vcpu_seconds", numberDetail(details, "vcpu_seconds_used")),
          usageLine("memory_gib_seconds", memoryBytes === undefined ? undefined : new Decimal(memoryBytes).div(GIB).times(durationSeconds)),
          usageLine("request_count", numberDetail(details, "invocation_count")),
        ]),
      };
    }
    case "gpu_cost": {
      const durationSeconds = (numberDetail(details, "duration_ms") ?? 0) / 1000;
      const measured = numberDetail(details, "gpu_seconds_used");
      const gpuCount = numberDetail(details, "gpu_count") ?? 1;
      const billingModel = stringDetail(details, "billing_model") ?? "";
      const billedSeconds = billingModel === "per_gpu_second_active" ? measured : durationSeconds * gpuCount;
      return { component: "gpu", durationSeconds, usage: compactUsage([usageLine("gpu_seconds", billedSeconds ?? measured)]) };
    }
    case "network":
      return {
        component: "network",
        usage: compactUsage([
          usageLine("bytes_out", numberDetail(details, "request_bytes")),
          usageLine("bytes_in", numberDetail(details, "response_bytes")),
        ]),
      };
    case "external_cost": {
      const explicitQuantity = numberDetail(details, "attribution_usage_quantity");
      const explicitMetric = stringDetail(details, "attribution_usage_metric");
      const explicitComponent = stringDetail(details, "attribution_component");
      const per = canonicalName(stringDetail(details, "attribution_usage_per"), "request");
      const inferredMetric: AttributionUsageMetric = per.includes("page") ? "page_count"
        : per.includes("credit") ? "credit_count"
          : per.includes("image") ? "image_count"
            : per.includes("call") ? "call_count"
              : per.includes("character") ? "characters"
                : "request_count";
      const metric = explicitMetric !== undefined && explicitMetric in ATTRIBUTION_UNIT_BY_METRIC
        ? explicitMetric as AttributionUsageMetric
        : inferredMetric;
      const component: AttributionComponent = explicitComponent === "speech_to_text"
        ? "speech_to_text"
        : "external";
      return {
        component,
        durationSeconds: numberDetail(details, "attribution_usage_duration_seconds"),
        usage: compactUsage([usageLine(metric, explicitQuantity ?? 1)]),
      };
    }
  }
}

/** Convert the SDK's durable v1 capture model into the strict v2 wire event. */
export function toAttributionEventV2(event: CostEvent): AttributionEventV2 | null {
  const mapped = componentAndUsage(event);
  if (mapped === null) return null;
  if (mapped.usage.length === 0) mapped.usage.push(usageLine("request_count", 1)!);
  const occurredAt = isoCanonical(event.occurredAt);
  const converted: AttributionEventV2 = {
    schema_version: "2",
    event_id: event.eventId,
    task_id: event.taskId,
    occurred_at: occurredAt,
    // CostEvent predates observed_at. Using occurred_at is stable across
    // retries; generating "now" here would change the ledger payload hash.
    observed_at: occurredAt,
    component: mapped.component,
    provider: providerFor(event),
    lifecycle: { state: "final", revision: 1 },
    usage: mapped.usage,
  };
  const resource = resourceFor(event);
  if (resource !== undefined) converted.resource = resource;
  const evidence = evidenceFor(event);
  if (evidence !== undefined) converted.cost_evidence = evidence;
  if (event.isRetry && event.retryOf) converted.retry_of = event.retryOf;
  const hasTimeBasedUsage = mapped.usage.some((line) => line.unit.endsWith("Seconds"));
  if (hasTimeBasedUsage || (mapped.durationSeconds !== undefined && mapped.durationSeconds > 0)) {
    const startOffsetMs = mapped.durationSeconds !== undefined && mapped.durationSeconds > 0
      ? mapped.durationSeconds * 1000
      : 0;
    converted.usage_period = {
      start_at: isoCanonical(new Date(event.occurredAt.getTime() - startOffsetMs)),
      end_at: occurredAt,
    };
  }

  const validation = validateAttributionEventV2(converted);
  if (!validation.success) {
    console.warn(`[dexcost] Event ${event.eventId} cannot be represented by attribution v2: ${validation.issues.map((issue) => issue.path).join(", ")}`);
    return null;
  }
  return converted;
}

export function toAttributionTaskIngestV1(task: Task): AttributionTaskIngestV1 {
  return {
    task_id: task.taskId,
    task_type: task.taskType,
    status: task.status,
    started_at: isoCanonical(task.startedAt),
    ended_at: task.endedAt ? isoCanonical(task.endedAt) : null,
    metadata: task.metadata,
    customer_id: task.customerId ?? null,
    project_id: task.projectId ?? null,
    parent_task_id: task.parentTaskId ?? null,
    experiment_id: task.experimentId ?? null,
    variant: task.variant ?? null,
    schema_version: "1",
  };
}
