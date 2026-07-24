import { readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const REQUIRED_SDKS = Object.freeze([
  "typescript",
  "python",
  "go",
  "rust",
]);

const PACKAGED_MANIFEST_PATHS = Object.freeze({
  typescript: "typescript/src/data/service_usage_observers.json",
  python: "python/src/dexcost/data/service_usage_observers.json",
  go: "go/pricing/data/service_usage_observers.json",
  rust: "rust/src/data/service_usage_observers.json",
});

const CONSUMER_TEST_PATHS = Object.freeze({
  typescript: "typescript/tests/service-usage-observer-conformance.test.ts",
  python: "python/tests/test_service_usage_observer_conformance.py",
  go: "go/pricing/service_usage_observers_test.go",
  rust: "rust/tests/service_usage_observer_conformance.rs",
});

const WORKFLOW_CONFORMANCE_MARKERS = Object.freeze({
  typescript: "tests/service-usage-observer-conformance.test.ts",
  python: "tests/test_service_usage_observer_conformance.py",
  go: "TestSharedServiceUsageObserverConformance",
  rust: "cargo test --test service_usage_observer_conformance",
});

const REQUIRED_OBSERVER_FIELDS = Object.freeze([
  "service_key",
  "provider_name",
  "provider_service",
  "component",
  "usage_metric",
  "source_url",
]);

const FORBIDDEN_MONETARY_KEYS = new Set([
  "amount",
  "cost",
  "cost_evidence",
  "cost_usd",
  "currency",
  "price",
  "price_usd",
  "pricing_version",
  "rate",
  "rate_amount",
  "rate_quantity",
  "tiers",
]);

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

function sameStringSet(left, right) {
  return (
    uniqueStrings(left) &&
    uniqueStrings(right) &&
    left.length === right.length &&
    left.every((value) => right.includes(value))
  );
}

function canonicalJson(value) {
  if (Array.isArray(value)) return value.map(canonicalJson);
  if (!isObject(value)) return value;
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((key) => [key, canonicalJson(value[key])]),
  );
}

function jsonEqual(left, right) {
  return JSON.stringify(canonicalJson(left)) === JSON.stringify(canonicalJson(right));
}

function resolvePath(value, dottedPath) {
  let current = value;
  for (const part of dottedPath.split(".")) {
    if (!isObject(current) || !(part in current)) return undefined;
    current = current[part];
  }
  return current;
}

function truthyQueryValue(value) {
  return !["", "0", "false", "no", "off"].includes(value.trim().toLowerCase());
}

function domainRoot(hostname) {
  const labels = hostname.toLowerCase().split(".").filter(Boolean);
  return labels.slice(-2).join(".");
}

function queryPredicateMatches(url, predicate) {
  if (!isObject(predicate) || !isNonEmptyString(predicate.parameter)) return false;
  const values = url.searchParams.getAll(predicate.parameter);
  if (predicate.operator === "present") return url.searchParams.has(predicate.parameter);
  if (predicate.operator === "truthy") return values.some(truthyQueryValue);
  return false;
}

function responsePredicateMatches(response, predicate) {
  if (!isObject(predicate) || !isNonEmptyString(predicate.path)) return false;
  const value = resolvePath(response, predicate.path);
  if (predicate.operator === "equals") return value === predicate.value;
  if (predicate.operator === "non_empty") {
    if (typeof value === "string") return value.trim().length > 0;
    if (Array.isArray(value)) return value.length > 0;
    if (isObject(value)) return Object.keys(value).length > 0;
  }
  return false;
}

function caseTargetsObserver(testCase, observer) {
  let url;
  try {
    url = new URL(testCase.url);
  } catch {
    return false;
  }

  const domainMatches =
    Array.isArray(observer.domains) && observer.domains.includes(url.hostname);
  const endpointMatches =
    Array.isArray(observer.endpoints) &&
    observer.endpoints.some(
      (endpoint) =>
        url.pathname === endpoint || url.pathname.startsWith(`${endpoint}/`),
    );
  if (!domainMatches || !endpointMatches) return false;

  if (
    Array.isArray(observer.query_any) &&
    !observer.query_any.some((predicate) => queryPredicateMatches(url, predicate))
  ) {
    return false;
  }

  if (
    Array.isArray(observer.response_all) &&
    !observer.response_all.every((predicate) =>
      responsePredicateMatches(testCase.response, predicate),
    )
  ) {
    return false;
  }

  return true;
}

