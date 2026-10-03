/** Durable browser-session usage. No URLs, pages, credentials, or prices. */
import { getCurrentTask, runWithTask } from "../core/context.js";
import { Decimal } from "../core/models.js";
import { ProviderJobRevision, providerJobFromDict } from "../core/provider-jobs.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";

export interface BrowserSessionIdentity {
  serviceKey: "browserbase" | "browserless";
  billingAccountId: string;
  /** Opaque provider session/connection ID, never a WebSocket endpoint. */
  sessionId: string;
}
export interface BrowserSessionBinding extends BrowserSessionIdentity {
  /** Browserbase project or explicitly mapped Browserless API-key/fleet ID. */
  resourceId: string;
  startedAt: Date;
  managedProxy?: boolean;
}
export interface BrowserSessionUsage extends BrowserSessionIdentity {
  /** Binding is revision 1; usage starts at 2. Explicitly increment for corrections. */
  revision?: number;
  endedAt: Date;
  status: "succeeded" | "failed" | "cancelled";
  proxyBytes?: string;
  /** Provider-reported per-connection units, NOT account totals or Agent Run model units. */
  timeUnits?: string;
  proxyUnits?: string;
  captchaUnits?: string;
}
function identity(o: BrowserSessionIdentity): string {
  if (!["browserbase", "browserless"].includes(o.serviceKey)) throw new Error("Unsupported browser service");
  return databaseResourceId(o.billingAccountId, o.sessionId);
}
function date(value: unknown): Date {
  if (!(value instanceof Date) || !Number.isFinite(value.getTime())) throw new Error("Invalid provider timestamp");
  return new Date(value.getTime());
}
function providerDate(value: unknown): Date {
  if (value instanceof Date) return date(value);
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3}0*)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new Error("Provider timestamps require an explicit timezone and millisecond precision");
  }
  return date(new Date(value));
}
function previous(tracker: CostTracker, o: BrowserSessionIdentity): ProviderJobRevision | undefined {
  const raw = tracker.buffer.getProviderJob(o.serviceKey, "browser", identity(o));
  return raw ? providerJobFromDict(raw) : undefined;
}
function same(a: ProviderJobRevision, b: ProviderJobRevision): boolean {
  return JSON.stringify(a.toDict()) === JSON.stringify(b.toDict());
}
/** Bind inside the owning task; repeated binding cannot steal ownership. */
export function bindBrowserSession(tracker: CostTracker, options: BrowserSessionBinding): boolean {
  const record = identity(options), resource = databaseResourceId(options.billingAccountId, options.resourceId);
  const started = date(options.startedAt), managed = options.managedProxy ?? false;
  if (typeof managed !== "boolean" || (managed && options.serviceKey !== "browserbase")) throw new Error("managedProxy is only valid for Browserbase");
  const task = getCurrentTask();
  if (!task) return false;
  const job = new ProviderJobRevision({ taskId: task.taskId, provider: options.serviceKey, service: "browser",
    providerRecordId: record, operation: "browser.session", component: "external", eventType: "external_cost",
    resourceType: "endpoint", resourceId: resource, status: "submitted", submittedAt: started, observedAt: started,
    billingDimensions: [["browser.session_id", options.sessionId], ["browser.managed_proxy", managed ? "true" : "false"]] });
  const old = previous(tracker, options);
  if (old) {
    if (!same(new ProviderJobRevision({ ...old, revision: 1, status: "submitted", observedAt: started, usage: [] }), job)) {
      throw new Error("Browser session already belongs to another task or resource");
    }
    return true;
  }
  tracker.buffer.insertProviderJobRevision(job);
  return true;
}
/** Complete full snapshot, not a delta. Missing meters stay unknown; no timing/price guesses. */
export function recordBrowserSession(tracker: CostTracker, options: BrowserSessionUsage): boolean {
  identity(options);
  const ended = date(options.endedAt);
  if (!["succeeded", "failed", "cancelled"].includes(options.status)) throw new Error("Browser session must be terminal");
  const old = previous(tracker, options);
  if (!old) return false;
  const revision = options.revision ?? 2;
  if (!Number.isSafeInteger(revision) || revision < 2) throw new Error("Usage revision must be an integer starting at 2");
  if (revision < old.revision) return false;
  if (revision > old.revision + 1) throw new Error("Usage revisions must be contiguous");
  if (ended < old.submittedAt || ended < old.observedAt) throw new Error("Provider completion cannot move backwards");
  if (options.serviceKey === "browserbase" && [options.timeUnits, options.proxyUnits, options.captchaUnits].some(v => v !== undefined)) {
    throw new Error("Browserless units are not Browserbase usage");
  }
  if (options.proxyBytes !== undefined && (options.serviceKey !== "browserbase" || new Map(old.billingDimensions).get("browser.managed_proxy") !== "true")) {
    throw new Error("Proxy bytes require a known Browserbase-managed proxy");
  }
  const usage = [];
  if (options.serviceKey === "browserbase") {
    const seconds = new Decimal(ended.getTime() - old.submittedAt.getTime()).div(1000);
    if (seconds.gt(0)) usage.push({ metric: "browser.session_seconds", quantity: seconds, unit: "Seconds" });
  }
  for (const [metric, value, unit] of [
    ["browser.proxy_bytes", options.proxyBytes, "Bytes"], ["browser.time_units", options.timeUnits, "Units"],
    ["browser.proxy_units", options.proxyUnits, "Units"], ["browser.captcha_units", options.captchaUnits, "Units"],
  ]) {
    if (value === undefined) continue;
    if (typeof value !== "string" || !/^(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,12})?$/.test(value)) throw new Error("Browser usage must be a nonnegative plain decimal string");
    const quantity = new Decimal(value);
    if (unit === "Bytes" && !quantity.isInteger()) throw new Error("Proxy bytes must be integral");
    if (quantity.gt(0)) usage.push({ metric: metric!, quantity, unit: unit! });
  }
  const next = new ProviderJobRevision({ ...old, revision, observedAt: ended,
    status: options.status === "succeeded" && !usage.length ? "unknown" : options.status, usage });
  if (same(next, old)) return true;
  tracker.buffer.insertProviderJobRevision(next);
  return true;
}

