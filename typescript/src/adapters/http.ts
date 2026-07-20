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
import { Buffer } from "node:buffer";
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
import { createAutoTask, finalizeAutoTask } from "../core/auto-task.js";
import { debugLog } from "../core/debug.js";
import { registerLlmCapture } from "../core/llm-dedup.js";
import { ServiceCatalog, type CostExtractionResult } from "../pricing/service-catalog.js";
import { serviceUsageObservers } from "../pricing/service-usage-observers.js";
import {
  SessionManager,
  setAmbientSessions,
  clearAmbientSessions,
} from "../core/session.js";
import { scrubUrl } from "../security/redaction.js";
import type { EventBuffer } from "../transport/buffer.js";
import type { PricingEngine, CostResult } from "../pricing/engine.js";

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

/** Map of domain → { costUsd, per } registered rates (user overrides). */
const _domainRates = new Map<string, { costUsd: number; per: string }>();

/**
 * Hostnames whose traffic is the SDK's OWN plumbing (event pusher, pricing
 * refresh, service-catalog refresh) and must be completely invisible to
 * capture. Without this, every telemetry push through the patched fetch
 * resolved an ambient session task, which was persisted, pushed on the
 * next cycle — ANOTHER fetch — and so on: a self-generating drip of empty
 * agent_session tasks (plus egress cost for dexcost pushing dexcost) that
 * never stopped, even on an idle app.
 */
const _DEFAULT_INTERNAL_HOSTS = ["api.dexcost.io"];
const _internalHosts = new Set<string>(_DEFAULT_INTERNAL_HOSTS);

/**
 * Mark a hostname as SDK-internal — its traffic bypasses capture entirely
 * (no events, no session, no byte accounting). The tracker registers the
 * resolved Control Layer endpoint (and any serviceCatalogUrl host)
 * automatically; call this yourself only for additional self-hosted
 * dexcost infrastructure.
 */
export function registerInternalHost(hostname: string): void {
  if (typeof hostname === "string" && hostname) {
    _internalHosts.add(hostname.toLowerCase());
  }
}

/** Test-only: restore the internal-host set to its default. */
export function _resetInternalHostsForTests(): void {
  _internalHosts.clear();
  for (const host of _DEFAULT_INTERNAL_HOSTS) _internalHosts.add(host);
}

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

/** Pricing engine for LLM-aware HTTP fallback. */
let _pricing: PricingEngine | null = null;

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
// LLM HTTP fallback — detect LLM API calls at the fetch level
// ---------------------------------------------------------------------------
//
// When LLM instruments cannot intercept a call (ESM consumers, Vercel AI SDK
// providers that use raw fetch instead of official SDK packages), the HTTP
// adapter detects known LLM API endpoints, parses the response for token
// usage, and emits proper `llm_call` events with token-based pricing.

/** Response formats the LLM fallback can parse. */
type LlmFormat = "openai" | "anthropic" | "gemini";

/** Known LLM API domains and their response format. */
const _LLM_DOMAINS: Record<string, LlmFormat> = {
  "api.openai.com": "openai",
  "api.anthropic.com": "anthropic",
  "api.kimi.com": "anthropic",
  "api.moonshot.ai": "openai",
  "api.moonshot.cn": "openai",
  "api.deepseek.com": "openai",
  "api.groq.com": "openai",
  "api.together.xyz": "openai",
  "api.fireworks.ai": "openai",
  "api.mistral.ai": "openai",
  "api.x.ai": "openai",
  "generativelanguage.googleapis.com": "gemini",
  "api.cohere.com": "openai",
  "api.perplexity.ai": "openai",
  "api.sambanova.ai": "openai",
  "openrouter.ai": "openai",
  "api.cerebras.ai": "openai",
};

/** Chat/completions endpoint prefixes that indicate an LLM inference call. */
const _LLM_ENDPOINTS = [
  "/v1/chat/completions",
  "/v1/messages",
  "/chat/completions",
  "/v1/complete",
  "/v1/completions",
  "/coding",        // Kimi Code Plan
  "/v1beta/models", // Google Gemini generateContent
];

/**
 * Derive the LLM response format from the URL path shape alone, matching the
 * canonical route ANYWHERE in the pathname — not just as a prefix.
 *
 * "OpenAI-compatible" and "Anthropic-compatible" vendors, gateways, and
 * proxies mount the canonical routes under arbitrary base-path prefixes:
 * Kimi/Moonshot serve the Anthropic Messages API at
 * `https://api.kimi.com/anthropic` (request path `/anthropic/v1/messages` from
 * the official SDK, `/anthropic/messages` from `@ai-sdk/anthropic`), DeepSeek
 * at `/anthropic/v1/messages`, OpenRouter at `/api/v1/chat/completions`,
 * LiteLLM-style gateways under `/<deployment>/v1/...`. A prefix match on
 * "/v1/messages" misses all of them, and the call then degrades to a generic
 * `network` event (or is dropped below the byte threshold) — losing the
 * llm_call entirely.
 */