function isPositiveDecimal(value) {
  if (
    typeof value !== "string" ||
    !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)
  ) {
    return false;
  }
  return value.replace(/[0.]/g, "").length > 0;
}

function findForbiddenKeys(value, path = "$", found = []) {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      findForbiddenKeys(item, `${path}[${index}]`, found),
    );
    return found;
  }
  if (!isObject(value)) return found;

  for (const [key, nested] of Object.entries(value)) {
    const nestedPath = `${path}.${key}`;
    if (FORBIDDEN_MONETARY_KEYS.has(key)) found.push(nestedPath);
    findForbiddenKeys(nested, nestedPath, found);
  }
  return found;
}

function validateObserverShape(observer, issues) {
  const key = isNonEmptyString(observer?.service_key)
    ? observer.service_key
    : "<missing-service-key>";
  for (const field of REQUIRED_OBSERVER_FIELDS) {
    if (!isNonEmptyString(observer?.[field])) {
      issues.push(`observer ${key} is missing ${field}`);
    }
  }

  if (
    !Array.isArray(observer?.domains) ||
    observer.domains.length === 0 ||
    !observer.domains.every(
      (domain) =>
        isNonEmptyString(domain) &&
        !domain.includes("/") &&
        domain === domain.toLowerCase(),
    )
  ) {
    issues.push(`observer ${key} has invalid domains`);
  }
  if (
    !Array.isArray(observer?.endpoints) ||
    observer.endpoints.length === 0 ||
    !observer.endpoints.every(
      (endpoint) => isNonEmptyString(endpoint) && endpoint.startsWith("/"),
    )
  ) {
    issues.push(`observer ${key} has invalid endpoints`);
  }

  let sourceUrl;
  try {
    sourceUrl = new URL(observer?.source_url);
  } catch {
    sourceUrl = undefined;
  }
  if (sourceUrl?.protocol !== "https:") {
    issues.push(`observer ${key} must cite an HTTPS provider API source`);
  } else {
    const providerDomainRoots = new Set(
      (observer?.domains ?? []).map(domainRoot),
    );
    if (!providerDomainRoots.has(domainRoot(sourceUrl.hostname))) {
      issues.push(`observer ${key} source is not provider-owned`);
    }
  }

  const quantitySources = [
    observer?.response_path,
    observer?.request_character_count_path,
  ].filter(isNonEmptyString);
  if (quantitySources.length !== 1) {
    issues.push(`observer ${key} must declare exactly one usage quantity source`);
  }

  const resourceSelectors = [
    observer?.resource_path,
    observer?.request_resource_path,
    observer?.resource_query_parameter,
    observer?.fixed_resource_id,
    observer?.default_resource_id,
  ].filter(isNonEmptyString);
  if (!isNonEmptyString(observer?.resource_type) || resourceSelectors.length === 0) {
    issues.push(`observer ${key} must declare typed resource identity`);
  }
}

