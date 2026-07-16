import {
  ATTRIBUTION_COMPONENTS,
  ATTRIBUTION_UNIT_BY_METRIC,
  ATTRIBUTION_USAGE_METRICS,
  ATTRIBUTION_USAGE_UNITS,
  type AttributionEventV2,
  type AttributionUsageMetric,
} from "./types.js";

export interface AttributionV2ValidationIssue {
  path: string;
  message: string;
}

export interface AttributionV2ValidationResult {
  success: boolean;
  issues: AttributionV2ValidationIssue[];
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CANONICAL_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const POSITIVE_DECIMAL = /^(?=.*[1-9])(?:0|[1-9]\d{0,25})(?:\.\d{1,12})?$/;
const CURRENCY = /^[A-Z]{3}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,6}))?(?:Z|[+-]\d{2}:\d{2})$/;
const RESOURCE_TYPES = new Set(["model", "sku", "instance", "endpoint", "session", "other"]);
const LIFECYCLE_STATES = new Set(["pending", "provisional", "final", "voided"]);
const EVIDENCE_SOURCES = new Set(["provider_reported", "sdk_catalog", "sdk_rate_registry", "manual"]);
const CONFIDENCES = new Set(["exact", "computed", "estimated", "unknown"]);
const COMPONENTS = new Set<string>(ATTRIBUTION_COMPONENTS);
const METRICS = new Set<string>(ATTRIBUTION_USAGE_METRICS);
const UNITS = new Set<string>(ATTRIBUTION_USAGE_UNITS);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function addUnknownKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
  prefix: string,
  issues: AttributionV2ValidationIssue[],
): void {
  const allowedKeys = new Set(allowed);
  for (const key of Object.keys(value)) {
    if (!allowedKeys.has(key)) {
      issues.push({ path: prefix ? `${prefix}.${key}` : key, message: "Unknown field" });
    }
  }
}

function validateString(
  value: unknown,
  path: string,
  issues: AttributionV2ValidationIssue[],
  pattern?: RegExp,
): value is string {
  if (typeof value !== "string" || value.length === 0 || (pattern !== undefined && !pattern.test(value))) {
    issues.push({ path, message: "Invalid string value" });
    return false;
  }
  return true;
}

function validateTimestamp(value: unknown, path: string, issues: AttributionV2ValidationIssue[]): value is string {
  if (!validateString(value, path, issues, TIMESTAMP)) return false;
  if (!Number.isFinite(Date.parse(value))) {
    issues.push({ path, message: "Timestamp must be a valid ISO 8601 instant" });
    return false;
  }
  return true;
}

function canonicalTimestamp(value: string): string {
  const match = TIMESTAMP.exec(value);
  const fraction = (match?.[1] ?? "").padEnd(6, "0");
  const milliseconds = Date.parse(value);
  const base = new Date(milliseconds).toISOString().slice(0, 19);
  return `${base}.${fraction}Z`;
}

/**
 * Validate the SDK's public attribution-v2 wire type without throwing.
 * This deliberately mirrors the control-plane boundary rather than relying
 * on TypeScript types, which do not protect JSON received at runtime.
 */
