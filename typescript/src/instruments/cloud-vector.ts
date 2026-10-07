/** Hosted vector response meters. Money belongs exclusively to reconciled server invoices. */
import { createHash, randomUUID } from "node:crypto";
import { getCurrentTask } from "../core/context.js";
import { createCostEvent, Decimal } from "../core/models.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";
import { providerCaptureIsClaimed, runWithProviderCapture } from "./provider-capture.js";

type Native = Record<string, any>;
type Provider = "qdrant_cloud" | "milvus_zilliz";
export interface CloudVectorBinding { billingAccountId: string; region: string; clusterHost: string }
const states = new WeakMap<object, { active: boolean }>();
function endpoint(value: unknown, ports: string[]): string | undefined {
  if (typeof value !== "string") return;
  const url = new URL(value.includes("://") ? value : `https://${value}`);
  if (url.protocol !== "https:" || !ports.includes(url.port) || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return;
  return url.hostname;
}
/** Explicit account/region/host identity, shared with Python. No collection contents persist. */
export function cloudVectorResourceId(provider: Provider, billingAccountId: string, region: string, clusterHost: string): string {
  databaseResourceId(billingAccountId, "validation");
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(region) || endpoint(clusterHost, [""]) !== clusterHost || clusterHost.length > 253) throw new Error("An explicit region and lowercase hosted cluster hostname are required");
  const hosted = provider === "qdrant_cloud" ? /^[a-z0-9][a-z0-9.-]*\.cloud\.qdrant\.io$/.test(clusterHost)
    : provider === "milvus_zilliz" && new RegExp(`^[a-z0-9-]+\\.serverless\\.${region}\\.vectordb\\.zillizcloud\\.com$`).test(clusterHost);
  if (!hosted) throw new Error("Only the bound hosted Qdrant or Zilliz serverless endpoint is supported");
  return databaseResourceId(billingAccountId, createHash("sha256").update(JSON.stringify([provider, region, clusterHost])).digest("hex"));
}
function quantity(value: unknown): string | undefined {
  if (typeof value === "string" && /^(0|[1-9][0-9]*)$/.test(value) && new Decimal(value).lte(Number.MAX_SAFE_INTEGER)) return value;
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return String(value);
  return;
}
function emit(tracker: CostTracker, provider: Provider, resource: string, region: string, taskId: string, start: Date, metric: string, units: string, value: string): void {
  // The wire requires positive usage. Explicit zero is no billable weight, not invented cash.
  if (value === "0") return;
  const end = new Date(Math.max(Date.now(), start.getTime() + 1)), milliseconds = end.getTime() - start.getTime();
  tracker.buffer.addEvent(createCostEvent({ eventId: randomUUID(), taskId, occurredAt: end, provider, serviceName: "vector_database",
    eventType: "external_cost", costConfidence: "unknown", latencyMs: milliseconds,
    details: { region, attribution_component: provider === "qdrant_cloud" ? "compute" : "storage",
      attribution_resource_type: "endpoint", attribution_resource_id: resource, attribution_operation_name: "vector_database.search",
      attribution_operation_status: "succeeded", attribution_usage_duration_seconds: new Decimal(milliseconds).div(1000).toFixed(),
      attribution_usage_lines: [{ metric, quantity: value, unit: units }], vector_capture_basis: "provider_response_meter" },
  }));
}
/** Qdrant REST query/search only. Observe the generated envelope before the SDK strips usage.
 * No SDK mutation or extra request; raw api(), inference and gRPC are outside this facade. */
