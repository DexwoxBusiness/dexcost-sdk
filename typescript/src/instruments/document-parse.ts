/** Hosted LlamaParse v2 credit evidence, never document contents or SDK prices. */
import { getCurrentTask, runWithTask } from "../core/context.js";
import { Decimal } from "../core/models.js";
import { ProviderJobRevision, providerJobFromDict } from "../core/provider-jobs.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";
import { providerCaptureIsClaimed, runWithProviderCapture } from "./provider-capture.js";

type Response = Record<string, unknown>;
interface Scope { billingAccountId: string; projectId: string }
const TIERS = new Set(["fast", "cost_effective", "agentic", "agentic_plus"]);
function id(value: unknown): string {
  if (typeof value !== "string") throw new Error("Provider ID must be a string");
  databaseResourceId("validation", value); return value;
}
function timestamp(value: unknown, end = false): Date {
  if (value instanceof Date && Number.isFinite(value.getTime())) return new Date(value);
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) throw new Error("LlamaParse timestamps must be timezone-aware");
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) throw new Error("Invalid LlamaParse timestamp");
  // Widen sub-millisecond source intervals rather than dropping billable time.
  const fraction = /\.(\d+)/.exec(value)?.[1] ?? "";
  if (end && /[1-9]/.test(fraction.slice(3))) parsed.setTime(parsed.getTime() + 1);
  return parsed;
}
function identity(response: Response, scope: Scope) {
  const job = (response.job ?? response) as Response;
  if (id(job.project_id) !== id(scope.projectId)) throw new Error("LlamaParse job does not belong to the mapped project");
  return { job, record: databaseResourceId(scope.billingAccountId, id(job.id)),
    resource: databaseResourceId(scope.billingAccountId, scope.projectId), started: timestamp(job.created_at) };
}
function previous(tracker: CostTracker, record: string): ProviderJobRevision | undefined {
  const raw = tracker.buffer.getProviderJob("llamaparse", "parse", record);
  return raw ? providerJobFromDict(raw) : undefined;
}
function same(a: ProviderJobRevision, b: ProviderJobRevision): boolean { return JSON.stringify(a.toDict()) === JSON.stringify(b.toDict()); }
/** Bind create/parse in its initiating task; explicit hosted account mapping and provider project/time.
 * Supply requested tier when create omits it; configured/unknown tiers are not guessed.
 */
export function bindLlamaParseJob(tracker: CostTracker, response: Response, options: Scope & { tier?: string }): boolean {
  const { job, record, resource, started } = identity(response, options);
  const tier = job.tier ?? options.tier;
  if (typeof tier !== "string" || !TIERS.has(tier)) return false;
  const task = getCurrentTask(); if (!task) return false;
  const next = new ProviderJobRevision({ taskId: task.taskId, provider: "llamaparse", service: "parse", providerRecordId: record,
    operation: "llamaparse.parse", component: "external", eventType: "external_cost", resourceType: "endpoint", resourceId: resource,
    status: "submitted", submittedAt: started, observedAt: started, billingDimensions: [["parse_tier", tier]] });
  const old = previous(tracker, record);
  if (old) {
    if (!same(new ProviderJobRevision({ ...old, revision: 1, status: "submitted", observedAt: started, usage: [] }), next)) throw new Error("LlamaParse job ownership or identity changed");
    return true;
  }
  tracker.buffer.insertProviderJobRevision(next); return true;
}
/** Full completed Parse v2 snapshot with expand=usage. Missing != zero; explicit revisions for corrections.
 * Interval uses provider created_at/updated_at, never archived-response import time. Invoice supplies money.
 */
