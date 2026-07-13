# TypeScript → Python SDK Parity Audit

**Date:** 2026-07-13
**Axis:** TypeScript SDK (`typescript/`, v0.11.1) → Python SDK (`python/`, v0.2.1)
**Method:** Fresh, code-level read of both trees. Every claim below is
grounded in a specific `file:line`. The recent TS releases (0.10.0 → 0.11.1)
landed a batch of improvements after the previous cross-SDK matrix; this audit
checks each of them against Python from source.

> This audit **does not** rely on the repo-root `PARITY-AUDIT.md`. That file
> is dated 2026-05-18 and several of its Python claims are now **stale** — see
> "Corrections to the previous matrix" below. Where the two disagree, this
> file is the code-verified one.

---

## 1. Corrections to the previous matrix (verified stale)

Read from current source; the older `PARITY-AUDIT.md` is wrong on these:

| Old claim (2026-05-18) | Reality in current code |
|---|---|
| "Python streaming not captured for gemini / bedrock / cohere" | **False.** All three have real streaming wrappers that capture usage on stream completion: `instruments/gemini.py:187-249` (`_SyncStreamWrapper`), `instruments/cohere.py:259-390` (`chat_stream` sync+async), `instruments/bedrock.py:206-341` (`InvokeModelWithResponseStream` EventStream). |
| "Python `RetryHeuristicEngine` unreachable via `init()`" | **False.** `init()` threads `enable_retry_heuristics` / `retry_heuristic_window` / `retry_heuristic_threshold` into `CostTracker` (`__init__.py:277-283`). |
| — | Python **has** a `litellm` instrument (`instruments/litellm.py`, registered `tracker.py:45`) that **TypeScript lacks** (`instruments/index.ts:15` — no litellm). A reverse gap. |

---

## 2. Landed in this pass (implemented + tested)

These three were genuine, portable gaps with low regression risk. All ship
with unit tests; the full Python suite (1397 tests) stays green.

### 2.1 SDK self-traffic exclusion — **correctness fix**
- **TS:** `registerInternalHost` + internal-host bypass, checked before any
  capture (`adapters/http.ts:52-81, 485-492`), wired to the resolved endpoint
  at init (`core/tracker.ts:1049-1060`). Prevents the SDK's own telemetry
  pushes from being recaptured into an endless "empty session task" drip.
- **Python before:** no such guard. Self-capture was avoided only
  *incidentally* because the SDK's own POSTs use stdlib `urllib.request`
  (`sync.py:357`, `pricing.py`, `service_catalog.py`) which is not a patched
  transport. Fragile: any internal call moved to `requests`/`httpx`, or a
  `service_catalog_url` reachable via a patched client, would start a drip.
- **Now:** `register_internal_host()` / `is_internal_host()` +
  `_DEFAULT_INTERNAL_HOSTS = {"api.dexcost.io"}` (`adapters/http.py`), an
  early-return bypass at the top of `_handle_http_call_inner`, and
  registration of the resolved endpoint + `service_catalog_url` hosts in
  `init()` before patching (`__init__.py`). Exported as a public API for
  self-hosted infra. Tests: `tests/test_self_traffic.py` (8).

### 2.2 Debug mode — **DX parity**
- **TS:** `core/debug.ts` — `DEXCOST_DEBUG` env (`1/true/yes/on`) + programmatic
  `init({ debug })`, scoped `debugLog(scope, msg)` to stderr, strict no-op when
  off; exported (`index.ts:181-182`).
- **Python before:** only ad-hoc `logging.debug`; no env toggle, no `init`
  flag, no scoped helper.
- **Now:** `dexcost/debug.py` — `set_debug_mode` / `is_debug_mode` /
  `debug_log(scope, message)` (stderr, `[dexcost:<scope>]` prefix), env var
  `DEXCOST_DEBUG`, and an `init(debug=...)` parameter (override wins over env).
  Exported publicly. Tests: `tests/test_debug_mode.py` (15).

### 2.3 `dexcost doctor` CLI — **DX parity**
- **TS:** `cli/doctor.ts` — a guarded diagnostic command.
- **Python before:** CLI had only `status` / `rates` / `scan` (`cli.py`).
- **Now:** `dexcost/doctor.py` + a `doctor` Click command (`--api-key`,
  `--endpoint`, `--offline`; exit 1 when unhealthy). Portable checks:
  Python version, task-context (contextvars) round-trip, SQLite buffer
  write/read round-trip, per-provider package presence+version for all 7
  instruments, API-key presence+format, endpoint HEAD reachability. JS-only
  checks (AsyncLocalStorage native, better-sqlite3/bun:sqlite bindings,
  `globalThis.fetch` patch) are replaced by their Python analog or dropped as
  N/A. Tests: `tests/test_doctor.py` (8).

---

## 3. Remaining real gaps (grounded, prioritized)