export function validateSdkAttributionAdmission({
  manifest,
  conformance,
  admission,
  packagedManifests,
  consumerTests,
  workflowText,
}) {
  const issues = [];

  if (admission?.schema_version !== "1") {
    issues.push("admission schema_version must be 1");
  }
  if (!sameStringSet(admission?.required_sdks, REQUIRED_SDKS)) {
    issues.push(`admission must require exactly: ${REQUIRED_SDKS.join(", ")}`);
  }
  if (manifest?._meta?.version !== admission?.observer_manifest_version) {
    issues.push("admission observer_manifest_version does not match the manifest");
  }
  if (conformance?.schema_version !== admission?.conformance_schema_version) {
    issues.push("admission conformance_schema_version does not match the corpus");
  }

  if (!Array.isArray(manifest?.observers)) {
    issues.push("observer manifest must contain an observers array");
    return issues;
  }
  if (manifest?._meta?.observer_count !== manifest.observers.length) {
    issues.push("observer manifest count does not match observers.length");
  }

  const observersByKey = new Map();
  for (const observer of manifest.observers) {
    validateObserverShape(observer, issues);
    if (!isNonEmptyString(observer?.service_key)) continue;
    if (observersByKey.has(observer.service_key)) {
      issues.push(`observer service_key is duplicated: ${observer.service_key}`);
    }
    observersByKey.set(observer.service_key, observer);
  }

  for (const path of findForbiddenKeys(manifest)) {
    issues.push(`SDK observer manifest asserts monetary authority at ${path}`);
  }

  if (!Array.isArray(conformance?.cases)) {
    issues.push("conformance corpus must contain a cases array");
    return issues;
  }
  const casesByName = new Map();
  for (const testCase of conformance.cases) {
    if (!isNonEmptyString(testCase?.name)) {
      issues.push("conformance case is missing a name");
      continue;
    }
    if (casesByName.has(testCase.name)) {
      issues.push(`conformance case name is duplicated: ${testCase.name}`);
    }
    casesByName.set(testCase.name, testCase);

    if (!Array.isArray(testCase.expected)) {
      issues.push(`conformance case ${testCase.name} must contain expected[]`);
      continue;
    }
    for (const expected of testCase.expected) {
      const observer = observersByKey.get(expected?.service_key);
      if (observer === undefined) {
        issues.push(
          `conformance case ${testCase.name} emits undeclared observer ${expected?.service_key}`,
        );
        continue;
      }
      for (const [expectedField, observerField] of [
        ["provider_name", "provider_name"],
        ["provider_service", "provider_service"],
        ["component", "component"],
        ["metric", "usage_metric"],
        ["resource_type", "resource_type"],
      ]) {
        if (expected[expectedField] !== observer[observerField]) {
          issues.push(
            `conformance case ${testCase.name} disagrees with ${expected.service_key}.${observerField}`,
          );
        }
      }
      if (!isPositiveDecimal(expected.quantity)) {
        issues.push(
          `conformance case ${testCase.name} must emit a positive exact quantity`,
        );
      }
      for (const path of findForbiddenKeys(expected)) {
        issues.push(
          `conformance case ${testCase.name} asserts monetary authority at ${path}`,
        );
      }
    }
  }

  const admissionsByKey = new Map();
  if (!Array.isArray(admission?.observers)) {
    issues.push("admission manifest must contain an observers array");
  } else {
    for (const entry of admission.observers) {
      if (!isNonEmptyString(entry?.service_key)) {
        issues.push("admission observer is missing service_key");
        continue;
      }
      if (admissionsByKey.has(entry.service_key)) {
        issues.push(`admission service_key is duplicated: ${entry.service_key}`);
      }
      admissionsByKey.set(entry.service_key, entry);
    }
  }

  for (const serviceKey of observersByKey.keys()) {
    if (!admissionsByKey.has(serviceKey)) {
      issues.push(`observer ${serviceKey} lacks an admission declaration`);
    }
  }
  for (const serviceKey of admissionsByKey.keys()) {
    if (!observersByKey.has(serviceKey)) {
      issues.push(`admission declares unknown observer ${serviceKey}`);
    }
  }

  for (const [serviceKey, entry] of admissionsByKey) {
    const observer = observersByKey.get(serviceKey);
    if (observer === undefined) continue;

    for (const field of ["positive_cases", "fail_open_cases"]) {
      if (!uniqueStrings(entry[field]) || entry[field].length === 0) {
        issues.push(`observer ${serviceKey} must declare non-empty ${field}`);
        continue;
      }
      for (const caseName of entry[field]) {
        const testCase = casesByName.get(caseName);
        if (testCase === undefined) {
          issues.push(`observer ${serviceKey} references missing case ${caseName}`);
          continue;
        }
        if (!caseTargetsObserver(testCase, observer)) {
          issues.push(`case ${caseName} does not target observer ${serviceKey}`);
        }
        const emitsService = testCase.expected.some(
          (expected) => expected.service_key === serviceKey,
        );
        if (field === "positive_cases" && !emitsService) {
          issues.push(`positive case ${caseName} does not emit ${serviceKey}`);
        }
        if (field === "fail_open_cases" && emitsService) {
          issues.push(`fail-open case ${caseName} unexpectedly emits ${serviceKey}`);
        }
      }
    }
  }

  for (const sdk of REQUIRED_SDKS) {
    if (!jsonEqual(packagedManifests?.[sdk], manifest)) {
      issues.push(`${sdk} packaged observer manifest differs from canonical`);
    }
    const consumer = consumerTests?.[sdk];
    if (
      !isNonEmptyString(consumer) ||
      !consumer.includes("service_usage_observation_conformance.json") ||
      !consumer.includes("service_usage_observers.json")
    ) {
      issues.push(`${sdk} lacks the shared observer conformance consumer`);
    }
    if (
      !isNonEmptyString(workflowText) ||
      !workflowText.includes(WORKFLOW_CONFORMANCE_MARKERS[sdk])
    ) {
      issues.push(`CI does not execute the ${sdk} observer conformance consumer`);
    }
  }

  if (
    !isNonEmptyString(workflowText) ||
    !workflowText.includes("sdk: [python, go, typescript, rust]")
  ) {
    issues.push("CI cross-SDK matrix must include python, go, typescript, and rust");
  }

  return [...new Set(issues)].sort();
}

