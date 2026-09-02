/** Provider-owned usage measurement for services withheld from SDK pricing. */

import { createRequire } from "node:module";
import { Decimal } from "../core/models.js";

export type ObservedUsageMetric =
  | "input_tokens"
  | "input_image_tokens"
  | "output_image_tokens"
  | "output_tokens"
  | "audio_seconds"
  | "characters"
  | "image_count"
  | "request_count"
  | "credit_count";
export type ObservedAttributionComponent = "external" | "speech_to_text" | "text_to_speech";
export type ObservedResourceType = "model" | "sku";

const DOMAIN_SUFFIX_PATTERN = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

interface QueryPredicate {
  parameter: string;
  operator: "present" | "truthy" | "equals" | "absent_or_equals" | "all_non_empty";
  value?: string;
}

interface ResourceVariant {
  query_parameter: string;
  equals: string;
  matched_suffix: string;
  default_suffix: string;
}

interface ResponsePredicate {
  path: string;
  operator: "equals" | "collection_all_equals" | "one_of" | "non_empty";
  value?: string | number | boolean;
  values?: Array<string | number | boolean>;
}

interface RequestPredicate {
  path: string;
  operator: "absent_or_null" | "absent_or_false_or_null" | "absent_or_lte" |
    "equals" | "not_equals" | "string_not_contains" | "array_contains" |
    "absent_or_empty_collection";
  value?: number | string | boolean;
}

type RequestHeaderPredicate = {
  name: string;
  operator: "present";
} | {
  name: string;
  operator: "absent";
} | {
  name: string;
  operator: "equals";
  value: string;
} | {
  name: string;
  operator: "basic_username_prefix";
  value: string;
} | {
  name: string;
  operator: "one_of";
  values: string[];
};

export type ObservedRequestHeaders = Readonly<Record<string, string | undefined>>;

interface CollectionPredicate {
  path: string;
  operator: "contains" | "not_contains";
  value: string | boolean | number;
}

export interface ObservedBillingDimension {
  key: string;
  value: { type: "string"; value: string };
}

interface UsageObserverDefinition {
  service_key: string;
  provider_name: string;
  provider_service: string;
  component: ObservedAttributionComponent;
  domains: string[];
  domain_suffixes?: string[];
  endpoints: string[];
  excluded_endpoints?: string[];
  endpoint_match?: "exact" | "prefix" | "path_template";
  methods?: string[];
  response_path?: string;
  response_collection_sum_path?: string;
  response_quantity_header?: string;
  response_all?: ResponsePredicate[];
  request_all?: RequestPredicate[];
  request_header_all?: RequestHeaderPredicate[];
  provider_region_domain_label?: number;
  allowed_provider_regions?: string[];
  request_collection_count_path?: string;
  request_collection_all?: CollectionPredicate[];
  paired_response_collection_path?: string;
  paired_response_all?: RequestPredicate[];
  request_character_count_path?: string;
  request_character_count_query_parameter?: string;
  request_character_count_case_insensitive?: true;
  character_count_encoding?: "unicode_code_points" | "utf16_code_units";
  minimum_quantity?: "1";
  fixed_quantity?: "1";
  usage_metric: ObservedUsageMetric;
  resource_type?: ObservedResourceType;
  resource_path?: string;
  request_resource_path?: string;
  allowed_resource_ids?: string[];
  resource_id_prefix_to_strip?: string;
  resource_query_parameter?: string;
  default_resource_id?: string;
  fixed_resource_id?: string;
  resource_variant?: ResourceVariant;
  query_any?: QueryPredicate[];
  query_all?: QueryPredicate[];
  quantity_multiplier_path?: string;
  quantity_multiplier_query_parameter?: string;
  quantity_multiplier_query_parameter_count?: string;
  record_id_path?: string;
  record_id_header?: string;
  provider_cost_usd_path?: string;
  provider_cost_usd_collection_sum_path?: string;
  provider_cost_minor_units_path?: string;
  provider_cost_currency_path?: string;
  provider_cost_minor_unit_exponent?: number;
  source_url: string;
}

interface UsageObserverManifest {
  _meta: { version: string; observer_count: number; purpose: string };
  observers: UsageObserverDefinition[];
}

export interface ServiceUsageObservation {
  serviceKey: string;
  providerName: string;
  providerService: string;
  component: ObservedAttributionComponent;
  metric: ObservedUsageMetric;
  quantity: string;
  resourceType?: ObservedResourceType;
  resourceId?: string;
  providerRecordId?: string;
  providerRegion?: string;
  providerCostUsd?: string;
  providerCostAmount?: string;
  providerCostCurrency?: string;
  dimensions?: ObservedBillingDimension[];
  manifestVersion: string;
}

const CANONICAL_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const METRICS = new Set<ObservedUsageMetric>([
  "input_tokens",
  "input_image_tokens",
  "output_image_tokens",
  "output_tokens",
  "audio_seconds",
  "characters",
  "image_count",
  "request_count",
  "credit_count",
]);
const COMPONENTS = new Set<ObservedAttributionComponent>(["external", "speech_to_text", "text_to_speech"]);
const RESOURCE_TYPES = new Set<ObservedResourceType>(["model", "sku"]);