export function recordLlamaParseJob(tracker: CostTracker, response: Response, options: Scope & { revision?: number }): boolean {
  const { job, record, resource, started } = identity(response, options);
  const old = previous(tracker, record); if (!old) return false;
  if (old.resourceId !== resource || old.submittedAt.getTime() !== started.getTime()) throw new Error("LlamaParse job identity changed");
  if (job.status !== "COMPLETED" || job.tier == null) return false;
  if (JSON.stringify([["parse_tier", job.tier]]) !== JSON.stringify(old.billingDimensions)) throw new Error("LlamaParse job tier changed");
  const value = (job.usage as Response | undefined)?.credits;
  if (value == null) return false;
  if (typeof value !== "number" && typeof value !== "string") throw new Error("LlamaParse credits must be an exact nonnegative decimal");
  if (typeof value === "string" && !/^(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,32})?$/.test(value)) throw new Error("LlamaParse credit strings must be plain decimals");
  const quantity = new Decimal(value);
  if (!quantity.isFinite() || quantity.lt(0) || quantity.gt(Number.MAX_SAFE_INTEGER) || quantity.decimalPlaces() > 12) throw new Error("LlamaParse credits must fit the exact 12-decimal quantity domain");
  const revision = options.revision ?? 2;
  if (!Number.isSafeInteger(revision) || revision < 2 || revision > old.revision + 1) throw new Error("Terminal usage revisions start at 2 and must be contiguous");
  if (revision < old.revision) return false;
  const ended = timestamp(job.updated_at, true);
  if (ended < started || ended < old.observedAt) throw new Error("Provider completion cannot move backwards");
  const next = new ProviderJobRevision({ ...old, revision, observedAt: ended, status: quantity.isZero() ? "unknown" : "succeeded",
    usage: quantity.isZero() ? [] : [{ metric: "llamaparse.credits", quantity, unit: "Credits" }] });
  if (!same(next, old)) tracker.buffer.insertProviderJobRevision(next);
  return true;
}
const facades = new WeakMap<object, { active: boolean }>();
// Generated SDK calls return APIPromise, not just Promise. Keep raw-response
// helpers and private receivers; observe only when the caller consumes parsed data.
function observePromise<T extends object>(promise: T, capture: (response: Response) => Response): T {
  return new Proxy(promise, { get(target, name) {
    const native = Reflect.get(target, name, target);
    if (typeof native !== "function") return native;
    if (name === "then") return (fulfilled?: (value: Response) => unknown, rejected?: (error: unknown) => unknown) =>
      Reflect.apply(native, target, [(value: Response) => { const observed = capture(value); return fulfilled ? fulfilled(observed) : observed; }, rejected]);
    // Observe the native fulfillment BEFORE user callbacks. A catch callback can
    // synthesize a result; that is not provider evidence and must not be captured.
    if (name === "catch" || name === "finally") {
      const then = Reflect.get(target, "then", target);
      if (typeof then !== "function") return native.bind(target);
      if (name === "catch") return (rejected?: (error: unknown) => unknown) =>
        Reflect.apply(then, target, [capture, rejected]);
      return (...args: unknown[]) => {
        const observed = Reflect.apply(then, target, [capture]);
        return Reflect.apply(observed.finally, observed, args);
      };
    }
    return native.bind(target);
  } });
}
function facade<T extends object>(client: T, tracker: CostTracker, scope: Scope, state = { active: true }, parsing = false): T {
  const wrapped = new Proxy(client, { get(target, name) {
    const native = Reflect.get(target, name, target);
    if (!parsing && name === "parsing" && state.active && typeof native === "object" && native !== null) return facade(native, tracker, scope, state, true);
    if (typeof native !== "function") return native;
    if (!parsing || !["create", "get", "parse"].includes(String(name))) return native.bind(target);
    return (...args: unknown[]) => {
      if (!state.active || providerCaptureIsClaimed()) return Reflect.apply(native, target, args);
      const task = getCurrentTask();
      const capture = (response: Response) => {
        if (!state.active) return response;
        const save = () => { try {
          if (name === "create" || name === "parse") bindLlamaParseJob(tracker, response, { ...scope, tier: (args[0] as { tier?: string } | undefined)?.tier });
          const { record } = identity(response, scope), old = previous(tracker, record);
          if (old?.revision === 1) recordLlamaParseJob(tracker, response, scope);
        } catch { /* Telemetry cannot change native provider behavior. */ } };
        if (task) runWithTask(task, save); else save(); return response;
      };
      return runWithProviderCapture("llamaparse", () => {
        const result = Reflect.apply(native, target, args);
        return result && typeof result.then === "function" ? observePromise(result, capture) : capture(result);
      });
    };
  } });
  facades.set(wrapped, state); return wrapped;
}
/** Wrap hosted llama-cloud parsing.create/get/parse. Explicitly request expand: ["usage", ...].
 * No extra requests/options/polling. Missing credits await caller's next get; v1, raw/streaming,
 * Extract/Index/self-hosted are excluded. Cached job IDs retain the original task owner.
 */
export function instrumentLlamaParse<T extends object>(client: T, tracker: CostTracker, options: Scope): T {
  return facade(client, tracker, { billingAccountId: id(options.billingAccountId), projectId: id(options.projectId) });
}
export function uninstrumentLlamaParse(client: object): void { const state = facades.get(client); if (state) state.active = false; }
