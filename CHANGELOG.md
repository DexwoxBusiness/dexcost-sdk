# Changelog

All notable cross-SDK changes are recorded here. Each SDK also keeps
its own per-language CHANGELOG (Python only, today) for fine-grained
notes.

## Unreleased — Sprint 0 → Sprint 4 remediation branch

This release lands the engineering work from the
[Dexcost SDK Remediation Plan](docs/superpowers/plans/) — Sprints 0–4
of a 5-sprint correctness + ship-readiness pass across Python, Go,
TypeScript, and Rust SDKs.

### ⚠ Breaking changes

Customers upgrading from the previous SDK release should review these
carefully — they change wire format, default behaviour, or remove
previously-tolerated configuration.

1. **Timestamp wire format (P1, all 4 SDKs)**
   `occurred_at` / `started_at` / `ended_at` now serialise as
   `YYYY-MM-DDTHH:MM:SS.ffffffZ` (RFC3339, **microsecond precision,
   "Z" suffix**). Previously: Python `+00:00` suffix, Go
   `RFC3339Nano` (9 fractional digits), TS `toISOString` (3 digits),
   Rust nanosecond. The control plane must accept the new form; a
   one-release tolerance window for the old form is recommended.

2. **`DEXCOST_ENDPOINT` allow-list (A2, all 4 SDKs)**
   Non-`https://` values are rejected with a warning and fall back to
   `https://api.dexcost.io`. `http://localhost` and
   `http://127.0.0.1` (any port/path) are still accepted for local
   mock servers. Customers pointing the SDK at an HTTP staging URL
   must switch to HTTPS or use a localhost mock.

3. **TypeScript default flush interval (P5)**
   `flushIntervalMs` default changed from **30 000 → 5 000** ms
   (5 s, matches Python). Customers will see costs in the control
   plane 6× sooner; the control plane sees 6× more push traffic per
   tracker instance. Pass `flushIntervalMs: 30_000` to keep the old
   cadence.

4. **TypeScript decimal accumulation (B3)**
   Per-task cost totals are now exact under high-volume accumulation
   (e.g. 10 000 × 1.23e-8 → exactly 0.000123). Pre-fix the SDK drifted
   by ~2e-16 per add. Customer dashboards that compared against
   spot-check totals may notice the fractional-cent change.

5. **GPU `gpu_seconds_used` semantics (B2, all 4 SDKs)**
   Python/Go/TS pre-fix reported wall-time × 100% utilization as SM
   time. Post-fix integrates `Σ sm_util × dt` across the NVML sample
   sequence — accurate but typically **lower** than the pre-fix
   number. Rust was already correct; the pin commit documents the
   formula. Customers' GPU cost will drop to its true level.

6. **Go `Fargate` vs ECS-EC2 classification (Fix 3)**
   ECS-EC2 tasks are no longer misclassified as Fargate. Customers
   running ECS-on-EC2 will see their `billing_model` flip from
   `fargate` to `ec2`, with the corresponding (lower) cost.

### Security

- **B1 URL credential scrubbing** — every site where a URL flows
  into an event payload now passes through `scrub_url`. Strips
  userinfo (`user:pass@`) and known-sensitive query parameters
  (api_key, access_token, AWS SigV4 family) before storage. Active
  on all 4 SDKs with byte-identical fixtures.

### Crash prevention

- **B4 Rust async-pricing panic** — `tokio::sync::RwLock` →
  `parking_lot::RwLock`. The synchronous pricing API no longer
  panics when called from inside a Tokio runtime.
- **B7 Go panic paths** — `RequireFromString` runtime sites now
  parse-or-zero-and-log; `NewRetryHeuristicEngine` returns an error
  instead of panicking; `mustTracker()` returns nil + log instead
  of panicking, and `*core.TrackedTask` methods are nil-receiver-safe.
