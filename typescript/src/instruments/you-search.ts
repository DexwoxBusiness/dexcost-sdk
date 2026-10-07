/** Current You.com direct fetch capture: base Search only; no query/result retention. */
import { createHash } from "node:crypto";
import { getCurrentTask } from "../core/context.js";
import { Decimal } from "../core/models.js";
import { ProviderJobRevision } from "../core/provider-jobs.js";
import type { CostTracker } from "../core/tracker.js";
import { databaseResourceId } from "./database.js";
import { providerCaptureIsClaimed, runWithProviderCapture } from "./provider-capture.js";

export interface YouSearchOptions {
  billingAccountId: string;
  endpoint: string;
  /** Caller-attested public PAYG eligibility, not actual credits/cash evidence. */
  billingTier: "paid" | "free" | "unknown";
  fetch?: typeof globalThis.fetch;
}
const states = new WeakMap<object, { active: boolean }>();
const BASE = new Set(["query", "count", "freshness", "offset", "country", "language", "safesearch", "knowledge", "include_domains", "exclude_domains", "boost_domains", "crawl_timeout"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
function endpoint(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:" || !["api.you.com", "ydc-index.io"].includes(url.hostname) || url.port || url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new Error("An exact direct You.com HTTPS endpoint is required");
  return url.origin;
}
async function isBase(request: Request, origin: string): Promise<boolean> {
  const url = new URL(request.url);
  if (endpoint(url.origin) !== origin || url.pathname !== "/v1/search" || url.hash ||
      !request.headers.has("x-api-key") || ["authorization", "payment-signature", "x-payment", "x-forwarded-host"].some(name => request.headers.has(name)) ||
      (request.headers.has("host") && ![url.hostname, `${url.hostname}:443`].includes(request.headers.get("host")!))) return false;
  let body: Record<string, unknown>;
  if (request.method === "POST") {
    if (url.search || request.headers.get("content-type")?.split(";")[0] !== "application/json") return false;
    // JSON duplicates are rejected conservatively: the serialized object must
    // retain the same number of top-level keys (including unknown/add-on keys).
    const text = await request.clone().text();
    body = JSON.parse(text);
    if (!body || typeof body !== "object" || Array.isArray(body)) return false;
    const keys = [...text.matchAll(/(?:^|[,{])\s*"((?:[^"\\]|\\.)*)"\s*:/g)].map(match => JSON.parse(`"${match[1]}"`));
    if (keys.length !== Object.keys(body).length) return false;
  } else if (request.method === "GET") {
    const pairs = [...url.searchParams];
    if (new Set(pairs.map(([key]) => key)).size !== pairs.length) return false;
    body = Object.fromEntries(pairs);
    if (typeof body.count === "string" && /^\d+$/.test(body.count)) body.count = Number(body.count);
  } else return false;
  const count = "count" in body ? body.count : 10;
  return Object.keys(body).every(key => BASE.has(key)) && typeof body.query === "string" && body.query.length > 0 &&
    typeof count === "number" && Number.isInteger(count) && count >= 1 && count <= 100 && (body.knowledge == null || body.knowledge === "core");
}
/** Bind an explicit account/tier to a direct fetch transport. Return Response/body unchanged.
 * No hidden retries/requests. SDK-generated MCP/Contents/Answer/Research and all extraction
 * options are excluded. Recreate this wrapper after changing account/credentials.
 */
export function createYouSearchFetch(tracker: CostTracker, options: YouSearchOptions): typeof globalThis.fetch {
  databaseResourceId(options.billingAccountId, "validation");
  const origin = endpoint(options.endpoint), account = options.billingAccountId, tier = options.billingTier;
  if (!["paid", "free", "unknown"].includes(tier)) throw new Error("Explicit paid, free or unknown public billing tier is required");
  const native = options.fetch ?? globalThis.fetch.bind(globalThis), state = { active: true };
  const wrapped: typeof globalThis.fetch = (input, init) => {
    const task = getCurrentTask(), started = new Date();
    if (!state.active || !task || providerCaptureIsClaimed()) return native(input, init);
    return runWithProviderCapture("you_com", async () => {
      let request: Request | undefined, eligible = false;
      try { request = input instanceof Request ? new Request(input.clone(), init) : new Request(input, init); } catch { /* unknown request is unpriced */ }
      // Invoke native fetch before the first await so caller mutation after
      // invocation cannot change the dispatched request after our snapshot.
      const response = await native(input, init);
      try {
        if (request) eligible = await isBase(request, origin);
        if (!state.active || !eligible || response.status !== 200 || response.redirected || (response.url && response.url !== request!.url)) return response;
        const data = await response.clone().json() as { results?: unknown; metadata?: { search_uuid?: unknown } }, identifier = data?.metadata?.search_uuid;
        if (!data?.results || typeof data.results !== "object" || Array.isArray(data.results) || typeof identifier !== "string" || !UUID.test(identifier)) return response;
        const record = createHash("sha256").update(JSON.stringify([account, identifier.toLowerCase()])).digest("hex");
        if (!tracker.buffer.getProviderJob("you_com", "search", record)) tracker.buffer.insertProviderJobRevision(new ProviderJobRevision({
          taskId: task.taskId, provider: "you_com", service: "search", providerRecordId: record,
          operation: "search.base", component: "external", eventType: "external_cost", resourceType: "sku", resourceId: "base",
          status: "succeeded", submittedAt: started, observedAt: new Date(),
          billingDimensions: tier === "paid" ? [["you_search_billing_lane", "public_payg_base"]] : [],
          usage: [{ metric: "service.request_count", quantity: new Decimal(1), unit: "Requests" }],
        }));
      } catch { /* Capture cannot alter provider response/errors. */ }
      return response;
    });
  };
  states.set(wrapped, state); return wrapped;
}
export function uninstrumentYouSearch(fetch: typeof globalThis.fetch): void { const state = states.get(fetch); if (state) state.active = false; }
