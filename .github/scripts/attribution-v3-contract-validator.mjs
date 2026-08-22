import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const requireFromTypescript = createRequire(join(root, "typescript", "package.json"));
const Ajv2020 = requireFromTypescript("ajv/dist/2020").default;
const addFormats = requireFromTypescript("ajv-formats").default;

const schemaDocument = JSON.parse(
  readFileSync(join(root, "fixtures/attribution_v3/schemas.json"), "utf8"),
);
const schemaId = schemaDocument.$id;
const ajv = new Ajv2020({ allErrors: true, strict: false, discriminator: true });
addFormats(ajv);
ajv.addSchema(schemaDocument);

const schemaValidators = Object.freeze({
  observation: requiredSchema("AttributionObservation"),
  business_identity: requiredSchema("AttributionBusinessIdentityRevision"),
  outcome: requiredSchema("AttributionOutcomeRevision"),
  revenue: requiredSchema("AttributionRevenueRevision"),
});

const KNOWN_UNIT_BY_METRIC = Object.freeze({
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

function requiredSchema(name) {
  const validator = ajv.getSchema(`${schemaId}#/components/schemas/${name}`);
  if (validator === undefined) throw new Error(`missing contract schema ${name}`);
  return validator;
}

function pointerParts(pointer) {
  if (pointer === "") return [];
  return pointer
    .slice(1)
    .split("/")
    .map((part) => part.replaceAll("~1", "/").replaceAll("~0", "~"));
}

function schemaErrorPath(error) {
  const parts = pointerParts(error.instancePath);
  if (error.keyword === "required") parts.push(error.params.missingProperty);
  if (error.keyword === "additionalProperties") {
    parts.push(error.params.additionalProperty);
  }
  return parts.join(".");
}

function addIssue(issues, path, message) {
  issues.push({ path, message });
}

function schemaIssues(kind, record) {
  const validate = schemaValidators[kind];
  if (validate(record)) return [];
  return (validate.errors ?? []).map((error) => ({
    path: schemaErrorPath(error),
    message: error.message ?? "schema validation failed",
  }));
}

function timestampMicroseconds(value) {
  if (typeof value !== "string") return undefined;
  const match = /\.(\d{1,6})(?:Z|[+-]\d{2}:\d{2})$/.exec(value);
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return undefined;
  const fractional = (match?.[1] ?? "").padEnd(6, "0");
  return BigInt(milliseconds) * 1000n + BigInt(fractional.slice(3) || "0");
}

function validateTimestamp(value, path, issues) {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value) ||
    timestampMicroseconds(value) === undefined
  ) {
    addIssue(issues, path, "must be a valid offset-aware ISO 8601 instant");
  }
}

