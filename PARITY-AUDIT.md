# dexcost SDK Cross-SDK Parity Matrix

**Date:** 2026-05-18 (re-audit #2)
**Method:** Fresh, code-level capability inventory of all four SDKs against an identical
25-point checklist. Conclusions are from reading actual source — not README, not
`PARITY.md`. Every cell has file:line evidence in the per-SDK notes.

**On audit variance — read this first.** This is a *stricter* pass than the prior
matrix: it inspects sub-features the earlier pass did not (per-provider streaming,
`cache_creation_tokens` parameters, whether the retry engine is reachable through
`init()`, whether the `tasks` table tracks `sync_status`). Some cells therefore moved
✅ → 🟡 **without the code regressing** — the audit simply looked closer. Cells that
genuinely changed because of a code fix are listed under "Recently fixed" below.

**Excluded (by prior decision):** storage-based retry fallback (Python's reference
branch is unreachable dead code); Rust browser adapter (no canonical Rust headless
library — environmental, by design).

## Legend

✅ present & equivalent · 🟡 partial (real gap, see notes) · ❌ missing · ➖ N/A by design

---

## The Matrix

| # | Capability | Python | TypeScript | Go | Rust |
|---|------------|:--:|:--:|:--:|:--:|
| 1 | init() / config params | 🟡 | 🟡 | 🟡 | 🟡 |
| 2 | Task lifecycle | ✅ | ✅ | ✅ | 🟡 |
| 3 | LLM cost capture (providers + streaming) | 🟡 | 🟡 | ✅ | ✅ |
| 4 | HTTP / non-LLM cost capture | ✅ | ✅ | ✅ | ✅ |
| 5 | `record_cost` + params | ✅ | ✅ | ✅ | ✅ |
| 6 | `record_usage` | ✅ | ✅ | ✅ | ✅ |
| 7 | `record_llm_call` + params | 🟡 | 🟡 | ✅ | 🟡 |
| 8 | Retry tracking | 🟡 | ✅ | 🟡 | ✅ |
| 9 | Attribution fields | ✅ | ✅ | ✅ | ✅ |
| 10 | Pricing engine | 🟡 | ✅ | 🟡 | 🟡 |
| 11 | Rate registry | ✅ | ✅ | ✅ | ✅ |
| 12 | Service catalog | ✅ | ✅ | ✅ | ✅ |
| 13 | Local SQLite buffer | 🟡 | 🟡 | ✅ | ✅ |
| 14 | Background sync/push | 🟡 | 🟡 | 🟡 | ✅ |
| 15 | Schema validation | ✅ | ✅ | ✅ | ✅ |
| 16 | Security (redact/hash/limit) | ✅ | ✅ | ✅ | ✅ |
| 17 | CLI | ✅ | ✅ | 🟡 | 🟡 |
| 18 | Codebase scanner | ✅ | ✅ | ✅ | ✅ |
| 19 | Framework integrations / middleware | 🟡 | ✅ | ✅ | ✅ |
| 20 | Model serialization | ✅ | ✅ | ✅ | 🟡 |
| 21 | Trace linking | ✅ | 🟡 | 🟡 | ✅ |
| 22 | Dev console mode | ✅ | ✅ | ✅ | ✅ |
| 23 | Browser adapter | ✅ | ✅ | ❌ | ➖ |
| 24 | AWS Lambda adapter | ✅ | ✅ | ✅ | ✅ |
| 25 | Top-level public API surface | ✅ | ✅ | 🟡 | 🟡 |

**Tally (✅ / 🟡 / ❌, excluding ➖):** Python 17 / 8 / 0 · TypeScript 19 / 6 / 0 ·
Go 17 / 7 / 1 · Rust 17 / 7 / 0.

> The tally is lower than re-audit #1's because of the stricter method described
> above — not regression. The number that answers your question ("are they paired?")
> is the next section.

---

## Where the four SDKs ARE paired (11 / 25 — identical ✅ in all four)

Rows 4, 5, 6, 9, 11, 12, 15, 16, 18, 22, 24:
HTTP capture · `record_cost` · `record_usage` · attribution fields · rate registry ·
service catalog · schema validation · security functions · codebase scanner ·
dev console · Lambda adapter.

These are functionally equivalent across Python, TypeScript, Go, and Rust (allowing for
idiomatic differences). The remaining 14 rows diverge — see below.

## Where they still diverge

**1 — init/config params (🟡 all four).** Every SDK has a per-SDK quirk: Python has no
explicit `dev_mode` param (derived from `environment`); TS naming drift + 30 s vs 5 s
default flush; **Go `TrackHTTP` defaults `false` (Python defaults `true`)**; Rust has no
`storage`/local-mode field and `auto_instrument` is a dead stored field.

**2 — Task lifecycle.** Python/TS/Go ✅. **Rust 🟡:** `start_task` never enters the
task-local scope, so child tasks can't discover their parent unless the caller manually
wraps with `with_task` (which isn't even exported); two divergent `create_auto_task`
impls.

**3 — LLM capture.** **Python 🟡:** streaming wrappers exist only for openai/anthropic/
litellm — gemini/bedrock/cohere streamed responses are not captured. **TypeScript 🟡:**
no `litellm` instrument (has the other 7). Go ✅ (6 wrappers + streaming + MCP). Rust ✅
(3 wrappers + map recorders + streaming behind a feature).

**7 — `record_llm_call` params.** **Python 🟡 / TypeScript 🟡:** neither can pass
`cache_creation_tokens` (the pricing engine supports it; the manual API can't reach it
— Anthropic cache-write pricing only works via auto-instrument). **Rust 🟡:**
`RecordLlmCallOptions` has no `pricing_version` field. **Go ✅** — full option set.

**8 — Retry tracking.** TS/Rust ✅. **Python 🟡 / Go 🟡:** the `RetryHeuristicEngine`
exists but is *unreachable through `init()`/`Init()`* — the public entry point builds
the tracker with heuristics off and exposes no knob. Go additionally has a bug: the
engine reads `error_type` from `Details` but `RecordLLMCall` writes it to the
`ErrorType` field — so even if enabled it never fires.

**10 — Pricing engine.** TS ✅. **Python 🟡:** the LiteLLM-repo background auto-update
isn't wired into `init()` (server refresh is). **Go 🟡:** background auto-update method
exists but `Init()` never calls it. **Rust 🟡:** model-alias resolution is weaker than
Python (strict date-suffix regex only — non-date suffixes fall through to Unknown).

**13 — Local buffer.** Go/Rust ✅ (both tables carry `sync_status`). **Python 🟡 /
TypeScript 🟡:** the `tasks` table has no `sync_status` column — tasks can't be marked
synced.

**14 — Background sync/push.** **Rust ✅** is the most complete (both purges, 401/403
stop, splitting, task sync tracking). **Python 🟡:** `mark_tasks_synced` is a no-op →
re-POSTs every task each cycle. **TypeScript 🟡 / Go 🟡:** no purge of old *pending*
events; TS also re-sends all tasks each push.

**17 — CLI.** Python/TS ✅. **Go 🟡:** the `rates` command has no persistent store
(`--import` then `--list` shows nothing). **Rust 🟡:** CLI `rates` uses flat JSON while
the library writes YAML — round-trip is broken; `generate_stubs` emits non-compiling code.

**19 — Framework middleware.** TS/Go/Rust ✅ (each ships HTTP middleware). **Python 🟡:**
LangChain handler only — no HTTP framework middleware. (Here the other three exceed Python.)

**20 — Model serialization.** Python/TS/Go ✅. **Rust 🟡:** no explicit `from_dict`/
`from_value` constructor (relies on a generic serde derive).

**21 — Trace linking.** Python/Rust ✅ (method + module-level). **TypeScript 🟡:** no
module-level functions. **Go 🟡:** no top-level `GetTraceLinks`.

**23 — Browser adapter.** Python ✅ and TypeScript ✅ (both now persist — see Recently
fixed). **Go ❌:** no browser adapter exists. **Rust ➖:** N/A by design.

**25 — Top-level API surface.** Python/TS ✅. **Go 🟡:** `ALL_SUPPORTED_INSTRUMENTS` is
inaccurate; no top-level `RecordLLMCall`/`RecordUsage`/`GetTraceLinks`. **Rust 🟡:**
`with_task` and the module-level trace helpers aren't re-exported.

---

## Recently fixed (verified in current code — genuine improvements)

- ✅ **Python HTTP adapter now persists** — `init()` wires `set_storage`; captured HTTP
  cost reaches SQLite and syncs.
- ✅ **Python browser adapter now persists** — `init()` wires browser storage;
  `track_browser` events reach SQLite and sync. (Was dead-ended in re-audit #1.)
- ✅ **TypeScript browser adapter persists** — `setBrowserBuffer` wired on construction.
- ✅ **Rust `record_cost`** — `cost_confidence`/`pricing_source` are caller-overridable
  (no longer hard-coded).
- ✅ **Go `TrackHTTP` / `ServiceCatalogURL`** — now wired into `Init()`.
- ✅ **Rate-registry YAML format** — Go library + CLI aligned on the canonical `rates:`
  shape.

---

## Universal gaps (still affect all/most SDKs)

- [ ] **Task metadata is not redacted/hashed on push — all four SDKs.** `redact`,
  `hash`, and `enforce_metadata_limit` are applied only to `event.details`; the `Task`
  object's `metadata` (and `_trace_links`) is serialized raw. `redact_fields` /
  `hash_customer_id` do not protect task-level data anywhere.
- [ ] **`schema.validate()` is wired into nothing — all four SDKs.** It is an exported
  utility; no SDK validates events/tasks before storage or sync.

(The earlier "browser adapter does not persist" universal gap is now resolved —
Python and TS both fixed.)

---

## Per-SDK bugs found (current code)

### Python
- [ ] 🟡 Task metadata not redacted/hashed on sync; `hash_customer_id` never hashes the
  real `task.customer_id` column.
- [ ] 🟡 `tasks` table has no `sync_status`; `mark_tasks_synced` is a no-op → every task
  re-uploaded each sync cycle.
- [ ] 🟡 `RetryHeuristicEngine` unreachable via `init()`.
- [ ] 🟡 Streaming not captured for gemini / bedrock / cohere.
- [ ] 🟡 `record_llm_call` cannot pass `cache_creation_tokens`.
- [ ] 🟡 `schema.validate()` schema-file path is fragile (`__file__/../../../schemas`).
- [ ] 🟡 `ServiceEntry._get_rate` and `_get_fixed_cost` are identical (dead duplication).

### TypeScript
- [ ] 🟡 No `purge_old_pending`; `tasks` table has no `sync_status` → re-sends all tasks.
- [ ] 🔴 Heuristic retry flag is not persisted — the event row is `addEvent`-ed *before*
  the heuristic mutates `isRetry`/`retryReason`; `updateEvent` is dead. The SQLite row
  keeps `is_retry=0`.
- [ ] 🟡 `recordLlmCall` cannot pass cache-creation tokens.
- [ ] 🟡 No `litellm` instrument.
- [ ] 🟡 Default `flushIntervalMs` 30 s vs Python 5 s.
- [ ] 🟡 Browser adapter silently drops cost when no active task (no auto-task, unlike
  the HTTP/LLM adapters).

### Go
- [ ] 🔴 `InsertTask` / `InsertTaskWithEvents` hardcode the metadata column to `"{}"` —
  **task metadata is never persisted to SQLite**, so `_trace_links` and session flags
  are silently dropped.
- [ ] 🔴 Heuristic retry detection reads `Details["error_type"]` but `RecordLLMCall`
  writes `event.ErrorType` — `WithErrorType`-based detection never fires.
- [ ] 🟡 `RetryHeuristicEngine` unreachable via `Init()` (no `Config` field).
- [ ] 🟡 Pricing background auto-update never started by `Init()`.
- [ ] 🟡 Outbound task metadata not sanitized; no purge of old pending events.
- [ ] 🟡 `TrackHTTP` defaults `false` (Python `true`).
- [ ] 🟡 `ALL_SUPPORTED_INSTRUMENTS` inaccurate (lists litellm/langchain, omits mcp).
- [ ] 🟡 CLI `rates` has no persistent store.

### Rust
- [x] ✅ **B5b — `price_iaas_share` math now mirrors Python.** Fixed in Sprint 1
  follow-on (commit log: `fix(rust): B5b ...`). The function now computes
  `cost = vcpu_seconds_used / (vcpu_count × 3600) × instance_hourly` — algebraic
  equivalent of Python's `share_factor × task_instance_hours × instance_hourly`.
  Regression test at
  `rust/tests/cross_sdk_parity.rs::cross_sdk_compute_pricing_exact_parity_b5b`
  no longer `#[ignore]`d and passes against both EC2-share and k8s-pod
  fixtures.
- [ ] 🟡 `start_task` never enters task-local scope → parent linking broken unless the
  caller manually uses `with_task` (which isn't exported).
- [ ] 🟡 CLI `rates` uses flat JSON; library uses YAML — round-trip broken.
- [ ] 🟡 `generate_stubs` emits non-compiling code (stale API signatures).
- [ ] 🟡 Duplicate divergent `create_auto_task` (`core::context` vs `core::auto_task`).
- [ ] 🟡 Two divergent trace-link stores (task metadata vs a global table) — don't share state.
- [ ] 🟡 `auto_instrument` config field is dead (stored, never read).
- [ ] 🟡 `RecordLlmCallOptions` missing `pricing_version`.
- [ ] 🟡 Model-alias resolution weaker than Python (non-date suffixes → Unknown cost).

---

## Idiomatic differences — NOT gaps

| Python | Equivalent | Verdict |
|--------|-----------|---------|
| Monkey-patch auto-instrumentation | Wrapper clients (Go/Rust); TS keeps patching | Equivalent |
| `with dexcost.task()` / decorators | Explicit start/end; TS `track()` callback | Equivalent |
| `contextvars` | Go `context.Context`, Rust `tokio::task_local!`, TS `AsyncLocalStorage` | Equivalent |
| Global HTTP auto-capture (Py monkey-patch, TS fetch patch, Go global transport) | Rust `reqwest-middleware` (manual attach, feature-gated) | Equivalent — Rust has no global HTTP hook |
| `litellm` instrument | TS `vercel-ai` instrument | Equivalent ecosystem coverage |
| `agent` attribution | Context-only field in all four SDKs (not a `Task` column anywhere) | Paired |

---

## Suggested fix order

1. **Data-loss bugs** (🔴): Go task metadata never persisted (`InsertTask` writes `{}`);
   Go heuristic field mismatch; TS heuristic retry flag not persisted.
2. **Sync correctness**: task-metadata redaction (universal); Python task-resend;
   TS/Go purge-of-old-pending.
3. **Reachability**: retry engine via `init()` (Python + Go); pricing auto-update (Go);
   Go `TrackHTTP` default.
4. **Capture coverage**: Python streaming for gemini/bedrock/cohere; `cache_creation_tokens`
   param (Python + TS); Rust model-alias resolution.
5. **Consistency**: Rust CLI rate format; `ALL_SUPPORTED_INSTRUMENTS` (Go);
   trace-link module-level APIs (TS/Go); Rust `from_dict`.
6. Regenerate `go/PARITY.md` and `rust/PARITY.md` from this matrix.