function resolvePath(value: unknown, path: string): unknown {
  let current = value;
  for (const part of path.split(".")) {
    if (current === null || typeof current !== "object" || !(part in current)) return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function boundedString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() !== ""
    ? value.trim().slice(0, 256)
    : undefined;
}

function resolveCollectionPath(value: unknown, path: string): unknown[] | undefined {
  let current: unknown[] = [value];
  for (const rawPart of path.split(".")) {
    const expands = rawPart.endsWith("[]");
    const part = expands ? rawPart.slice(0, -2) : rawPart;
    if (part.length === 0) return undefined;
    const next: unknown[] = [];
    for (const candidate of current) {
      if (
        candidate === null || typeof candidate !== "object" || Array.isArray(candidate) ||
        !(part in candidate)
      ) {
        return undefined;
      }
      const resolved = (candidate as Record<string, unknown>)[part];
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

function resolveCaseInsensitivePath(value: unknown, path: string): unknown {
  let current = value;
  for (const part of path.split(".")) {
    if (current === null || typeof current !== "object" || Array.isArray(current)) {
      return undefined;
    }
    const keys = Object.keys(current).filter((key) => key.toLowerCase() === part.toLowerCase());
    if (keys.length !== 1) return undefined;
    current = (current as Record<string, unknown>)[keys[0]];
  }
  return current;
}

function resolveCharacterCountPath(
  value: unknown,
  path: string,
  caseInsensitive: boolean,
): unknown {
  const resolver = caseInsensitive ? resolveCaseInsensitivePath : resolvePath;
  if (!Array.isArray(value)) return resolver(value, path);
  const resolved = value.map((item) => resolver(item, path));
  return resolved.some((item) => item === undefined) ? undefined : resolved;
}

function textCharacterCount(
  value: string,
  encoding: "unicode_code_points" | "utf16_code_units",
): number {
  return encoding === "utf16_code_units" ? value.length : Array.from(value).length;
}

function characterCount(
  value: unknown,
  encoding: "unicode_code_points" | "utf16_code_units" = "unicode_code_points",
): number | undefined {
  if (typeof value === "string") return textCharacterCount(value, encoding);
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    return undefined;
  }
  return value.reduce((total, item) => total + textCharacterCount(item, encoding), 0);
}

function positiveDecimal(value: unknown): string | undefined {
  if (typeof value !== "number" && typeof value !== "string") return undefined;
  try {
    const decimal = new Decimal(value);
    if (!decimal.isFinite() || !decimal.gt(0)) return undefined;
    return decimal.toFixed().replace(/(?:\.0+|(?:(\.\d*?)0+))$/, "$1");
  } catch {
    return undefined;
  }
}

function endpointMatches(
  pathname: string,
  endpoint: string,
  mode: "exact" | "prefix" | "path_template" = "prefix",
): boolean {
  if (mode === "path_template") {
    const pathSegments = pathname.split("/");
    const templateSegments = endpoint.split("/");
    return pathSegments.length === templateSegments.length &&
      templateSegments.every((segment, index) =>
        segment === "{id}" ? pathSegments[index].length > 0 : segment === pathSegments[index]);
  }
  return pathname === endpoint ||
    (mode === "prefix" && (endpoint === "/" || pathname.startsWith(`${endpoint}/`)));
}

function endpointBoundaryMatches(
  pathname: string,
  endpoint: string,
  mode: "exact" | "prefix" | "path_template" = "prefix",
): boolean {
  if (mode !== "path_template") return endpointMatches(pathname, endpoint, "prefix");
  const boundary = endpoint.slice(0, endpoint.indexOf("/{id}"));
  return pathname === boundary || pathname.startsWith(`${boundary}/`);
}

function requestHeaderPredicateMatches(
  requestHeaders: ReadonlyMap<string, string | undefined>,
  predicate: RequestHeaderPredicate,
): boolean {
  const name = predicate.name.toLowerCase();
  const present = requestHeaders.has(name);
  if (predicate.operator === "present") return present;
  if (predicate.operator === "absent") return !present;
  const value = requestHeaders.get(name);
  if (predicate.operator === "basic_username_prefix") {
    return value === `basic_username_prefix:${predicate.value}` ||
      basicUsernameHasPrefix(value, predicate.value);
  }
  if (predicate.operator === "equals") return present && value === predicate.value;
  return present && value !== undefined && predicate.values.includes(value);
}

function basicUsernameHasPrefix(value: unknown, prefix: string): boolean {
  if (typeof value !== "string" || !value.toLowerCase().startsWith("basic ")) return false;
  try {
    const decoded = globalThis.atob(value.slice(6).trim());
    const separator = decoded.indexOf(":");
    return separator >= 0 && decoded.slice(0, separator).startsWith(prefix);
  } catch {
    return false;
  }
}

function validRequestHeaderPredicate(predicate: unknown): predicate is RequestHeaderPredicate {
  if (predicate === null || typeof predicate !== "object") return false;
  const candidate = predicate as {
    name?: unknown;
    operator?: unknown;
    value?: unknown;
    values?: unknown;
  };
  if (
    typeof candidate.name !== "string" ||
    !/^[a-z0-9!#$%&'*+.^_`|~-]+$/.test(candidate.name) ||
    candidate.name !== candidate.name.toLowerCase()
  ) return false;
  if (candidate.operator === "present" || candidate.operator === "absent") {
    return Object.keys(candidate).length === 2;
  }
  if (candidate.operator === "equals" || candidate.operator === "basic_username_prefix") {
    return Object.keys(candidate).length === 3 &&
      typeof candidate.value === "string" && candidate.value.length > 0 &&
      candidate.value.length <= 256;
  }
  return candidate.operator === "one_of" &&
    Object.keys(candidate).length === 3 &&
    Array.isArray(candidate.values) && candidate.values.length > 0 &&
    candidate.values.length <= 100 &&
    candidate.values.every((value) =>
      typeof value === "string" && value.length > 0 && value.length <= 256) &&
    new Set(candidate.values).size === candidate.values.length;
}

function collectionPredicateMatches(value: unknown, predicate: CollectionPredicate): boolean {
  const resolved = resolveCollectionPath(value, predicate.path);
  if (resolved === undefined) return false;
  const contains = resolved.some((item) => item === predicate.value);
  return predicate.operator === "contains" ? contains : !contains;
}

function validCollectionPredicate(predicate: unknown): predicate is CollectionPredicate {
  if (predicate === null || typeof predicate !== "object") return false;
  const candidate = predicate as Partial<CollectionPredicate>;
  return Object.keys(candidate).length === 3 &&
    typeof candidate.path === "string" && candidate.path.length > 0 &&
    (candidate.operator === "contains" || candidate.operator === "not_contains") &&
    (typeof candidate.value === "string" || typeof candidate.value === "boolean" ||
      (typeof candidate.value === "number" && Number.isFinite(candidate.value)));
}

function queryValueIsTruthy(value: string | null): boolean {
  if (value === null) return false;
  return !new Set(["", "0", "false", "no", "off"]).has(value.trim().toLowerCase());
}

function predicateMatches(url: URL, predicate: QueryPredicate): boolean {
  const values = url.searchParams.getAll(predicate.parameter);
  if (predicate.operator === "present") return url.searchParams.has(predicate.parameter);
  if (predicate.operator === "truthy") return values.some(queryValueIsTruthy);
  if (predicate.operator === "all_non_empty") {
    return values.length > 0 && values.every((value) => value.trim().length > 0);
  }
  if (predicate.operator === "equals") {
    return values.length === 1 && values[0] === predicate.value;
  }
  return predicate.operator === "absent_or_equals" &&
    (!url.searchParams.has(predicate.parameter) ||
      (values.length === 1 && values[0] === predicate.value));
}

function validQueryPredicate(predicate: unknown): predicate is QueryPredicate {
  if (predicate === null || typeof predicate !== "object") return false;
  const candidate = predicate as Partial<QueryPredicate>;
  if (typeof candidate.parameter !== "string" || candidate.parameter.length === 0) return false;
  if (candidate.operator === "present" || candidate.operator === "truthy" ||
      candidate.operator === "all_non_empty") {
    return Object.keys(candidate).length === 2 && candidate.value === undefined;
  }
  return (candidate.operator === "equals" || candidate.operator === "absent_or_equals") &&
    Object.keys(candidate).length === 3 &&
    typeof candidate.value === "string" && candidate.value.length > 0;
}

function domainMatches(hostname: string, observer: UsageObserverDefinition): boolean {
  return observer.domains.includes(hostname) ||
    observer.domain_suffixes?.some((suffix) => hostname.endsWith(`.${suffix}`)) === true;
}

function responsePredicateMatches(value: unknown, predicate: ResponsePredicate): boolean {
  if (predicate.operator === "collection_all_equals") {
    const resolved = resolveCollectionPath(value, predicate.path);
    return resolved !== undefined && resolved.length > 0 &&
      resolved.every((candidate) => candidate === predicate.value);
  }
  const resolved = resolvePath(value, predicate.path);
  if (predicate.operator === "equals") return resolved === predicate.value;
  if (predicate.operator === "one_of") {
    return predicate.values?.some((candidate) => resolved === candidate) === true;
  }
  if (typeof resolved === "string") return resolved.trim().length > 0;
  if (Array.isArray(resolved)) return resolved.length > 0;
  return resolved !== null && typeof resolved === "object" && Object.keys(resolved).length > 0;
}

function validResponsePredicate(predicate: unknown): predicate is ResponsePredicate {
  if (predicate === null || typeof predicate !== "object") return false;
  const candidate = predicate as Partial<ResponsePredicate>;
  if (typeof candidate.path !== "string" || candidate.path.length === 0) return false;
  if (candidate.operator === "non_empty") {
    return Object.keys(candidate).length === 2 &&
      candidate.value === undefined && candidate.values === undefined;
  }
  if (candidate.operator === "one_of") {
    return Object.keys(candidate).length === 3 && candidate.value === undefined &&
      Array.isArray(candidate.values) && candidate.values.length > 0 &&
      candidate.values.length <= 20 &&
      candidate.values.every((value) =>
        typeof value === "string" || typeof value === "boolean" ||
        (typeof value === "number" && Number.isFinite(value))) &&
      new Set(candidate.values.map((value) => `${typeof value}:${String(value)}`)).size ===
        candidate.values.length;
  }
  return (candidate.operator === "equals" || candidate.operator === "collection_all_equals") &&
    Object.keys(candidate).length === 3 &&
    (typeof candidate.value === "string" ||
      typeof candidate.value === "boolean" ||
      (typeof candidate.value === "number" && Number.isFinite(candidate.value)));
}

function requestPredicateMatches(value: unknown, predicate: RequestPredicate): boolean {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const resolved = resolvePath(value, predicate.path);
  if (resolved === undefined || resolved === null) {
    return predicate.operator.startsWith("absent_or_");
  }
  if (predicate.operator === "equals") return resolved === predicate.value;
  if (predicate.operator === "not_equals") return resolved !== predicate.value;
  if (predicate.operator === "string_not_contains") {
    return typeof resolved === "string" &&
      typeof predicate.value === "string" &&
      !resolved.includes(predicate.value);
  }
  if (predicate.operator === "array_contains") {
    return Array.isArray(resolved) && resolved.some((item) => item === predicate.value);
  }
  if (predicate.operator === "absent_or_empty_collection") {
    return Array.isArray(resolved) && resolved.length === 0;
  }
  if (predicate.operator === "absent_or_false_or_null") return resolved === false;
  return predicate.operator === "absent_or_lte" &&
    typeof resolved === "number" && Number.isFinite(resolved) &&
    typeof predicate.value === "number" && resolved <= predicate.value;
}

function validRequestPredicate(predicate: unknown): predicate is RequestPredicate {
  if (predicate === null || typeof predicate !== "object") return false;
  const candidate = predicate as Partial<RequestPredicate>;
  if (typeof candidate.path !== "string" || candidate.path.length === 0) return false;
  if (candidate.operator === "absent_or_null" ||
      candidate.operator === "absent_or_false_or_null" ||
      candidate.operator === "absent_or_empty_collection") {
    return Object.keys(candidate).length === 2 && candidate.value === undefined;
  }
  if (candidate.operator === "equals" || candidate.operator === "not_equals") {
    return Object.keys(candidate).length === 3 &&
      (typeof candidate.value === "boolean" ||
        (typeof candidate.value === "number" && Number.isFinite(candidate.value)) ||
        (typeof candidate.value === "string" && candidate.value.length > 0));
  }
  if (candidate.operator === "string_not_contains") {
    return Object.keys(candidate).length === 3 &&
      typeof candidate.value === "string" && candidate.value.length > 0;
  }
  if (candidate.operator === "array_contains") {
    return Object.keys(candidate).length === 3 &&
      (typeof candidate.value === "boolean" ||
        (typeof candidate.value === "number" && Number.isFinite(candidate.value)) ||
        (typeof candidate.value === "string" && candidate.value.length > 0));
  }
  return candidate.operator === "absent_or_lte" &&
    Object.keys(candidate).length === 3 &&
    typeof candidate.value === "number" && Number.isFinite(candidate.value);
}

function pathDimensions(url: URL, definition: UsageObserverDefinition): ObservedBillingDimension[] {
  if (definition.service_key !== "elevenlabs_tts") return [];
  const prefix = "/v1/text-to-speech/";
  if (!url.pathname.startsWith(prefix)) return [];
  const encoded = url.pathname.slice(prefix.length).split("/", 1)[0];
  if (encoded === "") return [];
  let decoded: string;
  try {
    decoded = decodeURIComponent(encoded);
  } catch {
    return [];
  }
  const value = boundedString(decoded);
  return value === undefined ? [] : [{ key: "voice_id", value: { type: "string", value } }];
}

function validateManifest(raw: unknown): UsageObserverManifest {
  if (raw === null || typeof raw !== "object") throw new Error("usage observer manifest must be an object");
  const manifest = raw as Partial<UsageObserverManifest>;
  if (
    manifest._meta === undefined ||
    typeof manifest._meta.version !== "string" ||
    !Number.isInteger(manifest._meta.observer_count) ||
    !Array.isArray(manifest.observers) ||
    manifest._meta.observer_count !== manifest.observers.length
  ) {
    throw new Error("usage observer manifest metadata is inconsistent");
  }
  const keys = new Set<string>();
  for (const observer of manifest.observers) {
    const optionalStrings = [
      observer.resource_path,
      observer.request_resource_path,
      observer.response_collection_sum_path,
      observer.request_character_count_path,
      observer.request_character_count_query_parameter,
      observer.request_collection_count_path,
      observer.paired_response_collection_path,
      observer.resource_id_prefix_to_strip,
      observer.minimum_quantity,
      observer.response_quantity_header,
      observer.fixed_quantity,
      observer.resource_query_parameter,
      observer.default_resource_id,
      observer.fixed_resource_id,
      observer.quantity_multiplier_path,
      observer.quantity_multiplier_query_parameter,
      observer.quantity_multiplier_query_parameter_count,
      observer.record_id_path,
      observer.record_id_header,
      observer.provider_cost_usd_path,
      observer.provider_cost_usd_collection_sum_path,
      observer.provider_cost_minor_units_path,
      observer.provider_cost_currency_path,
      observer.endpoint_match,
    ];
    const hasResourceSelector = [
      observer.resource_path,
      observer.request_resource_path,
      observer.resource_query_parameter,
      observer.default_resource_id,
      observer.fixed_resource_id,
    ].some((value) => value !== undefined);
    if (
      observer === null ||
      typeof observer !== "object" ||
      !CANONICAL_NAME.test(observer.service_key) ||
      !CANONICAL_NAME.test(observer.provider_name) ||
      !CANONICAL_NAME.test(observer.provider_service) ||
      !COMPONENTS.has(observer.component) ||
      !METRICS.has(observer.usage_metric) ||
      !Array.isArray(observer.domains) ||
      observer.domains.length === 0 ||
      !observer.domains.every((domain) => typeof domain === "string" && domain.length > 0) ||
      (observer.domain_suffixes !== undefined && (
        !Array.isArray(observer.domain_suffixes) ||
        observer.domain_suffixes.length === 0 ||
        !observer.domain_suffixes.every((suffix) =>
          typeof suffix === "string" && suffix.length <= 253 &&
          DOMAIN_SUFFIX_PATTERN.test(suffix))
      )) ||
      ((observer.provider_region_domain_label !== undefined) !==
        (observer.allowed_provider_regions !== undefined)) ||
      (observer.provider_region_domain_label !== undefined && (
        !Number.isInteger(observer.provider_region_domain_label) ||
        observer.provider_region_domain_label < 0 ||
        observer.provider_region_domain_label > 10 ||
        !Array.isArray(observer.allowed_provider_regions) ||
        observer.allowed_provider_regions.length === 0 ||
        observer.allowed_provider_regions.length > 100 ||
        !observer.allowed_provider_regions.every((region) => CANONICAL_NAME.test(region)) ||
        new Set(observer.allowed_provider_regions).size !== observer.allowed_provider_regions.length
      )) ||
      !Array.isArray(observer.endpoints) ||
      observer.endpoints.length === 0 ||
      !observer.endpoints.every((endpoint) => typeof endpoint === "string" && endpoint.startsWith("/")) ||
      (observer.endpoint_match === "path_template" &&
        !observer.endpoints.every((endpoint) => endpoint.split("/").filter((part) => part === "{id}").length === 1)) ||
      (observer.excluded_endpoints !== undefined && (
        !Array.isArray(observer.excluded_endpoints) ||
        observer.excluded_endpoints.length === 0 ||
        !observer.excluded_endpoints.every((endpoint) =>
          typeof endpoint === "string" && endpoint.startsWith("/")) ||
        new Set(observer.excluded_endpoints).size !== observer.excluded_endpoints.length
      )) ||
      (observer.endpoint_match !== undefined &&
        observer.endpoint_match !== "exact" && observer.endpoint_match !== "prefix" &&
        observer.endpoint_match !== "path_template") ||
      (observer.methods !== undefined && (
        !Array.isArray(observer.methods) || observer.methods.length === 0 ||
        !observer.methods.every((method) =>
          typeof method === "string" && /^[A-Z]+$/.test(method)) ||
        new Set(observer.methods).size !== observer.methods.length
      )) ||
      [
        observer.response_path,
        observer.response_collection_sum_path,
        observer.response_quantity_header,
        observer.request_character_count_path ?? observer.request_character_count_query_parameter,
        observer.request_collection_count_path,
        observer.fixed_quantity,
      ].filter((value) => value !== undefined).length !== 1 ||
      (observer.fixed_quantity !== undefined && observer.fixed_quantity !== "1") ||
      (observer.minimum_quantity !== undefined && observer.minimum_quantity !== "1") ||
      (observer.minimum_quantity !== undefined &&
        observer.request_character_count_path === undefined &&
        observer.request_character_count_query_parameter === undefined) ||
      ((observer.fixed_quantity !== undefined) !== (observer.usage_metric === "request_count")) ||
      (observer.response_path !== undefined &&
        (typeof observer.response_path !== "string" || observer.response_path.length === 0)) ||
      optionalStrings.some(
        (value) => value !== undefined && (typeof value !== "string" || value.length === 0),
      ) ||
      (observer.resource_type !== undefined && !RESOURCE_TYPES.has(observer.resource_type)) ||
      (observer.allowed_resource_ids !== undefined && (
        observer.resource_type === undefined ||
        !Array.isArray(observer.allowed_resource_ids) ||
        observer.allowed_resource_ids.length === 0 ||
        !observer.allowed_resource_ids.every((id) => typeof id === "string" && id.length > 0)
      )) ||
      (hasResourceSelector && observer.resource_type === undefined) ||
      (observer.quantity_multiplier_query_parameter !== undefined &&
        observer.quantity_multiplier_path === undefined) ||
      (observer.character_count_encoding !== undefined &&
        observer.character_count_encoding !== "unicode_code_points" &&
        observer.character_count_encoding !== "utf16_code_units") ||
      (observer.character_count_encoding !== undefined &&
        observer.request_character_count_path === undefined &&
        observer.request_character_count_query_parameter === undefined) ||
      (observer.request_character_count_case_insensitive !== undefined &&
        observer.request_character_count_case_insensitive !== true) ||
      (observer.request_character_count_case_insensitive === true &&
        observer.request_character_count_path === undefined) ||
      (observer.quantity_multiplier_query_parameter_count !== undefined &&
        observer.request_character_count_path === undefined &&
        observer.request_character_count_query_parameter === undefined) ||
      (observer.quantity_multiplier_query_parameter_count !== undefined &&
        observer.quantity_multiplier_path !== undefined) ||
      (observer.provider_cost_usd_path !== undefined &&
        observer.provider_cost_usd_collection_sum_path !== undefined) ||
      ([
        observer.provider_cost_minor_units_path,
        observer.provider_cost_currency_path,
        observer.provider_cost_minor_unit_exponent,
      ].filter((value) => value !== undefined).length !== 0 &&
        [
          observer.provider_cost_minor_units_path,
          observer.provider_cost_currency_path,
          observer.provider_cost_minor_unit_exponent,
        ].filter((value) => value !== undefined).length !== 3) ||
      (observer.provider_cost_minor_unit_exponent !== undefined && (
        !Number.isInteger(observer.provider_cost_minor_unit_exponent) ||
        observer.provider_cost_minor_unit_exponent < 0 ||
        observer.provider_cost_minor_unit_exponent > 6 ||
        observer.provider_cost_usd_path !== undefined ||
        observer.provider_cost_usd_collection_sum_path !== undefined
      )) ||
      (observer.response_all !== undefined && (
        !Array.isArray(observer.response_all) ||
        observer.response_all.length === 0 ||
        !observer.response_all.every(validResponsePredicate)
      )) ||
      (observer.request_all !== undefined && (
        !Array.isArray(observer.request_all) ||
        observer.request_all.length === 0 ||
        !observer.request_all.every(validRequestPredicate)
      )) ||
      (observer.request_header_all !== undefined && (
        !Array.isArray(observer.request_header_all) ||
        observer.request_header_all.length === 0 ||
        !observer.request_header_all.every(validRequestHeaderPredicate)
      )) ||
      (observer.request_collection_all !== undefined && (
        !Array.isArray(observer.request_collection_all) ||
        observer.request_collection_all.length === 0 ||
        !observer.request_collection_all.every(validCollectionPredicate)
      )) ||
      ((observer.request_collection_count_path !== undefined) !==
        (observer.request_collection_all !== undefined)) ||
      (observer.paired_response_all !== undefined && (
        !Array.isArray(observer.paired_response_all) ||
        observer.paired_response_all.length === 0 ||
        !observer.paired_response_all.every(validRequestPredicate)
      )) ||
      ((observer.paired_response_collection_path !== undefined) !==
        (observer.paired_response_all !== undefined)) ||
      (observer.paired_response_collection_path !== undefined &&
        observer.request_collection_count_path === undefined) ||
      (observer.query_any !== undefined && (
        !Array.isArray(observer.query_any) || observer.query_any.length === 0 ||
        !observer.query_any.every(validQueryPredicate)
      )) ||
      (observer.query_all !== undefined && (
        !Array.isArray(observer.query_all) || observer.query_all.length === 0 ||
        !observer.query_all.every(validQueryPredicate)
      )) ||
      (observer.quantity_multiplier_query_parameter_count !== undefined &&
        !observer.query_all?.some((predicate) =>
          predicate.parameter === observer.quantity_multiplier_query_parameter_count &&
          predicate.operator === "all_non_empty")) ||
      (observer.resource_variant !== undefined && (
        typeof observer.resource_variant.query_parameter !== "string" ||
        observer.resource_variant.query_parameter.length === 0 ||
        typeof observer.resource_variant.equals !== "string" ||
        observer.resource_variant.equals.length === 0 ||
        typeof observer.resource_variant.matched_suffix !== "string" ||
        observer.resource_variant.matched_suffix.length === 0 ||
        typeof observer.resource_variant.default_suffix !== "string"
        || observer.resource_variant.default_suffix.length === 0
      )) ||
      typeof observer.source_url !== "string" ||
      !observer.source_url.startsWith("https://") ||
      keys.has(observer.service_key)
    ) {
      throw new Error("usage observer manifest contains an invalid observer");
    }
    keys.add(observer.service_key);
  }
  return manifest as UsageObserverManifest;
}

export class ServiceUsageObservers {
  readonly manifestVersion: string;
  private readonly observers: UsageObserverDefinition[];

  constructor(raw?: unknown) {
    const loaded = raw ?? createRequire(import.meta.url)("../data/service_usage_observers.json");
    const manifest = validateManifest(loaded);
    this.manifestVersion = manifest._meta.version;
    this.observers = manifest.observers;
  }

  get observerCount(): number {
    return this.observers.length;
  }

  private lookup(url: string): { parsed: URL; observers: UsageObserverDefinition[] } | undefined {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return undefined;
    }
    const observers = this.observers.filter(
      (candidate) => domainMatches(parsed.hostname, candidate) &&
        candidate.endpoints.some((endpoint) =>
          endpointMatches(parsed.pathname, endpoint, candidate.endpoint_match)) &&
        !candidate.excluded_endpoints?.includes(parsed.pathname) &&
        (candidate.query_any === undefined ||
          candidate.query_any.some((predicate) => predicateMatches(parsed, predicate))) &&
        (candidate.query_all === undefined ||
          candidate.query_all.every((predicate) => predicateMatches(parsed, predicate))),
    );
    return observers.length === 0 ? undefined : { parsed, observers };
  }

  matches(url: string): boolean {
    return this.lookup(url) !== undefined;
  }

  ownsEndpointBoundary(url: string): boolean {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return false;
    }
    return this.observers.some(
      (observer) => domainMatches(parsed.hostname, observer) &&
        observer.endpoints.some((endpoint) =>
          endpointBoundaryMatches(parsed.pathname, endpoint, observer.endpoint_match)),
    );
  }

  needsRequestBody(url: string): boolean {
    return this.lookup(url)?.observers.some(
      (observer) => observer.request_resource_path !== undefined ||
        observer.request_character_count_path !== undefined ||
        observer.request_collection_count_path !== undefined ||
        observer.request_all !== undefined,
    ) === true;
  }

  needsResponseBody(url: string): boolean {
    return this.lookup(url)?.observers.some(
      (observer) => observer.response_path !== undefined ||
        observer.response_collection_sum_path !== undefined ||
        observer.resource_path !== undefined ||
        observer.record_id_path !== undefined ||
        observer.provider_cost_usd_path !== undefined ||
        observer.provider_cost_usd_collection_sum_path !== undefined ||
        observer.provider_cost_minor_units_path !== undefined ||
        observer.provider_cost_currency_path !== undefined ||
        observer.response_all !== undefined ||
        observer.paired_response_collection_path !== undefined ||
        observer.quantity_multiplier_path !== undefined,
    ) === true;
  }

  /** Keep only headers referenced by matching observer rules. Presence-only
   * predicates use an undefined sentinel so credential values are not held. */
  selectRequestHeaders(
    url: string,
    requestHeaders: Readonly<Record<string, unknown>>,
  ): ObservedRequestHeaders {
    const matched = this.lookup(url);
    if (matched === undefined) return {};
    const source = new Map(
      Object.entries(requestHeaders).map(([name, value]) => [name.toLowerCase(), value]),
    );
    const selected: Record<string, string | undefined> = {};
    for (const observer of matched.observers) {
      for (const predicate of observer.request_header_all ?? []) {
        const name = predicate.name.toLowerCase();
        if (!source.has(name)) continue;
        if (predicate.operator === "present" || predicate.operator === "absent") {
          selected[name] = undefined;
          continue;
        }
        const raw = source.get(name);
        if (predicate.operator === "basic_username_prefix") {
          if (basicUsernameHasPrefix(raw, predicate.value)) {
            selected[name] = `basic_username_prefix:${predicate.value}`;
          }
          continue;
        }
        const value = Array.isArray(raw) ? raw.join(", ")
          : typeof raw === "string" ? raw
          : typeof raw === "number" ? String(raw)
          : undefined;
        if (value !== undefined) selected[name] = value;
      }
    }
    return selected;
  }

  observe(
    url: string,
    headers: Headers,
    responseBody: unknown,
    requestBody?: unknown,
    requestHeaders: readonly string[] | ObservedRequestHeaders = [],
    method?: string,
  ): ServiceUsageObservation[] {
    const matched = this.lookup(url);
    if (matched === undefined) return [];
    const observations: ServiceUsageObservation[] = [];
    const normalizedRequestHeaders = new Map<string, string | undefined>();
    if (Array.isArray(requestHeaders)) {
      for (const name of requestHeaders) {
        normalizedRequestHeaders.set(name.toLowerCase(), undefined);
      }
    } else {
      for (const [name, value] of Object.entries(requestHeaders)) {
        normalizedRequestHeaders.set(name.toLowerCase(), value);
      }
    }
    for (const observer of matched.observers) {
      if (observer.methods !== undefined &&
          (method === undefined || !observer.methods.includes(method.toUpperCase()))) {
        continue;
      }
      if (
        observer.request_all !== undefined &&
        !observer.request_all.every((predicate) =>
          requestPredicateMatches(requestBody, predicate))
      ) {
        continue;
      }
      if (
        observer.request_header_all !== undefined &&
        !observer.request_header_all.every((predicate) =>
          requestHeaderPredicateMatches(normalizedRequestHeaders, predicate))
      ) {
        continue;
      }
      if (
        observer.response_all !== undefined &&
        !observer.response_all.every((predicate) =>
          responsePredicateMatches(responseBody, predicate))
      ) {
        continue;
      }
      let quantity: Decimal;
      if (observer.request_collection_count_path !== undefined) {
        const collection = resolveCollectionPath(
          requestBody,
          observer.request_collection_count_path,
        );
        if (collection === undefined) continue;
        const pairedResponses = observer.paired_response_collection_path === undefined
          ? undefined
          : resolveCollectionPath(responseBody, observer.paired_response_collection_path);
        if (pairedResponses !== undefined && pairedResponses.length !== collection.length) continue;
        if (observer.paired_response_collection_path !== undefined && pairedResponses === undefined) {
          continue;
        }
        const count = collection.filter((item, index) =>
          observer.request_collection_all!.every((predicate) =>
            collectionPredicateMatches(item, predicate)) &&
          (pairedResponses === undefined || observer.paired_response_all!.every((predicate) =>
            requestPredicateMatches(pairedResponses[index], predicate)))).length;
        if (count === 0) continue;
        quantity = new Decimal(count);
      } else if (
        observer.request_character_count_path !== undefined ||
        observer.request_character_count_query_parameter !== undefined
      ) {
        let counted = observer.request_character_count_path === undefined
          ? undefined
          : characterCount(
            resolveCharacterCountPath(
              requestBody,
              observer.request_character_count_path,
              observer.request_character_count_case_insensitive === true,
            ),
            observer.character_count_encoding,
          );
        if (counted === undefined && observer.request_character_count_query_parameter !== undefined) {
          counted = characterCount(
            matched.parsed.searchParams.getAll(observer.request_character_count_query_parameter),
            observer.character_count_encoding,
          );
        }
        if (counted === undefined) continue;
        const billableCount = observer.minimum_quantity === "1" ? Math.max(counted, 1) : counted;
        if (billableCount === 0) continue;
        quantity = new Decimal(billableCount);
      } else if (observer.fixed_quantity !== undefined) {
        quantity = new Decimal(observer.fixed_quantity);
      } else if (observer.response_quantity_header !== undefined) {
        const rawQuantity = positiveDecimal(headers.get(observer.response_quantity_header));
        if (rawQuantity === undefined) continue;
        quantity = new Decimal(rawQuantity);
      } else if (observer.response_collection_sum_path !== undefined) {
        const values = resolveCollectionPath(responseBody, observer.response_collection_sum_path);
        if (values === undefined || values.length === 0) continue;
        let sum = new Decimal(0);
        let valid = true;
        for (const value of values) {
          if (typeof value !== "number" && typeof value !== "string") {
            valid = false;
            break;
          }
          try {
            const item = new Decimal(value);
            if (!item.isFinite() || item.lt(0)) {
              valid = false;
              break;
            }
            sum = sum.add(item);
          } catch {
            valid = false;
            break;
          }
        }
        if (!valid || !sum.gt(0)) continue;
        quantity = sum;
      } else {
        const rawQuantity = positiveDecimal(resolvePath(responseBody, observer.response_path!));
        if (rawQuantity === undefined) continue;
        quantity = new Decimal(rawQuantity);
      }
      if (observer.quantity_multiplier_query_parameter_count !== undefined) {
        const multiplier = matched.parsed.searchParams.getAll(
          observer.quantity_multiplier_query_parameter_count,
        ).length;
        if (multiplier === 0) continue;
        quantity = quantity.mul(multiplier);
      }
      let providerCostUsd: string | undefined;
      const providerCostValues = observer.provider_cost_usd_path !== undefined
        ? [resolvePath(responseBody, observer.provider_cost_usd_path)]
        : observer.provider_cost_usd_collection_sum_path !== undefined
          ? resolveCollectionPath(responseBody, observer.provider_cost_usd_collection_sum_path)
          : undefined;
      if (providerCostValues !== undefined) {
        if (providerCostValues.length === 0) continue;
        let providerCost = new Decimal(0);
        let validProviderCost = true;
        for (const rawProviderCost of providerCostValues) {
          if (typeof rawProviderCost !== "number" && typeof rawProviderCost !== "string") {
            validProviderCost = false;
            break;
          }
          try {
            const itemCost = new Decimal(rawProviderCost);
            if (!itemCost.isFinite() || itemCost.lt(0)) {
              validProviderCost = false;
              break;
            }
            providerCost = providerCost.add(itemCost);
          } catch {
            validProviderCost = false;
            break;
          }
        }
        if (!validProviderCost) continue;
        providerCostUsd = providerCost.toFixed()
          .replace(/(?:\.0+|(?:(\.\d*?)0+))$/, "$1");
      }
      let providerCostAmount: string | undefined;
      let providerCostCurrency: string | undefined;
      if (observer.provider_cost_minor_units_path !== undefined) {
        const rawMinorCost = resolvePath(responseBody, observer.provider_cost_minor_units_path);
        const rawCurrency = resolvePath(responseBody, observer.provider_cost_currency_path!);
        if ((typeof rawMinorCost !== "number" && typeof rawMinorCost !== "string") ||
            typeof rawCurrency !== "string" || !/^[A-Z]{3}$/.test(rawCurrency)) {
          continue;
        }
        try {
          const minorCost = new Decimal(rawMinorCost);
          if (!minorCost.isFinite() || minorCost.lt(0)) continue;
          providerCostAmount = minorCost.div(
            new Decimal(10).pow(observer.provider_cost_minor_unit_exponent!),
          ).toFixed().replace(/(?:\.0+|(?:(\.\d*?)0+))$/, "$1");
          providerCostCurrency = rawCurrency;
        } catch {
          continue;
        }
      }
      if (
        observer.quantity_multiplier_path !== undefined &&
        (observer.quantity_multiplier_query_parameter === undefined ||
          matched.parsed.searchParams.getAll(observer.quantity_multiplier_query_parameter)
            .some(queryValueIsTruthy))
      ) {
        const multiplier = positiveDecimal(resolvePath(responseBody, observer.quantity_multiplier_path));
        if (multiplier !== undefined) quantity = quantity.mul(multiplier);
      }
      const recordFromBody = observer.record_id_path === undefined
        ? undefined
        : boundedString(resolvePath(responseBody, observer.record_id_path));
      let recordFromHeader = observer.record_id_header === undefined
        ? undefined
        : boundedString(headers.get(observer.record_id_header));
      if (observer.service_key === "elevenlabs_tts") {
        recordFromHeader ??= boundedString(headers.get("x-trace-id"));
      }
      let resourceId = observer.resource_path === undefined
        ? undefined
        : boundedString(resolvePath(responseBody, observer.resource_path));
      const requestResourceId = observer.request_resource_path === undefined
        ? undefined
        : boundedString(resolvePath(requestBody, observer.request_resource_path));
      const queryResourceId = observer.resource_query_parameter === undefined
        ? undefined
        : boundedString(matched.parsed.searchParams.get(observer.resource_query_parameter));
      if (requestResourceId !== undefined && queryResourceId !== undefined &&
          requestResourceId !== queryResourceId) {
        continue;
      }
      resourceId ??= requestResourceId ?? queryResourceId;
      resourceId ??= boundedString(observer.fixed_resource_id);
      resourceId ??= boundedString(observer.default_resource_id);
      if (
        resourceId !== undefined &&
        observer.resource_id_prefix_to_strip !== undefined &&
        resourceId.startsWith(observer.resource_id_prefix_to_strip)
      ) {
        resourceId = resourceId.slice(observer.resource_id_prefix_to_strip.length);
      }
      if (
        observer.allowed_resource_ids !== undefined &&
        (resourceId === undefined || !observer.allowed_resource_ids.includes(resourceId))
      ) {
        continue;
      }
      if (resourceId !== undefined && observer.resource_variant !== undefined) {
        const variant = observer.resource_variant;
        resourceId += matched.parsed.searchParams.get(variant.query_parameter) === variant.equals
          ? variant.matched_suffix
          : variant.default_suffix;
        resourceId = resourceId.slice(0, 256);
      }
      let providerRegion: string | undefined;
      if (observer.provider_region_domain_label !== undefined) {
        const candidate = matched.parsed.hostname.split(".")[
          observer.provider_region_domain_label
        ];
        if (candidate === undefined || !observer.allowed_provider_regions?.includes(candidate)) {
          continue;
        }
        providerRegion = candidate;
      }
      observations.push({
        serviceKey: observer.service_key,
        providerName: observer.provider_name,
        providerService: observer.provider_service,
        component: observer.component,
        metric: observer.usage_metric,
        quantity: quantity.toFixed().replace(/(?:\.0+|(?:(\.\d*?)0+))$/, "$1"),
        resourceType: resourceId === undefined ? undefined : observer.resource_type,
        resourceId,
        providerRecordId: recordFromBody ?? recordFromHeader,
        providerRegion,
        providerCostUsd,
        providerCostAmount,
        providerCostCurrency,
        dimensions: pathDimensions(matched.parsed, observer),
        manifestVersion: this.manifestVersion,
      });
    }
    return observations;
  }
}

export let serviceUsageObservers: ServiceUsageObservers | null = (() => {
  try {
    return new ServiceUsageObservers();
  } catch (error) {
    console.warn("[dexcost] bundled service usage observers disabled", error);
    return null;
  }
})();

/** Atomically replace the process-wide declarative observer release. */
export function setServiceUsageObservers(observers: ServiceUsageObservers | null): void {
  serviceUsageObservers = observers;
}
