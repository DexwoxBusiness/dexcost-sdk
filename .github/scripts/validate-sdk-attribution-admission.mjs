import { readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const REQUIRED_SDKS = Object.freeze([
  "typescript",
  "python",
]);

const PACKAGED_MANIFEST_PATHS = Object.freeze({
  typescript: "typescript/src/data/service_usage_observers.json",
  python: "python/src/dexcost/data/service_usage_observers.json",
});

const CONSUMER_TEST_PATHS = Object.freeze({
  typescript: "typescript/tests/service-usage-observer-conformance.test.ts",
  python: "python/tests/test_service_usage_observer_conformance.py",
});

const WORKFLOW_CONFORMANCE_MARKERS = Object.freeze({
  typescript: "tests/service-usage-observer-conformance.test.ts",
  python: "tests/test_service_usage_observer_conformance.py",
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

// Some providers host their API reference on a different first-party domain
// from the request endpoint. Keep this allowlist explicit and provider-scoped
// so a documentation CDN cannot become a generic provenance bypass.
const OFFICIAL_PROVIDER_DOCUMENTATION_ROOTS = Object.freeze({
  aws: Object.freeze([
    Object.freeze({ apiRoot: "amazonaws.com", documentationRoot: "amazon.com" }),
    Object.freeze({ apiRoot: "api.aws", documentationRoot: "amazon.com" }),
  ]),
  azure: Object.freeze([
    Object.freeze({ apiRoot: "microsofttranslator.com", documentationRoot: "microsoft.com" }),
    Object.freeze({ apiRoot: "azure.com", documentationRoot: "microsoft.com" }),
  ]),
  google: Object.freeze([
    Object.freeze({ apiRoot: "googleapis.com", documentationRoot: "google.com" }),
  ]),
});

const DOMAIN_SUFFIX_PATTERN = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const CANONICAL_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;

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

function resolveCollectionPath(value, path) {
  let current = [value];
  for (const rawPart of path.split(".")) {
    const expands = rawPart.endsWith("[]");
    const part = expands ? rawPart.slice(0, -2) : rawPart;
    if (!part) return undefined;
    const next = [];
    for (const candidate of current) {
      if (!isObject(candidate) || !(part in candidate)) return undefined;
      const resolved = candidate[part];
      if (expands) {
        if (!Array.isArray(resolved)) return undefined;
        next.push(...resolved);
      } else {
        next.push(resolved);
      }
    }
    current = next;
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
  if (predicate.operator === "all_non_empty") {
    return values.length > 0 && values.every((value) => value.trim().length > 0);
  }
  if (predicate.operator === "equals") {
    return values.length === 1 && values[0] === predicate.value;
  }
  if (predicate.operator === "absent_or_equals") {
    return !url.searchParams.has(predicate.parameter) ||
      (values.length === 1 && values[0] === predicate.value);
  }
  return false;
}

function validQueryPredicate(predicate) {
  if (!isObject(predicate) || !isNonEmptyString(predicate.parameter)) return false;
  if (["present", "truthy", "all_non_empty"].includes(predicate.operator)) {
    return Object.keys(predicate).length === 2 && predicate.value === undefined;
  }
  return ["equals", "absent_or_equals"].includes(predicate.operator) &&
    Object.keys(predicate).length === 3 && isNonEmptyString(predicate.value);
}

function observerDomainMatches(observer, hostname) {
  return (Array.isArray(observer.domains) && observer.domains.includes(hostname)) ||
    (Array.isArray(observer.domain_suffixes) &&
      observer.domain_suffixes.some((suffix) => hostname.endsWith(`.${suffix}`)));
}

function observerEndpointMatches(observer, pathname, boundary = false) {
  if (!Array.isArray(observer.endpoints)) return false;
  const matchMode = boundary ? "prefix" : (observer.endpoint_match ?? "prefix");
  return observer.endpoints.some((endpoint) =>
    pathname === endpoint ||
    (matchMode === "prefix" &&
      (endpoint === "/" || pathname.startsWith(`${endpoint}/`)))
  );
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

function requestPredicateMatches(request, predicate) {
  if (!isObject(predicate) || !isNonEmptyString(predicate.path)) return false;
  const value = resolvePath(request, predicate.path);
  if (value === undefined || value === null) {
    return predicate.operator.startsWith("absent_or_");
  }
  if (predicate.operator === "equals") return value === predicate.value;
  if (predicate.operator === "not_equals") return value !== predicate.value;
  if (predicate.operator === "string_not_contains") {
    return typeof value === "string" && !value.includes(predicate.value);
  }
  if (predicate.operator === "array_contains") {
    return Array.isArray(value) && value.some((item) => item === predicate.value);
  }
  if (predicate.operator === "absent_or_empty_collection") {
    return Array.isArray(value) && value.length === 0;
  }
  if (predicate.operator === "absent_or_false_or_null") return value === false;
  return predicate.operator === "absent_or_lte" &&
    typeof value === "number" && Number.isFinite(value) &&
    value <= predicate.value;
}

function requestHeaderPredicateMatches(requestHeaders, predicate) {
  const normalized = new Map();
  if (Array.isArray(requestHeaders)) {
    for (const name of requestHeaders) {
      if (typeof name === "string") normalized.set(name.toLowerCase(), undefined);
    }
  } else if (isObject(requestHeaders)) {
    for (const [name, value] of Object.entries(requestHeaders)) {
      if (typeof value === "string") normalized.set(name.toLowerCase(), value);
    }
  }
  const present = normalized.has(predicate.name);
  if (predicate.operator === "present") return present;
  if (predicate.operator === "absent") return !present;
  const value = normalized.get(predicate.name);
  if (predicate.operator === "equals") return present && value === predicate.value;
  return present && typeof value === "string" &&
    Array.isArray(predicate.values) && predicate.values.includes(value);
}

function collectionPredicateMatches(value, predicate) {
  const resolved = resolveCollectionPath(value, predicate.path);
  if (resolved === undefined) return false;
  const contains = resolved.some((item) => item === predicate.value);
  return predicate.operator === "contains" ? contains : !contains;
}

function caseTargetsObserver(testCase, observer) {
  let url;
  try {
    url = new URL(testCase.url);
  } catch {
    return false;
  }

  const domainMatches = observerDomainMatches(observer, url.hostname);
  const endpointMatches = observerEndpointMatches(observer, url.pathname) &&
    !(Array.isArray(observer.excluded_endpoints) &&
      observer.excluded_endpoints.includes(url.pathname));
  if (!domainMatches || !endpointMatches) return false;

  if (Number.isInteger(observer.provider_region_domain_label)) {
    const candidate = url.hostname.split(".")[observer.provider_region_domain_label];
    if (!observer.allowed_provider_regions?.includes(candidate)) return false;
  }

  if (
    Array.isArray(observer.request_all) &&
    !observer.request_all.every((predicate) =>
      requestPredicateMatches(testCase.request, predicate),
    )
  ) {
    return false;
  }

  if (
    Array.isArray(observer.request_header_all) &&
    !observer.request_header_all.every((predicate) =>
      requestHeaderPredicateMatches(testCase.request_headers, predicate),
    )
  ) {
    return false;
  }

  if (isNonEmptyString(observer.request_collection_count_path)) {
    const collection = resolveCollectionPath(
      testCase.request,
      observer.request_collection_count_path,
    );
    const pairedResponses = isNonEmptyString(observer.paired_response_collection_path)
      ? resolveCollectionPath(testCase.response, observer.paired_response_collection_path)
      : undefined;
    if (
      collection === undefined ||
      !Array.isArray(observer.request_collection_all) ||
      (isNonEmptyString(observer.paired_response_collection_path) &&
        (!Array.isArray(observer.paired_response_all) || pairedResponses === undefined ||
          pairedResponses.length !== collection.length)) ||
      !collection.some((item, index) => observer.request_collection_all.every((predicate) =>
        collectionPredicateMatches(item, predicate)) &&
        (pairedResponses === undefined || observer.paired_response_all.every((predicate) =>
          requestPredicateMatches(pairedResponses[index], predicate))))
    ) {
      return false;
    }
  }

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

  if (isNonEmptyString(observer.response_collection_sum_path)) {
    const values = resolveCollectionPath(
      testCase.response,
      observer.response_collection_sum_path,
    );
    if (
      values === undefined ||
      values.length === 0 ||
      values.some((value) =>
        !["number", "string"].includes(typeof value) ||
        (typeof value === "string" && value.trim().length === 0) ||
        !Number.isFinite(Number(value)) ||
        Number(value) < 0) ||
      values.reduce((sum, value) => sum + Number(value), 0) <= 0
    ) {
      return false;
    }
  }

  return true;
}

function validRequestPredicate(predicate) {
  if (!isObject(predicate) || !isNonEmptyString(predicate.path)) return false;
  if (predicate.operator === "equals" || predicate.operator === "not_equals") {
    return Object.keys(predicate).length === 3 &&
      (typeof predicate.value === "boolean" ||
        (typeof predicate.value === "number" && Number.isFinite(predicate.value)) ||
        isNonEmptyString(predicate.value));
  }
  if (predicate.operator === "string_not_contains") {
    return Object.keys(predicate).length === 3 && isNonEmptyString(predicate.value);
  }
  if (
    predicate.operator === "absent_or_null" ||
    predicate.operator === "absent_or_false_or_null" ||
    predicate.operator === "absent_or_empty_collection"
  ) {
    return Object.keys(predicate).length === 2 && predicate.value === undefined;
  }
  if (predicate.operator === "array_contains") {
    return Object.keys(predicate).length === 3 &&
      (typeof predicate.value === "boolean" ||
        (typeof predicate.value === "number" && Number.isFinite(predicate.value)) ||
        isNonEmptyString(predicate.value));
  }
  return predicate.operator === "absent_or_lte" &&
    Object.keys(predicate).length === 3 &&
    typeof predicate.value === "number" && Number.isFinite(predicate.value);
}

function validRequestHeaderPredicate(predicate) {
  if (
    !isObject(predicate) ||
    typeof predicate.name !== "string" ||
    !/^[a-z0-9!#$%&'*+.^_`|~-]+$/.test(predicate.name) ||
    predicate.name !== predicate.name.toLowerCase()
  ) return false;
  if (["present", "absent"].includes(predicate.operator)) {
    return Object.keys(predicate).length === 2;
  }
  if (predicate.operator === "equals") {
    return Object.keys(predicate).length === 3 &&
      isNonEmptyString(predicate.value) && predicate.value.length <= 256;
  }
  return predicate.operator === "one_of" &&
    Object.keys(predicate).length === 3 &&
    uniqueStrings(predicate.values) &&
    predicate.values.length > 0 && predicate.values.length <= 100 &&
    predicate.values.every((value) => value.length <= 256);
}

function validCollectionPredicate(predicate) {
  return isObject(predicate) &&
    Object.keys(predicate).length === 3 &&
    isNonEmptyString(predicate.path) &&
    ["contains", "not_contains"].includes(predicate.operator) &&
    (typeof predicate.value === "string" || typeof predicate.value === "boolean" ||
      (typeof predicate.value === "number" && Number.isFinite(predicate.value)));
}

function caseExercisesObserverEndpointBoundary(testCase, observer) {
  let url;
  try {
    url = new URL(testCase.url);
  } catch {
    return false;
  }
  if (
    Array.isArray(observer.query_all) &&
    !observer.query_all.every((predicate) => queryPredicateMatches(url, predicate))
  ) {
    return false;
  }
  return (
    observerDomainMatches(observer, url.hostname) &&
    observerEndpointMatches(observer, url.pathname, true) &&
    !caseTargetsObserver(testCase, observer)
  );
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
    observer?.domain_suffixes !== undefined &&
    (!Array.isArray(observer.domain_suffixes) ||
      observer.domain_suffixes.length === 0 ||
      !observer.domain_suffixes.every(
        (suffix) =>
          isNonEmptyString(suffix) &&
          suffix.length <= 253 &&
          DOMAIN_SUFFIX_PATTERN.test(suffix),
      ))
  ) {
    issues.push(`observer ${key} has invalid domain suffixes`);
  }
  if (
    (observer?.provider_region_domain_label !== undefined) !==
      (observer?.allowed_provider_regions !== undefined) ||
    (observer?.provider_region_domain_label !== undefined && (
      !Number.isInteger(observer.provider_region_domain_label) ||
      observer.provider_region_domain_label < 0 ||
      observer.provider_region_domain_label > 10 ||
      !uniqueStrings(observer.allowed_provider_regions) ||
      observer.allowed_provider_regions.length === 0 ||
      observer.allowed_provider_regions.length > 100 ||
      !observer.allowed_provider_regions.every((region) => CANONICAL_NAME.test(region))
    ))
  ) {
    issues.push(`observer ${key} has invalid provider-region extraction`);
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
  if (
    observer?.excluded_endpoints !== undefined &&
    (!uniqueStrings(observer.excluded_endpoints) ||
      observer.excluded_endpoints.length === 0 ||
      !observer.excluded_endpoints.every((endpoint) => endpoint.startsWith("/")))
  ) {
    issues.push(`observer ${key} has invalid excluded endpoints`);
  }
  if (
    observer?.request_all !== undefined &&
    (!Array.isArray(observer.request_all) ||
      observer.request_all.length === 0 ||
      !observer.request_all.every(validRequestPredicate))
  ) {
    issues.push(`observer ${key} has invalid request predicates`);
  }
  if (
    observer?.request_header_all !== undefined &&
    (!Array.isArray(observer.request_header_all) ||
      observer.request_header_all.length === 0 ||
      !observer.request_header_all.every(validRequestHeaderPredicate))
  ) {
    issues.push(`observer ${key} has invalid request-header predicates`);
  }
  if (
    observer?.request_collection_all !== undefined &&
    (!Array.isArray(observer.request_collection_all) ||
      observer.request_collection_all.length === 0 ||
      !observer.request_collection_all.every(validCollectionPredicate))
  ) {
    issues.push(`observer ${key} has invalid request-collection predicates`);
  }
  if (
    isNonEmptyString(observer?.request_collection_count_path) !==
    Array.isArray(observer?.request_collection_all)
  ) {
    issues.push(`observer ${key} must pair collection count and predicates`);
  }
  if (
    observer?.paired_response_all !== undefined &&
    (!Array.isArray(observer.paired_response_all) ||
      observer.paired_response_all.length === 0 ||
      !observer.paired_response_all.every(validRequestPredicate))
  ) {
    issues.push(`observer ${key} has invalid paired-response predicates`);
  }
  if (
    isNonEmptyString(observer?.paired_response_collection_path) !==
    Array.isArray(observer?.paired_response_all) ||
    (isNonEmptyString(observer?.paired_response_collection_path) &&
      !isNonEmptyString(observer?.request_collection_count_path))
  ) {
    issues.push(`observer ${key} must pair response and request collections`);
  }
  for (const field of ["query_any", "query_all"]) {
    if (
      observer?.[field] !== undefined &&
      (!Array.isArray(observer[field]) ||
        observer[field].length === 0 ||
        !observer[field].every(validQueryPredicate))
    ) {
      issues.push(`observer ${key} has invalid ${field} predicates`);
    }
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
      [...(observer?.domains ?? []), ...(observer?.domain_suffixes ?? [])].map(domainRoot),
    );
    const sourceRoot = domainRoot(sourceUrl.hostname);
    const documentationMappings = OFFICIAL_PROVIDER_DOCUMENTATION_ROOTS[
      observer?.provider_name
    ] ?? [];
    const isMappedFirstPartySource = documentationMappings.some(
      ({ apiRoot, documentationRoot }) =>
        providerDomainRoots.has(apiRoot) && sourceRoot === documentationRoot,
    );
    if (
      !providerDomainRoots.has(sourceRoot) &&
      !isMappedFirstPartySource
    ) {
      issues.push(`observer ${key} source is not provider-owned`);
    }
  }

  const quantitySources = [
    observer?.response_path,
    observer?.response_collection_sum_path,
    observer?.response_quantity_header,
    observer?.request_character_count_path,
    observer?.request_collection_count_path,
    observer?.fixed_quantity,
  ].filter(isNonEmptyString);
  if (quantitySources.length !== 1) {
    issues.push(`observer ${key} must declare exactly one usage quantity source`);
  }
  if (observer?.fixed_quantity !== undefined && observer.fixed_quantity !== "1") {
    issues.push(`observer ${key} fixed_quantity must be exactly 1`);
  }
  if (
    (observer?.fixed_quantity === "1") !==
    (observer?.usage_metric === "request_count")
  ) {
    issues.push(
      `observer ${key} fixed_quantity and request_count must be declared together`,
    );
  }
  const hasCharacterCount = isNonEmptyString(observer?.request_character_count_path) ||
    isNonEmptyString(observer?.request_character_count_query_parameter);
  if (
    observer?.character_count_encoding !== undefined &&
    !["unicode_code_points", "utf16_code_units"].includes(
      observer.character_count_encoding,
    )
  ) {
    issues.push(`observer ${key} has invalid character count encoding`);
  }
  if (observer?.character_count_encoding !== undefined && !hasCharacterCount) {
    issues.push(`observer ${key} character encoding lacks a character quantity source`);
  }
  if (
    observer?.request_character_count_case_insensitive !== undefined &&
    observer.request_character_count_case_insensitive !== true
  ) {
    issues.push(`observer ${key} has invalid case-insensitive character path flag`);
  }
  if (
    observer?.request_character_count_case_insensitive === true &&
    !isNonEmptyString(observer?.request_character_count_path)
  ) {
    issues.push(`observer ${key} case-insensitive character path lacks a body path`);
  }
  if (observer?.quantity_multiplier_query_parameter_count !== undefined) {
    const parameter = observer.quantity_multiplier_query_parameter_count;
    if (!isNonEmptyString(parameter) || !hasCharacterCount) {
      issues.push(`observer ${key} has invalid query-count multiplier`);
    }
    if (observer.quantity_multiplier_path !== undefined) {
      issues.push(`observer ${key} combines incompatible quantity multipliers`);
    }
    if (!observer.query_all?.some((predicate) =>
      predicate.parameter === parameter && predicate.operator === "all_non_empty")) {
      issues.push(`observer ${key} query-count multiplier lacks a non-empty predicate`);
    }
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
        const targetsObserver = caseTargetsObserver(testCase, observer);
        const exercisesEndpointBoundary =
          field === "fail_open_cases" &&
          caseExercisesObserverEndpointBoundary(testCase, observer);
        if (!targetsObserver && !exercisesEndpointBoundary) {
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
    !workflowText.includes("sdk: [python, typescript]")
  ) {
    issues.push("CI cross-SDK matrix must include paired python and typescript SDKs");
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