async function loadJson(rootDir, relativePath) {
  return JSON.parse(await readFile(join(rootDir, relativePath), "utf8"));
}

export async function loadSdkAttributionAdmissionInputs(rootDir) {
  return {
    manifest: await loadJson(rootDir, "fixtures/service_usage_observers.json"),
    conformance: await loadJson(
      rootDir,
      "fixtures/service_usage_observation_conformance.json",
    ),
    admission: await loadJson(
      rootDir,
      "fixtures/sdk_attribution_admission.json",
    ),
    packagedManifests: Object.fromEntries(
      await Promise.all(
        Object.entries(PACKAGED_MANIFEST_PATHS).map(
          async ([sdk, relativePath]) => [sdk, await loadJson(rootDir, relativePath)],
        ),
      ),
    ),
    consumerTests: Object.fromEntries(
      await Promise.all(
        Object.entries(CONSUMER_TEST_PATHS).map(
          async ([sdk, relativePath]) => [
            sdk,
            await readFile(join(rootDir, relativePath), "utf8"),
          ],
        ),
      ),
    ),
    workflowText: await readFile(join(rootDir, ".github/workflows/ci.yml"), "utf8"),
  };
}

export async function runSdkAttributionAdmission(rootDir) {
  const inputs = await loadSdkAttributionAdmissionInputs(rootDir);
  const issues = validateSdkAttributionAdmission(inputs);
  if (issues.length > 0) {
    throw new Error(
      `SDK attribution admission failed with ${issues.length} issue(s):\n` +
        issues.map((issue) => `- ${issue}`).join("\n"),
    );
  }
  return {
    observers: inputs.manifest.observers.length,
    cases: inputs.conformance.cases.length,
    sdks: REQUIRED_SDKS.length,
  };
}

const scriptPath = fileURLToPath(import.meta.url);
const isMain = process.argv[1] && resolve(process.argv[1]) === resolve(scriptPath);
if (isMain) {
  const rootDir = resolve(dirname(scriptPath), "..", "..");
  try {
    const result = await runSdkAttributionAdmission(rootDir);
    console.log(
      `SDK attribution admission passed: ${result.observers} observers, ` +
        `${result.cases} cases, ${result.sdks} SDKs`,
    );
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
