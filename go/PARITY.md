# Go SDK Parity Analysis (DEX-212)

Reference: Python SDK (`sdks/python/src/dexcost/`)
Date: 2026-05-03

## Summary

The Go SDK covers ~75% of the Python SDK's surface. The remaining 25% is a mix of:
- **Missing top-level exports** (users can't access types from `dexcost` package)
- **Missing options on existing methods** (`RecordCost`, `RecordLLMCall`, `SetContext`)
- **Missing model deserialization** (`FromDict`)
- **Missing service catalog / rate registry YAML I/O**
- **Missing event pusher task syncing and purge**
- **Missing init params** (`track_http`, `service_catalog_url`)

## Idiomatic Differences (Expected)

| Python Feature | Go Equivalent | Status |
|---|---|---|
| Monkey-patch auto-instrumentation (`instrument_openai`) | Wrapper clients (`WrapOpenAI`) | By design |
| Decorators (`@tracker.track_task`) | Not applicable in Go | By design |
| Context managers (`with dexcost.task()`) | Explicit `StartTask` / `EndTask` | By design |
| `async with task_context()` | Context propagation via `context.Context` | By design |
| ThreadPoolExecutor patch | Not applicable (goroutines share context) | By design |

## Functional Gaps

### 1. Top-Level Public API Exports

Python `__init__.py` exports these; Go `dexcost.go` does not:

| Symbol | Python | Go | Action |
|---|---|---|---|
| `Task` | Exported | Missing | Add re-export |
| `Event` | Exported | Missing | Add re-export |
| `DexcostContext` | Exported | Missing (has `core.ContextData`) | Add re-export as `DexcostContext` |
| `CostTracker` | Exported | Missing (has `core.Tracker`) | Add type alias |
| `PricingEngine` | Exported | Missing (in `pricing` pkg) | Add re-export |
| `RateRegistry` | Exported | Missing (in `pricing` pkg) | Add re-export |
| `RateEntry` | Exported | Missing (in `pricing` pkg) | Add re-export |
| `ServiceCatalog` | Exported | Missing (in `pricing` pkg) | Add re-export |
| `CostResult` | Exported | Missing (in `pricing` pkg) | Add re-export |
| `SyncWorker` | Exported | Missing (has `transport.EventPusher`) | Add type alias |
| `SessionManager` | Exported | Missing | Add re-export |
| `validate` | Exported | Missing (in `schema` pkg) | Add re-export |
| `enforce_metadata_limit` | Exported | Missing (in `security` pkg) | Add re-export |
| `hash_value` | Exported | Missing (in `security` pkg) | Add re-export |
| `redact_dict` | Exported | Missing (in `security` pkg) | Add re-export |
| `ALL_SUPPORTED_INSTRUMENTS` | Exported | Missing | Add constant |
| `InvalidAPIKeyError` | Exported | Has `ErrInvalidAPIKey` | Add type alias |
| `get_current_task` | Exported | Missing (in `core` pkg) | Add re-export |
| `set_current_task` | Exported | Missing (in `core` pkg) | Add re-export |
| `clear_context` | Exported | Missing (in `core` pkg) | Add re-export |
| `get_context` | Exported | Missing (in `core` pkg) | Add re-export |
| `link_trace` | Exported | Missing (in `integrations` pkg) | Add re-export |
| `DexcostCallbackHandler` | Exported | Missing (in `integrations` pkg) | Add re-export |
| `__version__` | Exported | Missing | Add `Version` constant |

### 2. TrackedTask Method Gaps — RESOLVED (DEX-266)

| Method | Python | Go | Status |
|---|---|---|---|
| `record_llm_call(error_type=...)` | Supported | `WithErrorType` (core + top-level alias) | ✅ Resolved |
| `get_trace_links()` | Returns list | `TrackedTask.GetTraceLinks()` | ✅ Resolved |
| `record_cost(details=...)` | Accepts dict | `WithDetails` (core + top-level alias) | ✅ Resolved |
| `record_cost(cost_confidence=...)` | Supported | `WithCostConfidence` (core + top-level alias) | ✅ Resolved |
| `record_cost(pricing_source=...)` | Supported | `WithPricingSource` (core + top-level alias) | ✅ Resolved |
| `record_cost(pricing_version=...)` | Supported | `WithPricingVersion` (core + top-level alias) | ✅ Resolved |

Top-level `RecordCost(ctx, service, operation, costUSD, opts...)` accepts these options via `core.NewEventWithOptions`, so callers using `dexcost.RecordCost` (without a TrackedTask handle) have full parity with the Python `record_cost` keyword arguments.

### 3. SetContext Gaps

| Param | Python | Go | Action |
|---|---|---|---|
| `metadata` | Supported | Missing | Add `SetContextWithMetadata` or extend |
| `agent` | Supported | Missing | Add `agent` support for session tasks |

### 4. Model Serialization Gaps

| Method | Python | Go | Action |
|---|---|---|---|
| `Task.from_dict()` | Supported | Missing | Add `TaskFromDict` |
| `Event.from_dict()` | Supported | Missing | Add `EventFromDict` |

### 5. ServiceCatalog Gaps

| Method | Python | Go | Action |
|---|---|---|---|
| `refresh_from_url(url)` | Supported | Missing | Add `RefreshFromURL` |

### 6. RateRegistry Gaps

| Method | Python | Go | Action |
|---|---|---|---|
| `load(path)` | YAML load | Missing | Add `LoadYAML` |
| `export(path)` | YAML export | Missing | Add `ExportYAML` |

### 7. EventPusher (SyncWorker) Gaps — RESOLVED (DEX-266)

| Feature | Python | Go | Status |
|---|---|---|---|
| Sync tasks with events | Supported | `pushBatch` collects task IDs from each batch and sends `tasks: [...]` alongside `events: [...]` to `/v1/ingest` (via `taskSyncBuffer` interface) | ✅ Resolved |
| 401/403 permanent stop | Supported | `EventPusher.postRaw` flips `p.stopped=true` on `401`/`403` so subsequent calls short-circuit | ✅ Resolved |
| Purge old synced events | Supported | `pushBatch` calls `tsb.PurgeSyncedEvents(now − purgeRetention)` (default 7 days) after each successful push | ✅ Resolved |
| Mark tasks synced | Supported | `SQLiteBuffer.MarkTasksSynced(taskIDs)` sets `tasks.sync_status='synced'`, called by `pushBatch` after each successful push | ✅ Resolved |
| Restart loop after Stop | n/a | `EventPusher.Start()` re-creates `stopCh`/`flushCh` and re-spawns `run()` (race-safe via `running atomic.Bool`); idempotent if already running | ✅ Added |

### 8. Init() Gaps

| Param | Python | Go | Action |
|---|---|---|---|
| `track_http` | Auto-enable HTTP tracking | Missing | Add to `Config` |
| `service_catalog_url` | Refresh catalog on init | Missing | Add to `Config` |
| `auto_instrument` | List of SDKs to patch | N/A in Go | Document as not applicable |

### 9. Config Gaps

| Property | Python | Go | Action |
|---|---|---|---|
| `is_dev` | Property | Missing | Add `IsDev()` method |
| `endpoint` | Property | Unexported method | Already exists as `resolvedEndpoint` |

## Implementation Plan

1. **Expand `dexcost.go`** — Add all missing re-exports and aliases
2. **Expand `core/tracker.go`** — Add missing options (`WithErrorType`, `WithDetails`, `WithCostConfidence`, `WithPricingSource`, `WithPricingVersion`), add `GetTraceLinks`
3. **Expand `core/models.go`** — Add `TaskFromDict`, `EventFromDict`
4. **Expand `core/context.go`** — Add `ClearContext` public function
5. **Expand `pricing/service_catalog.go`** — Add `RefreshFromURL`
6. **Expand `pricing/rates.go`** — Add `LoadYAML`, `ExportYAML`
7. **Expand `transport/pusher.go`** — Add task syncing, auth handling, purge
8. **Expand `transport/buffer.go`** — Add `MarkTasksSynced`, `QueryTasksForSync`
9. **Expand `config.go`** — Add `IsDev()`, `TrackHTTP`, `ServiceCatalogURL`
10. **Expand `dexcost.go` Init** — Wire up `track_http` and `service_catalog_url`
11. **Expand `session.go`** — Wire session manager into `Init`
12. **Tests** — Run `go test ./... -race -cover`