These are genuine but larger / higher-risk or need a design decision, so they
are documented rather than rushed. Ordered by value.

### P1 — LLM-over-raw-HTTP fallback capture *(biggest capture gap)*
- **TS:** when an LLM provider is called over raw HTTP with no SDK wrapper, the
  fetch adapter detects it and records a real `llm_call` with token extraction
  — domain map + path classifier + JSON/SSE usage parse
  (`adapters/http.ts:189-436, 1246-1538`), tests in `tests/llm-http-fallback.test.ts`.
  Covers BYOK gateways, OpenAI-compatible proxies, Kimi/Moonshot base paths,
  Vertex, Gemini REST.
- **Python:** absent. `_handle_uncataloged` (`adapters/http.py:708-749`) only
  emits a generic `network` event; a suppressed call assumes an instrument
  already produced the `llm_call`. Raw-HTTP LLM calls are billed as bytes, not
  tokens.
- **Portable, self-contained.** Recommended as its own reviewed PR (hot path +
  false-positive guards + SSE + dedup interplay warrant focused testing).

### P2 — Shared network-finalize for auto/session tasks *(correctness)*
- **TS:** `core/network-finalize.ts::finalizeTaskNetwork` is a shared egress
  drain run by **every** task kind — `TrackedTask.end`, session finalize
  (`session.ts:173`), and `finalizeAutoTask` (`auto-task.ts:82`).
- **Python:** egress pricing is inlined in `tracker.py::_aggregate_costs`
  (~`tracker.py:1092-1186`) and runs **only** for explicit `track()` tasks.
  `finalize_auto_task` (`auto_task.py:48-74`) and
  `SessionManager.finalize_idle_sessions` (`session.py:100-123`) only set
  status — they never drain/price `task._network`. **Consequence:** auto-tasks
  and idle-finalized session tasks ship `network_cost_usd = 0` even though
  bytes were recorded. Fix = extract the egress block into a shared helper and
  call it from all three finalize paths.

### P3 — OpenTelemetry ingestion bridge
- **TS:** `integrations/otel.ts::DexcostSpanProcessor` ingests GenAI/AI-SDK LLM
  spans (`gen_ai.usage.*`, `ai.usage.*`) into `llm_call` cost events, guarded by
  a cross-layer dedup registry (`core/llm-dedup.ts`).
- **Python:** none (`integrations/traces.py` is *outbound* trace-linking, not
  ingestion). Portable via `opentelemetry-sdk` (optional dep). Requires porting
  the dedup registry (`llm-dedup.ts`) too, since the span processor observes
  from outside the `suppress_network_event` scope.

### P4 — Inbound HTTP framework middleware
- **TS:** per-request task middleware for Express / Fastify / Hono / NestJS
  (`middleware/*.ts`) — start a task per request, attach attribution, finalize
  on response status.
- **Python:** none. The concept is portable to Flask (`before/after_request`),
  FastAPI/Starlette (ASGI middleware), and Django, using existing primitives
  (`context.py`, `auto_task.py`, `redaction.scrub_url`). **Needs a decision on
  which frameworks to ship** (recommend FastAPI/Starlette + Flask first).

### P5 — Smaller items
- **Queue-job wrapper** (`adapters/worker-wrap.ts::wrapJobHandler`): a
  per-message tracked-task decorator for BullMQ/SQS/Kafka-style consumers. No
  Python equivalent; portable (Celery/RQ/Kafka analog).
- **Injectable tracked-fetch factory** (`createDexcostFetch`): Python users can
  already inject `http_client=` into openai/anthropic clients, so this is lower
  value; a `TrackedTask`-aware helper could be added.
- **Ambient-session bridge** (`session.ts:242-274`): TS lets instruments join
  the HTTP adapter's per-request session; Python instruments create a per-call
  auto-task each (`instruments/*.py`). Couples with P2.

---

## 4. N/A by design for Python (not gaps)

- **Vercel AI SDK integration** (`integrations/ai-sdk.ts`) + `instruments/ai-usage.ts`
  + `instruments/vercel-ai.ts` — the `ai` package is JS-only. Python's analog
  is the existing LangChain handler (`integrations/langchain.py`) + the
  `litellm` instrument.
- **`instrumentModules` bundler escape hatch** (`instruments/index.ts:55-76`) —
  a webpack/esbuild dual-copy concern; Python's import model doesn't have it.
- **Runtime detection** (`core/runtime.ts`, Bun/Deno) and **`bun:sqlite`
  compat** (`transport/bun-sqlite.ts`) — JS-runtime-specific; Python ships
  `sqlite3` in the stdlib.

---

## 5. Reverse gaps (Python ahead of TS)

- `litellm` instrument exists in Python, not TS.
- Python propagates contextvars into `ThreadPoolExecutor` workers
  (`context.py:135-165`) — no TS analog needed (Node is single-threaded).
