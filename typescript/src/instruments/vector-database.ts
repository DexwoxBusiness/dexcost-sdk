/** Native vector usage only. Explicit invoice/resource bindings own money;
 * no vectors, IDs, documents, filters, credentials, namespace names or rates persist. */
import { createHash, randomUUID } from "node:crypto";
import { getCurrentTask } from "../core/context.js";
import { createCostEvent, Decimal } from "../core/models.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";
import { providerCaptureIsClaimed, runWithProviderCapture } from "./provider-capture.js";

type Provider = "pinecone" | "turbopuffer";
type Native = Record<string, any>;
export interface VectorDatabaseBinding { billingAccountId: string; region: string; namespace: string }
export interface PineconeBinding extends VectorDatabaseBinding { indexHost: string }
const states = new WeakMap<object, { active: boolean }>();
function host(value: unknown): string | undefined {
  if (typeof value !== "string") return;
  const url = new URL(value.includes("://") ? value : `https://${value}`);
  if (url.protocol !== "https:" || url.port || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return;
  return url.hostname;
}
function namespace(value: unknown): string {
  if (typeof value !== "string" || [...value].length > 128 || /[\x00-\x1f]/.test(value)) throw new Error("An explicit namespace of at most 128 characters is required");
  return value || "__default__";
}
/** Same account/SHA256(JSON([provider,region,host,namespace])) identity as Python.
 * Set BOTH invoice resource.id and resource scope.id to this returned value. */
export function vectorDatabaseResourceId(provider: Provider, billingAccountId: string, region: string, endpointHost: string, namespaceName: string): string {
  databaseResourceId(billingAccountId, "validation");
  if (!["pinecone", "turbopuffer"].includes(provider) || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(region)) throw new Error("An explicit supported provider and region are required");
  if (host(endpointHost) !== endpointHost || endpointHost.length > 253) throw new Error("Use a lowercase provider hostname, not a URL");
  if (provider === "pinecone" ? !/^[a-z0-9.-]+\.svc(?:\.[a-z0-9-]+)?\.pinecone\.io$/.test(endpointHost) : endpointHost !== `${region}.turbopuffer.com` || namespaceName === "") throw new Error("A hosted endpoint and namespace are required");
  namespace(namespaceName);
  const digest = createHash("sha256").update(JSON.stringify([provider, region, endpointHost, namespaceName])).digest("hex");
  return databaseResourceId(billingAccountId, digest);
}
function count(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) throw new Error("Provider usage must be a nonnegative safe integer");
  return value;
}
function usage(provider: Provider, operation: string, response: Native): Array<{ metric: string; quantity: Decimal; unit: string }> {
  const values: Array<[string, number | Decimal, string]> = [];
  if (provider === "pinecone") {
    const camel = response?.usage?.readUnits, snake = response?.usage?.read_units;
    if (camel !== undefined && snake !== undefined && camel !== snake) throw new Error("Conflicting Pinecone read units");
    const value = camel ?? snake;
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > Number.MAX_SAFE_INTEGER) throw new Error("Pinecone read units must be finite and nonnegative");
    const units = new Decimal(value);
    if (units.decimalPlaces() > 12) throw new Error("Pinecone read units must fit the exact 12-decimal domain");
    values.push(["pinecone.read_units_rounded", units, "ReadUnits"]);
  } else {
    const billing = response?.billing, query = operation === "query" ? billing : billing?.query;
    if (operation === "write") values.push(["turbopuffer.logical_bytes_written", count(billing?.billable_logical_bytes_written), "Bytes"]);
    if (operation === "query" || query != null) for (const suffix of ["queried", "returned"]) values.push([`turbopuffer.logical_bytes_${suffix}`, count(query?.[`billable_logical_bytes_${suffix}`]), "Bytes"]);
  }
  return values.filter(([, n]) => new Decimal(n).gt(0)).map(([metric, n, unit]) => ({ metric, quantity: new Decimal(n), unit }));
}
// Preserve APIPromise helpers/private receivers. Observe native fulfillment only,
// never a fabricated catch fallback, and at most once across multiple awaits.
function observePromise<T extends object>(promise: T, capture: (value: Native) => Native): T {
  return new Proxy(promise, { get(target, key) {
    const native = Reflect.get(target, key, target);
    if (typeof native !== "function") return native;
    const then = Reflect.get(target, "then", target);
    if (typeof then !== "function") return native.bind(target);
    if (key === "then") return (fulfilled?: (value: Native) => unknown, rejected?: (error: unknown) => unknown) => Reflect.apply(then, target, [(value: Native) => { const result = capture(value); return fulfilled ? fulfilled(result) : result; }, rejected]);
    if (key === "catch") return (rejected?: (error: unknown) => unknown) => Reflect.apply(then, target, [capture, rejected]);
    if (key === "finally") return (...args: unknown[]) => { const observed = Reflect.apply(then, target, [capture]); return Reflect.apply(observed.finally, observed, args); };
    if (key === "withResponse") return (...args: unknown[]) => Reflect.apply(native, target, args).then((result: Native) => { capture(result.data); return result; });
    return native.bind(target);
  } });
}
function facade<T extends object>(client: T, tracker: CostTracker, provider: Provider, options: VectorDatabaseBinding, endpoint: string): T {
  const scope = { ...options }, ns = namespace(scope.namespace), resource = vectorDatabaseResourceId(provider, scope.billingAccountId, scope.region, endpoint, scope.namespace), state = { active: true };
  const wrapped = new Proxy(client, { get(target, key) {
    const native = Reflect.get(target, key, target), operations = provider === "pinecone" ? ["query", "fetch"] : ["query", "write"];
    if (typeof native !== "function") return native;
    if (!operations.includes(String(key))) return native.bind(target);
    return (...args: unknown[]) => {
      if (!state.active || providerCaptureIsClaimed()) return Reflect.apply(native, target, args);
      const task = getCurrentTask(), started = new Date(), request = args[0] as Native | undefined, extra = args[1] as Native | undefined;
      let eligible = false, captured = false;
      try {
        const config = (target as Native).target, nativeClient = (target as Native)._client;
        const selected = request?.namespace ?? (provider === "pinecone" ? config?.namespace : nativeClient?.defaultNamespace);
        const endpointValue = provider === "pinecone" ? config?.indexHostUrl : nativeClient?.baseURL;
        eligible = !!task && args.length <= 2 && host(endpointValue) === endpoint && namespace(selected) === ns &&
          (!extra || Object.keys(extra).every(k => ["timeout", "maxRetries", "signal"].includes(k)));
      } catch { /* Missing/unknown endpoint or namespace stays unattributed. */ }
      const capture = (response: Native): Native => {
        if (captured || !eligible || !state.active) return response;
        captured = true;
        try {
          if (provider === "pinecone" && namespace(response?.namespace) !== ns) return response;
          const meters = usage(provider, String(key), response);
          if (!meters.length) return response;
          const ended = new Date(Math.max(Date.now(), started.getTime() + 1)), milliseconds = ended.getTime() - started.getTime();
          for (const component of ["storage", "network"] as const) {
            const lines = meters.filter(line => line.metric.endsWith("bytes_returned") === (component === "network"));
            if (!lines.length) continue;
            tracker.buffer.addEvent(createCostEvent({ eventId: randomUUID(), taskId: task!.taskId,
              occurredAt: ended, provider, serviceName: "vector_database", eventType: "external_cost", costConfidence: "unknown", latencyMs: milliseconds,
              details: { attribution_component: component, attribution_resource_type: "endpoint", attribution_resource_id: resource,
                attribution_operation_name: `vector_database.${String(key)}`, attribution_operation_status: "succeeded",
                attribution_usage_duration_seconds: new Decimal(milliseconds).div(1000).toFixed(),
                attribution_usage_lines: lines.map(line => ({ ...line, quantity: line.quantity.toFixed() })),
                vector_capture_basis: "provider_response_meter",
              },
            }));
          }
        } catch { /* Telemetry cannot alter native results/errors. */ }
        return response;
      };
      return runWithProviderCapture(provider, () => {
        const result = Reflect.apply(native, target, args);
        return result && typeof result.then === "function" ? observePromise(result, capture) : capture(result);
      });
    };
  } });
  states.set(wrapped, state); return wrapped;
}
/** Wrap a Pinecone index created with an explicit host (query/fetch only).
 * Bind the exact selected namespace; fanout/document/inference/write helpers are excluded. */
export function instrumentPinecone<T extends object>(index: T, tracker: CostTracker, options: PineconeBinding): T {
  return facade(index, tracker, "pinecone", options, options.indexHost);
}
/** Wrap client.namespace(name). query/write capture provider BILLABLE bytes;
 * raw/streaming helpers, multi-query, storage and minimum charges are excluded. */
export function instrumentTurbopuffer<T extends object>(resource: T, tracker: CostTracker, options: VectorDatabaseBinding): T {
  return facade(resource, tracker, "turbopuffer", options, `${options.region}.turbopuffer.com`);
}
export function uninstrumentVectorDatabase(client: object): void { const state = states.get(client); if (state) state.active = false; }

/** Disable capture on the facade returned by instrumentPinecone. */
export function uninstrumentPinecone(client: object): void { uninstrumentVectorDatabase(client); }

/** Disable capture on the facade returned by instrumentTurbopuffer. */
export function uninstrumentTurbopuffer(client: object): void { uninstrumentVectorDatabase(client); }
