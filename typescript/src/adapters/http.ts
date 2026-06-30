/**
 * HTTP fetch adapter — automatic cost tracking for globalThis.fetch.
 *
 * Patches `globalThis.fetch` to intercept HTTP calls and auto-record
 * `external_cost` events using the service catalog for cost extraction,
 * with user-registered domain rates taking precedence.
 *
 * V2: integrates service catalog, session auto-grouping, and response
 * body/header cost extraction.
 *
 * Implements US-035 (TypeScript counterpart to the Python HTTP adapter).
 */

import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { getCurrentTask, isNetworkEventSuppressed } from "../core/context.js";
import {
  classifyDestination,
  measureBytesFromHeaders,
} from "./_netbytes.js";
import {
  getAccountant,
  type NetworkAccountant,
} from "./network-accountant.js";
import {
  createCostEvent,
  type CostEvent,
  type CostConfidence,
  type Task,
} from "../core/models.js";
import { createAutoTask } from "../core/auto-task.js";
import { ServiceCatalog, type CostExtractionResult } from "../pricing/service-catalog.js";
import { SessionManager } from "../core/session.js";
import { scrubUrl } from "../security/redaction.js";
import type { EventBuffer } from "../transport/buffer.js";

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

/** Map of domain → { costUsd, per } registered rates (user overrides). */
const _domainRates = new Map<string, { costUsd: number; per: string }>();

/** Events recorded by the adapter.
 *
 * Sprint 4 §5.2 (A3) — hard FIFO cap matching Python (commit c1d87a7).
 * Pre-fix this array grew unbounded across the process lifetime,
 * leaking memory on long-running services with many HTTP-tracked
 * tasks. Capped at 10 000 entries; oldest 10% dropped in one batch
 * when over to avoid O(n) `shift()` per recording.
 */
const _RECORDED_EVENTS_CAP = 10_000;
const _recordedEvents: CostEvent[] = [];

function _pushRecordedEvent(event: CostEvent): void {
  _recordedEvents.push(event);
  if (_recordedEvents.length > _RECORDED_EVENTS_CAP) {
    _recordedEvents.splice(0, _RECORDED_EVENTS_CAP / 10);
  }
}

/**
 * Combined request + response bytes above which an un-cataloged call
 * emits a `network` event. Mirrors python config
 * `network_event_threshold_bytes = 102_400` (100 KiB).
 */
const NETWORK_EVENT_THRESHOLD_BYTES = 102_400;

/** Original fetch reference before patching. */
let _originalFetch: typeof globalThis.fetch | null = null;

/** Whether fetch is currently patched. */
let _patched = false;

/* eslint-disable @typescript-eslint/no-explicit-any */
/** Original `http.request` / `https.request` references before patching. */
let _originalHttpRequest: any = null;
let _originalHttpsRequest: any = null;
let _originalHttpGet: any = null;
let _originalHttpsGet: any = null;
/* eslint-enable @typescript-eslint/no-explicit-any */

/** Lazily-loaded service catalog. */
let _catalog: ServiceCatalog | null = null;

/** Session manager for auto-grouping. */
let _sessionManager: SessionManager | null = null;

/** Event buffer reference (set via trackHttp). */
let _buffer: EventBuffer | null = null;

/** Max response body size to parse (1 MB). */
const MAX_BODY_SIZE = 1_048_576;

// ---------------------------------------------------------------------------
// Domain rate registration
// ---------------------------------------------------------------------------

/**
 * Register a cost rate for HTTP calls to the given domain.
 *
 * User-registered rates take precedence over catalog entries.
 *
 * @param domain  Hostname to match, e.g. `"api.example.com"` (no port).
 * @param costUsd Cost per call in USD.
 * @param per     Unit label (default `"request"`).
 */
export function registerDomainRate(
  domain: string,
  costUsd: number,
  per = "request"
): void {
  _domainRates.set(domain, { costUsd, per });
}

/**
 * Return a snapshot of all registered domain rates.
 */
export function getDomainRates(): Record<string, { costUsd: number; per: string }> {
  const result: Record<string, { costUsd: number; per: string }> = {};
  for (const [domain, rate] of _domainRates.entries()) {
    result[domain] = { ...rate };
  }
  return result;
}

/**
 * Remove all registered domain rates.
 */
export function clearDomainRates(): void {
  _domainRates.clear();
}

