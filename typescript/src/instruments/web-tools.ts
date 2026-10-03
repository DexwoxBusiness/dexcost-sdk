/** Explicit native response capture. Bind async jobs in their owning task first.
 * Only allowlisted identities, timestamps and meters persist; never bodies/prices.
 */
import { getCurrentTask, runWithTask } from "../core/context.js";
import { providerCaptureIsClaimed, runWithProviderCapture } from "./provider-capture.js";
import { Decimal } from "../core/models.js";
import { ProviderJobRevision, providerJobFromDict, type ProviderJobStatus } from "../core/provider-jobs.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";

type NativeResponse = Record<string, unknown>;
function id(value: unknown): string {
  if (typeof value !== "string") throw new Error("Provider ID must be a string");
  databaseResourceId("validation", value);
  return value;
}
function date(value: unknown): Date {
  if (value instanceof Date && Number.isFinite(value.getTime())) return new Date(value.getTime());
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3}0*)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) throw new Error("Provider timestamp must be timezone-aware with millisecond precision");
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) throw new Error("Invalid provider timestamp");
  return parsed;
}
function previous(tracker: CostTracker, provider: string, service: string, record: string): ProviderJobRevision | undefined {
  const raw = tracker.buffer.getProviderJob(provider, service, record);
  return raw ? providerJobFromDict(raw) : undefined;
}
function same(a: ProviderJobRevision, b: ProviderJobRevision): boolean { return JSON.stringify(a.toDict()) === JSON.stringify(b.toDict()); }
function bind(tracker: CostTracker, provider: string, service: string, record: string, resource: string, started: Date, operation: string): boolean {
  const task = getCurrentTask();
  if (!task) return false;
  const job = new ProviderJobRevision({ taskId: task.taskId, provider, service, providerRecordId: record,
    operation, component: "external", eventType: "external_cost", resourceType: "endpoint", resourceId: resource,
    status: "submitted", submittedAt: started, observedAt: started });
  const old = previous(tracker, provider, service, record);
  if (old) {
    if (!same(new ProviderJobRevision({ ...old, revision: 1, status: "submitted", observedAt: started, usage: [] }), job)) throw new Error("Provider run belongs to another task, resource or operation");
    return true;
  }
  tracker.buffer.insertProviderJobRevision(job);
  return true;
}
function record(tracker: CostTracker, old: ProviderJobRevision, revision: number, ended: Date, status: ProviderJobStatus, usage: Array<{ metric: string; quantity: Decimal; unit: string }>): boolean {
  if (!Number.isSafeInteger(revision) || revision < 2) throw new Error("Terminal usage revisions start at 2");
  if (revision < old.revision) return false;
  if (revision > old.revision + 1) throw new Error("Usage revisions must be contiguous");
  if (ended < old.submittedAt || ended < old.observedAt) throw new Error("Provider completion cannot move backwards");
  const next = new ProviderJobRevision({ ...old, revision, observedAt: ended, status: status === "succeeded" && !usage.length ? "unknown" : status, usage });
  if (!same(next, old)) tracker.buffer.insertProviderJobRevision(next);
  return true;
}
/** Call with the native actor.start/call result in its initiating task. */
export function bindApifyRun(tracker: CostTracker, run: NativeResponse): boolean {
  return bind(tracker, "apify", "actor_runs", id(run.id), id(run.actId), date(run.startedAt), "actor.run");
}
/** Terminal ownership only. Authenticated server import supplies final money (including zero); no SDK formula or polling. */
export function recordApifyRun(tracker: CostTracker, run: NativeResponse, options: { revision?: number } = {}): boolean {
  const old = previous(tracker, "apify", "actor_runs", id(run.id));
  if (!old) return false;
  if (old.resourceId !== id(run.actId) || old.submittedAt.getTime() !== date(run.startedAt).getTime()) throw new Error("Apify run identity changed");
  const status = ({ SUCCEEDED: "succeeded", FAILED: "failed", "TIMED-OUT": "failed", ABORTED: "cancelled" } as const)[String(run.status) as "SUCCEEDED"];
  if (!status) return false;
  return record(tracker, old, options.revision ?? 2, date(run.finishedAt), status, [{ metric: "apify.run_count", quantity: new Decimal(1), unit: "Runs" }]);
}
export interface FirecrawlBinding {
  billingAccountId: string;
  resourceId: string;
  jobId: string;
  /** Provider createdAt from the first status response; bind in its owning task. */
  startedAt: Date;
  operation: "crawl" | "batch_scrape";
}
export function bindFirecrawlJob(tracker: CostTracker, options: FirecrawlBinding): boolean {
  if (!["crawl", "batch_scrape"].includes(options.operation)) throw new Error("Unsupported Firecrawl operation");
  return bind(tracker, "firecrawl", "web", databaseResourceId(options.billingAccountId, options.jobId), databaseResourceId(options.billingAccountId, options.resourceId), date(options.startedAt), `firecrawl.${options.operation}`);
}
function credits(value: unknown): Array<{ metric: string; quantity: Decimal; unit: string }> {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw new Error("Firecrawl creditsUsed must be an exact nonnegative integer");
  return value ? [{ metric: "firecrawl.credits", quantity: new Decimal(value as number), unit: "Credits" }] : [];
}
/** Full v2 crawl/batch snapshot. Explicit next revision for corrections; never sum polling pages. */
export function recordFirecrawlJob(tracker: CostTracker, response: NativeResponse, options: { billingAccountId: string; jobId: string; revision?: number }): boolean {
  const old = previous(tracker, "firecrawl", "web", databaseResourceId(options.billingAccountId, options.jobId));
  if (!old) return false;
  const status = ({ completed: "succeeded", failed: "failed", cancelled: "cancelled" } as const)[String(response.status) as "completed"];
  if (!status) return false;
  if (old.submittedAt.getTime() !== date(response.createdAt).getTime()) throw new Error("Firecrawl job identity changed");
  return record(tracker, old, options.revision ?? 2, date(response.completedAt), status, credits(response.creditsUsed));
}
/** Synchronous search native response in its owning task; account-scoped invoice resource, no guessed cash.
 * Requires original request start and response-observed finish, not later archived-response import time.
 */
