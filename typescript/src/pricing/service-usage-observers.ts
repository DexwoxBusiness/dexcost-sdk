/** Provider-owned usage measurement for services withheld from SDK pricing. */

import { createRequire } from "node:module";
import { Decimal } from "../core/models.js";

export type ObservedUsageMetric = "input_tokens" | "audio_seconds" | "characters";
export type ObservedAttributionComponent = "external" | "speech_to_text" | "text_to_speech";
export type ObservedResourceType = "model" | "sku";

interface QueryPredicate {
  parameter: string;
  operator: "present" | "truthy";
}

interface ResourceVariant {
  query_parameter: string;
  equals: string;
  matched_suffix: string;
  default_suffix: string;
}

interface ResponseEqualsPredicate {
  path: string;
  equals: string;
}

interface UsageObserverDefinition {
  service_key: string;
  provider_name: string;
  provider_service: string;
  component: ObservedAttributionComponent;
  domains: string[];
  endpoints: string[];
  response_path?: string;
  response_equals?: ResponseEqualsPredicate;
  request_character_count_path?: string;
  usage_metric: ObservedUsageMetric;
  resource_type?: ObservedResourceType;
  resource_path?: string;
  request_resource_path?: string;
  allowed_resource_ids?: string[];
  resource_query_parameter?: string;
  default_resource_id?: string;
  fixed_resource_id?: string;
  resource_variant?: ResourceVariant;
  query_any?: QueryPredicate[];
  quantity_multiplier_path?: string;
  quantity_multiplier_query_parameter?: string;
  record_id_path?: string;
  record_id_header?: string;
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
  manifestVersion: string;
}

const CANONICAL_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const METRICS = new Set<ObservedUsageMetric>(["input_tokens", "audio_seconds", "characters"]);
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

function endpointMatches(pathname: string, endpoint: string): boolean {
  return pathname === endpoint || pathname.startsWith(`${endpoint}/`);
}

function queryValueIsTruthy(value: string | null): boolean {
  if (value === null) return false;
  return !new Set(["", "0", "false", "no", "off"]).has(value.trim().toLowerCase());
}

function predicateMatches(url: URL, predicate: QueryPredicate): boolean {
  return predicate.operator === "present"
    ? url.searchParams.has(predicate.parameter)
    : url.searchParams.getAll(predicate.parameter).some(queryValueIsTruthy);
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
      observer.request_character_count_path,
      observer.resource_query_parameter,
      observer.default_resource_id,
      observer.fixed_resource_id,
      observer.quantity_multiplier_path,
      observer.quantity_multiplier_query_parameter,
      observer.record_id_path,
      observer.record_id_header,
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
      !Array.isArray(observer.endpoints) ||
      observer.endpoints.length === 0 ||
      !observer.endpoints.every((endpoint) => typeof endpoint === "string" && endpoint.startsWith("/")) ||
      ((observer.response_path === undefined) ===
        (observer.request_character_count_path === undefined)) ||
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
      (observer.response_equals !== undefined && (
        observer.response_equals === null ||
        typeof observer.response_equals !== "object" ||
        Object.keys(observer.response_equals).length !== 2 ||
        typeof observer.response_equals.path !== "string" ||
        observer.response_equals.path.length === 0 ||
        typeof observer.response_equals.equals !== "string" ||
        observer.response_equals.equals.length === 0
      )) ||
      (observer.query_any !== undefined && (
        !Array.isArray(observer.query_any) || observer.query_any.length === 0 ||
        !observer.query_any.every((predicate) =>
          typeof predicate.parameter === "string" && predicate.parameter.length > 0 &&
          (predicate.operator === "present" || predicate.operator === "truthy"))
      )) ||
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

  private lookup(url: string): { parsed: URL; observers: UsageObserverDefinition[] } | undefined {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return undefined;
    }
    const observers = this.observers.filter(
      (candidate) => candidate.domains.includes(parsed.hostname) &&
        candidate.endpoints.some((endpoint) => endpointMatches(parsed.pathname, endpoint)) &&
        (candidate.query_any === undefined ||
          candidate.query_any.some((predicate) => predicateMatches(parsed, predicate))),
    );
    return observers.length === 0 ? undefined : { parsed, observers };
  }

  matches(url: string): boolean {
    return this.lookup(url) !== undefined;
  }

  needsRequestBody(url: string): boolean {
    return this.lookup(url)?.observers.some(
      (observer) => observer.request_resource_path !== undefined ||
        observer.request_character_count_path !== undefined,
    ) === true;
  }

  needsResponseBody(url: string): boolean {
    return this.lookup(url)?.observers.some(
      (observer) => observer.response_path !== undefined,
    ) === true;
  }

  observe(
    url: string,
    headers: Headers,
    responseBody: unknown,
    requestBody?: unknown,
  ): ServiceUsageObservation[] {
    const matched = this.lookup(url);
    if (matched === undefined) return [];
    const observations: ServiceUsageObservation[] = [];
    for (const observer of matched.observers) {
      if (
        observer.response_equals !== undefined &&
        resolvePath(responseBody, observer.response_equals.path) !== observer.response_equals.equals
      ) {
        continue;
      }
      let quantity: Decimal;
      if (observer.request_character_count_path !== undefined) {
        const text = resolvePath(requestBody, observer.request_character_count_path);
        if (typeof text !== "string") continue;
        const characterCount = Array.from(text).length;
        if (characterCount === 0) continue;
        quantity = new Decimal(characterCount);
      } else {
        const rawQuantity = positiveDecimal(resolvePath(responseBody, observer.response_path!));
        if (rawQuantity === undefined) continue;
        quantity = new Decimal(rawQuantity);
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
      const recordFromHeader = observer.record_id_header === undefined
        ? undefined
        : boundedString(headers.get(observer.record_id_header));
      let resourceId = observer.resource_path === undefined
        ? undefined
        : boundedString(resolvePath(responseBody, observer.resource_path));
      resourceId ??= observer.request_resource_path === undefined
        ? undefined
        : boundedString(resolvePath(requestBody, observer.request_resource_path));
      resourceId ??= observer.resource_query_parameter === undefined
        ? undefined
        : boundedString(matched.parsed.searchParams.get(observer.resource_query_parameter));
      resourceId ??= boundedString(observer.fixed_resource_id);
      resourceId ??= boundedString(observer.default_resource_id);
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
        manifestVersion: this.manifestVersion,
      });
    }
    return observations;
  }
}

export const serviceUsageObservers: ServiceUsageObservers | null = (() => {
  try {
    return new ServiceUsageObservers();
  } catch (error) {
    console.warn("[dexcost] bundled service usage observers disabled", error);
    return null;
  }
})();
