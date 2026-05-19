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
import { getCurrentTask } from "../core/context.js";
import {
  createCostEvent,
  type CostEvent,
  type CostConfidence,
  type Task,
} from "../core/models.js";
import { createAutoTask } from "../core/auto-task.js";
import { ServiceCatalog, type CostExtractionResult } from "../pricing/service-catalog.js";
import { SessionManager } from "../core/session.js";
import type { EventBuffer } from "../transport/buffer.js";

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

/** Map of domain → { costUsd, per } registered rates (user overrides). */
const _domainRates = new Map<string, { costUsd: number; per: string }>();

/** Events recorded by the adapter. */
const _recordedEvents: CostEvent[] = [];

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
export function trackHttp(buffer?: EventBuffer): void {
  if (_patched) return;

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
    init?: RequestInit
  ): Promise<Response> {
    // Call original first, preserve response
    const response = await _originalFetch!(input, init);

    // Determine URL string from either string, URL, or Request
    let urlStr: string;
    if (typeof input === "string") {
      urlStr = input;
    } else if (input instanceof URL) {
      urlStr = input.toString();
    } else {
      // Request object
      urlStr = (input as Request).url;
    }

    await _maybeRecordCost(urlStr, response);

    return response;
  };

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
        const urlStr = _urlFromRequestArgs(isHttps, args);
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

  try {
    nodeHttp.request = makeWrapper(_originalHttpRequest, false);
    nodeHttps.request = makeWrapper(_originalHttpsRequest, true);
    nodeHttp.get = makeWrapper(_originalHttpGet, false);
    nodeHttps.get = makeWrapper(_originalHttpsGet, true);
  } catch {
    // Some environments freeze these — best-effort, fetch patching still works.
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
async function _maybeRecordCost(
  urlStr: string,
  response?: Response,
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
  // the Python adapter's session auto-creation).
  const task = _resolveHttpTask();

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
      details: { url: urlStr, per: rate.per },
    });

    _recordedEvents.push(event);
    if (_buffer) {
      _buffer.addEvent(event);
    }
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
          },
        });

        _recordedEvents.push(event);
        if (_buffer) {
          _buffer.addEvent(event);
        }
        return;
      }
    }
  }

  // 3. Unknown domain — record with confidence="unknown", cost=0
  const event = createCostEvent({
    eventId: randomUUID(),
    taskId: task.taskId,
    eventType: "external_cost",
    costUsd: 0,
    costConfidence: "unknown",
    serviceName: domain,
    details: { url: urlStr },
  });

  _recordedEvents.push(event);
  if (_buffer) {
    _buffer.addEvent(event);
  }
}