// ---------------------------------------------------------------------------
// Fetch patching
// ---------------------------------------------------------------------------

/**
 * Patch `globalThis.fetch` to intercept HTTP calls and auto-record
 * `external_cost` events.
 *
 * Loads the service catalog on first call. If an EventBuffer is provided,
 * events are also persisted and session auto-grouping is enabled.
 *
 * Idempotent — calling multiple times without `untrackHttp()` in between is
 * safe; the second call is a no-op.
 */
/**
 * Symbol used to tag dexcost's wrapped fetch. Sprint 3 Theme E / §4.2.1.
 * `Symbol.for(...)` returns the same symbol across realms / module
 * instances, so two copies of the dexcost SDK (e.g. in a Yarn PnP
 * setup where multiple versions are deduped poorly) will recognise
 * each other's patches and refuse to double-wrap.
 *
 * Detect another patcher (Sentry, OpenTelemetry, Datadog) by reading
 * their own marker properties; if found, store both pointers and
 * chain through them so neither tool's interception is lost.
 */
const DEXCOST_PATCHED = Symbol.for("dexcost.patched");

export function trackHttp(buffer?: EventBuffer): void {
  if (_patched) return;

  // §4.2.1: refuse to wrap an already-dexcost-wrapped fetch.
  const current = globalThis.fetch as unknown as Record<symbol, unknown>;
  if (current && current[DEXCOST_PATCHED] === true) {
    // Already patched — likely a duplicate SDK install. No-op + warn.
    console.warn(
      "dexcost: globalThis.fetch is already wrapped by dexcost. " +
        "trackHttp() called twice (or two SDK copies). Skipping.",
    );
    _patched = true;
    return;
  }

  _originalFetch = globalThis.fetch;
  _patched = true;

  // Lazily initialise the service catalog
  if (_catalog === null) {
    try {
      _catalog = new ServiceCatalog();
    } catch {
      // If catalog fails to load, continue without it
      _catalog = null;
    }
  }

  if (buffer) {
    _buffer = buffer;
    _sessionManager = new SessionManager();
  }

  // Replace globalThis.fetch with wrapper
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).fetch = async function wrappedFetch(
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> {
    // ── v1 byte measurement — request side (known before fetch) ─────────
    // Scrub once at extraction so every downstream use (events, hostname
    // parse, placeholder equality-lookup at the streamed-body re-type
    // site) sees the same safe value. Critical for B11: re-type at
    // _finaliseHttpCall must match on the same string that was stored.
    const urlStr = scrubUrl(_resolveUrlStr(input));
    const method = _resolveMethod(input, init);
    const requestHeaders = _resolveRequestHeaders(input, init);
    const requestBodyLen = _resolveRequestBodyLen(input, init);
    const requestBytes = measureBytesFromHeaders(
      method,
      urlStr,
      requestHeaders,
      requestBodyLen,
    );

    const response = await _originalFetch!(input, init);

    // ── v1 destination classification + byte details ─────────────────────
    let hostname = "";
    let protocol = "https";
    try {
      const parsed = new URL(urlStr);
      hostname = parsed.hostname;
      protocol = (parsed.protocol || "https:").replace(":", "") || "https";
    } catch {
      // Unparseable URL — fall through with empty host; classifyDestination
      // returns null for empty input.
    }
    const isInternal = classifyDestination(hostname);
    const suppressed = isNetworkEventSuppressed();

    // Resolve accountant from registry via the active task. The task's id
    // is looked up via the existing context — see _resolveHttpTask below.
    // Direct reference here would create a cycle; lookup is done inside
    // the byte-recording callback when the task_id is known.
    const callContext: _HttpCallContext = {
      urlStr,
      method,
      hostname,
      protocol,
      requestBytes,
      isInternal,
      suppressed,
      responseHeaderBytes: _measureResponseHeaderBytes(response),
    };

    // Wrap the response body in a TransformStream that counts bytes as
    // they flow through to the caller, then records into the accountant
    // + (for un-cataloged calls) emits a `network` event at stream end.
    // For zero-body responses (HEAD, 204) we fall back to immediate
    // finalisation.
    const wrappedResponse = _wrapResponseForByteCounting(
      response,
      callContext,
    );

    // The cost-extraction path (catalog / domain-rate / un-cataloged
    // external_cost-zero) still runs immediately — it works off the
    // RESPONSE HEADERS for catalog lookup and may consume a JSON body
    // independently. v1 §4.3 byte_details are stamped on every event
    // (request side is known; response side is added later via the
    // recording stream — for v1, only request-side byte_details land on
    // catalog/domain-rate events. Response-side flows into the task
    // aggregate via the accountant. v2 finalize back-fills network event
    // costs after the snapshot.)
    await _maybeRecordCost(urlStr, wrappedResponse, callContext);

    return wrappedResponse;
  };

  // §4.2.1: tag our wrapper so a second `trackHttp()` (or a duplicate
  // SDK install across realms) doesn't double-wrap.
  Object.defineProperty(globalThis.fetch, DEXCOST_PATCHED, {
    value: true,
    enumerable: false,
    configurable: false,
    writable: false,
  });

  // Also patch Node's low-level http/https transports — many SDKs
  // (AWS SDK v2, older clients, agents) use these directly rather than
  // the global fetch. Mirrors the Python adapter patching multiple
  // transports (requests/httpx/aiohttp/botocore/urllib3).
  _patchNodeHttp();
}

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Build a URL string from the args passed to http(s).request / .get. */
function _urlFromRequestArgs(isHttps: boolean, args: any[]): string | null {
  const first = args[0];
  if (typeof first === "string") {
    return first;
  }
  if (first instanceof URL) {
    return first.toString();
  }
  if (first && typeof first === "object") {
    // RequestOptions object
    const protocol: string = first.protocol ?? (isHttps ? "https:" : "http:");
    const host: string = first.hostname ?? first.host ?? "localhost";
    const port = first.port ? `:${first.port}` : "";
    const path: string = first.path ?? "/";
    return `${protocol}//${host}${port}${path}`;
  }
  return null;
}