function _formatFromPath(pathname: string): LlmFormat | null {
  if (/\/messages(\/|$)/.test(pathname)) return "anthropic";
  if (/\/chat\/completions(\/|$)/.test(pathname)) return "openai";
  if (/\/completions(\/|$)/.test(pathname)) return "openai";
  // Google Gemini / Vertex AI REST shape (models/<id>:generateContent).
  // Matching by path shape also covers Vertex regional hosts
  // (<region>-aiplatform.googleapis.com) without a domain-map entry.
  if (pathname.includes(":generateContent") || pathname.includes(":streamGenerateContent")) {
    return "gemini";
  }
  return null;
}

/**
 * Check if a parsed URL is an LLM chat/completions endpoint.
 * Returns the response format if matched, null otherwise.
 *
 * Detection is two-tier:
 * 1. Known LLM hosts (`_LLM_DOMAINS`): match the canonical path shape
 *    anywhere in the pathname, or the legacy endpoint-prefix list. The path
 *    shape decides the format when unambiguous (a host can serve BOTH an
 *    OpenAI-compatible `/chat/completions` and an Anthropic-compatible
 *    `/anthropic/v1/messages`), falling back to the domain default.
 * 2. Unknown hosts (BYOK "…-compatible" vendors, gateways, self-hosted
 *    proxies): the canonical path shape alone is enough to *attempt* LLM
 *    extraction. Usage extraction is the correctness gate — the call only
 *    becomes an `llm_call` if the response actually carries token usage in
 *    the detected format; otherwise it falls through to normal network
 *    accounting.
 *
 * LLM inference calls are always POST; other methods never match (avoids
 * false positives on e.g. `GET /api/messages` chat-history routes).
 */
function _detectLlmEndpoint(parsedUrl: URL, method?: string): LlmFormat | null {
  if (method !== undefined && method.toUpperCase() !== "POST") return null;
  const pathname = parsedUrl.pathname;
  const pathFormat = _formatFromPath(pathname);
  const domainFormat = _LLM_DOMAINS[parsedUrl.hostname];

  if (domainFormat) {
    if (pathFormat) return pathFormat;
    for (const ep of _LLM_ENDPOINTS) {
      if (pathname.startsWith(ep)) return domainFormat;
    }
    return null;
  }

  return pathFormat;
}

/**
 * Extract model name and token usage from a parsed LLM response body.
 */
function _extractLlmUsage(
  body: unknown,
  format: LlmFormat,
  modelHint?: string,
): { model: string; inputTokens: number; outputTokens: number } | null {
  if (!body || typeof body !== "object") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const b = body as Record<string, any>;

  if (format === "gemini") {
    // Gemini / Vertex responses carry usage in `usageMetadata`, not `usage`,
    // and the model in `modelVersion` (else derivable from the request path:
    // .../models/<id>:generateContent — passed in as modelHint).
    const meta = b.usageMetadata;
    if (!meta || typeof meta !== "object") return null;
    const inTok = meta.promptTokenCount;
    const outTok = meta.candidatesTokenCount;
    if (typeof inTok !== "number" && typeof outTok !== "number") return null;
    // Thinking tokens are billed as output but reported separately.
    const thoughts = typeof meta.thoughtsTokenCount === "number" ? meta.thoughtsTokenCount : 0;
    return {
      model:
        typeof b.modelVersion === "string" && b.modelVersion
          ? b.modelVersion
          : modelHint ?? "unknown",
      inputTokens: typeof inTok === "number" ? inTok : 0,
      outputTokens: (typeof outTok === "number" ? outTok : 0) + thoughts,
    };
  }

  const model: string = typeof b.model === "string" ? b.model : modelHint ?? "unknown";
  const usage = b.usage;
  if (!usage || typeof usage !== "object") return null;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const u = usage as Record<string, any>;
  const inKey = format === "openai" ? "prompt_tokens" : "input_tokens";
  const outKey = format === "openai" ? "completion_tokens" : "output_tokens";

  // Require at least one numeric token field in the expected format. Path-
  // shape detection now matches unknown hosts too, so a response that merely
  // happens to carry a differently-shaped `usage` object must not produce a
  // phantom $0 llm_call.
  if (typeof u[inKey] !== "number" && typeof u[outKey] !== "number") return null;

  return {
    model,
    inputTokens: typeof u[inKey] === "number" ? u[inKey] : 0,
    outputTokens: typeof u[outKey] === "number" ? u[outKey] : 0,
  };
}

/**
 * Try to extract LLM usage from a cloned Response.
 * Returns null if the response is not JSON or does not contain usage data.
 */
async function _tryExtractLlmFromResponse(
  response: Response,
  format: LlmFormat,
  modelHint?: string,
): Promise<{ model: string; inputTokens: number; outputTokens: number } | null> {
  try {
    const cloned = response.clone();
    const contentType = cloned.headers.get("content-type") ?? "";
    // Only parse JSON responses (non-streaming)
    if (!contentType.includes("application/json")) return null;
    const contentLength = cloned.headers.get("content-length");
    const cl = Number.parseInt(contentLength ?? "", 10);
    if (contentLength !== null && !Number.isNaN(cl) && cl > MAX_BODY_SIZE) return null;
    const body = await cloned.json();
    return _extractLlmUsage(body, format, modelHint);
  } catch {
    return null;
  }
}

/**
 * Derive a model hint from a Gemini/Vertex request path
 * (.../models/<id>:generateContent). Returns undefined for other shapes.
 */
