import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import type { ErrorObject, ValidateFunction } from "ajv";

import type { AttributionObservationV3 } from "./v3-types.js";

export interface AttributionV3ValidationIssue {
  path: string;
  message: string;
}

export interface AttributionV3ValidationResult {
  success: boolean;
  issues: AttributionV3ValidationIssue[];
}

const schemaPath = join(
  dirname(fileURLToPath(import.meta.url)),
  "attribution-v3-schema.json",
);
const schemaDocument = JSON.parse(readFileSync(schemaPath, "utf8")) as {
  $id: string;
};
type AjvRuntime = {
  addSchema(schema: unknown): unknown;
  getSchema(key: string): ValidateFunction | undefined;
};
type AjvConstructor = new (options: Record<string, unknown>) => AjvRuntime;
type AddFormats = (instance: AjvRuntime) => unknown;
const runtimeRequire = createRequire(import.meta.url);
const AjvModule = runtimeRequire("ajv/dist/2020") as unknown;
const AjvClass = ((AjvModule as { default?: unknown }).default ?? AjvModule) as AjvConstructor;
const formatsModule = runtimeRequire("ajv-formats") as unknown;
const installFormats = ((formatsModule as { default?: unknown }).default ?? formatsModule) as AddFormats;
const ajv = new AjvClass({ allErrors: true, strict: false, discriminator: true });
installFormats(ajv);
ajv.addSchema(schemaDocument);
const compiledSchemaValidator = ajv.getSchema(
  `${schemaDocument.$id}#/components/schemas/AttributionObservation`,
);
if (compiledSchemaValidator === undefined) {
  throw new Error("DexCost v3 observation schema is missing from the SDK package");
}
const schemaValidator: ValidateFunction = compiledSchemaValidator;