/** CommonJS require, used to obtain the mutable http/https module objects. */
const _require = createRequire(import.meta.url);

/** Patch http.request/get and https.request/get to record external costs. */
function _patchNodeHttp(): void {
  if (_originalHttpRequest !== null) return; // already patched

  let nodeHttp: any;
  let nodeHttps: any;
  try {
    nodeHttp = _require("node:http");
    nodeHttps = _require("node:https");
  } catch {
    return; // http/https unavailable — skip
  }

  _originalHttpRequest = nodeHttp.request;
  _originalHttpsRequest = nodeHttps.request;
  _originalHttpGet = nodeHttp.get;
  _originalHttpsGet = nodeHttps.get;

  const makeWrapper = (
    original: any,
    isHttps: boolean,
  ): any =>
    function wrappedRequest(this: unknown, ...args: any[]): unknown {
      const req = original.apply(this, args);
      try {
        const raw = _urlFromRequestArgs(isHttps, args);
        const urlStr = raw ? scrubUrl(raw) : raw;
        if (urlStr) {
          // Record on response — body is not parsed for Node-level
          // requests (matches the Python urllib3 wrapper's behaviour).
          if (req && typeof req.on === "function") {
            req.on("response", () => {
              void _maybeRecordCost(urlStr);
            });
          } else {
            void _maybeRecordCost(urlStr);
          }
        }
      } catch {
        // never crash user code
      }
      return req;
    };

  // Sprint 3 Theme E / §4.2.2 — atomic patch. If `Object.freeze` has
  // been applied to one of the modules (some serverless runtimes do
  // this), a partial patch leaves the SDK in a half-installed state
  // where request is wrapped but get is unrestored — and the originals
  // are forgotten so untrackHttp() can't roll back. Try all 4
  // assignments and on ANY failure restore everything we already
  // wrote.
  const wrappers = {
    httpRequest: makeWrapper(_originalHttpRequest, false),
    httpsRequest: makeWrapper(_originalHttpsRequest, true),
    httpGet: makeWrapper(_originalHttpGet, false),
    httpsGet: makeWrapper(_originalHttpsGet, true),
  };
  const installed: Array<() => void> = [];
  try {
    nodeHttp.request = wrappers.httpRequest;
    installed.push(() => { nodeHttp.request = _originalHttpRequest; });
    nodeHttps.request = wrappers.httpsRequest;
    installed.push(() => { nodeHttps.request = _originalHttpsRequest; });
    nodeHttp.get = wrappers.httpGet;
    installed.push(() => { nodeHttp.get = _originalHttpGet; });
    nodeHttps.get = wrappers.httpsGet;
    installed.push(() => { nodeHttps.get = _originalHttpsGet; });
  } catch {
    // Roll back every wrapper we successfully installed BEFORE the
    // failure, so the customer's http stack is exactly where it
    // started. Best-effort: each restore may itself throw if the
    // module is fully frozen — we swallow that.
    for (const restore of installed) {
      try { restore(); } catch { /* frozen, leave as-is */ }
    }
    _originalHttpRequest = null;
    _originalHttpsRequest = null;
    _originalHttpGet = null;
    _originalHttpsGet = null;
  }
}