export function instrumentQdrant<T extends object>(client: T, tracker: CostTracker, options: CloudVectorBinding): T {
  const binding = { ...options }, resource = cloudVectorResourceId("qdrant_cloud", binding.billingAccountId, binding.region, binding.clusterHost), state = { active: true };
  const facade = new Proxy(client, { get(target, key) {
    const native = Reflect.get(target, key, target);
    if (typeof native !== "function") return native;
    if (!["query", "search"].includes(String(key))) return native.bind(target);
    return (...args: unknown[]) => {
      const task = getCurrentTask(), start = new Date(), original = target as Native, request = args[1] as Native | undefined;
      let eligible = false;
      try { eligible = state.active && !!task && !providerCaptureIsClaimed() && args.length === 2 && typeof args[0] === "string" &&
        endpoint(original._restUri, ["", "6333"]) === binding.clusterHost && original._openApiClient != null && !!request &&
        !request.prefetch && (request.query == null || typeof request.query === "string" || typeof request.query === "number" ||
          Array.isArray(request.query) && request.query.every((value: unknown) => typeof value === "number" && Number.isFinite(value))); } catch { /* unverified route */ }
      if (!eligible) return Reflect.apply(native, target, args);
      let cpu: string | undefined;
      const api = new Proxy(original._openApiClient, { get(apiTarget, apiKey) {
        const method = Reflect.get(apiTarget, apiKey, apiTarget);
        if (typeof method !== "function") return method;
        if (!["queryPoints", "searchPoints"].includes(String(apiKey))) return method.bind(apiTarget);
        return (...parameters: unknown[]) => (Reflect.apply(method, apiTarget, parameters) as Promise<Native>).then((response: Native) => {
          const data = response?.data;
          if (data?.status === "ok" && data?.result != null) cpu = quantity(data?.usage?.hardware?.cpu);
          return response;
        });
      } });
      const receiver = new Proxy(target, { get(object, name) { return name === "_openApiClient" ? api : Reflect.get(object, name, object); } });
      return runWithProviderCapture("qdrant_cloud", () => Reflect.apply(native, receiver, args).then((response: unknown) => {
        try { if (state.active && cpu !== undefined) emit(tracker, "qdrant_cloud", resource, binding.region, task!.taskId, start, "qdrant.hardware.cpu", "Units", cpu); } catch { /* fail open */ }
        return response;
      }));
    };
  } });
  states.set(facade, state); return facade;
}
/** Zilliz serverless native gRPC/HTTP search only; reported cost is vCU, not money. */
export function instrumentZilliz<T extends object>(client: T, tracker: CostTracker, options: CloudVectorBinding): T {
  const binding = { ...options }, resource = cloudVectorResourceId("milvus_zilliz", binding.billingAccountId, binding.region, binding.clusterHost), state = { active: true };
  const facade = new Proxy(client, { get(target, key) {
    const native = Reflect.get(target, key, target);
    if (typeof native !== "function") return native;
    if (key !== "search") return native.bind(target);
    return (...args: unknown[]) => {
      const task = getCurrentTask(), start = new Date(), config = (target as Native).config, request = args[0] as Native | undefined;
      let eligible = false, http = false;
      try { http = endpoint(config?.endpoint, [""]) === binding.clusterHost &&
        (target as Native).baseURL === `https://${binding.clusterHost}/v2`; } catch { /* not HTTP */ }
      try { eligible = state.active && !!task && !providerCaptureIsClaimed() && args.length === 1 && !!request &&
        (http || (endpoint(config?.address, ["", "19530"]) === binding.clusterHost &&
        (config?.ssl === true || config?.address?.startsWith("https://")) && !config?.tls && !config?.channelOptions)) &&
        !request.cluster_id && !request.session && !request.metadata; } catch { /* unverified route */ }
      if (!eligible) return Reflect.apply(native, target, args);
      return runWithProviderCapture("milvus_zilliz", () => Reflect.apply(native, target, args).then((response: Native) => {
        try {
          const status = response?.status;
          if (state.active && (http ? response?.code === 0 : status?.error_code === "Success" && (status.code === undefined || status.code === 0))) {
            const extra = status?.extra_info;
            const raw = Array.isArray(extra) ? extra.filter((entry: Native) => entry.key === "report_value") : [];
            const value = quantity(http ? response.cost : raw.length === 1 ? raw[0].value : !Array.isArray(extra) ? extra?.report_value : undefined);
            if (value !== undefined) emit(tracker, "milvus_zilliz", resource, binding.region, task!.taskId, start, "zilliz.read_vcu", "VCU", value);
          }
        } catch { /* fail open */ }
        return response;
      }));
    };
  } });
  states.set(facade, state); return facade;
}
export function uninstrumentQdrant(client: object): void { const state = states.get(client); if (state) state.active = false; }
export function uninstrumentZilliz(client: object): void { const state = states.get(client); if (state) state.active = false; }