function _modelHintFromPath(pathname: string): string | undefined {
  const match = /\/models\/([^/:]+):(?:stream)?[gG]enerateContent/.exec(pathname);
  return match?.[1];
}

/**
 * Parse accumulated SSE tail data for LLM token usage.
 * Works for both OpenAI-compatible and Anthropic-compatible SSE formats.
 */
function _parseSseUsage(
  sseData: string,
  format: LlmFormat,
): { model: string; inputTokens: number; outputTokens: number } | null {
  // Split into SSE events (separated by double newlines)
  const events = sseData.split(/\n\n+/).filter(Boolean);
  let model = "unknown";
  let inputTokens = 0;
  let outputTokens = 0;
  let found = false;

  for (const event of events) {
    // Extract the data line(s)
    const dataLines = event.split("\n")
      .filter((l: string) => l.startsWith("data:") || l.startsWith("data: "))
      .map((l: string) => l.replace(/^data:\s*/, ""));
    for (const dataStr of dataLines) {
      if (dataStr === "[DONE]") continue;
      try {
        const data = JSON.parse(dataStr);
        if (typeof data.model === "string" && data.model !== "unknown") {
          model = data.model;
        }
        if (format === "gemini") {
          if (typeof data.modelVersion === "string" && data.modelVersion) {
            model = data.modelVersion;
          }
          const meta = data.usageMetadata;
          if (meta && typeof meta === "object") {
            // Every streamed chunk may carry usageMetadata; the last one is
            // authoritative (cumulative counts).
            if (typeof meta.promptTokenCount === "number") inputTokens = meta.promptTokenCount;
            const outTok = typeof meta.candidatesTokenCount === "number" ? meta.candidatesTokenCount : 0;
            const thoughts = typeof meta.thoughtsTokenCount === "number" ? meta.thoughtsTokenCount : 0;
            if (outTok || thoughts) outputTokens = outTok + thoughts;
            if (typeof meta.promptTokenCount === "number" || outTok) found = true;
          }
        }
        if (format === "openai" && data.usage) {
          inputTokens = data.usage.prompt_tokens ?? inputTokens;
          outputTokens = data.usage.completion_tokens ?? outputTokens;
          found = true;
        }
        if (format === "anthropic") {
          if (data.type === "message_start" && data.message?.usage) {
            inputTokens = data.message.usage.input_tokens ?? 0;
            model = data.message?.model ?? model;
            found = true;
          }
          if (data.type === "message_delta" && data.usage) {
            outputTokens = data.usage.output_tokens ?? 0;
            found = true;
          }
        }
      } catch { /* not valid JSON */ }
    }
  }

  return found ? { model, inputTokens, outputTokens } : null;
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


/**
 * Build an instrumented fetch over an arbitrary base fetch. Used for BOTH
 * the global patch (trackHttp) and injectable per-client fetches
 * (createDexcostFetch). The returned function carries the DEXCOST_PATCHED
 * marker so layered wrapping is detected and refused.
 */
function _buildInstrumentedFetch(
  base: typeof globalThis.fetch,
): typeof globalThis.fetch {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const wrapped = async function wrappedFetch(
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> {
    // ── SDK self-traffic bypass ──────────────────────────────────────────
    // The SDK's own HTTP (telemetry push, pricing/catalog refresh) must
    // never be captured: tracking it feeds the buffer, whose push is
    // itself a fetch — an endless loop of empty session tasks. Checked
    // FIRST so internal calls pay zero capture overhead.
    try {
      const host = new URL(_resolveUrlStr(input)).hostname.toLowerCase();
      if (_internalHosts.has(host)) {
        return base(input, init);
      }
    } catch {
      // Unparseable URL — fall through to normal capture handling.
    }

    // ── v1 byte measurement — request side (known before fetch) ─────────
    // Scrub once at extraction so every downstream use (events, hostname
    // parse, placeholder equality-lookup at the streamed-body re-type
    // site) sees the same safe value. Critical for B11: re-type at
    // _finaliseHttpCall must match on the same string that was stored.
    const urlStr = scrubUrl(_resolveUrlStr(input));
    const method = _resolveMethod(input, init);
    const requestHeaders = _resolveRequestHeaders(input, init);
    const requestBodyLen = _resolveRequestBodyLen(input, init);
    const observerRequestBodyPromise = serviceUsageObservers?.needsRequestBody(urlStr) === true
      ? _resolveObserverRequestBody(input, init)
      : Promise.resolve(undefined);
    const requestBytes = measureBytesFromHeaders(
      method,
      urlStr,
      requestHeaders,
      requestBodyLen,
    );

    const response = await base(input, init);
    const observerRequestBody = await observerRequestBodyPromise;

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
    // Detect LLM streaming responses for deferred cost extraction.
    let llmStreamFormat: LlmFormat | undefined;
    if (!suppressed && _pricing) {
      try {
        const parsedForLlm = new URL(urlStr);
        const detectedFormat = _detectLlmEndpoint(parsedForLlm, method);
        if (detectedFormat) {
          const ct = response.headers.get("content-type") ?? "";
          if (ct.includes("text/event-stream") || ct.includes("text/plain")) {
            llmStreamFormat = detectedFormat;
          }
        }
      } catch { /* ignore */ }
    }

    const callContext: _HttpCallContext = {
      urlStr,
      method,
      hostname,
      protocol,
      requestBytes,
      isInternal,
      suppressed,
      responseHeaderBytes: _measureResponseHeaderBytes(response),
      llmStreamFormat,
      observerRequestBody,
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
  // §4.2.1: tag the wrapper so a second trackHttp() (or a duplicate SDK
  // install across realms) doesn't double-wrap.
  Object.defineProperty(wrapped, DEXCOST_PATCHED, {
    value: true,
    enumerable: false,
    configurable: false,
    writable: false,
  });
  return wrapped as typeof globalThis.fetch;
}

/**
 * Create an instrumented fetch for explicit injection into HTTP/LLM
 * clients — the bundler-proof, global-state-free capture point:
 *
 *   import { createDexcostFetch } from "@dexcost/sdk";
 *   const anthropic = createAnthropic({ fetch: createDexcostFetch() });
 *   const openai = new OpenAI({ fetch: createDexcostFetch() });
 *
 * Prefer this over the global patch when you disable `trackHttp` at init
 * (shared processes, other fetch-wrapping tools) or when a provider takes
 * a `fetch` option anyway. Calls captured here go through the exact same
 * classification pipeline as the global patch (LLM fallback incl.
 * anthropic/openai/gemini formats + SSE, service catalog, byte counting).
 *
 * Returns the base fetch UNWRAPPED (and warns) when dexcost has no
 * buffer/pricing wired yet — i.e. init() has not run and no `tracker` was
 * passed — so client construction never breaks.
 */
export function createDexcostFetch(
  options: {
    /** Explicit tracker (required only when init() ran with trackHttp:false
     *  and this is called before any other wiring). */
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    tracker?: { buffer: EventBuffer; pricing: PricingEngine };
    /** Base fetch to instrument. Defaults to globalThis.fetch. */
    fetch?: typeof globalThis.fetch;
  } = {},
): typeof globalThis.fetch {
  const base = options.fetch ?? globalThis.fetch;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if ((base as any)?.[DEXCOST_PATCHED] === true) {
    // Already instrumented (global patch active) — wrapping again would
    // double-count every call.
    return base;
  }
  if (options.tracker) {
    if (!_buffer) _buffer = options.tracker.buffer;
    if (!_pricing) _pricing = options.tracker.pricing;
  }
  if (_catalog === null) {
    try {
      _catalog = new ServiceCatalog();
    } catch {
      _catalog = null;
    }
  }
  if (!_buffer || !_pricing) {
    // eslint-disable-next-line no-console
    console.warn(
      "[dexcost] createDexcostFetch: dexcost has no buffer/pricing wired " +
        "(call init() first, or pass { tracker }) — returning the base " +
        "fetch untracked.",
    );
    return base;
  }
  return _buildInstrumentedFetch(base);
}

export function trackHttp(buffer?: EventBuffer, pricing?: PricingEngine): void {
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
    _pricing = pricing ?? null;
    // Publish the session manager so LLM instruments join the same
    // ambient sessions instead of creating per-call auto-tasks.
    setAmbientSessions(_sessionManager, buffer);
  }

  // Replace globalThis.fetch with the instrumented wrapper.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (globalThis as any).fetch = _buildInstrumentedFetch(_originalFetch);

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
        // SDK self-traffic bypass — mirror of the fetch wrapper's check.
        let internal = false;
        if (urlStr) {
          try {
            internal = _internalHosts.has(new URL(urlStr).hostname.toLowerCase());
          } catch {
            internal = false;
          }
        }
        if (urlStr && !internal) {
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
  clearAmbientSessions();
  _buffer = null;
  _pricing = null;
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
function _resolveHttpTask(): { task: Task; autoCreated: boolean } {
  const current = getCurrentTask();
  if (current !== undefined) {
    return { task: current, autoCreated: false };
  }

  if (_sessionManager && _buffer) {
    const sessionTask = _sessionManager.runInSession("http", _buffer, () =>
      getCurrentTask(),
    );
    if (sessionTask !== undefined) {
      // Session tasks are owned + finalized by the SessionManager (idle
      // sweep / shutdown), not by the adapter.
      return { task: sessionTask, autoCreated: false };
    }
  }

  // No task and no session — create an auto-task (mirrors Python). The
  // adapter OWNS this task's lifecycle: pre-fix it was never finalized and
  // stayed "pending" forever (and, since createAutoTask now registers a
  // NetworkAccountant, would also leak the registry entry). Callers
  // finalize it via finalizeAutoTask once the call completes.
  const autoTask = createAutoTask("http_call");
  if (_buffer) {
    _buffer.upsertTask(autoTask);
  }
  return { task: autoTask, autoCreated: true };
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
  /** LLM response format detected for this call (for SSE stream parsing). */
  llmStreamFormat?: LlmFormat;
  /** Bounded JSON request metadata used only for observer billing identity. */
  observerRequestBody?: unknown;
  /** Response BODY bytes, known once the counting stream has drained.
   *  Stamped by _finaliseHttpCall so late event emission (e.g. the JSON
   *  llm_call path, whose extraction drains the body via clone()) can
   *  attach complete byte details instead of request-side only. */
  responseBodyBytes?: number;
  /** Accumulated tail of SSE data for LLM usage extraction on stream end. */
  sseTailBuffer?: string;
  /** Head of SSE stream preserved so Anthropic message_start is not lost. */
  sseHeadBuffer?: string;
  /** Shared TextDecoder for SSE chunk decoding (avoids per-chunk allocation). */
  _sseDecoder?: InstanceType<typeof TextDecoder>;
  /** Task resolved once in _maybeRecordCost, reused in _finaliseHttpCall.
   *  Avoids a second getCurrentTask() lookup which would either create a
   *  duplicate auto-task or (before the auto-task leak fix) find a stale one. */
  resolvedTask?: Task;
  /** True when resolvedTask is an adapter-created auto-task whose lifecycle
   *  the adapter owns — _finaliseHttpCall finalizes it (status + network
   *  drain) once the response body has drained. */
  resolvedTaskAutoCreated?: boolean;
  /** Placeholder event stored here so _finaliseHttpCall can find it without
   *  a fragile O(n) backward scan of _recordedEvents that is vulnerable to
   *  FIFO eviction under high concurrency. */
  placeholderEvent?: CostEvent;
  /**
   * Two-signal gate for the network outcome (placeholder retype/drop +
   * adapter auto-task finalize). The two producers can complete in EITHER
   * order: _maybeRecordCost's own extraction (clone().json()) drains the
   * wrapped body, firing _finaliseHttpCall BEFORE the placeholder is
   * created; conversely for streamed bodies the caller drains long after
   * classification finished. The outcome runs exactly once, when the
   * second signal arrives.
   */
  bodyDrained?: boolean;
  classificationDone?: boolean;
  outcomeEmitted?: boolean;
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

const MAX_OBSERVER_REQUEST_BODY_BYTES = 1_048_576;
const OBSERVER_REQUEST_BODY_TIMEOUT_MS = 50;

async function _resolveObserverRequestBody(
  input: string | URL | Request,
  init?: RequestInit,
): Promise<unknown> {
  try {
    const body = init?.body;
    if (typeof body === "string") {
      if (Buffer.byteLength(body, "utf-8") > MAX_OBSERVER_REQUEST_BODY_BYTES) return undefined;
      return JSON.parse(body);
    }
    if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
      const bytes = body instanceof ArrayBuffer
        ? new Uint8Array(body)
        : new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
      if (bytes.byteLength > MAX_OBSERVER_REQUEST_BODY_BYTES) return undefined;
      return JSON.parse(new TextDecoder().decode(bytes));
    }

    if (!(input instanceof Request) || input.bodyUsed || input.body === null) return undefined;
    const contentType = input.headers.get("content-type")?.toLowerCase() ?? "";
    if (!contentType.includes("application/json") && !contentType.startsWith("text/plain")) {
      return undefined;
    }
    const declaredLength = Number(input.headers.get("content-length"));
    if (Number.isFinite(declaredLength) && declaredLength > MAX_OBSERVER_REQUEST_BODY_BYTES) {
      return undefined;
    }

    const reader = input.clone().body?.getReader();
    if (reader === undefined) return undefined;
    const decoder = new TextDecoder();
    let byteLength = 0;
    let text = "";
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const timedOut = new Promise<undefined>((resolve) => {
      timeout = setTimeout(() => {
        void reader.cancel().catch(() => undefined);
        resolve(undefined);
      }, OBSERVER_REQUEST_BODY_TIMEOUT_MS);
    });
    const parsed = (async (): Promise<unknown> => {
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          byteLength += value.byteLength;
          if (byteLength > MAX_OBSERVER_REQUEST_BODY_BYTES) {
            await reader.cancel();
            return undefined;
          }
          text += decoder.decode(value, { stream: true });
        }
        text += decoder.decode();
        return JSON.parse(text);
      } catch {
        return undefined;
      }
    })();
    const resolved = await Promise.race([parsed, timedOut]);
    if (timeout !== undefined) clearTimeout(timeout);
    return resolved;
  } catch {
    // Request metadata is fail-open; provider usage can still be observed.
  }
  return undefined;
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
      // Buffer tail (and head) of SSE data for LLM usage extraction.
      // Reuse a single TextDecoder so multi-byte chars split across
      // chunks are decoded correctly.
      if (ctx.llmStreamFormat) {
        try {
          if (!ctx._sseDecoder) ctx._sseDecoder = new TextDecoder();
          const text = ctx._sseDecoder.decode(chunk, { stream: true });
          ctx.sseTailBuffer = ((ctx.sseTailBuffer ?? "") + text).slice(-8192);
          if ((ctx.sseHeadBuffer?.length ?? 0) < 4096) {
            ctx.sseHeadBuffer = ((ctx.sseHeadBuffer ?? "") + text).slice(0, 4096);
          }
        } catch { /* ignore decode errors */ }
      }
    },
    flush() {
      // Flush remaining bytes from the streaming TextDecoder
      if (ctx.llmStreamFormat && ctx._sseDecoder) {
        try {
          const text = ctx._sseDecoder.decode();
          if (text) {
            ctx.sseTailBuffer = ((ctx.sseTailBuffer ?? "") + text).slice(-8192);
          }
        } catch { /* ignore decode errors */ }
      }
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
  ctx.responseBodyBytes = responseBodyBytes;
  const responseBytes = ctx.responseHeaderBytes + responseBodyBytes;
  // Reuse the task resolved in _maybeRecordCost — avoids a second
  // getCurrentTask() lookup which would create a duplicate auto-task
  // now that createAutoTask no longer pollutes AsyncLocalStorage.
  let task = ctx.resolvedTask;
  let taskAutoCreated = ctx.resolvedTaskAutoCreated === true;
  if (!task) {
    // Zero-body responses finalise BEFORE _maybeRecordCost runs — resolve
    // here and stamp the ctx so _maybeRecordCost reuses the same task
    // instead of resolving a second one.
    const resolved = _resolveHttpTask();
    task = resolved.task;
    taskAutoCreated = resolved.autoCreated;
    ctx.resolvedTask = task;
    ctx.resolvedTaskAutoCreated = taskAutoCreated;
  }
  const accountant: NetworkAccountant | undefined = getAccountant(task.taskId);
  if (accountant) {
    accountant.record(ctx.hostname, responseBytes, ctx.requestBytes, ctx.isInternal);
  }

  // LLM streaming fallback — extract usage from accumulated SSE data.
  if (ctx.llmStreamFormat && ctx.sseTailBuffer && _pricing && !ctx.suppressed) {
    // Merge head + tail buffers so Anthropic message_start (model + input
    // tokens) survives even when the stream exceeds the 8k tail window.
    // Only merge when the tail does NOT already start with the head — for
    // short streams the 8k tail already contains the 4k head, and blind
    // concatenation would duplicate the overlapping prefix.
    const sseData =
      ctx.sseHeadBuffer && ctx.sseTailBuffer && !ctx.sseTailBuffer.startsWith(ctx.sseHeadBuffer)
        ? ctx.sseHeadBuffer + ctx.sseTailBuffer
        : (ctx.sseTailBuffer ?? ctx.sseHeadBuffer ?? "");
    const llmUsage = _parseSseUsage(sseData, ctx.llmStreamFormat);
    if (llmUsage) {
      const costResult: CostResult = _pricing.getCost(
        llmUsage.model,
        llmUsage.inputTokens,
        llmUsage.outputTokens,
      );
      const event = createCostEvent({
        eventId: randomUUID(),
        taskId: task.taskId,
        eventType: "llm_call",
        costUsd: costResult.costUsd,
        costConfidence: costResult.costConfidence,
        pricingSource: costResult.pricingSource,
        provider: ctx.hostname,
        model: llmUsage.model,
        inputTokens: llmUsage.inputTokens,
        outputTokens: llmUsage.outputTokens,
        details: {
          url: ctx.urlStr,
          source: "http_llm_fallback_stream",
          request_bytes: ctx.requestBytes,
          response_bytes: responseBytes,
        },
      });
      _pushRecordedEvent(event);
      if (_buffer) {
        _buffer.addEvent(event);
      }
      debugLog(
        "http",
        `llm_call captured via http fallback (sse): ${ctx.hostname} model=${llmUsage.model} ` +
          `in=${llmUsage.inputTokens} out=${llmUsage.outputTokens}`,
      );
      registerLlmCapture(task.taskId, llmUsage.inputTokens, llmUsage.outputTokens);
      task.llmCostUsd = task.llmCostUsd.plus(costResult.costUsd);
      task.totalCostUsd = task.totalCostUsd.plus(costResult.costUsd);
      task.totalInputTokens += llmUsage.inputTokens;
      task.totalOutputTokens += llmUsage.outputTokens;
      if (_buffer) {
        _buffer.upsertTask(task);
      }
      ctx._matchedCatalog = true; // prevent network event emission
    }
  }

  ctx.bodyDrained = true;
  _maybeEmitNetworkOutcome(ctx);
}

/**
 * Emit the network outcome for a call: retype-or-drop the placeholder
 * event and finalize the adapter-owned auto-task.
 *
 * Runs exactly once, and only after BOTH the response body has drained
 * (byte totals known) AND _maybeRecordCost's classification has completed
 * (placeholder created / matched flag set). The two happen in either
 * order: extraction's clone().json() drains the body mid-classification
 * for usage-less JSON responses, while streamed bodies drain long after
 * classification. Gating on both closes the race where a too-early
 * finalisation found no placeholder — large usage-less calls silently
 * lost their network event and small ones leaked a phantom $0
 * external_cost entry in the in-memory list.
 */
function _maybeEmitNetworkOutcome(ctx: _HttpCallContext): void {
  if (!ctx.bodyDrained || !ctx.classificationDone || ctx.outcomeEmitted) return;
  ctx.outcomeEmitted = true;

  const task = ctx.resolvedTask;
  const taskAutoCreated = ctx.resolvedTaskAutoCreated === true;

  try {
    // Network-event emission is the un-cataloged path's responsibility.
    // _maybeRecordCost decides which path was taken — for catalog /
    // domain-rate calls it sets a "matched" flag we check here. Stored on
    // the ctx so a single-call closure threads it.
    if (ctx._matchedCatalog || ctx.suppressed) {
      if (ctx.suppressed) {
        debugLog("http", `network event suppressed for ${ctx.hostname} (owned by an instrument)`);
      }
      return;
    }

    const responseBytes = ctx.responseHeaderBytes + (ctx.responseBodyBytes ?? 0);
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
      debugLog(
        "http",
        `network event emitted for ${ctx.hostname} (${combined} bytes > threshold, cost_pending)`,
      );
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
  } finally {
    if (task) {
      _finaliseAdapterAutoTask(task, taskAutoCreated);
    }
  }
}

/**
 * Finalize an adapter-owned auto-task once its (single) HTTP call has
 * fully drained. Session and explicit tasks are owned elsewhere
 * (SessionManager sweep / TrackedTask.end) and pass autoCreated=false.
 * Pre-fix these tasks were never finalized and stayed "pending" forever.
 */
function _finaliseAdapterAutoTask(task: Task, autoCreated: boolean): void {
  if (!autoCreated) return;
  try {
    finalizeAutoTask(task, "success", _buffer ?? undefined);
  } catch {
    // never crash user code
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
    // Unparseable URL — possible when a custom or mocked fetch accepts
    // inputs that `new URL()` rejects (relative URLs in browser-ish
    // runtimes, test stubs). Classification is trivially "done" (nothing
    // to classify), and the outcome gate must still be released: without
    // this, ctx.classificationDone stayed unset, _maybeEmitNetworkOutcome
    // never fired after the body drained, and an adapter-created
    // http_call auto-task stayed "pending" forever with its
    // NetworkAccountant registry entry leaked.
    if (ctx) {
      ctx.classificationDone = true;
      _maybeEmitNetworkOutcome(ctx);
    }
    return;
  }

  const domain = hostname.includes(":") ? hostname.split(":")[0] : hostname;

  // Resolve the task to attribute this cost to. An auto-task is created
  // when none is active so HTTP costs are never silently lost (mirrors
  // the Python adapter's session auto-creation). Store on ctx so
  // _finaliseHttpCall reuses the same task without a second lookup.
  // Reuse a task already resolved by _finaliseHttpCall (zero-body
  // responses finalise first); otherwise resolve and stamp the ctx.
  const resolved: { task: Task; autoCreated: boolean } = ctx?.resolvedTask
    ? { task: ctx.resolvedTask, autoCreated: ctx.resolvedTaskAutoCreated === true }
    : _resolveHttpTask();
  const task = resolved.task;
  if (ctx) {
    ctx.resolvedTask = task;
    ctx.resolvedTaskAutoCreated = resolved.autoCreated;
  }

  // Node-level http/https calls (no ctx) have no byte-counting stream and
  // therefore no _finaliseHttpCall hook. When the adapter created the
  // auto-task for such a call, finalize it here once recording is done —
  // pre-fix it leaked as "pending" forever.
  try {

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
        costConfidence: "computed",
        pricingSource: "manual",
        serviceName: domain,
        details: {
          url: urlStr,
          attribution_usage_quantity: 1,
          attribution_usage_per: rate.per,
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

    // 1.5. LLM HTTP fallback â when no LLM instrument suppressed the call,
    // detect known LLM API endpoints and emit llm_call events using the
    // pricing engine for proper token-based cost attribution.
    if (!ctx?.suppressed && response && _pricing) {
      const llmFormat = _detectLlmEndpoint(parsedUrl, ctx?.method);
      if (llmFormat) {
        const llmUsage = await _tryExtractLlmFromResponse(
        response,
        llmFormat,
        _modelHintFromPath(parsedUrl.pathname),
      );
        if (llmUsage) {
          const costResult: CostResult = _pricing.getCost(
            llmUsage.model,
            llmUsage.inputTokens,
            llmUsage.outputTokens,
          );
          // The clone().json() above drained the counting stream, so by now
          // _finaliseHttpCall has stamped the response body bytes on ctx.
          // Attach the complete byte picture: this llm_call REPLACES the
          // standalone network event for the call (≤1 event per HTTP call),
          // so it must carry the byte details the network event would have.
          const responseBytesKnown =
            ctx !== undefined && ctx.responseBodyBytes !== undefined
              ? { response_bytes: ctx.responseHeaderBytes + ctx.responseBodyBytes }
              : {};
          const event = createCostEvent({
            eventId: randomUUID(),
            taskId: task.taskId,
            eventType: "llm_call",
            costUsd: costResult.costUsd,
            costConfidence: costResult.costConfidence,
            pricingSource: costResult.pricingSource,
            provider: domain,
            model: llmUsage.model,
            inputTokens: llmUsage.inputTokens,
            outputTokens: llmUsage.outputTokens,
            details: {
              url: urlStr,
              source: "http_llm_fallback",
              ...byteDetailsRequestOnly,
              ...responseBytesKnown,
            },
          });

          _pushRecordedEvent(event);
          if (_buffer) {
            _buffer.addEvent(event);
          }
          debugLog(
            "http",
            `llm_call captured via http fallback (json): ${domain} model=${llmUsage.model} ` +
              `in=${llmUsage.inputTokens} out=${llmUsage.outputTokens}`,
          );
          registerLlmCapture(task.taskId, llmUsage.inputTokens, llmUsage.outputTokens);
          // Update task aggregates
          task.llmCostUsd = task.llmCostUsd.plus(costResult.costUsd);
          task.totalCostUsd = task.totalCostUsd.plus(costResult.costUsd);
          task.totalInputTokens += llmUsage.inputTokens;
          task.totalOutputTokens += llmUsage.outputTokens;
          if (_buffer) {
            _buffer.upsertTask(task);
          }
          if (ctx) ctx._matchedCatalog = true;
          return;
        }
      }
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
          const isUserOverride = extractionResult.pricingSource === "user_override";
          const event = createCostEvent({
            eventId: randomUUID(),
            taskId: task.taskId,
            eventType: "external_cost",
            costUsd: extractionResult.costUsd,
            costConfidence: extractionResult.confidence as CostConfidence,
            pricingSource: isUserOverride ? "manual" : "service_catalog",
            pricingVersion: isUserOverride ? undefined : _catalog.catalogVersion,
            serviceName: extractionResult.serviceName,
            details: {
              url: urlStr,
              pricingSource: extractionResult.pricingSource,
              catalogService: entry.display_name,
              attribution_usage_quantity: extractionResult.usageQuantity,
              attribution_usage_metric: extractionResult.usageMetric,
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

    // 2.5. Usage-only observation for safety-disabled services. These
    // definitions deliberately contain no rates: the SDK reports provider-
    // owned quantities and the control plane decides whether they are priced.
    if (response?.ok && serviceUsageObservers?.matches(urlStr)) {
      try {
        let observerResponseBody: unknown;
        if (serviceUsageObservers?.needsResponseBody(urlStr) === true) {
          try {
            observerResponseBody = await response.clone().json();
          } catch {
            observerResponseBody = undefined;
          }
        }
        const observations = serviceUsageObservers?.observe(
          urlStr,
          response.headers,
          observerResponseBody,
          ctx?.observerRequestBody,
        ) ?? [];
        if (observations.length > 0) {
          for (const observation of observations) {
            const duration = observation.metric === "audio_seconds"
              ? { attribution_usage_duration_seconds: observation.quantity }
              : {};
            const event = createCostEvent({
              eventId: randomUUID(),
              taskId: task.taskId,
              eventType: "external_cost",
              costUsd: 0,
              costConfidence: "unknown",
              pricingSource: "unknown",
              provider: observation.providerName,
              model: observation.resourceType === "model" ? observation.resourceId : undefined,
              serviceName: observation.providerService,
              details: {
                url: urlStr,
                provider_record_id: observation.providerRecordId,
                attribution_component: observation.component,
                attribution_resource_type: observation.resourceType,
                attribution_resource_id: observation.resourceId,
                attribution_usage_quantity: observation.quantity,
                attribution_usage_metric: observation.metric,
                attribution_observer_version: observation.manifestVersion,
                attribution_observer_service: observation.serviceKey,
                ...duration,
                ...byteDetailsRequestOnly,
              },
            });
            _pushRecordedEvent(event);
            if (_buffer) _buffer.addEvent(event);
          }
          if (ctx) ctx._matchedCatalog = true;
          return;
        }
      } catch {
        // Observation is fail-open. Network accounting continues below.
      }
    }

    // 3. Un-cataloged — emit a placeholder external_cost-zero event that
    // _finaliseHttpCall will RE-TYPE to a `network` event with
    // cost_pending=true once the response body has been fully drained
    // (and byte counts are known). If combined bytes stay below
    // threshold and there's no error, the placeholderis silently
    // dropped — no phantom event reaches the durable buffer.
    // node-level http (no ctx) OR a suppressed LLM-host call: no placeholder.
    // Suppressed calls MUST be skipped here — _finaliseHttpCall returns early
    // for suppressed calls (before the drop path), so a placeholder created
    // here would never be dropped and would leak as a phantom $0 event.
    if (!ctx || ctx.suppressed) return;

    const event = createCostEvent({
      eventId: randomUUID(),
      taskId: task.taskId,
      eventType: "external_cost",
      costUsd: 0,
      costConfidence: "unknown",
      pricingSource: "unknown",
      serviceName: domain,
      details: { url: urlStr, ...byteDetailsRequestOnly },
    });

    _pushRecordedEvent(event);
    // NOTE: intentionally NOT persisting to _buffer here. The event is
    // held in-memory only. _finaliseHttpCall promotes it to a `network`
    // event with cost_pending=true (and persists it) IF the combined
    // bytes exceed the threshold. If they don't, the placeholder is
    // silently dropped from _recordedEvents — no phantom $0 event ever
    // reaches the push/ingest pipeline.
    ctx.placeholderEvent = event;
  } finally {
    if (!ctx && resolved.autoCreated) {
      finalizeAutoTask(task, "success", _buffer ?? undefined);
    }
    if (ctx) {
      // Classification is done (matched flag / placeholder are final).
      // If extraction already drained the body, the network outcome runs
      // now; otherwise it runs when the caller drains the stream.
      ctx.classificationDone = true;
      _maybeEmitNetworkOutcome(ctx);
    }
  }
}