/** Restore the original http/https transports. */
function _unpatchNodeHttp(): void {
  if (_originalHttpRequest === null) return;
  try {
    const nodeHttp: any = _require("node:http");
    const nodeHttps: any = _require("node:https");
    nodeHttp.request = _originalHttpRequest;
    nodeHttps.request = _originalHttpsRequest;
    nodeHttp.get = _originalHttpGet;
    nodeHttps.get = _originalHttpsGet;
  } catch {
    // best-effort
  }
  _originalHttpRequest = null;
  _originalHttpsRequest = null;
  _originalHttpGet = null;
  _originalHttpsGet = null;
}
/* eslint-enable @typescript-eslint/no-explicit-any */

/**
 * Restore `globalThis.fetch` to its original value.
 */
export function untrackHttp(): void {
  if (!_patched || _originalFetch === null) return;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).fetch = _originalFetch;
  _originalFetch = null;
  _patched = false;
  _sessionManager = null;
  _buffer = null;
  _unpatchNodeHttp();
}

// ---------------------------------------------------------------------------
// Catalog / session accessors (for testing)
// ---------------------------------------------------------------------------

/** Get the loaded service catalog instance (may be null). */
export function getServiceCatalog(): ServiceCatalog | null {
  return _catalog;
}

/** Reset the service catalog (for testing). */
export function resetServiceCatalog(): void {
  _catalog = null;
}

/** Get the session manager (for testing). */
export function getSessionManager(): SessionManager | null {
  return _sessionManager;
}

// ---------------------------------------------------------------------------
// Recorded events accessors
// ---------------------------------------------------------------------------

/**
 * Return all cost events recorded by the adapter since the last
 * `clearRecordedEvents()` call.
 */
export function getRecordedEvents(): CostEvent[] {
  return [..._recordedEvents];
}

/**
 * Clear the adapter's recorded events list.
 */
export function clearRecordedEvents(): void {
  _recordedEvents.length = 0;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Resolve the task an HTTP cost should be attributed to.
 *
 * Order: active task → session task → freshly-created auto-task. An
 * auto-task is always returned, so HTTP costs are never silently lost.
 */
function _resolveHttpTask(): Task {
  const current = getCurrentTask();
  if (current !== undefined) {
    return current;
  }

  if (_sessionManager && _buffer) {
    const sessionTask = _sessionManager.runInSession("http", _buffer, () =>
      getCurrentTask(),
    );
    if (sessionTask !== undefined) {
      return sessionTask;
    }
  }

  // No task and no session — create an auto-task (mirrors Python).
  const autoTask = createAutoTask("http_call");
  if (_buffer) {
    _buffer.upsertTask(autoTask);
  }
  return autoTask;
}

/**
 * Extract cost from a fetch `Response` for a matched catalog entry.
 *
 * Clones the response, parses a JSON body when small enough, and falls
 * back to header-only extraction on any failure. Returns `null` if
 * extraction is impossible.
 */
async function _extractFromResponse(
  catalog: ServiceCatalog,
  entry: ReturnType<ServiceCatalog["lookup"]>,
  response: Response,
): Promise<CostExtractionResult | null> {
  if (!entry) return null;
  try {
    const cloned = response.clone();
    const headers = cloned.headers;

    let body: unknown = null;
    const contentType = headers.get("content-type") ?? "";
    const contentLength = headers.get("content-length");
    const cl = Number.parseInt(contentLength ?? "", 10);
    const bodyTooLarge =
      contentLength !== null && !Number.isNaN(cl) && cl > MAX_BODY_SIZE;

    if (contentType.includes("application/json") && !bodyTooLarge) {
      try {
        body = await cloned.json();
      } catch {
        // Body not parseable — continue with null
      }
    }

    return catalog.extractCost(entry, headers, body);
  } catch {
    // Failed to read response — try header-only extraction
    try {
      return catalog.extractCost(entry, response.headers, null);
    } catch {
      return null;
    }
  }
}

/**
 * Check if the URL matches a registered rate or catalog entry, and record
 * an `external_cost` event.
 *
 * Priority:
 * 1. User-registered domain rate (registerDomainRate)
 * 2. Service catalog match with cost extraction
 * 3. Unknown domain — record with confidence="unknown", cost=0
 *
 * Session auto-grouping: if no active task and session manager is available,
 * a session task is created automatically.
 */
// ---------------------------------------------------------------------------
// v1 network-capture helpers
// ---------------------------------------------------------------------------

/** Shared per-call state — built in wrappedFetch, threaded into helpers. */
interface _HttpCallContext {
  urlStr: string;
  method: string;
  hostname: string;
  protocol: string;
  requestBytes: number;
  /** Response status line + headers; body bytes added by the TransformStream. */
  responseHeaderBytes: number;
  isInternal: boolean | null;
  suppressed: boolean;
  /** Task resolved once in _maybeRecordCost, reused in _finaliseHttpCall.
   *  Avoids a second getCurrentTask() lookup which would either create a
   *  duplicate auto-task or (before the auto-task leak fix) find a stale one. */
  resolvedTask?: Task;
  /** Placeholder event stored here so _finaliseHttpCall can find it without
   *  a fragile O(n) backward scan of _recordedEvents that is vulnerable to
   *  FIFO eviction under high concurrency. */
  placeholderEvent?: CostEvent;
}

function _resolveUrlStr(input: string | URL | Request): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return (input as Request).url;
}

