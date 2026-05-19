# Rust SDK Parity Analysis

Reference: Python SDK (`sdks/python/src/dexcost/`)
Date: 2026-05-18
Status: **v0.1.0 baseline. Documents the live surface of the Rust SDK against
the Python reference implementation. Idiomatic differences are noted as
"by design" and are not gaps.**

> Companion to [DEX-295](/DEX/issues/DEX-295). Mirrors the shape of
> [`sdks/go/PARITY.md`](../go/PARITY.md) (DEX-212).

## Summary

The Rust SDK exposes the full Python module surface but has historically
re-exported only a small subset at the top level. This audit lands the
missing top-level re-exports so downstream agents and examples can write
`use dexcost::{Task, TrackedTask, ...};` without reaching into private-feeling
sub-modules. The remaining items in this document are open gaps tracked
for future work.

What this audit closes (DEX-295):

- ✅ Top-level `pub use` block expanded to mirror the Python `__all__`
  surface (Section 1).
- ✅ `pub const VERSION` and `pub const ALL_SUPPORTED_INSTRUMENTS` added
  to `lib.rs`.
- ✅ `pub fn rate_registry()` accessor added to match `pricing_engine()`
  and `buffer()`.
- ✅ `README.md` quickstart compiles when copy-pasted (`start_task` now
  unwraps the `Result`; `record_cost` has the correct 4-argument arity).
- ✅ `lib.rs` rustdoc quickstart uses top-level imports only.

What this round closes (Python parity sweep, 2026-05-18):

- ✅ `RateRegistry` load/export now use the Python YAML `rates:` format
  (`serde_yaml_ng`), with a legacy-JSON-array fallback (Section 6).
- ✅ `PricingEngine` models Anthropic `cache_creation_input_token_cost`;
  `get_cost` / `get_cost_sync` accept a `cache_creation_tokens` parameter
  (Section 12).
- ✅ `record_cost` accepts a `RecordCostOptions` builder
  (`cost_confidence` / `pricing_source` / `pricing_version`); the
  hard-coded `Exact` / `Manual` false signal is fixed (Sections 2, 11).
- ✅ `record_llm_call` gains `record_llm_call_with` + `RecordLlmCallOptions`
  (`error_type`, `details`, confidence/source overrides) (Section 2).
- ✅ `TrackedTask::get_trace_links()` getter added (Section 2).
- ✅ `EventBuffer::purge_synced` / `purge_old_pending` implemented for real
  and wired into the `EventPusher` sync cycle (Section 7).
- ✅ `EventPusher` stops permanently on HTTP 401/403 (Section 7).
- ✅ `ServiceCatalog::refresh_from_url` added (Section 5).
- ✅ `Config` gains `auto_instrument`, `track_http`, `service_catalog_url`,
  `buffer_path`; `init()` honours them (Section 8).
- ✅ `DexcostContext.agent` added and used as the auto-session `task_type`
  (Section 3).
- ✅ `redact_map` deletes matched keys; `enforce_metadata_limit` returns the
  deterministic `{_truncated, _original_size_bytes}` stub (Section 13).
- ✅ `record_http_cost` falls back to the `ServiceCatalog` when no domain
  rate is registered (Section 14).
- ✅ LangChain integration: `integrations::langchain::DexcostCallbackHandler`
  records `llm_call` events including `error_type` failures (Section 15).

Open gaps (deferred to follow-up issues):

- `Task` / `CostEvent` lack `from_value` / `to_value` convenience helpers
  (Section 4).
- `EventPusher` background-task panics are silent (Section 7 — DEX-290
  Elephant).

## Idiomatic Differences (Expected, do not "fix")

