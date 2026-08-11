import { createHash } from "node:crypto";

import {
  Decimal,
  canonicalDecimal,
  isoCanonical,
  type CostEvent,
} from "../core/models.js";
import {
  ATTRIBUTION_COMPONENTS,
  ATTRIBUTION_UNIT_BY_METRIC,
  type AttributionComponent,
  type AttributionUsageMetric,
} from "./types.js";
import {
  attributionComponentAndUsage,
  attributionEvidenceFor,
  attributionProviderFor,
  attributionResourceFor,
} from "./convert.js";
import type {
  AttributionBillingDimension,
  AttributionBillingDimensionValue,
  AttributionEventV3,
  AttributionOperationIdentityV3,
  AttributionOperationStatusV3,
  AttributionUsageLineV3,
} from "./v3-types.js";
import { validateAttributionObservationV3 } from "./v3-validate.js";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CANONICAL_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const UNIT = /^[A-Za-z0-9][A-Za-z0-9._{}/*^+-]{0,63}$/;
const TRACE_ID = /^[0-9a-f]{32}$/;
const SPAN_ID = /^[0-9a-f]{16}$/;
const COMPONENTS = new Set<string>(ATTRIBUTION_COMPONENTS);

function stringDetail(details: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = details[key];
    if (typeof value === "string" && value.trim() !== "") return value.trim();
  }
  return undefined;
}

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

function positiveQuantity(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  try {
    const decimal = value instanceof Decimal ? value : new Decimal(String(value));
    if (!decimal.isFinite() || !decimal.gt(0)) return undefined;
    const rounded = decimal.toDecimalPlaces(12);
    return rounded.gt(0) ? canonicalDecimal(rounded) : undefined;
  } catch {
    return undefined;
  }
}

function deterministicUuid(namespace: string, ...parts: string[]): string {
  const bytes = createHash("sha256")
    .update([namespace, ...parts].join("\u0000"), "utf8")
    .digest()
    .subarray(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function parseDimensionValue(value: unknown): AttributionBillingDimensionValue | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  const candidate = value as Record<string, unknown>;
  if (candidate.type === "string" && typeof candidate.value === "string" &&
      candidate.value.length > 0 && candidate.value.length <= 256) {
    return { type: "string", value: candidate.value };
  }
  if (candidate.type === "boolean" && typeof candidate.value === "boolean") {
    return { type: "boolean", value: candidate.value };
  }
  if (candidate.type === "integer" && typeof candidate.value === "string" &&
      /^-?(?:0|[1-9]\d{0,25})$/.test(candidate.value)) {
    return { type: "integer", value: candidate.value };
  }
  if (candidate.type === "decimal" && typeof candidate.value === "string" &&
      /^-?(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$/.test(candidate.value)) {
    return { type: "decimal", value: candidate.value };
  }
  return undefined;
}

function explicitDimensions(details: Record<string, unknown>): AttributionBillingDimension[] | null {
  const raw = details.attribution_dimensions;
  if (raw === undefined) return [];
  if (!Array.isArray(raw) || raw.length > 24) return null;
  const dimensions: AttributionBillingDimension[] = [];
  for (const candidate of raw) {
    if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) return null;
    const dimension = candidate as Record<string, unknown>;
    if (typeof dimension.key !== "string" || !CANONICAL_NAME.test(dimension.key)) return null;
    const value = parseDimensionValue(dimension.value);
    if (value === undefined) return null;
    dimensions.push({ key: dimension.key, value });
  }
  dimensions.sort((left, right) => left.key.localeCompare(right.key));
  return dimensions;
}

function gpuSignalUsage(event: CostEvent): {
  component: AttributionComponent;
  usage: Array<{ metric: string; quantity: string; unit: string }>;
  durationSeconds?: number;
  dimensions: AttributionBillingDimension[];
} {
  const details = event.details;
  const candidates: Array<[string, unknown, string]> = [
    ["gpu.sm_utilization_percent", details.sm_util_pct, "Percent"],
    ["gpu.memory_utilization_percent", details.mem_util_pct, "Percent"],
    ["gpu.vram_peak_bytes", details.vram_used_peak_bytes, "Bytes"],
    ["gpu.vram_capacity_bytes", details.vram_total_bytes, "Bytes"],
    ["gpu.process_count", details.process_count, "Processes"],
    ["gpu.sample_count", details.sample_count, "Samples"],
  ];
  const usage = candidates.flatMap(([metric, rawQuantity, unit]) => {
    const quantity = positiveQuantity(rawQuantity);
    return quantity === undefined ? [] : [{ metric, quantity, unit }];
  });
  const dimensions: AttributionBillingDimension[] = [];
  const gpuIndex = numberDetail(details, "gpu_index");
  if (gpuIndex !== undefined && Number.isInteger(gpuIndex) && gpuIndex >= 0) {
    dimensions.push({ key: "gpu_index", value: { type: "integer", value: String(gpuIndex) } });
  }
  const sku = stringDetail(details, "gpu_sku");
  if (sku !== undefined) {
    dimensions.push({ key: "gpu_sku", value: { type: "string", value: sku.slice(0, 256) } });
  }
  const durationMs = numberDetail(details, "task_duration_ms");
  return {
    component: "gpu",
    usage,
    durationSeconds: durationMs !== undefined && durationMs > 0 ? durationMs / 1_000 : undefined,
    dimensions,
  };
}

function selectedComponent(
  event: CostEvent,
  fallback: AttributionComponent,
): AttributionComponent | null {
  const explicit = stringDetail(event.details, "attribution_component");
  if (explicit === undefined) return fallback;
  return COMPONENTS.has(explicit) ? explicit as AttributionComponent : null;
}

function operationName(event: CostEvent): string {
  const explicit = stringDetail(event.details, "attribution_operation_name");
  if (explicit !== undefined && CANONICAL_NAME.test(explicit)) return explicit;
  switch (event.eventType) {
    case "llm_call": return "llm.call";
    case "external_cost": return "external.call";
    case "compute_cost": return "compute.consume";
    case "gpu_cost": return "gpu.consume";
    case "gpu_utilization_signal": return "gpu.observe";
    case "network": return "network.transfer";
    case "retry_marker": return "retry.attempt";
  }
}

function operationStatus(event: CostEvent): AttributionOperationStatusV3 {
  const explicit = stringDetail(event.details, "attribution_operation_status");
  if (explicit === "in_progress" || explicit === "succeeded" || explicit === "failed" ||
      explicit === "cancelled" || explicit === "unknown") return explicit;
  if (event.eventType === "gpu_utilization_signal") return "unknown";
  if (event.eventType === "retry_marker" || stringDetail(event.details, "error_type") !== undefined) {
    return "failed";
  }
  return "succeeded";
}

function operationFor(event: CostEvent): AttributionOperationIdentityV3 | null {
  const explicitOperationId = stringDetail(event.details, "attribution_operation_id");
  if (event.retryOf !== undefined &&
      (typeof event.retryOf !== "string" || !UUID.test(event.retryOf))) return null;
  const retryOf = event.retryOf?.toLowerCase();
  const hasValidOperationId = explicitOperationId !== undefined && UUID.test(explicitOperationId);
  const operationId = hasValidOperationId
    ? explicitOperationId!.toLowerCase()
    : event.eventId.toLowerCase();
  const explicitAttemptId = stringDetail(event.details, "attribution_attempt_id");
  const attemptId = explicitAttemptId !== undefined && UUID.test(explicitAttemptId)
    ? explicitAttemptId.toLowerCase()
    : event.eventId.toLowerCase();
  const explicitAttempt = numberDetail(event.details, "attribution_attempt_number");
  const hasValidAttemptNumber = explicitAttempt !== undefined && Number.isInteger(explicitAttempt) &&
    explicitAttempt > 0;
  if (retryOf !== undefined &&
      (!hasValidOperationId || !hasValidAttemptNumber || (explicitAttempt ?? 0) <= 1)) return null;
  const attemptNumber = hasValidAttemptNumber ? explicitAttempt! : 1;
  const operation: AttributionOperationIdentityV3 = {
    id: operationId,
    name: operationName(event),
    status: operationStatus(event),
    attempt: { id: attemptId, number: attemptNumber },
  };
  if (retryOf !== undefined) operation.attempt.retry_of = retryOf;
  const traceId = stringDetail(event.details, "trace_id")?.toLowerCase();
  const spanId = stringDetail(event.details, "span_id")?.toLowerCase();
  if (traceId !== undefined && spanId !== undefined && TRACE_ID.test(traceId) && SPAN_ID.test(spanId)) {
    operation.trace = { trace_id: traceId, span_id: spanId };
  }
  return operation;
}

function unknownExplicitUsage(event: CostEvent): {
  component: AttributionComponent;
  usage: Array<{ metric: string; quantity: string; unit: string }>;
  durationSeconds?: number;
} | undefined {
  if (event.eventType !== "external_cost") return undefined;
  const metric = stringDetail(event.details, "attribution_usage_metric");
  if (metric === undefined || metric in ATTRIBUTION_UNIT_BY_METRIC || !CANONICAL_NAME.test(metric)) {
    return undefined;
  }
  const unit = stringDetail(event.details, "attribution_usage_unit");
  const quantity = positiveQuantity(event.details.attribution_usage_quantity);
  if (unit === undefined || !UNIT.test(unit) || quantity === undefined) return undefined;
  return {
    component: "external",
    usage: [{ metric, quantity, unit }],
    durationSeconds: numberDetail(event.details, "attribution_usage_duration_seconds"),
  };
}

/** Convert durable v1 capture into the strict, details-free v3 observation. */
export function toAttributionObservationV3(event: CostEvent): AttributionEventV3 | null {
  const explicit = unknownExplicitUsage(event);
  const gpuSignal = event.eventType === "gpu_utilization_signal" ? gpuSignalUsage(event) : undefined;
  const legacy = explicit === undefined && gpuSignal === undefined
    ? attributionComponentAndUsage(event)
    : undefined;
  const mapped = explicit ?? gpuSignal ?? legacy;
  if (mapped === null || mapped === undefined) return null;

  const explicitBillingDimensions = explicitDimensions(event.details);
  if (explicitBillingDimensions === null) {
    console.warn(`[dexcost] Event ${event.eventId} has invalid attribution_dimensions`);
    return null;
  }
  const dimensions = gpuSignal === undefined
    ? explicitBillingDimensions
    : [...explicitBillingDimensions, ...gpuSignal.dimensions]
      .sort((left, right) => left.key.localeCompare(right.key));
  const stableDimensions = JSON.stringify(dimensions);
  const usage: AttributionUsageLineV3[] = mapped.usage.map((line) => ({
    line_id: deterministicUuid(
      "dexcost:attribution-usage-line:v3",
      event.eventId.toLowerCase(),
      line.metric,
      line.unit,
      stableDimensions,
    ),
    metric: line.metric,
    quantity: line.quantity,
    unit: line.unit,
    dimensions,
  }));
  const component = selectedComponent(event, mapped.component);
  if (component === null) {
    console.warn(`[dexcost] Event ${event.eventId} has an invalid attribution_component`);
    return null;
  }
  const occurredAt = isoCanonical(event.occurredAt);
  const operation = operationFor(event);
  if (operation === null) {
    console.warn(`[dexcost] Event ${event.eventId} has invalid or incomplete retry lineage`);
    return null;
  }
  const converted: AttributionEventV3 = {
    schema_version: "3",
    event_id: event.eventId.toLowerCase(),
    task_id: event.taskId.toLowerCase(),
    occurred_at: occurredAt,
    observed_at: occurredAt,
    component,
    provider: attributionProviderFor(event),
    operation,
    lifecycle: { state: "final", revision: 1 },
    usage_snapshot: "full",
    usage,
  };
  const resource = attributionResourceFor(event);
  if (resource !== undefined) converted.resource = resource;
  if (event.eventType !== "gpu_utilization_signal") {
    const evidence = attributionEvidenceFor(event);
    if (evidence !== undefined) converted.cost_evidence = evidence;
  }
  const hasTimeBasedUsage = usage.some((line) =>
    line.metric in ATTRIBUTION_UNIT_BY_METRIC &&
    line.unit === ATTRIBUTION_UNIT_BY_METRIC[line.metric as AttributionUsageMetric] &&
    line.unit.endsWith("Seconds"),
  );
  if (hasTimeBasedUsage || (mapped.durationSeconds !== undefined && mapped.durationSeconds > 0)) {
    const durationMs = mapped.durationSeconds !== undefined && mapped.durationSeconds > 0
      ? mapped.durationSeconds * 1_000
      : 0;
    converted.usage_period = {
      start_at: isoCanonical(new Date(event.occurredAt.getTime() - durationMs)),
      end_at: occurredAt,
    };
  }

  const validation = validateAttributionObservationV3(converted);
  if (!validation.success) {
    console.warn(
      `[dexcost] Event ${event.eventId} cannot be represented by attribution v3: ` +
      validation.issues.map((issue) => issue.path || "<root>").join(", "),
    );
    return null;
  }
  return converted;
}

export const toAttributionEventV3 = toAttributionObservationV3;