export function recordFirecrawlSearch(tracker: CostTracker, response: NativeResponse, options: { billingAccountId: string; resourceId: string; occurredAt: Date; observedAt: Date; revision?: number }): boolean {
  return recordFirecrawlRequest(tracker, response, options, "firecrawl.search");
}
function recordFirecrawlRequest(tracker: CostTracker, response: NativeResponse, options: { billingAccountId: string; resourceId: string; occurredAt: Date; observedAt: Date; revision?: number }, operation: string): boolean {
  if (response.success !== true) return false;
  const key = databaseResourceId(options.billingAccountId, id(response.id)), resource = databaseResourceId(options.billingAccountId, options.resourceId);
  const usage = credits(response.creditsUsed);
  const started = date(options.occurredAt), ended = date(options.observedAt);
  if (ended < started) throw new Error("Response observation cannot precede request start");
  if (!bind(tracker, "firecrawl", "web", key, resource, started, operation)) return false;
  return record(tracker, previous(tracker, "firecrawl", "web", key)!, options.revision ?? 2, ended, "succeeded", usage);
}

type WebToolScope = { billingAccountId: string; resourceId?: string };
const facades = new WeakMap<object, { active: boolean; provider: string }>();
function instrument<T extends object>(client: T, tracker: CostTracker, scope: WebToolScope, provider: string, kind = "root", state = { active: true, provider }): T {
  const facade = new Proxy(client, {
    get(target, name) {
      const native = Reflect.get(target, name, target);
      if (typeof native !== "function") return native;
      if (provider === "apify" && kind === "root" && ["actor", "task", "run"].includes(String(name))) {
        return (...args: unknown[]) => {
          const result = Reflect.apply(native, target, args);
          return state.active ? instrument(result, tracker, scope, provider, String(name), state) : result;
        };
      }
      const enabled = (provider === "firecrawl" && ["scrape", "search"].includes(String(name))) ||
        (provider === "apify" && ((["actor", "task"].includes(kind) && ["start", "call"].includes(String(name))) ||
          (kind === "run" && ["get", "waitForFinish"].includes(String(name)))));
      if (!enabled) return native.bind(target);
      return (...args: unknown[]) => {
        if (!state.active || providerCaptureIsClaimed()) return Reflect.apply(native, target, args);
        const task = getCurrentTask(), started = new Date();
        const capture = (result: NativeResponse) => {
          const ended = new Date();
          if (!state.active) return result;
          const save = () => {
            try {
              if (provider === "apify") {
                if (result?.userId !== scope.billingAccountId) return;
                if (["actor", "task"].includes(kind)) bindApifyRun(tracker, result);
                const old = previous(tracker, "apify", "actor_runs", id(result.id));
                if (old?.revision === 1) recordApifyRun(tracker, result);
              } else if (task) {
                const metadata = result?.metadata as NativeResponse | undefined;
                const payload = name === "scrape" ? { success: true, id: metadata?.scrapeId, creditsUsed: metadata?.creditsUsed } :
                  { success: true, id: result?.id, creditsUsed: result?.creditsUsed };
                recordFirecrawlRequest(tracker, payload, { billingAccountId: scope.billingAccountId, resourceId: scope.resourceId!, occurredAt: started, observedAt: ended }, `firecrawl.${String(name)}`);
              }
            } catch { /* Telemetry never changes native provider behavior. */ }
          };
          if (task) runWithTask(task, save); else save();
          return result;
        };
        return runWithProviderCapture(provider, () => {
          const result = Reflect.apply(native, target, args);
          return result && typeof result.then === "function" ? result.then(capture) : capture(result);
        });
      };
    },
  });
  facades.set(facade, state);
  return facade;
}
/** Wrap native actor/task start/call and run get/waitForFinish. Account must equal run.userId.
 * No extra polling; native run prices are ignored in favor of authenticated server reconciliation.
 */
export function instrumentApify<T extends object>(client: T, tracker: CostTracker, options: { billingAccountId: string }): T {
  return instrument(client, tracker, { billingAccountId: id(options.billingAccountId) }, "apify");
}
export function uninstrumentApify(client: object): void {
  const state = facades.get(client); if (state?.provider === "apify") state.active = false;
}
/** Hosted Firecrawl v2 facade: requires exact metadata scrapeId/creditsUsed or search id/creditsUsed.
 * Missing fields stay unknown. Async jobs use explicit bind/record with provider timestamps.
 * Mapping identifies the opaque account/resource, never the API key or a page URL.
 */
export function instrumentFirecrawl<T extends object>(client: T, tracker: CostTracker, options: { billingAccountId: string; resourceId: string }): T {
  return instrument(client, tracker, { billingAccountId: id(options.billingAccountId), resourceId: id(options.resourceId) }, "firecrawl");
}
export function uninstrumentFirecrawl(client: object): void {
  const state = facades.get(client); if (state?.provider === "firecrawl") state.active = false;
}
