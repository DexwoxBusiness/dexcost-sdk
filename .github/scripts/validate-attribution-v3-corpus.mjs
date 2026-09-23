import { readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { validateCorpusContracts } from "./attribution-v3-contract-validator.mjs";

const REQUIRED_SDKS = Object.freeze(["typescript", "python", "go", "rust"]);
const REQUIRED_COVERAGE = Object.freeze([
  "business.currency_preserved",
  "business.outcome_lifecycle",
  "business.revenue_lifecycle",
  "business.task_hierarchy",
  "business.user_product_assignment",
  "business.workflow_agent_assignment",
  "cost.no_synthetic_zero",
  "cost.sdk_evidence_diagnostic",
  "lifecycle.failed_final_no_usage",
  "lifecycle.final",
  "lifecycle.pending",
  "lifecycle.provisional",
  "lifecycle.voided",
  "observation.capability_identity",
  "observation.environment",
  "observation.known_meter",
  "observation.operation_attempt_trace",
  "observation.operation_error",
  "observation.operation_latency",
  "observation.retry_linkage",
  "observation.tool_resource",
  "observation.typed_dimensions",
  "observation.unknown_meter_visible",
  "privacy.arbitrary_details_not_transported",
  "privacy.hash_customer_project",
  "privacy.redact_before_promotion",
  "quantity.exact_decimal",
  "quantity.reject_float_or_scientific",
  "revision.full_snapshot",
  "revision.stable_line_identity",
  "timestamps.microsecond_order",
  "unit.canonical_mapping",
]);
const CASE_GROUPS = Object.freeze([
  ["valid_observations"],
  ["invalid_observations"],
  ["revision_sequences"],
  ["business_identities", "valid"],
  ["business_identities", "invalid"],
  ["outcomes", "valid"],
  ["outcomes", "invalid"],
  ["revenues", "valid"],
  ["revenues", "invalid"],
  ["redaction_cases"],
]);

const CANONICAL_UNITS = Object.freeze({
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

const EXACT_POSITIVE_DECIMAL = /^(?:0|[1-9]\d*)(?:\.\d{1,12})?$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function uniqueStrings(values) {
  return (
    Array.isArray(values) &&
    values.every(isNonEmptyString) &&
    new Set(values).size === values.length
  );
}

function isPositiveDecimal(value) {
  return (
    typeof value === "string" &&
    EXACT_POSITIVE_DECIMAL.test(value) &&
    value.replace(/[0.]/g, "").length > 0
  );
}

function getPath(root, path) {
  let value = root;
  for (const part of path) {
    if (!isObject(value) || !(part in value)) return undefined;
    value = value[part];
  }
  return value;
}

function sameOrderedStrings(actual, expected) {
  return (
    uniqueStrings(actual) &&
    actual.length === expected.length &&
    actual.every((value, index) => value === expected[index])
  );
}

function validateUsageLine(line, path, issues) {
  if (!isObject(line)) {
    issues.push(`${path} must be an object`);
    return;
  }
  if (!UUID.test(line.line_id ?? "")) {
    issues.push(`${path}.line_id must be a UUID`);
  }
  if (!isNonEmptyString(line.metric)) {
    issues.push(`${path}.metric must be a non-empty string`);
  }
  if (!isPositiveDecimal(line.quantity)) {
    issues.push(`${path}.quantity must be a positive exact decimal string`);
  }
  if (!isNonEmptyString(line.unit)) {
    issues.push(`${path}.unit must be a non-empty string`);
  }
  const canonicalUnit = CANONICAL_UNITS[line.metric];
  if (canonicalUnit !== undefined && line.unit !== canonicalUnit) {
    issues.push(`${path}.unit must be ${canonicalUnit} for ${line.metric}`);
  }
  if (!Array.isArray(line.dimensions)) {
    issues.push(`${path}.dimensions must be an array`);
    return;
  }
  const keys = line.dimensions.map((dimension) => dimension?.key);
  if (!uniqueStrings(keys)) {
    issues.push(`${path}.dimensions must have unique non-empty keys`);
  }
  for (const [index, dimension] of line.dimensions.entries()) {
    const value = dimension?.value;
    const valuePath = `${path}.dimensions.${index}.value`;
    if (!isObject(value) || !["string", "boolean", "integer", "decimal"].includes(value.type)) {
      issues.push(`${valuePath} must be a typed dimension value`);
      continue;
    }
    if (value.type === "string" && !isNonEmptyString(value.value)) {
      issues.push(`${valuePath}.value must be a non-empty string`);
    }
    if (value.type === "boolean" && typeof value.value !== "boolean") {
      issues.push(`${valuePath}.value must be boolean`);
    }
    if (value.type === "integer" && !/^-?(?:0|[1-9]\d*)$/.test(value.value ?? "")) {
      issues.push(`${valuePath}.value must be an exact integer string`);
    }
    if (value.type === "decimal" && !/^-?(?:0|[1-9]\d*)(?:\.\d{1,12})?$/.test(value.value ?? "")) {
      issues.push(`${valuePath}.value must be an exact decimal string`);
    }
  }
}

function validateObservationCase(testCase, issues) {
  const event = testCase?.event;
  if (!isObject(event)) {
    issues.push(`${testCase.id} must contain event`);
    return;
  }
  if (event.schema_version !== "3") {
    issues.push(`${testCase.id} event schema_version must be 3`);
  }
  if (event.usage_snapshot !== "full") {
    issues.push(`${testCase.id} must use a full usage snapshot`);
  }
  if (!Array.isArray(event.usage)) {
    issues.push(`${testCase.id} usage must be an array`);
    return;
  }
  const lineIds = event.usage.map((line) => line?.line_id);
  if (new Set(lineIds).size !== lineIds.length) {
    issues.push(`${testCase.id} usage line IDs must be unique`);
  }
  event.usage.forEach((line, index) =>
    validateUsageLine(line, `${testCase.id}.usage.${index}`, issues),
  );

  const evidence = event.cost_evidence;
  if (isObject(evidence) && ["sdk_catalog", "sdk_rate_registry"].includes(evidence.source)) {
    if (evidence.confidence === "exact") {
      issues.push(`${testCase.id} SDK cost evidence cannot claim exact confidence`);
    }
    if (!isNonEmptyString(evidence.pricing_version)) {
      issues.push(`${testCase.id} SDK cost evidence requires pricing_version`);
    }
  }
}

function validateMutationCase(testCase, validIds, path, issues) {
  if (!isNonEmptyString(testCase.expected_error_path)) {
    issues.push(`${path} must declare expected_error_path`);
  }
  if (testCase.event !== undefined) return;
  if (!validIds.has(testCase.mutate_from)) {
    issues.push(`${path} references unknown mutate_from ${testCase.mutate_from}`);
  }
  if (
    !isObject(testCase.set) &&
    !Array.isArray(testCase.delete) &&
    testCase.append_usage === undefined &&
    testCase.append_dimension === undefined
  ) {
    issues.push(`${path} must declare an explicit mutation`);
  }
}

function validateRevisionSequence(testCase, issues) {
  if (!UUID.test(testCase.stable_event_id ?? "")) {
    issues.push(`${testCase.id} stable_event_id must be a UUID`);
  }
  if (!Array.isArray(testCase.revisions) || testCase.revisions.length < 2) {
    issues.push(`${testCase.id} must contain at least two revisions`);
    return;
  }
  if (
    !Array.isArray(testCase.expected_active_line_ids_after_each_revision) ||
    testCase.expected_active_line_ids_after_each_revision.length !== testCase.revisions.length
  ) {
    issues.push(`${testCase.id} must declare active line IDs for every revision`);
  }

  const lineByMetric = new Map();
  for (const [index, revision] of testCase.revisions.entries()) {
    if (revision.revision !== index + 1) {
      issues.push(`${testCase.id} revisions must be consecutive from 1`);
    }
    if (revision.usage_snapshot !== "full" || !Array.isArray(revision.usage)) {
      issues.push(`${testCase.id} revision ${index + 1} must be a full usage snapshot`);
      continue;
    }
    revision.usage.forEach((line, lineIndex) => {
      validateUsageLine(line, `${testCase.id}.revisions.${index}.usage.${lineIndex}`, issues);
      const prior = lineByMetric.get(line.metric);
      if (prior !== undefined && prior !== line.line_id) {
        issues.push(`${testCase.id} changed line identity for metric ${line.metric}`);
      }
      lineByMetric.set(line.metric, line.line_id);
    });
    const expected = testCase.expected_active_line_ids_after_each_revision?.[index];
    const actual = revision.usage.map((line) => line.line_id);
    if (!sameOrderedStrings(expected, actual)) {
      issues.push(`${testCase.id} active line IDs disagree at revision ${index + 1}`);
    }
  }
}

function validateBusinessGroup(group, label, issues) {
  if (!isObject(group) || !Array.isArray(group.valid) || !Array.isArray(group.invalid)) {
    issues.push(`${label} must contain valid and invalid arrays`);
    return;
  }
  const validIds = new Set();
  for (const testCase of group.valid) {
    validIds.add(testCase.id);
    if (!isObject(testCase.record) || testCase.record.schema_version !== "1") {
      issues.push(`${testCase.id} record schema_version must be 1`);
    }
  }
  for (const testCase of group.invalid) {
    validateMutationCase(testCase, validIds, testCase.id ?? label, issues);
  }
}

function validateRedactionCase(testCase, issues) {
  if (!isObject(testCase.source) || !isObject(testCase.policy) || !isObject(testCase.expected)) {
    issues.push(`${testCase.id} must contain source, policy, and expected objects`);
    return;
  }
  if (
    !uniqueStrings(testCase.policy.redact_fields) ||
    testCase.policy.redact_fields.length === 0
  ) {
    issues.push(`${testCase.id} redact_fields must be unique strings`);
  }
  if (!Array.isArray(testCase.expected.forbidden_values)) {
    issues.push(`${testCase.id} must declare forbidden_values`);
    return;
  }
  const { forbidden_values: forbiddenValues, ...wireExpectation } = testCase.expected;
  const expectedJson = JSON.stringify(wireExpectation);
  for (const forbidden of forbiddenValues) {
    if (!isNonEmptyString(forbidden)) {
      issues.push(`${testCase.id} forbidden_values must be non-empty strings`);
    } else if (expectedJson.includes(forbidden)) {
      issues.push(`${testCase.id} leaks forbidden value ${forbidden} into expected output`);
    }
  }
  if (testCase.policy.hash_customer_id === true) {
    for (const field of ["customer_id", "project_id"]) {
      if (!/^[0-9a-f]{64}$/.test(testCase.expected[field] ?? "")) {
        issues.push(`${testCase.id} expected ${field} must be a SHA-256 hex digest`);
      }
    }
  }
}

export function validateAttributionV3Corpus({ manifest, corpus }) {
  const issues = [];
  if (manifest?.contracts?.observation !== "3.2.0") {
    issues.push("observation contract version must remain 3.2.0");
  }
  if (manifest?.contracts?.business_attribution !== "1.1.0") {
    issues.push("business attribution contract version must remain 1.1.0");
  }
  if (manifest?.corpus_version !== corpus?.corpus_version) {
    issues.push("manifest and corpus versions must match");
  }
  if (manifest?.contracts?.observation !== corpus?.observation_contract_version) {
    issues.push("observation contract versions must match");
  }
  if (manifest?.contracts?.business_attribution !== corpus?.business_contract_version) {
    issues.push("business attribution contract versions must match");
  }
  if (corpus?.schema_version !== "1") {
    issues.push("corpus schema_version must be 1");
  }
  if (!sameOrderedStrings(manifest?.required_sdks, REQUIRED_SDKS)) {
    issues.push(`required_sdks must be exactly ${REQUIRED_SDKS.join(", ")}`);
  }
  if (!sameOrderedStrings(manifest?.required_coverage, REQUIRED_COVERAGE)) {
    issues.push("required_coverage does not match the locked v3 guarantee inventory");
  }

  const allCases = [];
  for (const path of CASE_GROUPS) {
    const group = getPath(corpus, path);
    if (!Array.isArray(group) || group.length === 0) {
      issues.push(`${path.join(".")} must be a non-empty array`);
      continue;
    }
    allCases.push(...group);
  }

  const ids = new Set();
  const covered = new Set();
  const allowedCoverage = new Set(REQUIRED_COVERAGE);
  for (const testCase of allCases) {
    if (!isNonEmptyString(testCase?.id)) {
      issues.push("every corpus case must have a non-empty id");
      continue;
    }
    if (ids.has(testCase.id)) issues.push(`case id is duplicated: ${testCase.id}`);
    ids.add(testCase.id);
    if (!Array.isArray(testCase.covers)) {
      issues.push(`${testCase.id} must declare covers[]`);
      continue;
    }
    for (const tag of testCase.covers) {
      if (!allowedCoverage.has(tag)) issues.push(`${testCase.id} uses unknown coverage tag ${tag}`);
      covered.add(tag);
    }
  }
  for (const tag of REQUIRED_COVERAGE) {
    if (!covered.has(tag)) issues.push(`required coverage is missing: ${tag}`);
  }

  for (const testCase of corpus?.valid_observations ?? []) {
    validateObservationCase(testCase, issues);
  }
  const validObservationIds = new Set(
    (corpus?.valid_observations ?? []).map((testCase) => testCase.id),
  );
  for (const testCase of corpus?.invalid_observations ?? []) {
    validateMutationCase(testCase, validObservationIds, testCase.id ?? "invalid_observation", issues);
  }
  for (const testCase of corpus?.revision_sequences ?? []) {
    validateRevisionSequence(testCase, issues);
  }
  validateBusinessGroup(corpus?.business_identities, "business_identities", issues);
  validateBusinessGroup(corpus?.outcomes, "outcomes", issues);
  validateBusinessGroup(corpus?.revenues, "revenues", issues);
  for (const testCase of corpus?.redaction_cases ?? []) {
    validateRedactionCase(testCase, issues);
  }
  issues.push(...validateCorpusContracts(corpus));

  return [...new Set(issues)].sort();
}

export async function loadAttributionV3Corpus(rootDir) {
  const manifest = JSON.parse(
    await readFile(join(rootDir, "fixtures/attribution_v3/manifest.json"), "utf8"),
  );
  const corpus = JSON.parse(
    await readFile(join(rootDir, manifest.corpus_file), "utf8"),
  );
  return { manifest, corpus };
}

export async function runAttributionV3CorpusValidation(rootDir) {
  const inputs = await loadAttributionV3Corpus(rootDir);
  const issues = validateAttributionV3Corpus(inputs);
  if (issues.length > 0) {
    throw new Error(
      `Attribution v3 corpus validation failed with ${issues.length} issue(s):\n` +
        issues.map((issue) => `- ${issue}`).join("\n"),
    );
  }
  return {
    cases: CASE_GROUPS.reduce(
      (count, path) => count + getPath(inputs.corpus, path).length,
      0,
    ),
    coverage: inputs.manifest.required_coverage.length,
    sdks: inputs.manifest.required_sdks.length,
  };
}

const scriptPath = fileURLToPath(import.meta.url);
const isMain = process.argv[1] && resolve(process.argv[1]) === resolve(scriptPath);
if (isMain) {
  const rootDir = resolve(dirname(scriptPath), "..", "..");
  try {
    const result = await runAttributionV3CorpusValidation(rootDir);
    console.log(
      `Attribution v3 corpus passed: ${result.cases} cases, ` +
        `${result.coverage} guarantees, ${result.sdks} SDK consumers`,
    );
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