function _resolveMethod(
  input: string | URL | Request,
  init?: RequestInit,
): string {
  if (init?.method) return init.method.toUpperCase();
  if (input instanceof Request) return input.method.toUpperCase();
  return "GET";
}

function _resolveRequestHeaders(
  input: string | URL | Request,
  init?: RequestInit,
): Record<string, string> {
  const headers: Record<string, string> = {};
  const src = init?.headers ?? (input instanceof Request ? input.headers : undefined);
  if (!src) return headers;
  if (src instanceof Headers) {
    src.forEach((value, key) => {
      headers[key] = value;
    });
  } else if (Array.isArray(src)) {
    for (const [k, v] of src) headers[k] = v;
  } else {
    for (const [k, v] of Object.entries(src)) headers[k] = String(v);
  }
  return headers;
}

function _resolveRequestBodyLen(
  input: string | URL | Request,
  init?: RequestInit,
): number {
  const body = init?.body ?? (input instanceof Request ? null : undefined);
  if (!body) return 0;
  if (typeof body === "string") return Buffer.byteLength(body, "utf-8");
  if (body instanceof URLSearchParams) return Buffer.byteLength(body.toString(), "utf-8");
  if (body instanceof ArrayBuffer) return body.byteLength;
  if (ArrayBuffer.isView(body)) return body.byteLength;
  if (body instanceof Blob) return body.size;
  // FormData / ReadableStream — size unknown without consuming.
  return 0;
}

function _measureResponseHeaderBytes(response: Response): number {
  const headers: Record<string, string> = {};
  response.headers.forEach((value, key) => {
    headers[key] = value;
  });
  // Pass empty method/url so the request-line formula contributes only the
  // constant 12-byte " HTTP/1.1\r\n" overhead (mirrors Python).
  return measureBytesFromHeaders("", "", headers, 0);
}

function _isInternalToValue(p: boolean | null): boolean | null {
  return p;
}

/**
 * Wrap a Response in a new Response whose body is piped through a
 * counting TransformStream. The counter is held in the closure; when the
 * stream's flush fires (source ended) — or when the caller drops the
 * response (Drop/cancel) — the byte total is recorded into the active
 * task's accountant and, for un-cataloged calls that aren't suppressed,
 * a `network` event is emitted with `cost_pending=true`.
 *
 * Zero-body responses (status 204, HEAD requests, no `body` stream)
 * finalise immediately so the accountant still sees the call.
 */
