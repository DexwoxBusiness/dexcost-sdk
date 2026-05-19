# Go SDK Parity Implementation Plan

**Goal:** Close every verified parity gap in the Go SDK against the Python reference.

**Architecture:** Edit in place (no git). Each task is verified with `go build ./...` and `go test ./...`. Cross-package wiring uses the existing function-hook pattern (`clients.SetDevLogFunc`) where `adapters`/`core` cannot import the top-level `dexcost` package.

**Tech Stack:** Go 1.26, `shopspring/decimal`, `gopkg.in/yaml.v3`, `google/uuid`.

---

## Audit corrections (verified, not implemented)

- **Cross-SDK claim #1 (storage retry fallback): SKIP.** Python's `_detect_retry`
  (`tracker.py:404`) is unreachable — `CostTracker.__init__` only sets
  `_enable_retry_heuristics=True` together with creating `_heuristic_engine`
  (`tracker.py:643-659`), so the `elif` storage-fallback branch (`tracker.py:386`)
  never runs. Go's behaviour (engine when `EnableRetryHeuristics`, nothing otherwise)
  already matches Python. No fix.

## Tasks

### Task 1 — 🔴 Wire the refreshed service catalog
`dexcost.go:115-122` discards the catalog after `RefreshFromURL`. Add
`adapters.SetServiceCatalog(catalog)` after a successful refresh.

### Task 2 — 🔴 Anthropic cache-creation token pricing
`pricing/engine.go` models only `cache_read`. Add `CacheCreationCost`/`HasCacheCreation`
to `modelPricing`, parse `cache_creation_input_token_cost` in `newEngineFromBytes` and
`RefreshFromServer`, extend `GetCost`/`computeCost` with a `cacheCreationTokens` param
(Python `pricing.py:170-186` semantics: read+creation subtracted from input, charged at
own rates). Add `core.WithCacheCreationTokens` LLMCallOption; thread it through
`RecordLLMCall`. Update all `GetCost` callers (`core/tracker.go`, `clients/litellm.go`).

### Task 3 — 🟠 `track_http` wire-up
`Config.TrackHTTP` is never read. Add `adapters.EnableGlobalHTTPTracking()` (wraps
`http.DefaultTransport` + `http.DefaultClient`) and `DisableGlobalHTTPTracking()`;
call it from `doInit` when `cfg.TrackHTTP`, undo in `Close()`.

### Task 4 — 🟠 HTTP session grouping
`adapters/http.go:resolveTaskID` calls `core.CreateAutoTask` per request. Add an
`adapters.SetSessionResolver(func)` hook; `dexcost.init()` registers a resolver backed
by `SessionMgr().GetOrCreateSession`, so consecutive HTTP calls share one session task.

### Task 5 — 🟡 Rate-registry YAML format unification
Python uses `{rates: {service: {per, cost_usd}}}`. Change `pricing/rates.go`
`LoadYAML`/`ExportYAML` and `cmd/dexcost/main.go` rates import/export to that format.
`LoadYAML` accepts both the new nested form and the legacy flat map for back-compat.

### Task 6 — 🟡 `EnforceMetadataLimit` deterministic stub
`security/redaction.go` drops random keys. Match Python `redaction.py:39-56`: return
`{_truncated: true, _original_size_bytes: N}` when over limit, else the original.

### Task 7 — 🟡 Provider wrapper parity
Add top-level `WrapBedrock`/`WrapCohere`/`WrapGroq`. Fix `ALL_SUPPORTED_INSTRUMENTS`
to the Python 7-item set.

### Task 8 — 🟡 LiteLLM top-level export
Export `RecordLiteLLM` at top level (thin wrapper over `clients.RecordLiteLLMResponse`).

### Task 9 — nits: top-level re-exports
Re-export `TaskFromDict`, `EventFromDict` as top-level funcs; `IsDevMode` already
top-level — confirm and add to docs.

## Verification
`go build ./...` and `go test ./...` after every task; both must pass.