const captures = new WeakMap<object, { closed: boolean }>();
/** Facade over native sessions.create/retrieve/update; no background calls or forced closes. */
export function instrumentBrowserbase<T extends object>(client: T, tracker: CostTracker, options: { billingAccountId: string }): T {
  const account = options.billingAccountId;
  databaseResourceId(account, "validation");
  if (captures.has(client)) throw new Error("Browserbase client is already instrumented");
  const state = { closed: false };
  // Duck typing keeps Browserbase optional. Read only explicitly allowlisted metadata.
  const wrapped = new Proxy(client, { get(target, key) {
    const value = Reflect.get(target, key, target);
    if (key !== "sessions") return typeof value === "function" ? value.bind(target) : value;
    return new Proxy(value as object, { get(sessions, method) {
      const fn = Reflect.get(sessions, method, sessions);
      if (!["create", "retrieve", "update"].includes(String(method))) return typeof fn === "function" ? fn.bind(sessions) : fn;
      return (...args: unknown[]) => {
        const task = getCurrentTask();
        let managedProxy = false;
        try { managedProxy = (args[0] as { proxies?: unknown } | undefined)?.proxies === true; } catch { /* Unknown proxy provenance. */ }
        const result = fn.apply(sessions, args);
        const capture = (response: unknown): unknown => {
          if (state.closed) return response;
          try {
            const r = response as Record<string, unknown>;
            const started = providerDate(r.startedAt), sessionId = r.id as string;
            const id = { serviceKey: "browserbase" as const, billingAccountId: account, sessionId };
            if (method === "create" && task) runWithTask(task, () => bindBrowserSession(tracker, { ...id,
              resourceId: r.projectId as string, startedAt: started,
              managedProxy }));
            const old = previous(tracker, id);
            if (!old || old.submittedAt.getTime() !== started.getTime() || old.resourceId !== databaseResourceId(account, r.projectId as string)) return response;
            // Native polling is not revision-ordered. Later provider corrections
            // must be supplied explicitly via recordBrowserSession.
            if (old.terminal) return response;
            const status = ({ COMPLETED: "succeeded", ERROR: "failed", TIMED_OUT: "failed" } as const)[r.status as "COMPLETED" | "ERROR" | "TIMED_OUT"];
            if (status && r.endedAt) {
              const managed = new Map(old.billingDimensions).get("browser.managed_proxy") === "true";
              if (managed && (!Number.isSafeInteger(r.proxyBytes) || (r.proxyBytes as number) < 0)) return response;
              recordBrowserSession(tracker, { ...id, endedAt: providerDate(r.endedAt), status,
                proxyBytes: managed && Number.isSafeInteger(r.proxyBytes) ? String(r.proxyBytes) : undefined });
            }
          } catch { /* Telemetry must not change provider results/errors. */ }
          return response;
        };
        if (!result || typeof result.then !== "function") return capture(result);
        // Keep APIPromise helpers/brands accessible; await/then and withResponse
        // capture parsed data, while asResponse remains a raw passthrough.
        return new Proxy(result, { get(promise, property) {
          const member = Reflect.get(promise, property, promise);
          if (property === "then") return (fulfilled: ((r: unknown) => unknown) | undefined, rejected: ((e: unknown) => unknown) | undefined) =>
            member.call(promise, (r: unknown) => { const value = capture(r); return typeof fulfilled === "function" ? fulfilled(value) : value; }, rejected);
          if (property === "catch" || property === "finally") return (...values: unknown[]) => promise.then(capture)[property](...values);
          if (property === "withResponse" && typeof member === "function") return (...values: unknown[]) => member.apply(promise, values).then((r: { data: unknown }) => { capture(r.data); return r; });
          return typeof member === "function" ? member.bind(promise) : member;
        } });
      };
    } });
  } });
  captures.set(wrapped, state);
  return wrapped;
}
/** Stop local capture; never terminates a provider session. */
export function uninstrumentBrowserbase(client: object): void {
  const state = captures.get(client);
  if (state) state.closed = true;
}