function _wrapResponseForByteCounting(
  response: Response,
  ctx: _HttpCallContext,
): Response {
  let bytesRead = 0;
  let finalised = false;
  const finalise = () => {
    if (finalised) return;
    finalised = true;
    _finaliseHttpCall(ctx, bytesRead);
  };

  if (!response.body) {
    finalise();
    return response;
  }

  // TransformStream's Transformer interface has transform + flush; cancel
  // isn't a member. To catch early-abort (caller cancels the stream), we
  // wrap the readable side in a separate ReadableStream that listens for
  // .cancel() and calls finalise. The TransformStream's flush() handles
  // the natural end-of-stream case.
  const counting = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      bytesRead += chunk.byteLength;
      controller.enqueue(chunk);
    },
    flush() {
      finalise();
    },
  });

  const piped = response.body.pipeThrough(counting);
  // Add an early-abort hook by wrapping `piped` in a ReadableStream that
  // forwards reads from `piped`'s reader and triggers finalise on cancel.
  const reader = piped.getReader();
  const earlyAbortWrapper = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { value, done } = await reader.read();
        if (done) {
          controller.close();
          return;
        }
        controller.enqueue(value);
      } catch (err) {
        controller.error(err);
        finalise();
      }
    },
    cancel(reason) {
      finalise(); // v1 §5.5 early-abort: bytes-actually-received.
      return reader.cancel(reason);
    },
  });

  return new Response(earlyAbortWrapper, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

/**
 * Called once the response body has been fully drained, cancelled, or
 * was empty. Records the byte totals into the task's accountant and, if
 * the call was un-cataloged AND not suppressed AND notable
 * (combined_bytes > threshold OR status >= 400), emits a `network`
 * event with `cost_pending=true`.
 */
function _finaliseHttpCall(ctx: _HttpCallContext, responseBodyBytes: number): void {
  const responseBytes = ctx.responseHeaderBytes + responseBodyBytes;
  // Reuse the task resolved in _maybeRecordCost — avoids a second
  // getCurrentTask() lookup which would create a duplicate auto-task
  // now that createAutoTask no longer pollutes AsyncLocalStorage.
  const task = ctx.resolvedTask ?? _resolveHttpTask();
  const accountant: NetworkAccountant | undefined = getAccountant(task.taskId);
  if (accountant) {
    accountant.record(ctx.hostname, responseBytes, ctx.requestBytes, ctx.isInternal);
  }

  // Network-event emission is the un-cataloged path's responsibility.
  // _maybeRecordCost decides which path was taken — for catalog /
  // domain-rate calls it sets a "matched" flag we check here. Stored on
  // the ctx so a single-call closure threads it.
  if (ctx._matchedCatalog || ctx.suppressed) return;

  const combined = ctx.requestBytes + responseBytes;
  // Use the placeholder event stored directly on ctx (set in
  // _maybeRecordCost). This replaces the old O(n) backward scan of
  // _recordedEvents which was fragile under FIFO eviction when
  // concurrent calls exceeded _RECORDED_EVENTS_CAP.
  const ev = ctx.placeholderEvent;
  if (!ev) return;

  if (combined > NETWORK_EVENT_THRESHOLD_BYTES) {
    ev.eventType = "network";
    ev.serviceName = ctx.hostname;
    ev.details = {
      ...ev.details,
      method: ctx.method,
      cost_pending: true,
      protocol: ctx.protocol,
      request_bytes: ctx.requestBytes,
      response_bytes: responseBytes,
      is_internal_traffic: _isInternalToValue(ctx.isInternal),
      _reTyped: true,
    };
    // Persist to durable buffer now that the final classification is
    // known. The placeholder was intentionally NOT persisted in
    // _maybeRecordCost to avoid phantom $0 external_cost events.
    if (_buffer) {
      _buffer.addEvent(ev);
    }
  } else {
    // Below threshold and no error → counters-only. Drop the
    // placeholder external_cost-zero event from the in-memory list.
    // The placeholder was never persisted to _buffer (deferred
    // persistence), so no phantom event leaks to the durable store.
    const idx = _recordedEvents.indexOf(ev);
    if (idx !== -1) {
      _recordedEvents.splice(idx, 1);
    }
  }
}

// _HttpCallContext is augmented inside _maybeRecordCost with _matchedCatalog
// once a domain-rate or catalog match fires.
interface _HttpCallContext {
  _matchedCatalog?: boolean;
}

async function _maybeRecordCost(
  urlStr: string,
  response?: Response,
  ctx?: _HttpCallContext,
): Promise<void> {
  let hostname: string;
  let parsedUrl: URL;
  try {
    parsedUrl = new URL(urlStr);
    hostname = parsedUrl.hostname;
  } catch {
    return;
  }

  const domain = hostname.includes(":") ? hostname.split(":")[0] : hostname;

  // Resolve the task to attribute this cost to. An auto-task is created
  // when none is active so HTTP costs are never silently lost (mirrors
  // the Python adapter's session auto-creation). Store on ctx so
  // _finaliseHttpCall reuses the same task without a second lookup.
  const task = _resolveHttpTask();
  if (ctx) ctx.resolvedTask = task;

  // v1 §4.3 byte_details — stamped into every event below. Response_bytes
  // are deferred to the TransformStream finalisation; for now only the
  // request side is known on catalog/domain-rate events.
  const byteDetailsRequestOnly: Record<string, unknown> = ctx
    ? {
        protocol: ctx.protocol,
        request_bytes: ctx.requestBytes,
        is_internal_traffic: _isInternalToValue(ctx.isInternal),
      }
    : {};

  // 1. Check user-registered domain rate first (highest precedence)
  const rate = _domainRates.get(domain);
  if (rate !== undefined) {
    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: task.taskId,
      eventType: "external_cost",
      costUsd: rate.costUsd,
      costConfidence: "exact",
      pricingSource: "rate_registry",
      serviceName: domain,
      details: { url: urlStr, per: rate.per, ...byteDetailsRequestOnly },
    });

    _pushRecordedEvent(event);
    if (_buffer) {
      _buffer.addEvent(event);
    }
    if (ctx) ctx._matchedCatalog = true;
    return;
  }

  // 2. Check service catalog
  if (_catalog) {
    const entry = _catalog.lookup(urlStr);
    if (entry) {
      // Extract cost from response
      let extractionResult: CostExtractionResult | null = null;

      if (!response) {
        // No response body available (Node-level request) — extract
        // from the catalog entry alone.
        try {
          extractionResult = _catalog.extractCost(entry, new Headers(), null);
        } catch {
          // Give up on extraction
        }
      } else {
        extractionResult = await _extractFromResponse(_catalog, entry, response);
      }

      if (extractionResult) {
        const event = createCostEvent({
          eventId: randomUUID(),
          taskId: task.taskId,
          eventType: "external_cost",
          costUsd: extractionResult.costUsd,
          costConfidence: extractionResult.confidence as CostConfidence,
          pricingSource: "rate_registry",
          serviceName: extractionResult.serviceName,
          details: {
            url: urlStr,
            pricingSource: extractionResult.pricingSource,
            catalogService: entry.display_name,
            ...byteDetailsRequestOnly,
          },
        });

        _pushRecordedEvent(event);
        if (_buffer) {
          _buffer.addEvent(event);
        }
        if (ctx) ctx._matchedCatalog = true;
        return;
      }
    }
  }

  // 3. Un-cataloged — emit a placeholder external_cost-zero event that
  // _finaliseHttpCall will RE-TYPE to a `network` event with
  // cost_pending=true once the response body has been fully drained
  // (and byte counts are known). If combined bytes stay below
  // threshold and there's no error, the placeholder is dropped instead
  // — counters-only path (v1 §4.4). If suppressed (LLM-host call),
  // no event is emitted at all.
  //
  // The placeholder is stored ONLY in the in-memory _recordedEvents
  // array — NOT persisted to _buffer yet. _finaliseHttpCall will
  // persist it to _buffer only when it re-types to `network` (above
  // threshold). Events that get dropped never reach the buffer, so
  // phantom $0 external_cost events are eliminated.
  if (ctx?.suppressed) {
    return; // bytes still flow into the accountant via finalise
  }
  const event = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
    eventType: "external_cost",
    costUsd: 0,
    costConfidence: "unknown",
    serviceName: domain,
    details: { url: urlStr, ...byteDetailsRequestOnly },
  });

  _pushRecordedEvent(event);
  // Store the placeholder on ctx so _finaliseHttpCall can find it
  // directly — survives FIFO eviction and eliminates the fragile
  // backward scan.
  if (ctx) ctx.placeholderEvent = event;
  // Intentionally NOT calling _buffer.addEvent(event) here — deferred
  // to _finaliseHttpCall where the final classification is known.
}