| Python feature | Rust equivalent | Status |
|---|---|---|
| Monkey-patch auto-instrumentation (`instrument_openai`) | Wrapper clients (`TrackedOpenAI`, `TrackedAnthropic`, `TrackedGemini`) | By design |
| Decorators (`@tracker.track_task`) | Not applicable in Rust | By design |
| Context managers (`with dexcost.task()`) | Explicit `start_task` / `task.end()` | By design |
| `async with task_context()` | Tokio task-locals (`tokio::task_local!`) via `core::context::with_task` | By design |
| ThreadPoolExecutor patch | Tokio runtime; spawn boundary is `tokio::spawn` | By design |
| `wrapt`-style import-time patching | Rejected per closed [DEX-208] audit + [d5144b5e] parity work | By design |
| `__init__` re-init after `close()` | `OnceLock<SdkState>` blocks re-init for the process lifetime | By design (v1.x invariant — see Section 10) |
| `clear_context` / `get_context` (Python names) | Re-exported as `clear_dexcost_context` / `get_dexcost_context` | By design (avoids collision with Tokio's `Context`) |

## Functional Gaps

### 1. Top-Level Public API Re-exports — CLOSED in DEX-295

Python `__init__.py` `__all__` (47 symbols) → Rust `lib.rs` top-level
surface. After DEX-295 every Python `__all__` entry has a Rust top-level
analogue (or a documented "by design" omission).

| Python symbol | Rust top-level symbol | Notes |
|---|---|---|
| `Task` | `dexcost::Task` | re-exported from `core::models` |
| `Event` | `dexcost::CostEvent` | Rust name; `Event` is too generic for the public surface |
| `EventType` | `dexcost::EventType` | re-exported from `core::models` |
| `TaskStatus` | `dexcost::TaskStatus` | re-exported from `core::models` |
| `CostConfidence` | `dexcost::CostConfidence` | re-exported from `core::models` |
| `PricingSource` | `dexcost::PricingSource` | re-exported from `core::models` |
| `TrackedTask` | `dexcost::TrackedTask` | re-exported from `core::tracker` |
| `CostTracker` | (no equivalent) | Rust uses `OnceLock<SdkState>` + free fns (`start_task`, `flush`, `close`); `CostTracker` is not a struct in Rust |
| `DexcostContext` | `dexcost::DexcostContext` | already re-exported |
| `DexcostConfig` | `dexcost::Config` | aliased; `DexcostConfig` would be redundantly stuttering in Rust |
| `PricingEngine` | `dexcost::PricingEngine` | re-exported from `pricing::engine` |
| `RateRegistry` | `dexcost::RateRegistry` | re-exported from `pricing::rates` |
| `RateEntry` | `dexcost::RateEntry` | re-exported from `pricing::rates` |
| `ServiceCatalog` | `dexcost::ServiceCatalog` | re-exported from `pricing::service_catalog` |
| `CostResult` | `dexcost::CostResult` | re-exported from `pricing::engine` |
| `SyncWorker` | `dexcost::EventPusher` | Rust name; `SyncWorker` is implementation-detail terminology |
| `SessionManager` | `dexcost::SessionManager` | re-exported from `core::session` |
| `get_session_manager` | `dexcost::get_session_manager` | re-exported from `core::session` |
| `validate` | `dexcost::validate` | re-exported from `schema::validate` |
| `enforce_metadata_limit` | `dexcost::enforce_metadata_limit` | re-exported from `security::redaction` |
| `hash_value` | `dexcost::hash_value` | re-exported from `security::redaction` |
| `redact_dict` | `dexcost::redact_map` | Rust name; `Map<String, Value>` is the input type, "dict" is Python jargon |
| `ALL_SUPPORTED_INSTRUMENTS` | `dexcost::ALL_SUPPORTED_INSTRUMENTS` | new `pub const &[&str]` |
| `__version__` | `dexcost::VERSION` | new `pub const`, sourced from `Cargo.toml` |
| `InvalidAPIKeyError` | `DexcostError::InvalidApiKey(String)` | variant, not a separate error type |
| `validate_api_key` | `dexcost::config::validate_api_key` | free fn on `config`; `Config::validate()` is the high-level entry point |
| `get_current_task` | `dexcost::get_current_task` | re-exported from `core::context` |
| `set_current_task` | (no equivalent) | Rust uses `core::context::with_task` (RAII via Tokio task-local scope) — by design |
| `set_context` | `dexcost::set_context` | re-exported |
| `clear_context` | `dexcost::clear_dexcost_context` | re-exported under disambiguated name |
| `get_context` | `dexcost::get_dexcost_context` | re-exported under disambiguated name |
| `create_auto_task` | `dexcost::create_auto_task` | re-exported |
| `link_trace` | `TrackedTask::link_trace` | method on the struct (matches Python) |
| `task` (context manager) | `dexcost::start_task` | async fn returning `TrackedTask`; explicit `.end()` instead of context-manager `__exit__` |
| `record_cost` (free fn) | `TrackedTask::record_cost` | method only; the Python free fn relies on a global current-task that Rust resolves via task-local scope |
| `init` | `dexcost::init` | free fn |
| `start_task` | `dexcost::start_task` | free fn |
| `flush` | `dexcost::flush` | free fn |
| `close` | `dexcost::close` | free fn |
| `buffer` | `dexcost::buffer()` | accessor fn |
| `pricing_engine` | `dexcost::pricing_engine()` | accessor fn |
| `rate_registry` | `dexcost::rate_registry()` | accessor fn (added in DEX-295) |
| `TrackedAnthropic` | `dexcost::TrackedAnthropic` | re-exported from `clients::tracked_anthropic` |
| `TrackedOpenAI` | `dexcost::TrackedOpenAI` | re-exported from `clients::tracked_openai` |
| `TrackedGemini` | `dexcost::TrackedGemini` | re-exported from `clients::tracked_gemini` |
| `RetryHeuristicEngine` | `dexcost::RetryHeuristicEngine` | re-exported from `core::heuristics` |
| `HeuristicConfig` | `dexcost::HeuristicConfig` | re-exported from `core::tracker` |

### 2. `TrackedTask` Method Gaps — CLOSED

Resolved via builder structs (`RecordLlmCallOptions`, `RecordCostOptions`)
mirroring `TaskOptions`. The original positional methods are retained as
thin wrappers for backward compatibility.

| Method | Python kwarg | Rust today |
|---|---|---|
| `record_llm_call_with` | `error_type=...` | `RecordLlmCallOptions.error_type` — merged into `details["error_type"]` |
| `record_llm_call_with` | `details=...` | `RecordLlmCallOptions.details` |
| `record_llm_call_with` | `cost_confidence=...` | `RecordLlmCallOptions.cost_confidence` — overrides auto-derived value |
| `record_llm_call_with` | `pricing_source=...` | `RecordLlmCallOptions.pricing_source` — overrides auto-derived value |
| `record_cost_with` | `cost_confidence=...` | `RecordCostOptions.cost_confidence` (defaults to `Exact`) |
| `record_cost_with` | `pricing_source=...` | `RecordCostOptions.pricing_source` (defaults to `Manual`) |
| `record_cost_with` | `pricing_version=...` | `RecordCostOptions.pricing_version` |
| `get_trace_links()` | Returns list of dicts | `TrackedTask::get_trace_links() -> Vec<serde_json::Value>` |

### 3. `set_context()` Gaps — CLOSED

`DexcostContext` now carries `pub agent: Option<String>`. `create_auto_task`
uses `ctx.agent` as the `task_type` of the auto-created session task when
set, matching Python `session.py:73-75`.

### 4. Model Serialization Gaps (open)

| Python method | Rust today | Action |
|---|---|---|
| `Task.to_dict()` | `Task` derives `serde::Serialize` | Add convenience `pub fn to_value(&self) -> serde_json::Value` (forwards to `serde_json::to_value`) |
| `Event.to_dict()` | Same | Add same convenience on `CostEvent` |
| `Task.from_dict()` | `Task` derives `serde::Deserialize` | Add convenience `pub fn from_value(v: serde_json::Value) -> Result<Self, ...>` |
| `Event.from_dict()` | Same | Add same convenience on `CostEvent` |

### 5. `ServiceCatalog` Gaps — CLOSED

`ServiceCatalog::refresh_from_url(&mut self, url: &str)` is implemented. It
fetches a remote catalog JSON over `reqwest`, skips the `_meta` key, and
merges entries (new keys added, existing keys updated) into both `entries`
and `raw_data` so `catalog_version()` reflects the merge. Mirrors Python
`service_catalog.py:386-404`.

### 6. `RateRegistry` Gaps — CLOSED

`load_from_file` / `save_to_file` now use the Python YAML format: a
top-level `rates:` mapping of `service -> {per, cost_usd}`. Serialised via
the maintained `serde_yaml_ng` crate (the archived `serde_yaml` is not
used). `load_from_file` also accepts the legacy flat JSON array
(`[{service, per, cost_usd}]`) for backward compatibility. The default
on-disk path is now `~/.dexcost/rates.yaml`.

| Python method | Rust today | Notes |
|---|---|---|
| `load(path)` | `RateRegistry::load_from_file(&Path) -> Result<usize, String>` | YAML `rates:` map; legacy JSON-array fallback. |
| `export(path)` | `RateRegistry::save_to_file(&Path) -> Result<usize, String>` | Writes YAML `rates:` map, sorted by service. |

### 7. `EventPusher` (SyncWorker) Gaps

| Feature | Python | Rust today | Status |
|---|---|---|---|
| Sync tasks alongside events | Supported | `buffer.rs::pending_tasks` + `pusher.rs::push_batch` | ✅ Done |
| 401/403 permanent stop | Supported | `post_raw` sets a permanent `auth_failed` flag on HTTP 401/403; the sync loop then stops and `is_auth_failed()` reports it | ✅ Done |
| Purge old synced/pending events | Supported | `EventBuffer::purge_synced` (48h) + `purge_old_pending` (7d), each followed by `VACUUM`; wired into the `EventPusher` sync cycle (throttled to once per hour) | ✅ Done |
| Background-task panic surfacing | Best-effort `atexit` | **Silent — DEX-290 Elephant** | Open — wrap pusher loop body + report panic via stderr / a status channel |

### 8. `init()` / `Config` Gaps — CLOSED

| Param | Python | Rust today |
|---|---|---|
| `track_http` | default `True` | `Config::track_http: bool` (default `true`); `init()` builds a `ServiceCatalog` when enabled |
| `service_catalog_url` | optional | `Config::service_catalog_url: Option<String>`; `init()` refreshes the catalog in the background (fail-silent) |
| `auto_instrument` | optional list | `Config::auto_instrument: Vec<String>` (carried for parity; Rust uses wrapper clients, not monkey-patching) |
| `buffer_path` | optional | `Config::buffer_path: Option<PathBuf>`; `init()` resolution order: `buffer_path` → `DEXCOST_BUFFER_PATH` → `~/.dexcost/buffer.db` |
| `redact_fields` | optional | `Config::redact_fields: Vec<String>` ✅ |
| `hash_customer_id` | default `False` | `Config::hash_customer_id: bool` ✅ |
| `flush_interval` | default `5.0`s | `Config::flush_interval_secs: u64` ✅ |
| `batch_size` | default `100` | `Config::batch_size: usize` ✅ |
| `environment` | optional str | `Config::environment: Option<String>` ✅ |

### 9. `Config` Property Gaps

| Python property | Rust today | Notes |
|---|---|---|
| `is_dev` | implied via `environment == "development"` (handled in `Config::validate`) | Add explicit `pub fn is_dev(&self) -> bool` for ergonomic parity |
| `endpoint` | `Config::endpoint(&self) -> String` (currently `pub(crate)`) | Promote to `pub fn` for parity |
| `storage_mode` | implied via `api_key.is_some()` | Document mapping; no code change needed |
| `is_sandbox` | `Config::is_sandbox(&self) -> bool` ✅ | None |
| `key_type` | `Config::key_type(&self) -> Option<&str>` ✅ | None |

### 10. SDK Lifecycle (v1.x invariant — DOCUMENT, do not "fix")

| Aspect | Python | Rust today | Action |
|---|---|---|---|
| Re-init after `close()` | Allowed (resets module-level globals) | **Blocked by `OnceLock<SdkState>`** | Document loudly. Tests requiring re-init use a `#[cfg(test)]` reset helper. Production: one process = one `init()`. |

This is by design for v1.x: `OnceLock` gives us thread-safe lazy
initialization with no synchronization cost on the hot path. A future
v2 may relax this if user demand justifies the trade-off.

### 11. `record_cost` cost_confidence false signal (DEX-290 Elephant — CLOSED)

`record_cost` previously always set `cost_confidence = Exact` and
`pricing_source = Manual` regardless of how the caller produced the
`cost_usd` value. This is fixed: `record_cost_with` accepts a
`RecordCostOptions` builder with `cost_confidence`, `pricing_source`, and
`pricing_version`. The legacy `record_cost` wrapper preserves the
`Exact` / `Manual` defaults, so callers that do not opt in are unchanged,
but callers can now record an accurate price provenance.

### 12. Anthropic cache-creation token pricing — CLOSED

`ModelPricing` now parses `cache_creation_input_token_cost`. `get_cost` /
`get_cost_sync` / `compute_cost_from` accept a `cache_creation_tokens`
parameter and apply the Python algorithm (`pricing.py:174-186`):
`effective_cached = min(cached, input)`,
`remaining = input - effective_cached`,
`effective_creation = min(cache_creation, remaining)`,
`non_cached = remaining - effective_creation`. The Anthropic wrapper
(`TrackedAnthropic`, `wrappers::record_anthropic_response`, the
`reqwest-middleware` adapter) threads cache-creation token counts through.

### 13. Redaction behavior — CLOSED

`redact_map` now **deletes** matched keys recursively (matching Python
`redact_dict`) instead of masking them with `"[REDACTED]"`.
`enforce_metadata_limit` now returns the deterministic stub
`{"_truncated": true, "_original_size_bytes": N}` when the payload exceeds
the limit, instead of popping trailing entries.

### 14. `record_http_cost` service-catalog fallback — CLOSED

The standalone `record_http_cost` only consulted the domain rate registry.
A new `record_http_cost_with_catalog(url, task_id, Option<&ServiceCatalog>)`
falls back to the `ServiceCatalog` when the domain has no registered rate
(domain rate still takes precedence). The `reqwest-middleware` adapter uses
this catalog-aware path.

### 15. LangChain integration — CLOSED (new capability)

`integrations::langchain::DexcostCallbackHandler` is the Rust analogue of
Python's `DexcostCallbackHandler` (`integrations/langchain.py`). Driven
through `on_llm_start` / `on_llm_end` / `on_llm_error`, it records
`llm_call` events (provider `"langchain"`), computes cost via the
`PricingEngine`, and records failure events carrying `error_type` in their
details. It depends on no LangChain crate (none is canonical in Rust);
callers drive it through its public methods. Re-exported from
`integrations::DexcostCallbackHandler`.

## Out of Scope (per closed [d5144b5e] parity work)

These were explicitly cut from the prior parity sweep and remain out of
scope for v0.1:

- **Browser adapter** — no canonical Rust headless browser library in
  the baseline dependency set.
- **Budget primitive** — not implemented in Python either.
- **`wrapt`-style import-time patching** — not idiomatic in Rust;
  `Tracked*` wrappers are the canonical surface.

## Acceptance Criteria for this Audit

- [x] `sdks/rust/PARITY.md` lands on master with the full Python → Rust
      mapping above.
- [x] Each gap has a referenced section, ticket, or "by design"
      justification.
- [x] `lib.rs` top-level `pub use` block expanded so README and examples
      compile from `dexcost::*` without reaching into `core::` /
      `pricing::` / `transport::` submodules.
- [x] README quickstart compiles when copy-pasted.
- [x] `cargo build`, `cargo test --all-features`, and `cargo doc --no-deps`
      succeed.

## Notes

- **Prior parity work** ([d5144b5e](/DEX/issues/d5144b5e-2e83-4dbe-b232-322475c12851))
  "Rust SDK parity: close gaps vs Python SDK" closed `done`. It addressed
  *implementation* gaps (reqwest middleware, provider wrappers, tower /
  actix middleware, Lambda adapter) but did not produce a `PARITY.md`
  document. DEX-295 fills that documentation gap.
- **Cancelled implementation issue**
  ([56f925eb](/DEX/issues/56f925eb-532e-448a-a7f2-96234b9a84cd)) "Rust SDK
  parity: implementation" — cancelled, do not revive.
- **DEX-287 analogue** — Go SDK's `WithCostConfidence` / `WithDetails`
  gap-fix is the same shape as Section 2. Rust's idiomatic equivalent is
  a builder struct, not function options.