export function validateAttributionEventV2(value: unknown): AttributionV2ValidationResult {
  const issues: AttributionV2ValidationIssue[] = [];
  if (!isRecord(value)) {
    return { success: false, issues: [{ path: "", message: "Event must be an object" }] };
  }

  addUnknownKeys(value, [
    "schema_version", "event_id", "task_id", "occurred_at", "observed_at", "component",
    "provider", "resource", "lifecycle", "usage_period", "usage", "cost_evidence", "retry_of",
  ], "", issues);

  if (value.schema_version !== "2") issues.push({ path: "schema_version", message: "Must equal 2" });
  validateString(value.event_id, "event_id", issues, UUID);
  validateString(value.task_id, "task_id", issues, UUID);
  validateTimestamp(value.occurred_at, "occurred_at", issues);
  validateTimestamp(value.observed_at, "observed_at", issues);
  if (typeof value.component !== "string" || !COMPONENTS.has(value.component)) {
    issues.push({ path: "component", message: "Unknown attribution component" });
  }
  if (value.retry_of !== undefined) validateString(value.retry_of, "retry_of", issues, UUID);

  if (!isRecord(value.provider)) {
    issues.push({ path: "provider", message: "Provider must be an object" });
  } else {
    addUnknownKeys(value.provider, ["name", "service", "record_id", "region"], "provider", issues);
    validateString(value.provider.name, "provider.name", issues, CANONICAL_NAME);
    validateString(value.provider.service, "provider.service", issues, CANONICAL_NAME);
    if (value.provider.record_id !== undefined) {
      if (typeof value.provider.record_id !== "string" || value.provider.record_id.length < 1 || value.provider.record_id.length > 256) {
        issues.push({ path: "provider.record_id", message: "Invalid provider record ID" });
      }
    }
    if (value.provider.region !== undefined) validateString(value.provider.region, "provider.region", issues, CANONICAL_NAME);
  }

  if (value.resource !== undefined) {
    if (!isRecord(value.resource)) {
      issues.push({ path: "resource", message: "Resource must be an object" });
    } else {
      addUnknownKeys(value.resource, ["type", "id"], "resource", issues);
      if (typeof value.resource.type !== "string" || !RESOURCE_TYPES.has(value.resource.type)) {
        issues.push({ path: "resource.type", message: "Invalid resource type" });
      }
      if (typeof value.resource.id !== "string" || value.resource.id.length < 1 || value.resource.id.length > 256) {
        issues.push({ path: "resource.id", message: "Invalid resource ID" });
      }
    }
  }

  let lifecycleState: string | undefined;
  let lifecycleRevision: number | undefined;
  if (!isRecord(value.lifecycle)) {
    issues.push({ path: "lifecycle", message: "Lifecycle must be an object" });
  } else {
    addUnknownKeys(value.lifecycle, ["state", "revision"], "lifecycle", issues);
    if (typeof value.lifecycle.state !== "string" || !LIFECYCLE_STATES.has(value.lifecycle.state)) {
      issues.push({ path: "lifecycle.state", message: "Invalid lifecycle state" });
    } else lifecycleState = value.lifecycle.state;
    if (!Number.isInteger(value.lifecycle.revision) || (value.lifecycle.revision as number) < 1 || (value.lifecycle.revision as number) > 2_147_483_647) {
      issues.push({ path: "lifecycle.revision", message: "Revision must be a positive integer" });
    } else lifecycleRevision = value.lifecycle.revision as number;
  }

  let usagePeriod: Record<string, unknown> | undefined;
  let startAt: string | undefined;
  let endAt: string | undefined;
  if (value.usage_period !== undefined) {
    if (!isRecord(value.usage_period)) {
      issues.push({ path: "usage_period", message: "Usage period must be an object" });
    } else {
      usagePeriod = value.usage_period;
      addUnknownKeys(usagePeriod, ["start_at", "end_at"], "usage_period", issues);
      if (validateTimestamp(usagePeriod.start_at, "usage_period.start_at", issues)) startAt = usagePeriod.start_at;
      if (usagePeriod.end_at !== undefined && validateTimestamp(usagePeriod.end_at, "usage_period.end_at", issues)) endAt = usagePeriod.end_at;
      if (startAt !== undefined && endAt !== undefined && canonicalTimestamp(endAt) < canonicalTimestamp(startAt)) {
        issues.push({ path: "usage_period.end_at", message: "End cannot precede start" });
      }
    }
  }

  const usage = Array.isArray(value.usage) ? value.usage : undefined;
  const seenMetrics = new Set<string>();
  let hasTimeBasedUsage = false;
  if (usage === undefined) {
    issues.push({ path: "usage", message: "Usage must be an array" });
  } else {
    if (usage.length > 32) issues.push({ path: "usage", message: "At most 32 usage lines are allowed" });
    usage.forEach((rawLine, index) => {
      const prefix = `usage.${index}`;
      if (!isRecord(rawLine)) {
        issues.push({ path: prefix, message: "Usage line must be an object" });
        return;
      }
      addUnknownKeys(rawLine, ["metric", "quantity", "unit"], prefix, issues);
      const metric = rawLine.metric;
      if (typeof metric !== "string" || !METRICS.has(metric)) {
        issues.push({ path: `${prefix}.metric`, message: "Invalid usage metric" });
      } else {
        if (seenMetrics.has(metric)) issues.push({ path: `${prefix}.metric`, message: "Duplicate usage metric" });
        seenMetrics.add(metric);
      }
      if (typeof rawLine.quantity !== "string" || !POSITIVE_DECIMAL.test(rawLine.quantity)) {
        issues.push({ path: `${prefix}.quantity`, message: "Must be a positive plain decimal string" });
      }
      if (typeof rawLine.unit !== "string" || !UNITS.has(rawLine.unit)) {
        issues.push({ path: `${prefix}.unit`, message: "Invalid usage unit" });
      } else {
        hasTimeBasedUsage ||= rawLine.unit.endsWith("Seconds");
        if (typeof metric === "string" && METRICS.has(metric) && rawLine.unit !== ATTRIBUTION_UNIT_BY_METRIC[metric as AttributionUsageMetric]) {
          issues.push({ path: `${prefix}.unit`, message: "Metric must use its canonical unit" });
        }
      }
    });
  }

  const cost = value.cost_evidence;
  if (cost !== undefined) {
    if (!isRecord(cost)) {
      issues.push({ path: "cost_evidence", message: "Cost evidence must be an object" });
    } else {
      addUnknownKeys(cost, ["amount", "currency", "source", "confidence", "pricing_version"], "cost_evidence", issues);
      if (typeof cost.amount !== "string" || !POSITIVE_DECIMAL.test(cost.amount)) issues.push({ path: "cost_evidence.amount", message: "Must be a positive plain decimal string" });
      if (typeof cost.currency !== "string" || !CURRENCY.test(cost.currency)) issues.push({ path: "cost_evidence.currency", message: "Invalid currency" });
      const source = typeof cost.source === "string" && EVIDENCE_SOURCES.has(cost.source) ? cost.source : undefined;
      if (source === undefined) issues.push({ path: "cost_evidence.source", message: "Invalid evidence source" });
      const confidence = typeof cost.confidence === "string" && CONFIDENCES.has(cost.confidence) ? cost.confidence : undefined;
      if (confidence === undefined) issues.push({ path: "cost_evidence.confidence", message: "Invalid confidence" });
      if (cost.pricing_version !== undefined && (typeof cost.pricing_version !== "string" || cost.pricing_version.length < 1 || cost.pricing_version.length > 128)) {
        issues.push({ path: "cost_evidence.pricing_version", message: "Invalid pricing version" });
      }
      if (source === "provider_reported" && confidence !== undefined && confidence !== "exact" && confidence !== "estimated") issues.push({ path: "cost_evidence.confidence", message: "Provider-reported cost must be exact or estimated" });
      if ((source === "sdk_catalog" || source === "sdk_rate_registry") && confidence === "exact") issues.push({ path: "cost_evidence.confidence", message: "SDK-derived cost cannot be exact" });
      if ((source === "sdk_catalog" || source === "sdk_rate_registry") && cost.pricing_version === undefined) issues.push({ path: "cost_evidence.pricing_version", message: "SDK-derived cost requires pricing_version" });
    }
  }

  if (hasTimeBasedUsage && (lifecycleState === "provisional" || lifecycleState === "final") && endAt === undefined) {
    issues.push({ path: "usage_period.end_at", message: "Finalized time-based usage requires a closed usage period" });
  }
  if (lifecycleState === "pending") {
    if ((usage?.length ?? 0) !== 0) issues.push({ path: "usage", message: "Pending events cannot assert usage" });
    if (cost !== undefined) issues.push({ path: "cost_evidence", message: "Pending events cannot assert cost" });
    if (usagePeriod?.end_at !== undefined) issues.push({ path: "usage_period.end_at", message: "Pending events cannot close usage" });
  } else if (lifecycleState === "provisional") {
    if ((usage?.length ?? 0) === 0) issues.push({ path: "usage", message: "Provisional events require usage" });
    if (isRecord(cost) && cost.confidence === "exact") issues.push({ path: "cost_evidence.confidence", message: "Provisional cost cannot be exact" });
  } else if (lifecycleState === "final") {
    if ((usage?.length ?? 0) === 0) issues.push({ path: "usage", message: "Final events require usage" });
  } else if (lifecycleState === "voided") {
    if (lifecycleRevision === 1) issues.push({ path: "lifecycle.revision", message: "Voided events must supersede an earlier revision" });
    if ((usage?.length ?? 0) !== 0 || cost !== undefined) issues.push({ path: "usage", message: "Voided events must be tombstones" });
  }

  return { success: issues.length === 0, issues };
}

export function assertAttributionEventV2(value: unknown): asserts value is AttributionEventV2 {
  const result = validateAttributionEventV2(value);
  if (!result.success) {
    throw new Error(result.issues.map((issue) => `${issue.path}: ${issue.message}`).join("; "));
  }
}