- **B8 TypeScript** — `better-sqlite3` is now an optional peer
  dependency. When unavailable (Vercel Edge, Cloudflare Workers, Bun
  without bindings) the SDK falls back to a Map-based in-memory
  buffer (10 k cap) and `init()` returns normally.
- **B10 Python** — `init()` is idempotent (second call returns the
  existing tracker with a warning) and registers
  `os.register_at_fork(after_in_child=...)` so child processes get a
  fresh SQLite connection and SyncWorker.
- **Goroutine `recover()`** — every detached goroutine in the Go
  SDK now goes through `safego.Go(name, fn)` which wraps a top-level
  `defer recover()`. Panics in cloud probes / pricing-refresh
  tickers no longer crash the customer's process.
- **Poisoned-lock recovery** — Rust `cloud_detect::get_cloud_env`
  no longer panics when a writer thread panicked mid-update.

### Correctness — math + data

- **B11 Streaming response handling (Python)** — the HTTP adapter
  no longer drains chunked / SSE / unknown-Content-Length response
  bodies. Customers iterating LLM streaming responses with
  `iter_content` / `iter_lines` no longer get an empty stream.
- **B12 Pusher partial-success accounting (Go, TS, Rust)** — split
  payloads now mark events synced at each leaf POST. A
  first-half-succeeds-second-half-fails sequence no longer re-sends
  the first half (no duplicates at the control plane).
- **B13 Go SessionManager race** — single locked find-or-create.
  100 concurrent callers with the same identity now get one
  session, not 2–4.
- **B14 `set_api_key` for auth recovery (all 4 SDKs)** —
  `dexcost.set_api_key(new_key)` / `dexcost.SetAPIKey(new_key)` /
  `dexcost.setApiKey(new_key)` / `EventPusher::set_api_key(...)`
  let customers rotate the API key without restarting the process.
- **§3.1.3 compute math fixes (Python)** — `memory.peak` now
  per-task instead of cgroup-lifetime; vCPU counter resets emit
  `cost_confidence="estimated"` instead of a silent zero.
- **§3.1.3 fix 5 (Rust)** — `total_cost_usd` recomputation in
  `record_cost` / `record_usage` / LLM record paths now uses the
  5-subsystem sum (llm + external + compute + network + gpu)
  instead of dropping network + gpu.
- **B5 + B5b (Rust)** — IaaS-share `billing_model` discriminator
  aligned with Python/Go canonical (`ec2`, `gce`, `azure_vm`,
  `k8s_pod` — no `_share` suffix). `price_iaas_share` math now
  matches Python.
- **B6 (Go schema)** — `gpu_cost` and `gpu_utilization_signal`
  added to the event_type enum; `network_cost_usd` and `gpu_cost_usd`
  added to the task field set.
- **B9 TypeScript flush on exit** — `init()` registers
  `beforeExit`, `SIGTERM`, `SIGINT` handlers that await
  `closeAsync()`. Events recorded just before `process.exit(0)`
  reach the control plane instead of being lost.

### Parity reconciliation

- **P2 LLM cost map sync** — all 4 SDKs now ship a byte-identical
  cost map (md5 `78d13aea...`, 2591 models). Drift gated in CI by
  `scripts/check_cost_map_drift.sh`.
- **P3 PricingSource enum** — canonical 8-value set
  (`litellm`, `tokencost`, `provider_response`, `manual`, `custom`,
  `rate_registry`, `service_catalog`, `unknown`) aligned across all
  4 SDKs.
- **P4 Network-event config fields** — Go/TS/Rust now expose
  `network_event_threshold_bytes`, `network_event_on_error`,
  `network_event_latency_ms` matching Python's defaults. Wiring
  into each SDK's HTTP adapter is deferred to a follow-on commit.

### TypeScript runtime support (§4.2)

- Fetch double-patch detection via `Symbol.for("dexcost.patched")` —
  duplicate `trackHttp()` and duplicate-SDK installs no longer
  infinite-recurse.