const KNOWN_UNIT_BY_METRIC: Readonly<Record<string, string>> = Object.freeze({
  input_tokens: "Tokens",
  input_image_tokens: "Tokens",
  output_image_tokens: "Tokens",
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

const TIME_METRICS = new Set([
  "audio_seconds",
  "connected_seconds",
  "recording_seconds",
  "agent_seconds",
  "compute_seconds",
  "vcpu_seconds",
  "memory_gib_seconds",
  "gpu_seconds",
]);

const TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:Z|[+-](\d{2}):(\d{2}))$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function schemaErrorPath(error: ErrorObject): string {
  const parts = error.instancePath === ""
    ? []
    : error.instancePath
      .slice(1)
      .split("/")
      .map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"));
  if (error.keyword === "required") {
    parts.push(String((error.params as { missingProperty?: unknown }).missingProperty ?? ""));
  }
  if (error.keyword === "additionalProperties") {
    parts.push(String((error.params as { additionalProperty?: unknown }).additionalProperty ?? ""));
  }
  return parts.filter((part) => part !== "").join(".");
}

function validCalendarTimestamp(value: string): RegExpExecArray | undefined {
  const match = TIMESTAMP.exec(value);
  if (match === null) return undefined;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const offsetHour = match[8] === undefined ? 0 : Number(match[8]);
  const offsetMinute = match[9] === undefined ? 0 : Number(match[9]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    month < 1 || month > 12 || day < 1 || day > daysInMonth[month - 1] ||
    hour > 23 || minute > 59 || second > 59 || offsetHour > 23 || offsetMinute > 59 ||
    !Number.isFinite(Date.parse(value))
  ) {
    return undefined;
  }
  return match;
}

function timestampMicroseconds(value: unknown): bigint | undefined {
  if (typeof value !== "string") return undefined;
  const match = validCalendarTimestamp(value);
  if (match === undefined) return undefined;
  const milliseconds = Date.parse(value);
  const fractional = (match[7] ?? "").padEnd(6, "0");
  return BigInt(milliseconds) * 1_000n + BigInt(fractional.slice(3) || "0");
}

function addTimestampIssue(
  value: unknown,
  path: string,
  issues: AttributionV3ValidationIssue[],
): void {
  if (timestampMicroseconds(value) === undefined) {
    issues.push({ path, message: "Must be a valid offset-aware ISO 8601 instant" });
  }
}

function semanticIssues(value: unknown): AttributionV3ValidationIssue[] {
  if (!isRecord(value)) return [];
  const issues: AttributionV3ValidationIssue[] = [];
  addTimestampIssue(value.occurred_at, "occurred_at", issues);
  addTimestampIssue(value.observed_at, "observed_at", issues);

  const usagePeriod = isRecord(value.usage_period) ? value.usage_period : undefined;
  if (usagePeriod !== undefined) {
    addTimestampIssue(usagePeriod.start_at, "usage_period.start_at", issues);
    if (usagePeriod.end_at !== undefined) {
      addTimestampIssue(usagePeriod.end_at, "usage_period.end_at", issues);
      const start = timestampMicroseconds(usagePeriod.start_at);
      const end = timestampMicroseconds(usagePeriod.end_at);
      if (start !== undefined && end !== undefined && end < start) {
        issues.push({ path: "usage_period.end_at", message: "Cannot precede start_at" });
      }
    }
  }

  const usage = Array.isArray(value.usage) ? value.usage : [];
  const lineIds = new Set<unknown>();
  for (const [lineIndex, rawLine] of usage.entries()) {
    if (!isRecord(rawLine)) continue;
    if (lineIds.has(rawLine.line_id)) {
      issues.push({
        path: `usage.${lineIndex}.line_id`,
        message: "Must be unique in a full snapshot",
      });
    }
    lineIds.add(rawLine.line_id);
    const metric = typeof rawLine.metric === "string" ? rawLine.metric : undefined;
    const canonicalUnit = metric === undefined ? undefined : KNOWN_UNIT_BY_METRIC[metric];
    if (canonicalUnit !== undefined && rawLine.unit !== canonicalUnit) {
      issues.push({ path: `usage.${lineIndex}.unit`, message: `Must be ${canonicalUnit}` });
    }
    const dimensions = Array.isArray(rawLine.dimensions) ? rawLine.dimensions : [];
    const dimensionKeys = new Set<unknown>();
    for (const [dimensionIndex, rawDimension] of dimensions.entries()) {
      if (!isRecord(rawDimension)) continue;
      if (dimensionKeys.has(rawDimension.key)) {
        issues.push({
          path: `usage.${lineIndex}.dimensions.${dimensionIndex}.key`,
          message: "Must be unique within the usage line",
        });
      }
      dimensionKeys.add(rawDimension.key);
    }
  }

  const operation = isRecord(value.operation) ? value.operation : undefined;
  const attempt = operation !== undefined && isRecord(operation.attempt)
    ? operation.attempt
    : undefined;
  if (attempt?.number === 1 && attempt.retry_of !== undefined) {
    issues.push({
      path: "operation.attempt.retry_of",
      message: "Attempt 1 cannot retry another attempt",
    });
  }
  if (typeof attempt?.number === "number" && attempt.number > 1 && attempt.retry_of === undefined) {
    issues.push({
      path: "operation.attempt.retry_of",
      message: "Later attempts require retry_of",
    });
  }
  if (attempt?.id !== undefined && attempt.id === attempt.retry_of) {
    issues.push({ path: "operation.attempt.retry_of", message: "Attempt cannot retry itself" });
  }
  if (operation?.status === "succeeded" && operation.error !== undefined) {
    issues.push({ path: "operation.error", message: "A succeeded operation cannot carry an error" });
  }

  const capability = isRecord(value.capability) ? value.capability : undefined;
  if (capability?.source_id !== undefined && capability.source === undefined) {
    issues.push({ path: "capability.source_id", message: "source_id requires source" });
  }

  const lifecycle = isRecord(value.lifecycle) ? value.lifecycle : undefined;
  const state = lifecycle?.state;
  const costEvidence = isRecord(value.cost_evidence) ? value.cost_evidence : undefined;
  if (state === "pending") {
    if (usage.length !== 0) issues.push({ path: "usage", message: "Pending cannot assert usage" });
    if (value.cost_evidence !== undefined) {
      issues.push({ path: "cost_evidence", message: "Pending cannot assert cost evidence" });
    }
    if (usagePeriod?.end_at !== undefined) {
      issues.push({ path: "usage_period.end_at", message: "Pending cannot close usage" });
    }
  } else if (state === "provisional") {
    if (usage.length === 0) issues.push({ path: "usage", message: "Provisional requires usage" });
    if (costEvidence?.confidence === "exact") {
      issues.push({ path: "cost_evidence.confidence", message: "Provisional cost cannot be exact" });
    }
  } else if (state === "final") {
    if (operation?.status === "in_progress") {
      issues.push({ path: "operation.status", message: "Final operation cannot be in progress" });
    }
    if (operation?.status === "succeeded" && usage.length === 0) {
      issues.push({ path: "usage", message: "Successful final operation requires usage" });
    }
  } else if (state === "voided") {
    if (typeof lifecycle?.revision !== "number" || lifecycle.revision <= 1) {
      issues.push({ path: "lifecycle.revision", message: "Voided revision must exceed 1" });
    }
    if (usage.length !== 0) issues.push({ path: "usage", message: "Voided cannot assert usage" });
    if (value.cost_evidence !== undefined) {
      issues.push({ path: "cost_evidence", message: "Voided cannot assert cost evidence" });
    }
  }

  if (
    (state === "provisional" || state === "final") &&
    usage.some((rawLine) => isRecord(rawLine) && TIME_METRICS.has(String(rawLine.metric))) &&
    usagePeriod?.end_at === undefined
  ) {
    issues.push({
      path: "usage_period.end_at",
      message: "Time-based usage requires a closed period",
    });
  }

  if (costEvidence?.source === "provider_reported" &&
      costEvidence.confidence !== "exact" && costEvidence.confidence !== "estimated") {
    issues.push({
      path: "cost_evidence.confidence",
      message: "Provider-reported evidence must be exact or estimated",
    });
  }
  if (costEvidence?.source === "sdk_catalog" || costEvidence?.source === "sdk_rate_registry") {
    if (costEvidence.confidence === "exact") {
      issues.push({ path: "cost_evidence.confidence", message: "SDK evidence cannot be exact" });
    }
    if (typeof costEvidence.pricing_version !== "string" || costEvidence.pricing_version.length === 0) {
      issues.push({
        path: "cost_evidence.pricing_version",
        message: "SDK evidence requires a pricing version",
      });
    }
  }
  return issues;
}

/** Validate the complete v3 schema plus control-plane cross-field invariants. */
export function validateAttributionObservationV3(value: unknown): AttributionV3ValidationResult {
  const issues: AttributionV3ValidationIssue[] = [];
  if (!schemaValidator(value)) {
    for (const error of schemaValidator.errors ?? []) {
      issues.push({
        path: schemaErrorPath(error),
        message: error.message ?? "Schema validation failed",
      });
    }
  }
  issues.push(...semanticIssues(value));
  const unique = issues.filter(
    (issue, index, all) => all.findIndex(
      (candidate) => candidate.path === issue.path && candidate.message === issue.message,
    ) === index,
  );
  return { success: unique.length === 0, issues: unique };
}

export function assertAttributionObservationV3(
  value: unknown,
): asserts value is AttributionObservationV3 {
  const result = validateAttributionObservationV3(value);
  if (!result.success) {
    throw new Error(result.issues.map((issue) => `${issue.path}: ${issue.message}`).join("; "));
  }
}