function validateObservationSemantics(event) {
  const issues = [];
  if (event === null || typeof event !== "object" || Array.isArray(event)) return issues;

  validateTimestamp(event.occurred_at, "occurred_at", issues);
  validateTimestamp(event.observed_at, "observed_at", issues);
  if (event.usage_period !== undefined) {
    validateTimestamp(event.usage_period?.start_at, "usage_period.start_at", issues);
    if (event.usage_period?.end_at !== undefined) {
      validateTimestamp(event.usage_period.end_at, "usage_period.end_at", issues);
      const start = timestampMicroseconds(event.usage_period.start_at);
      const end = timestampMicroseconds(event.usage_period.end_at);
      if (start !== undefined && end !== undefined && end < start) {
        addIssue(issues, "usage_period.end_at", "cannot precede start_at");
      }
    }
  }

  const usage = Array.isArray(event.usage) ? event.usage : [];
  const lineIds = new Set();
  for (const [lineIndex, line] of usage.entries()) {
    if (lineIds.has(line?.line_id)) {
      addIssue(issues, `usage.${lineIndex}.line_id`, "must be unique in a snapshot");
    }
    lineIds.add(line?.line_id);
    const canonicalUnit = KNOWN_UNIT_BY_METRIC[line?.metric];
    if (canonicalUnit !== undefined && line?.unit !== canonicalUnit) {
      addIssue(issues, `usage.${lineIndex}.unit`, `must be ${canonicalUnit}`);
    }
    const dimensionKeys = new Set();
    for (const [dimensionIndex, dimension] of (line?.dimensions ?? []).entries()) {
      if (dimensionKeys.has(dimension?.key)) {
        addIssue(
          issues,
          `usage.${lineIndex}.dimensions.${dimensionIndex}.key`,
          "must be unique within the usage line",
        );
      }
      dimensionKeys.add(dimension?.key);
    }
  }

  const attempt = event.operation?.attempt;
  if (attempt?.number === 1 && attempt.retry_of !== undefined) {
    addIssue(issues, "operation.attempt.retry_of", "attempt 1 cannot retry another attempt");
  }
  if (Number.isInteger(attempt?.number) && attempt.number > 1 && attempt.retry_of === undefined) {
    addIssue(issues, "operation.attempt.retry_of", "later attempts require retry_of");
  }
  if (attempt?.id !== undefined && attempt.id === attempt.retry_of) {
    addIssue(issues, "operation.attempt.retry_of", "attempt cannot retry itself");
  }
  if (event.operation?.status === "succeeded" && event.operation.error !== undefined) {
    addIssue(issues, "operation.error", "a succeeded operation cannot carry an error");
  }

  if (event.capability?.source_id !== undefined && event.capability.source === undefined) {
    addIssue(issues, "capability.source_id", "source_id requires source");
  }

  const lifecycle = event.lifecycle;
  if (lifecycle?.state === "pending") {
    if (usage.length !== 0) addIssue(issues, "usage", "pending cannot assert usage");
    if (event.cost_evidence !== undefined) {
      addIssue(issues, "cost_evidence", "pending cannot assert cost evidence");
    }
    if (event.usage_period?.end_at !== undefined) {
      addIssue(issues, "usage_period.end_at", "pending cannot close its usage period");
    }
  }
  if (lifecycle?.state === "provisional") {
    if (usage.length === 0) addIssue(issues, "usage", "provisional requires usage");
    if (event.cost_evidence?.confidence === "exact") {
      addIssue(issues, "cost_evidence.confidence", "provisional cost cannot be exact");
    }
  }
  if (lifecycle?.state === "final") {
    if (event.operation?.status === "in_progress") {
      addIssue(issues, "operation.status", "final operation cannot remain in progress");
    }
    if (event.operation?.status === "succeeded" && usage.length === 0) {
      addIssue(issues, "usage", "successful final operation requires usage");
    }
  }
  if (lifecycle?.state === "voided") {
    if (!(Number.isInteger(lifecycle.revision) && lifecycle.revision > 1)) {
      addIssue(issues, "lifecycle.revision", "voided revision must be greater than 1");
    }
    if (usage.length !== 0) addIssue(issues, "usage", "voided revision cannot assert usage");
    if (event.cost_evidence !== undefined) {
      addIssue(issues, "cost_evidence", "voided revision cannot assert cost evidence");
    }
  }

  if (
    ["provisional", "final"].includes(lifecycle?.state) &&
    usage.some((line) => TIME_METRICS.has(line?.metric)) &&
    event.usage_period?.end_at === undefined
  ) {
    addIssue(issues, "usage_period.end_at", "time-based usage requires a closed period");
  }

  const evidence = event.cost_evidence;
  if (evidence?.source === "provider_reported" && !["exact", "estimated"].includes(evidence.confidence)) {
    addIssue(issues, "cost_evidence.confidence", "provider-reported evidence must be exact or estimated");
  }
  if (["sdk_catalog", "sdk_rate_registry"].includes(evidence?.source)) {
    if (evidence.confidence === "exact") {
      addIssue(issues, "cost_evidence.confidence", "SDK evidence cannot be exact");
    }
    if (typeof evidence.pricing_version !== "string" || evidence.pricing_version.length === 0) {
      addIssue(issues, "cost_evidence.pricing_version", "SDK evidence requires a pricing version");
    }
  }
  return issues;
}

function validateIdentitySemantics(record) {
  const issues = [];
  if (record === null || typeof record !== "object" || Array.isArray(record)) return issues;
  validateTimestamp(record.effective_at, "effective_at", issues);
  validateTimestamp(record.observed_at, "observed_at", issues);
  const task = record.task;
  if (task?.parent_task_id === undefined && task?.root_task_id !== record.task_id) {
    addIssue(issues, "task.root_task_id", "a root task must identify itself as root");
  }
  if (task?.parent_task_id !== undefined) {
    if (task.parent_task_id === record.task_id) {
      addIssue(issues, "task.parent_task_id", "a task cannot parent itself");
    }
    if (task.root_task_id === record.task_id) {
      addIssue(issues, "task.root_task_id", "a child cannot identify itself as root");
    }
  }
  if (record.assignment?.variant !== undefined && record.assignment?.experiment_id === undefined) {
    addIssue(issues, "assignment.variant", "variant requires experiment_id");
  }
  return issues;
}

