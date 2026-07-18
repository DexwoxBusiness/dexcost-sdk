/** Provider-owned usage measurement for services withheld from SDK pricing. */

import { createRequire } from "node:module";
import { Decimal } from "../core/models.js";

export type ObservedUsageMetric = "input_tokens" | "audio_seconds";
export type ObservedAttributionComponent = "external" | "speech_to_text";

interface UsageObserverDefinition {
  service_key: string;
  provider_name: string;
  provider_service: string;
  component: ObservedAttributionComponent;
  domains: string[];
  endpoints: string[];
  response_path: string;
  usage_metric: ObservedUsageMetric;
  resource_path?: string;
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
  resourceId?: string;
  providerRecordId?: string;
  manifestVersion: string;
}

const CANONICAL_NAME = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const METRICS = new Set<ObservedUsageMetric>(["input_tokens", "audio_seconds"]);
const COMPONENTS = new Set<ObservedAttributionComponent>(["external", "speech_to_text"]);

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
      typeof observer.response_path !== "string" ||
      observer.response_path.length === 0 ||
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

  private lookup(url: string): UsageObserverDefinition | undefined {
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch {
      return undefined;
    }
    return this.observers.find(
      (candidate) => candidate.domains.includes(parsed.hostname) &&
        candidate.endpoints.some((endpoint) => endpointMatches(parsed.pathname, endpoint)),
    );
  }

  matches(url: string): boolean {
    return this.lookup(url) !== undefined;
  }

  observe(url: string, headers: Headers, responseBody: unknown): ServiceUsageObservation | null {
    const observer = this.lookup(url);
    if (observer === undefined) return null;
    const quantity = positiveDecimal(resolvePath(responseBody, observer.response_path));
    if (quantity === undefined) return null;
    const recordFromBody = observer.record_id_path === undefined
      ? undefined
      : boundedString(resolvePath(responseBody, observer.record_id_path));
    const recordFromHeader = observer.record_id_header === undefined
      ? undefined
      : boundedString(headers.get(observer.record_id_header));
    const resourceId = observer.resource_path === undefined
      ? undefined
      : boundedString(resolvePath(responseBody, observer.resource_path));
    return {
      serviceKey: observer.service_key,
      providerName: observer.provider_name,
      providerService: observer.provider_service,
      component: observer.component,
      metric: observer.usage_metric,
      quantity,
      resourceId,
      providerRecordId: recordFromBody ?? recordFromHeader,
      manifestVersion: this.manifestVersion,
    };
  }
}

export const serviceUsageObservers: ServiceUsageObservers | null = (() => {
  try {
    return new ServiceUsageObservers();
  } catch {
    return null;
  }
})();