- Atomic `node:http` / `node:https` patching — partial failure on
  frozen modules rolls back installed wrappers.
- Runtime JSON loads (`fs.readFileSync` + `JSON.parse` instead of
  Node 22+ `with { type: "json" }`) for Node 18 compat.
- `engines.node` set to `>=18.0.0` in `package.json`.

### Architecture — bounded memory (A3, Sprint 4 §5.2)

Hard FIFO caps added to previously-unbounded module-level stores:

- Python `adapters.browser._recorded_events` — 10 000 entries.
- TS `adapters/http.ts::_recordedEvents` + `browser.ts::_recordedEvents` — 10 000.
- Rust `adapters/http.rs::RECORDED_EVENTS` — 10 000.
- Rust `core/heuristics.rs::RetryHeuristicEngine.recent_events` — 1 000 per task.
- Go `session.go::SessionManager.sessions` — 10 000 active sessions
  (LRU eviction on top of the existing idle-timeout reaper).

### Not in this release

The plan's Sprint 4 ship-readiness items below are tracked for the
next milestone — they require infrastructure that doesn't fit a
single-PR change:

- A1 async write-behind buffering (architectural refactor; 5-day
  per-SDK piece).
- Load-test report (p99 < 1 ms at 10 k events/sec per SDK).
- 72 h soak test (real wall-clock time).
- External security audit.
- TypeScript dual ESM/CJS build (tsup/unbuild pipeline).
- Per-language medium backlog (~18 items across Python/Go/TS/Rust,
  enumerated in commit `c1d87a7`).

### Audit findings closed in this release

| Finding | Description | Status |
|---------|-------------|--------|
| B1  | URL credential leak | ✅ |
| B2  | GPU SM-time math (4 SDKs) | ✅ |
| B3  | TS decimal accumulation | ✅ |
| B4  | Rust async-pricing panic | ✅ |
| B5  | Rust ec2_share discriminator | ✅ |
| B5b | Rust IaaS share math (discovered during B5) | ✅ |
| B6  | Go schema gpu enum + task fields | ✅ |
| B7-1a | Go mustTracker panic | ✅ |
| B7-1c | Go RequireFromString runtime panics | ✅ |
| B7-1d | Go NewRetryHeuristicEngine bad config | ✅ |
| B8  | TS better-sqlite3 fallback (audit-min + 10 k in-memory buffer) | ✅ |
| B9  | TS flush on exit | ✅ |
| B10 | Python init() idempotency + fork safety | ✅ |
| B11 | Python streaming response | ✅ |
| B12 | Pusher partial-success (Go, TS, Rust) | ✅ |
| B13 | Go session manager race | ✅ |
| B14 | set_api_key auth recovery (4 SDKs) | ✅ |
| A2  | DEXCOST_ENDPOINT allow-list (4 SDKs) | ✅ |
| A3  | Unbounded growth (highest-impact stores) | ✅ |
| §2.2.5 | Go goroutine recover() | ✅ |
| §2.2.6 | Rust poisoned RwLock | ✅ |
| §3.1.3 | Compute math fixes (Python + Go Fargate + Rust 5-subsystem) | ✅ |
| P1  | Timestamp canonicalization | ✅ |
| P2  | LLM cost map sync | ✅ |
| P3  | PricingSource enum | ✅ |
| P4  | Network event config fields | ✅ (fields only; wiring deferred) |
| P5  | TS flush interval default | ✅ |
| §4.2.1 | Fetch double-patch | ✅ |
| §4.2.2 | Frozen http/https atomic patch | ✅ |
| §4.2.3 | Node 18 JSON loads | ✅ (dual build deferred) |
| §4.3 | Per-language medium backlog | partial — top items in commit `c1d87a7`, rest tracked |
| A1  | Async write-behind buffering | deferred to next milestone |
| A4  | Monkey-patching coexistence | partial via §4.2.1 fetch symbol |