function validateOutcomeSemantics(record) {
  const issues = [];
  if (record === null || typeof record !== "object" || Array.isArray(record)) return issues;
  validateTimestamp(record.effective_at, "effective_at", issues);
  validateTimestamp(record.observed_at, "observed_at", issues);
  if (["pending", "voided"].includes(record.lifecycle?.state) && record.value !== undefined) {
    addIssue(issues, "value", `${record.lifecycle.state} outcome cannot assert a value`);
  }
  if (record.lifecycle?.state === "voided" && !(record.lifecycle.revision > 1)) {
    addIssue(issues, "lifecycle.revision", "voided revision must be greater than 1");
  }
  return issues;
}

function validateRevenueSemantics(record) {
  const issues = [];
  if (record === null || typeof record !== "object" || Array.isArray(record)) return issues;
  validateTimestamp(record.effective_at, "effective_at", issues);
  validateTimestamp(record.observed_at, "observed_at", issues);
  const state = record.lifecycle?.state;
  if (["pending", "voided"].includes(state) && record.amount !== undefined) {
    addIssue(issues, "amount", `${state} revenue cannot assert an amount`);
  }
  if (["provisional", "recognized"].includes(state) && record.amount === undefined) {
    addIssue(issues, "amount", `${state} revenue requires an amount`);
  }
  if (state === "voided" && !(record.lifecycle.revision > 1)) {
    addIssue(issues, "lifecycle.revision", "voided revision must be greater than 1");
  }
  return issues;
}

function validateRecord(kind, record) {
  const issues = schemaIssues(kind, record);
  if (kind === "observation") issues.push(...validateObservationSemantics(record));
  if (kind === "business_identity") issues.push(...validateIdentitySemantics(record));
  if (kind === "outcome") issues.push(...validateOutcomeSemantics(record));
  if (kind === "revenue") issues.push(...validateRevenueSemantics(record));
  return issues.filter(
    (issue, index, all) =>
      all.findIndex((candidate) => candidate.path === issue.path && candidate.message === issue.message) === index,
  );
}

function setPath(target, path, value) {
  const parts = path.split(".");
  let current = target;
  for (const part of parts.slice(0, -1)) current = current[part];
  current[parts.at(-1)] = structuredClone(value);
}

function deletePath(target, path) {
  const parts = path.split(".");
  let current = target;
  for (const part of parts.slice(0, -1)) current = current[part];
  delete current[parts.at(-1)];
}

function materializeMutation(testCase, validById, recordKey) {
  if (testCase[recordKey] !== undefined) return structuredClone(testCase[recordKey]);
  const base = validById.get(testCase.mutate_from);
  if (base === undefined) return undefined;
  const record = structuredClone(base);
  for (const [path, value] of Object.entries(testCase.set ?? {})) {
    setPath(record, path, value);
  }
  for (const path of testCase.delete ?? []) deletePath(record, path);
  if (testCase.append_usage !== undefined) record.usage.push(structuredClone(testCase.append_usage));
  if (testCase.append_dimension !== undefined) {
    record.usage[0].dimensions.push(structuredClone(testCase.append_dimension));
  }
  return record;
}

function validateGroup(kind, validCases, invalidCases, recordKey) {
  const issues = [];
  const validById = new Map();
  for (const testCase of validCases ?? []) {
    const record = testCase?.[recordKey];
    validById.set(testCase?.id, record);
    const failures = validateRecord(kind, record);
    if (failures.length > 0) {
      issues.push(
        `${testCase.id} valid record fails the ${kind} contract at ` +
          failures.map((failure) => failure.path || "<root>").join(", "),
      );
    }
  }
  for (const testCase of invalidCases ?? []) {
    const record = materializeMutation(testCase, validById, recordKey);
    if (record === undefined) continue;
    const failures = validateRecord(kind, record);
    if (failures.length === 0) {
      issues.push(`${testCase.id} mutation remains valid under the ${kind} contract`);
      continue;
    }
    const paths = new Set(failures.map((failure) => failure.path));
    if (!paths.has(testCase.expected_error_path)) {
      issues.push(
        `${testCase.id} mutation does not fail at ${testCase.expected_error_path}; got ` +
          [...paths].map((path) => path || "<root>").join(", "),
      );
    }
  }
  return issues;
}

export function validateCorpusContracts(corpus) {
  return [
    ...validateGroup(
      "observation",
      corpus?.valid_observations,
      corpus?.invalid_observations,
      "event",
    ),
    ...validateGroup(
      "business_identity",
      corpus?.business_identities?.valid,
      corpus?.business_identities?.invalid,
      "record",
    ),
    ...validateGroup("outcome", corpus?.outcomes?.valid, corpus?.outcomes?.invalid, "record"),
    ...validateGroup("revenue", corpus?.revenues?.valid, corpus?.revenues?.invalid, "record"),
  ];
}
