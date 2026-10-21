/** Single-region PAYG native command evidence. Reconciled server invoices alone supply money. */
import { AsyncLocalStorage } from "node:async_hooks";
import { createHash, randomUUID } from "node:crypto";
import { getCurrentTask } from "../core/context.js";
import { createCostEvent, Decimal } from "../core/models.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";
import { providerCaptureIsClaimed, runWithProviderCapture } from "./provider-capture.js";

type Native = Record<string, any>;
export interface UpstashRedisBinding {
  billingAccountId: string; region: string; databaseId: string; endpointHost: string;
  billingPlan: "pay_as_you_go"; topology: "single_region";
}
const commands = new Set(["get", "set", "mget", "del", "exists", "incr"]);
interface State { active: boolean; close(): void }
interface Call { state: State; command: string; requests: number; valid: boolean; endpointHost: string; attempts: number; routeVerified: boolean }
const current = new AsyncLocalStorage<Call>();
const observingFetch = new AsyncLocalStorage<boolean>();
const states = new WeakMap<object, State>();
interface FetchObserver { original: typeof fetch; wrapper: typeof fetch; users: number }
let fetchObserver: FetchObserver | undefined;

function host(value: unknown): string | undefined {
  if (typeof value !== "string") return;
  const url = new URL(value);
  if (url.protocol !== "https:" || url.port && url.port !== "443" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return;
  return url.hostname;
}
/** Observe, never rewrite, actual transport responses before the native SDK
 * discards status/URL. Other traffic is delegated unchanged; no bodies are read.
 * Ownership also suppresses duplicate observations when another instrument wraps
 * fetch between two Upstash bindings. Missing transport evidence fails open. */
function observeFetch(): () => void {
  let observer = fetchObserver;
  if (!observer || globalThis.fetch !== observer.wrapper) {
    const original = globalThis.fetch;
    const wrapper: typeof fetch = (input, init) => {
      const call = current.getStore();
      if (!call?.state.active || observingFetch.getStore()) return original(input, init);
      return observingFetch.run(true, async () => {
        call.attempts++;
        const response = await original(input, init);
        try {
          const url = typeof input === "string" || input instanceof URL ? String(input) : input.url;
          call.routeVerified = host(url) === call.endpointHost && host(response.url) === call.endpointHost &&
            response.status === 200 && response.redirected === false;
        } catch { call.routeVerified = false; }
        return response;
      });
    };
    observer = { original, wrapper, users: 0 };
    globalThis.fetch = wrapper; fetchObserver = observer;
  }
  const owned = observer; owned.users++;
  return () => {
    owned.users--;
    if (owned.users === 0) {
      if (globalThis.fetch === owned.wrapper) globalThis.fetch = owned.original;
      if (fetchObserver === owned) fetchObserver = undefined;
    }
  };
}
/** Account/SHA256(JSON([provider,region,database,host,payg_single_region])), paired with Python. */
export function upstashRedisResourceId(billingAccountId: string, region: string, databaseId: string, endpointHost: string): string {
  databaseResourceId(billingAccountId, databaseId);
  if (/^[a-z0-9][a-z0-9-]{0,63}$/.exec(region)?.[0] !== region || /^[a-z0-9][a-z0-9-]{0,62}\.upstash\.io$/.exec(endpointHost)?.[0] !== endpointHost) throw new Error("An explicit region and exact hosted Upstash endpoint are required");
  return databaseResourceId(billingAccountId, createHash("sha256").update(JSON.stringify(["upstash_redis", region, databaseId, endpointHost, "payg_single_region"])).digest("hex"));
}

/** Returns a facade; requires retry:{retries:0}, enableAutoPipelining:false.
 * No retries/options are changed. Pipelines/scripts/raw calls remain unpriced.
 * The native request envelope, not a caller's catch/finally fallback, supplies evidence. */
export function instrumentUpstashRedis<T extends object>(client: T, tracker: CostTracker, options: UpstashRedisBinding): T {
  const binding = { ...options };
  if (binding.billingPlan !== "pay_as_you_go" || binding.topology !== "single_region") throw new Error("Only explicit pay_as_you_go and single_region bindings are admitted");
  const resource = upstashRedisResourceId(binding.billingAccountId, binding.region, binding.databaseId, binding.endpointHost);
  const native = client as Native, http = native.client as Native | undefined;
  // Installing twice on a single shared transport must never double bill or let
  // one binding silently overwrite another. A closed original can be rewrapped.
  if (states.get(client)?.active || http && states.get(http)?.active) throw new Error("Upstash client is already instrumented");
  let releaseFetch: (() => void) | undefined;
  const state: State = { active: true, close() {
    if (!state.active) return;
    state.active = false; releaseFetch?.();
    if (http?.request === wrappedRequest) http.request = request;
  } };
  const request = http?.request;
  const supportedTransport = typeof request === "function" && request === Object.getPrototypeOf(http)?.request && Object.getPrototypeOf(http)?.constructor?.name === "HttpClient";
  function eligible(): boolean {
    try { return state.active && supportedTransport && native.client === http && native.enableAutoPipelining === false &&
      host(http?.baseUrl) === binding.endpointHost && http?.retry?.attempts === 0 &&
      !http?.options?.signal && !http?.options?.agent && !http?.options?.backend; } catch { return false; }
  }
  function wrappedRequest(this: unknown, req: Native): unknown {
    const call = current.getStore();
    if (!call || call.state !== state) return Reflect.apply(request, http, [req]);
    call.requests++;
    const body = req?.body;
    const valid = eligible() && (!req.path || Array.isArray(req.path) && req.path.length === 0) && !req.signal && !req.headers && !req.onMessage &&
      Array.isArray(body) && body.length >= 2 && typeof body[0] === "string" && body[0].toUpperCase() === call.command;
    return (Reflect.apply(request, http, [req]) as Promise<Native>).then((response: Native) => {
      call.valid = valid && response !== null && typeof response === "object" && !Array.isArray(response) &&
        Object.prototype.hasOwnProperty.call(response, "result") && response.error == null;
      return response;
    });
  }
  if (supportedTransport) { releaseFetch = observeFetch(); http!.request = wrappedRequest; states.set(http!, state); }
  const facade = new Proxy(client, { get(target, key) {
    const method = Reflect.get(target, key, target);
    if (typeof method !== "function") return method;
    if (!commands.has(String(key))) return method.bind(target);
    return (...args: unknown[]) => {
      const task = getCurrentTask(), start = new Date();
      if (!task || providerCaptureIsClaimed() || !eligible()) return Reflect.apply(method, target, args);
      const call: Call = { state, command: String(key).toUpperCase(), requests: 0, valid: false, endpointHost: binding.endpointHost, attempts: 0, routeVerified: false };
      return runWithProviderCapture("upstash_redis", () => current.run(call, () => Reflect.apply(method, target, args).then((result: unknown) => {
        try {
          if (state.active && call.valid && call.requests === 1 && call.attempts === 1 && call.routeVerified) {
            const end = new Date(Math.max(Date.now(), start.getTime() + 1)), milliseconds = end.getTime() - start.getTime();
            tracker.buffer.addEvent(createCostEvent({ eventId: randomUUID(), taskId: task.taskId, occurredAt: end,
              provider: "upstash_redis", serviceName: "redis", eventType: "external_cost", costConfidence: "unknown", latencyMs: milliseconds,
              details: { region: binding.region, attribution_component: "storage", attribution_resource_type: "endpoint", attribution_resource_id: resource,
                attribution_operation_name: "redis.command", attribution_operation_status: "succeeded", attribution_usage_duration_seconds: new Decimal(milliseconds).div(1000).toFixed(),
                attribution_usage_lines: [{ metric: "upstash_redis.payg_single_region_commands", quantity: "1", unit: "Commands" }], redis_capture_basis: "single_acknowledged_command_not_invoice" } }));
          }
        } catch { /* Capture failure must not change the provider result. */ }
        return result;
      })));
    };
  } });
  states.set(client, state); states.set(facade, state); return facade;
}
/** Stop only this capture; the underlying provider connection remains usable. */
export function uninstrumentUpstashRedis(client: object): void { states.get(client)?.close(); }
