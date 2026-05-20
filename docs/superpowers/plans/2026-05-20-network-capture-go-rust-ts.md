# Network Capture (v1+v2) — Go / Rust / TypeScript Ports — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the Python SDK's network capture (v1 — bytes-only) and egress pricing (v2 — `network_cost_usd` from a bundled catalog + cloud detection) to the Go, Rust, and TypeScript SDKs. One unified plan; per-SDK task sets share the design contracts but use language-idiomatic implementations.

**Reference specs (already approved, applied to Python on this branch):**
- `docs/superpowers/specs/2026-05-19-network-capture-design.md` (v1 — bytes, per-host breakdown, `network` event type, suppression flag)
- `docs/superpowers/specs/2026-05-20-network-cost-v2-design.md` (v2 — egress pricing, dual-invoice attribution Decision #7, cloud detection)

**Reference implementation (Python — already shipped on branch `claude/network-capture-cost-attribution-bEszJ`):**
- v1 plan: `docs/superpowers/plans/2026-05-19-network-capture-python.md` (11 tasks)
- v2 plan: `docs/superpowers/plans/2026-05-20-network-cost-v2-python.md` (10 tasks)
- Code paths to mirror: `python/src/dexcost/network_accountant.py`, `python/src/dexcost/adapters/http.py`, `python/src/dexcost/egress_pricing.py`, `python/src/dexcost/cloud_detect.py`, `python/src/dexcost/data/egress_prices.json` (shared across all SDKs), `python/src/dexcost/tracker.py:_aggregate_costs`.

**Tech stacks:**
- Go: 1.22+, `net/http`, stdlib `sync.Mutex`, `context.Context`, `os.ReadFile` for DMI, `encoding/json`, `github.com/shopspring/decimal`.
- Rust: 1.80+, `reqwest` + `reqwest-middleware` (already in use), `tokio::task_local!`, `tokio::sync::Mutex`, `rust_decimal::Decimal`, `serde_json`, `std::fs::read_to_string` for DMI.
- TypeScript: Node 20+, `globalThis.fetch` patch (already in use), `AsyncLocalStorage`, `Decimal.js` (preferred) or `bigint`-string for cost precision (verify which is already used).

**Test runners:**
- Go: `cd go && go test ./...`
- Rust: `cd rust && cargo test`
- TypeScript: `cd typescript && pnpm test` (vitest)

---

## 0a. Decisions Log (locked 2026-05-20)

The seven open items from the planning conversation. Each is now fixed; if a decision needs to change, file a separate change request — don't mutate this section mid-implementation.

| # | Decision | Final answer | Rationale |
|---|---|---|---|
| 1 | New providers — strict-mirror or per-SDK freedom? | **Strict mirror.** New providers (env-vars, DMI rules, IMDS endpoints, catalog entries) land in Python first and propagate to Go/Rust/TS via PR. | Cross-SDK parity testability. Customer attribution stays consistent regardless of SDK choice. |
| 2 | Rust streaming-body byte counting (SSE / chunked responses without `Content-Length`) | **Spike-then-decide.** During Rust Task 7, spend ≤ 1 hour reading reqwest 0.12 `Response`/`Body` API. If a stream-interception pattern is clean (`Body::wrap_stream` + reconstructed `Response`, or `tokio_util::InspectReader` via `StreamReader`), implement it. If the API doesn't support it cleanly, ship Rust v2 with the Content-Length-only gap **documented loudly** in the Rust adapter's module docstring and a follow-up issue opened. Do not block the rest of the Rust port on this. | Streaming LLM responses are the dominant AI workload; undercounting them is a real correctness issue. But there's a real chance reqwest 0.12 can't support clean response-body interception in middleware. Time-boxed spike is the honest path. |
| 3 | Runtime target for the TypeScript SDK | **Node 20+ on Linux/macOS/Windows is the target.** Bun runtime works via Node compat (no special code paths). Deno is not supported (the existing `node:` imports already preclude it). Package managers (npm / pnpm / yarn / `bun install`) are orthogonal. | The TS SDK already uses `node:async_hooks` / `node:crypto` — Node is the established target. Bun's Node compat handles `node:fs` for DMI reads. DMI is Linux-only on all platforms; macOS/Windows hosts no-op silently and fall through to Phase 2 IMDS. |
| 4 | Decision #7 dual-invoice test — mandatory? | **Mandatory per SDK.** Each of Go/Rust/TS must include the explicit pinning test from `test_network_cost_dual_invoice.py` (cataloged vendor call with 0.5 GB response → exactly one `external_cost` event + `task.external_cost_usd == $0.01` + `task.network_cost_usd == $0.045` + event `cost_usd` unchanged). No opt-out. | Executable spec of the most subtle architectural call (v2 §3.3 + Decision #7). Silently dropping the egress half of vendor-call cost is the failure mode this test prevents. |
| 5 | Phase B leader — which SDK ports the accountant + adapter first? | **Rust.** | Surfaces the streaming-body decision (item 2) earliest. If reqwest 0.12's API can't support clean stream interception, we know before Go and TS effort compounds. |
| 6 | Execution model | **Parallel subagents for Phase A only** (Tasks 1, 2, 4 — types, schema, pure helpers — trivial ports per SDK). Then the primary agent sequences Phase B (Rust → Go → TS) since adapter wiring interacts with each SDK's HTTP layer differently. | Phase A is mechanical work with clean SDK boundaries; parallelization is safe. Phase B has cross-cutting decisions (suppression-flag wiring into each LLM instrument, body counting wrappers) that benefit from one agent's continuity. |
| 7 | Catalog distribution — how do the 4 SDKs stay in sync when each is installed separately? | **Canonical file in Python; sync script copies to the other three; CI check enforces parity; each SDK bundles its own local copy in its published artifact.** Canonical: `python/src/dexcost/data/egress_prices.json`. Sync script: `scripts/sync_egress_catalog.sh` (writes by default, `--check` mode for CI). Bundling: Python wheel via `hatch.build.targets.wheel`, Rust `include_str!`, TS `package.json files`, Go `//go:embed`. | `pip install dexcost` / `cargo add dexcost` / `npm install dexcost` / `go get` each only ship the SDK's own tarball — a shared file at the repo root would be invisible to installed packages. Four local copies + a sync script + CI guard is the standard monorepo pattern. Each SDK works offline at runtime; no network call, no shared path. |

---

## 0. Pre-flight — Verify shared contracts

These items are non-negotiable invariants from the design specs. If any SDK already violates one, fix it before the v1 work.

| Invariant | Spec ref | Verification step per SDK |
|---|---|---|
| Per-event suppression flag (≤1 event per HTTP call) | v1 §5.3 | Search for context-scoped boolean in `core/context.*` |
| `is_internal_traffic` classifier returns `true`/`false`/`null` | v1 §4.2 | Confirm IP-only classification (no DNS lookup) |
| Decimal arithmetic for cost (no float) | v2 §6.3 | Confirm existing `*_cost_usd` uses Decimal-as-string in serialization |
| `update_event` re-marks sync_status pending | v2 §6.4 + §8.2 | Verify storage UPDATE statement |
| The shared `egress_prices.json` is the single source of truth | v2 §10.4 | The Python file at `python/src/dexcost/data/egress_prices.json` is canonical. Each SDK either reads it from a relative path or vendors it during build. See §1 below. |

---

## 1. Shared egress catalog — `egress_prices.json` distribution

Per Decision #7 in the log above: **canonical file is in Python; a sync script copies it to the other three SDKs; CI enforces parity; each SDK bundles its own local copy in its published artifact** (so `pip install` / `cargo add` / `npm install` / `go get` each work standalone — no shared paths at runtime, no network calls).

### Implementation order

- [ ] **Step 1:** Create `scripts/sync_egress_catalog.sh` at repo root:

```bash
#!/usr/bin/env bash
set -euo pipefail
CANONICAL="python/src/dexcost/data/egress_prices.json"
TARGETS=(
  "rust/src/data/egress_prices.json"
  "typescript/src/data/egress_prices.json"
  "go/pricing/data/egress_prices.json"
)
MODE="${1:---write}"   # --check (CI) or --write (default, local)
for target in "${TARGETS[@]}"; do
  if [[ "$MODE" == "--check" ]]; then
    if ! cmp -s "$CANONICAL" "$target"; then
      echo "::error::$target is out of sync with $CANONICAL"
      echo "Run: bash scripts/sync_egress_catalog.sh"
      exit 1
    fi
  else
    mkdir -p "$(dirname "$target")"
    cp "$CANONICAL" "$target"
    echo "synced → $target"
  fi
done
```

- [ ] **Step 2:** Run `bash scripts/sync_egress_catalog.sh` once to populate the three target files.

- [ ] **Step 3:** Add a CI step that runs `bash scripts/sync_egress_catalog.sh --check` on every PR. Wire into the existing GitHub Actions workflow (find it under `.github/workflows/`).

- [ ] **Step 4:** Bundle the file in each SDK's published artifact:
  - **Rust** — already works via `include_str!("../data/egress_prices.json")` because `src/data/` is inside the crate src tree.
  - **TypeScript** — verify the `package.json` `files` array includes `src/data/**/*.json` (or the equivalent path); add a build step that copies `src/data/` into `dist/data/` so `import` from the published package resolves correctly.
  - **Go** — already works via `//go:embed data/egress_prices.json` because `go/pricing/data/` is inside the package.

- [ ] **Step 5:** Each SDK still ships its own integrity test (mirroring Python's `test_egress_catalog_integrity.py`) — JSON parseability, Decimal-string rate validation, `_last_verified` freshness check. The SHA-equality parity test is **not needed at runtime** because the sync script + CI guard already enforce it; the per-SDK integrity test guards against partial bundling failures.

---

## 2. Language-idiomatic mapping table

This is the canonical translation reference. The per-SDK task lists below assume these idioms.

| Concern | Python (reference) | Go | Rust | TypeScript |
|---|---|---|---|---|
| HTTP interception | `wrapt` monkey-patch on `requests` / `httpx` / `aiohttp` | `http.RoundTripper` wrapper (already in `go/adapters/http.go`) | `reqwest_middleware::Middleware` (already in `rust/src/adapters/reqwest_middleware.rs`) | Monkey-patch `globalThis.fetch` (already in `typescript/src/adapters/http.ts`) |
| Per-task accumulator (mutable) | `NetworkAccountant` with `threading.Lock` | `*NetworkAccountant` with `sync.Mutex` | `Arc<Mutex<NetworkAccountant>>` (use `std::sync::Mutex`, contention is microseconds) | `NetworkAccountant` class (single-threaded event loop — no lock) |
| Per-host map | `dict[str, list[int]]` | `map[string][4]int64` | `HashMap<String, [u64; 4]>` | `Map<string, [number, number, number, number]>` |
| Context-scoped suppression flag | `contextvars.ContextVar[bool]` | `context.WithValue(ctx, suppressKey{}, true)` | `tokio::task_local! { static SUPPRESS_NETWORK_EVENT: bool; }` | `AsyncLocalStorage<boolean>` |
| Cloud detection — env vars | `os.environ.get` | `os.LookupEnv` | `std::env::var` | `process.env[name]` |
| DMI file read | `open("/sys/class/dmi/id/<field>")` | `os.ReadFile("/sys/class/dmi/id/<field>")` | `std::fs::read_to_string("/sys/class/dmi/id/<field>")` | `fs.readFile` (async) — Node only, no-op on Bun/Deno/browser |
| IMDS HTTP probe | `urllib.request` with timeout | `http.NewRequestWithContext` + `context.WithTimeout(250ms)` | `reqwest::Client::builder().timeout(Duration::from_millis(250))` | `fetch(url, { signal: AbortSignal.timeout(250) })` |
| Background detection thread | `threading.Thread(daemon=True)` | `go func()` (no need for daemon flag — process exit kills) | `tokio::spawn` (background task) | `void detect()` — Promise that runs in the background, never awaited |
| Decimal arithmetic | `decimal.Decimal` | `decimal.Decimal` (`shopspring/decimal` — already used) | `rust_decimal::Decimal` (already used) | TBD per existing SDK — verify whether `Decimal.js` or string math is used |
| GB conversion divisor | `Decimal("1000000000")` | `decimal.NewFromInt(1_000_000_000)` | `Decimal::from(1_000_000_000_u64)` | `1_000_000_000n` (BigInt) or `new Decimal("1000000000")` |
| Daemon thread "never blocks init" | `start_background_detection()` returns < 10 ms | `go func() { ... }()` (synchronous Phase 1a + 1b, async Phase 2) | `tokio::spawn(async move { ... })` | Bare `void` async IIFE; promise not awaited |
| Storage / event back-fill | `SQLiteStorage.update_event` | Whatever the Go SDK uses for event sync (verify per task) | Same for Rust (no SQLite yet — `EventBuffer` in memory) | Same for TS (`EventBuffer` in memory) |

**Rust note:** v2 says "use `tokio::sync::Mutex`" in spec §5.2 but the accountant is hit from sync code paths inside reqwest middleware too — use `std::sync::Mutex` instead (contention is microseconds, no `.await` inside). Update spec wording in the Rust plan task 6.

**TypeScript note:** No threading model — single-threaded event loop means the accountant lock is a no-op. Keep the lock-acquire/release call sites in the code as comments for cross-SDK code review parity, but use a plain object.

---

## 3. Per-SDK task list

Each SDK gets the same 10-task structure mirroring the Python v1 + v2 plans, with language-specific notes per task. **Tasks 1-7 are v1 (bytes-only); tasks 8-10 are v2 (egress pricing).**

For each task below, the format is:
- **Files** — exact paths to create/modify
- **Steps** — TDD red→green→commit cycle with idiomatic code snippets

### Task 1 — `network` event type

**Goal:** Add `"network"` to the event-type enum so emitted events validate against the schema.

**Per-SDK files:**
| SDK | Type enum | Schema JSON |
|---|---|---|
| Go | `go/core/models.go` (find `EventType` constants) | `go/schema/dexcost-event.v1.json` (verify path with `ls go/schema/`) |
| Rust | `rust/src/core/models.rs` (find `EventType` enum) | `rust/src/schema/dexcost-event.v1.json` |
| TS | `typescript/src/core/models.ts` (find `EventType` type union) | `typescript/src/schema/dexcost-event.v1.json` |

**TDD:**
1. Write a test asserting `EventType.NETWORK == "network"` (or the language equivalent).
2. Verify FAIL.
3. Add the enum member and update the schema's `event_type.enum` array.
4. Verify PASS.
5. Commit.

---

### Task 2 — Four network fields on `Task` + schema

**Goal:** Add `network_bytes_in / network_bytes_out / network_call_count / network_by_host` to the Task model. `network_by_host` is `{"hosts": [...]}` JSON (array, capped at 20 entries + `_other`).

**Per-SDK shape:**
| SDK | Type for `network_by_host` |
|---|---|
| Go | `map[string]interface{}` (matches existing JSON-blob columns) |
| Rust | `serde_json::Value` (matches existing pattern) |
| TS | `Record<string, unknown>` |

**TDD:**
1. Test: default `Task` has `network_bytes_in == 0`, `network_by_host == {"hosts": []}`.
2. Test: round-trip `ToDict` / `to_dict()` / `JSON.stringify` preserves the four fields.
3. Verify FAIL.
4. Add the fields with defaults; extend `ToDict`/`to_dict`/serialization map.
5. Extend `dexcost-task.v1.json` schema with the four fields (NOT in `required` — old payloads predate).
6. Verify PASS.
7. Commit.

---

### Task 3 — Storage / buffer schema (SQLite for Python only)

**Important per-SDK divergence:**
- **Go** — No SQLite buffer in the Go SDK today. Network fields live on `Task` in memory + ship over wire via `ToDict`. Skip migration. Verify the in-memory `EventPusher` / buffer passes the four fields through.
- **Rust** — Same: in-memory `EventBuffer`, no SQLite. Skip migration.
- **TypeScript** — Same: in-memory `EventBuffer` (`typescript/src/transport/buffer.ts`). Skip migration.

> Confirmation step per SDK: `grep -r "SQLite\|sqlite" <sdk>/` should return zero hits (or only matches in unrelated tests). If any SDK has added SQLite since this plan was written, add a v0→v1 migration task following the Python pattern.

---

### Task 4 — `_netbytes` helpers (classifier + byte measurement)

**Goal:** Port `python/src/dexcost/adapters/_netbytes.py` exactly. Two pure functions, no I/O.

**Per-SDK files:**
- Go: `go/adapters/netbytes.go` (new) + `go/adapters/netbytes_test.go`
- Rust: `rust/src/adapters/netbytes.rs` (new) — re-export from `adapters/mod.rs`
- TS: `typescript/src/adapters/_netbytes.ts` (new)

**Function 1 — `classify_destination(host string) (*bool, error)` / Rust `Option<bool>` / TS `boolean | null`:**

Pure IP classification, no DNS lookup. RFC1918 / loopback / link-local → `true`; public IP literal → `false`; named host or invalid → `null`.

Idiomatic implementations:
- **Go:** `net.ParseIP(host)` → check `IsPrivate() || IsLoopback() || IsLinkLocalUnicast()`. Returns `nil` when `ParseIP` returns `nil`. **Watch out:** Go's `net.IP.IsPrivate` (added in Go 1.17) covers RFC1918 + the IPv6 ULA range. Add a CGNAT comment matching Python's behaviour.
- **Rust:** `host.parse::<std::net::IpAddr>()` → `.is_private() || .is_loopback() || .is_link_local()`. **Note:** Rust's `IpAddr::is_private` was stabilized in 1.77; check `Cargo.toml` MSRV. If MSRV < 1.77, use the `unstable_ip` feature or open-code RFC1918 check.
- **TS:** No built-in IP classifier. Either depend on `ipaddr.js` (already a transitive dep of many fetch libs) or open-code RFC1918/loopback/link-local check via string parsing. **Recommended:** open-code, ~20 lines, no new dep.

**Function 2 — `measure_bytes_from_headers(method, url, headers, body_len)`:**

Approximate on-the-wire size. `request_line + header_block + body`. Header block = sum of `len(key) + len(value) + 4` per header + 2 trailing CRLF.

All three SDKs: trivial port of the Python function, ~10 lines.

**TDD:** Unit tests for both functions covering (i) IPv4 private/public/loopback, (ii) IPv6 ULA/link-local, (iii) named hostnames → null, (iv) empty string → null, (v) header byte counting against known fixtures.

---

### Task 5 — `NetworkAccountant`

**Goal:** Port `python/src/dexcost/network_accountant.py` including the v2 `external_bytes_out` split.

**Per-SDK files:**
- Go: `go/adapters/network_accountant.go` + test
- Rust: `rust/src/adapters/network_accountant.rs` + tests
- TS: `typescript/src/adapters/network_accountant.ts` + test

**API contract (identical across SDKs, language-idiomatic naming):**
```
Constructor: NewNetworkAccountant() / NetworkAccountant::new() / new NetworkAccountant()

Record(host string, bytesIn, bytesOut int64, isInternal *bool)
LiveHostCount() int
Finalize() NetworkSnapshot  // freezes; subsequent Record is no-op

NetworkSnapshot fields:
  bytes_in, bytes_out, external_bytes_out, call_count
  by_host: { hosts: [{host, calls, bytes_in, bytes_out, external_bytes_out}, ...] }
```

**Per-SDK locking:**
- Go: `sync.Mutex` (`accountant.mu.Lock(); defer accountant.mu.Unlock()`)
- Rust: `std::sync::Mutex<NetworkAccountantInner>` wrapped in `Arc` so the accountant is cheaply cloneable across the reqwest-middleware boundary
- TS: no lock (event loop is single-threaded — concurrent record from two async branches is impossible without `await`)

**Constants:**
- `FINALIZE_CAP = 20` (top-N hosts surfaced in `by_host`)
- `LIVE_CAP = 500` (overflow folds into `_other` bucket)

**Top-20 selection rule:** sort by `bytes_in + bytes_out` descending. Tie-break: stable order (insertion order).

**Critical v2 detail:** every host entry carries `external_bytes_out` so per-host egress cost survives the cap. The `_other` overflow bucket also carries `external_bytes_out`. The scalar `external_bytes_out` returned at the top level is the canonical truth — the sum of all per-host external bytes equals this scalar (property invariant from Python plan §10.3).

**`record(host="_other", ...)` edge case:** if a real host is literally named `_other`, fold it into the synthetic overflow bucket so the output never has two entries with the same name.

**Frozen-after-finalize:** the `_frozen` boolean blocks late writes after `finalize()`.

**TDD:** Mirror the Python `test_network_accountant.py` and `test_network_accountant_external.py` tests exactly — same scenarios, same numbers. Parametrize the 15-shape invariant test from `test_network_cost_invariants.py` (host_count ∈ {1, 5, 20, 100, 1000} × is_internal ∈ {true, false, null}).

---

### Task 6 — Suppression flag (context-scoped)

**Goal:** Port the `is_network_event_suppressed()` / `suppress_network_event()` pair from `python/src/dexcost/context.py:103-120`. LLM instruments wrap their HTTP call so bytes still count but the standalone `network` event is withheld.

**Per-SDK files:**
- Go: extend `go/core/context.go` with `WithSuppressNetworkEvent(ctx) context.Context` + `IsNetworkEventSuppressed(ctx) bool`
- Rust: add to `rust/src/core/context.rs`: `tokio::task_local! { static SUPPRESS: bool; }` + `with_suppress_network_event(future)` + `is_suppressed()`
- TS: add to `typescript/src/core/context.ts`: separate `AsyncLocalStorage<boolean>` plus a `suppressNetworkEvent(fn)` helper

**Then wire it into every LLM instrument** so each provider call runs inside the suppression scope:
- Go: every wrapper client (`WrapOpenAI`, `WrapAnthropic`, etc.) sets the flag on the request `ctx` before issuing the HTTP call. `go/clients/` — verify exact filenames.
- Rust: every instrument's HTTP call inside `with_suppress_network_event(...).await`. `rust/src/clients/` — verify.
- TS: every instrument (`typescript/src/instruments/*.ts`) wraps the fetch call in `suppressNetworkEvent(() => fetchedCall)`.

**TDD per SDK:** one test per LLM provider: instrument the call, fire it, assert the recorded events for that call contain exactly one `llm_call` and zero `network` events.

---

### Task 7 — HTTP adapter — byte accounting + re-typed un-cataloged calls

This is the heart of v1. Mirrors `python/src/dexcost/adapters/http.py:_handle_http_call_inner` + the three branch handlers.

**Existing adapter layout to extend (verified above):**
- Go: `go/adapters/http.go:484` — `RoundTripper` that wraps Transport. The cost paths are inside the `RoundTrip` method. Add byte measurement around the catalog/domain-rate dispatch.
- Rust: `rust/src/adapters/reqwest_middleware.rs:421` — the active `Middleware` (the passive `record_http_cost` in `http.rs` is for SDK consumers; keep it but don't add byte accounting there). Byte measurement goes around the existing cost dispatch.
- TS: `typescript/src/adapters/http.ts:512` — the patched `globalThis.fetch`. Byte measurement around the existing cost dispatch.

**Steps (same for all three SDKs):**

1. **Compute `is_internal_traffic` once** at the top of the per-call handler from the parsed host.
2. **Measure bytes** — call `measure_bytes_from_headers` for both request and response. For response: read from the response's headers when `Content-Length` is present, otherwise wrap the response body in a counting reader.
3. **Attribute to the resolved task** via the existing `_resolveTask()` helper. If no task: no-op (existing behaviour — never create orphan rows).
4. **Call `task._network.Record(host, bytes_in, bytes_out, is_internal)`** before the cost dispatch.
5. **Three dispatch branches** (preserve existing order):
   - (a) Domain rate match → emit `external_cost` event, stamp `request_bytes`/`response_bytes`/`protocol`/`is_internal_traffic` into `details`.
   - (b) Catalog match → same.
   - (c) Un-cataloged → if suppressed (LLM call), counters-only. Otherwise check threshold (`network_event_threshold_bytes`, default 102_400 = 100 KiB) OR `status >= 400` (when `network_event_on_error=true`) OR latency trigger. Emit a `network` event with `cost_usd=0`, `cost_confidence="unknown"`, `details["cost_pending"] = true`, and the byte fields.

**Counting reader wrappers per SDK:**

- **Go** — wrap the `http.Response.Body` in a `countingReadCloser` that delegates to the inner `io.ReadCloser` and increments an atomic counter on each `Read`. The caller still drains the body normally (or doesn't — early-abort is fine, we count actually-received bytes per v1 §5.5). Idiom:
  ```go
  type countingReadCloser struct {
      io.ReadCloser
      n *int64
  }
  func (c *countingReadCloser) Read(p []byte) (int, error) {
      m, err := c.ReadCloser.Read(p)
      atomic.AddInt64(c.n, int64(m))
      return m, err
  }
  ```

- **Rust (reqwest)** — reqwest's `Response::bytes_stream()` already exposes byte chunks. **But** the middleware sees the response BEFORE the user has streamed it; wrapping requires intercepting the stream. The cleanest pattern: if `Content-Length` is present, use it directly (lossy but accurate for non-streaming bodies). For streaming responses without `Content-Length`, fall back to a chunk-counting wrapper — but `reqwest` v0.12's `Response` doesn't trivially allow body replacement. **Recommended v1:** read `content-length` header only, document the streaming-body gap as a known limitation. Address in a follow-up.

- **TS (fetch)** — `Response.body` is a `ReadableStream<Uint8Array>`. **Two viable patterns:**
  1. **Tee the body**: `const [a, b] = response.body.tee()`; consume `b` in a counter, hand `a` to the user. Memory cost is unbounded (must buffer one branch) — not viable for streaming.
  2. **`TransformStream` interceptor**: `response.body.pipeThrough(new TransformStream({ transform(chunk, controller) { counter += chunk.byteLength; controller.enqueue(chunk); }}))`. Zero buffering, full streaming, accurate count. This is the right pattern.
  ```ts
  function wrapBodyForCounting(res: Response, counter: { bytes: number }): Response {
    if (!res.body) return res;
    const counting = new TransformStream({
      transform(chunk: Uint8Array, controller) {
        counter.bytes += chunk.byteLength;
        controller.enqueue(chunk);
      },
    });
    return new Response(res.body.pipeThrough(counting), res);
  }
  ```

**Body-length 1 MB cap:** existing adapters already cap cost-extraction body parsing at 1 MB. The byte counter counts the full body beyond 1 MB without buffering (v1 spec §6.4). Make sure the new counting wrapper does NOT inherit the 1 MB cap.

**TDD per SDK:**
- Unit tests: each of the three dispatch branches with mocked transports.
- Integration tests: full HTTP roundtrip with a mock server; un-cataloged above-threshold → `network` event; below-threshold → counters only; cataloged → bytes stamped in `external_cost`; LLM-suppressed → bytes counted, no `network` event.
- Double-count regression: hit an endpoint via each instrumented client (Go's `http.Client`, Rust reqwest, TS `fetch`) and assert exactly ONE byte-count update per call (Python pinned this against urllib3-nesting; the Go/Rust/TS equivalent is "the same middleware doesn't fire twice on retries").
- Zero-call task: a task with no HTTP calls ships the four fields as `0 / 0 / 0 / {"hosts": []}`.

---

### Task 8 — `egress_pricing` — resolver + degradation ladder

**Goal:** Port `python/src/dexcost/egress_pricing.py`. Same 5-tier degradation ladder, same warn-once-per-failure-mode discipline.

**Per-SDK files:**
- Go: `go/pricing/egress_pricing.go` + test
- Rust: `rust/src/pricing/egress_pricing.rs` + tests
- TS: `typescript/src/pricing/egress-pricing.ts` + test

**API:**
```
type EgressRate { rate_per_gb Decimal, pricing_source string, cost_confidence string }
NewEgressPricingEngine(catalogPath optional) → engine
engine.resolve_rate(provider, region) → EgressRate
engine.rate_for_internal() → EgressRate{0, "egress_catalog:internal", "exact"}
engine.catalog_version → string
```

**5-tier ladder (identical to Python §7.1):**
1. `(provider, region)` exact catalog match → region rate, `computed`
2. Provider known, region missing → provider default, `estimated`
3. Provider unknown → `_meta.default_rate_usd_per_gb`, `estimated`
4. Catalog unreadable / malformed / meta default missing → hardcoded `Decimal("0.09")`, `estimated` + WARN_ONCE
5. (Tier 5 lives in the task-finalize step — see Task 10)

**Catalog loading per SDK:**
- Go: `//go:embed data/egress_prices.json` into a `string` constant, parsed once at engine construction.
- Rust: `include_str!("../../data/egress_prices.json")` parsed once into `serde_json::Value` at construction.
- TS: `import egressPricesJson from "../data/egress_prices.json" with { type: "json" }` (or `require("./data/egress_prices.json")` depending on existing pattern — check `typescript/src/pricing/cost_map.json` import to match).

**Warn-once-per-failure-mode:**
- Go: package-level `sync.Map[string]struct{}` keyed by failure-mode token (`catalog_missing`, `catalog_malformed`, `meta_default_missing`, `region_rate_malformed:<prov>:<region>`). Reset helper exposed for tests.
- Rust: `LazyLock<Mutex<HashSet<String>>>` + a `pub(crate) fn reset_warning_state()` test helper.
- TS: module-level `Set<string>` + `_resetWarningState()` test helper.

**TDD:** Mirror Python's `test_egress_pricing.py` exactly (12 tests). Includes `test_decimal_no_float_drift` — assert `Decimal("0.1093") * Decimal("1000000000") == Decimal("109300000.0000")` and `Decimal("0.087") * Decimal("12345678") == Decimal("1074073.986")`. **Critical:** never use floating-point GB conversion. The divisor is `1_000_000_000` (decimal GB), not `1 << 30` (binary GiB).

---

### Task 9 — `cloud_detect` — non-blocking provider/region detection

**Goal:** Port `python/src/dexcost/cloud_detect.py` faithfully — the version on this branch that just passed the deep-research audit. **All env-var names, DMI strings, and IMDS endpoints have been verified against May-2026 docs; do NOT change them.**

**Per-SDK files:**
- Go: `go/cloud/cloud_detect.go` + test (new package — `cloud` doesn't conflict with `core`)
- Rust: `rust/src/cloud_detect.rs` (top-level module) + tests
- TS: `typescript/src/cloud-detect.ts` + test

**Phase 1a — Environment variables (verified May 2026):**

| Provider | Env vars (verified) |
|---|---|
| Modal | `MODAL_TASK_ID` OR `MODAL_IMAGE_ID`; region from `MODAL_REGION` |
| RunPod | `RUNPOD_POD_ID` OR `RUNPOD_POD_HOSTNAME`; region from `RUNPOD_DC_ID` |
| Render | `RENDER` OR `RENDER_SERVICE_ID`; no region env var |
| Railway | `RAILWAY_PROJECT_ID` OR `RAILWAY_ENVIRONMENT_ID`; region from `RAILWAY_REPLICA_REGION` (NOT `RAILWAY_REGION`) |
| Heroku | `DYNO` |
| Koyeb | `KOYEB_SERVICE_NAME` OR `KOYEB_APP_NAME`; region from `KOYEB_REGION` |
| Fly.io | `FLY_REGION` OR `FLY_APP_NAME`; region from `FLY_REGION` |
| Vercel | `VERCEL` OR `VERCEL_REGION`; region from `VERCEL_REGION` |
| AWS | `AWS_LAMBDA_FUNCTION_NAME` OR `AWS_EXECUTION_ENV` OR `ECS_CONTAINER_METADATA_URI_V4` OR `ECS_CONTAINER_METADATA_URI` OR `AWS_REGION` OR `AWS_DEFAULT_REGION`; region from `AWS_REGION` ?? `AWS_DEFAULT_REGION` |
| Azure | `WEBSITE_SITE_NAME` OR `FUNCTIONS_WORKER_RUNTIME` OR `CONTAINER_APP_NAME`; region from `REGION_NAME` ?? parsed-from-`CONTAINER_APP_HOSTNAME` ?? parsed-from-`CONTAINER_APP_ENV_DNS_SUFFIX` |
| GCP | `K_SERVICE` OR `K_CONFIGURATION` OR `GAE_ENV` OR `FUNCTION_TARGET` OR `FUNCTION_NAME`; no region env var (Phase 2) |

**Detection priority — match Python order exactly:** Modal → RunPod → Render → Railway → Heroku → Koyeb → Fly → Vercel → AWS → Azure → GCP. Earlier matches win (Vercel runs on AWS but should surface as Vercel; Modal runs on AWS/GCP/OCI but should surface as Modal).

**Azure Container Apps region parsing** — regex: `\.([a-z0-9-]+)\.azurecontainerapps\.io$` (case-insensitive) applied to `CONTAINER_APP_HOSTNAME` or `CONTAINER_APP_ENV_DNS_SUFFIX`.

**Phase 1b — DMI rules (verified against cloud-init `ds-identify`, May 2026):**

Field-aware rules, ordered canonical-first:

```
chassis_asset_tag == "oraclecloud.com"                        → oci
chassis_asset_tag == "7783-7084-3265-9085-8269-3286-77"       → azure
product_name      == "google compute engine"                  → gcp
product_name      == "alibaba cloud ecs"                      → alibaba
sys_vendor        == "amazon ec2"                             → aws
sys_vendor        == "digitalocean"                           → digitalocean
sys_vendor        == "hetzner"                                → hetzner
sys_vendor        == "vultr"                                  → vultr
sys_vendor        == "scaleway"                               → scaleway
sys_vendor        == "microsoft corporation"                  → azure
sys_vendor   contains "amazon"                                → aws  (looser backup)
sys_vendor   contains "google"                                → gcp  (looser backup)
sys_vendor   contains "alibaba cloud"                         → alibaba
sys_vendor   contains "ovh"                                   → ovh
```

DMI fields read: `sys_vendor`, `board_vendor`, `product_name`, `chassis_asset_tag`, `bios_vendor`, `product_serial`. All from `/sys/class/dmi/id/<field>`. Missing files silently skipped (non-Linux hosts have none of these).

**Phase 2 — Metadata probes (verified endpoints + headers):**

| Provider | URL | Method | Headers | Response parse |
|---|---|---|---|---|
| AWS | `http://169.254.169.254/latest/api/token` | PUT | `X-aws-ec2-metadata-token-ttl-seconds: 21600` | body = token; then GET `…/latest/meta-data/placement/region` with `X-aws-ec2-metadata-token: <token>` |
| GCP | `http://metadata.google.internal/computeMetadata/v1/instance/region` (preferred) → fall back to `…/instance/zone` | GET | `Metadata-Flavor: Google` | strip `projects/<num>/regions/` from /region; strip `projects/<num>/zones/` + trailing `-<letter>` from /zone |
| Azure | `http://169.254.169.254/metadata/instance?api-version=2021-02-01` | GET | `Metadata: true` | JSON parse, read `.compute.location` |
| OCI | `http://169.254.169.254/opc/v2/instance/canonicalRegionName` | GET | `Authorization: Bearer Oracle` | body = full canonical region (us-phoenix-1) — NOT `/region` which returns abbreviated codes |
| DigitalOcean | `http://169.254.169.254/metadata/v1/region` | GET | (none) | body = region |
| Alibaba | `http://100.100.100.200/latest/meta-data/region-id` | GET | (none) | body = region |

**Probe timeout:** 250 ms per HTTP call. Worst-case wall time = 250 ms for the parallel fanout (NOT 3× serial).

**Fanout strategy:** when provider is known (from env or DMI) → probe only that provider's endpoint. When unknown → fan out AWS / GCP / Azure in parallel (the major 3); first success wins. OCI, DigitalOcean, Alibaba only run when DMI pre-classifies — they share AWS's IP (169.254.169.254) and would hit the wrong endpoint on a vanilla AWS host.

**Never-blocks-init contract:**
- Go: `cloud.StartBackgroundDetection()` returns immediately after running Phase 1a + 1b synchronously (< 1 ms total). Phase 2 runs in a `go func() {}()` goroutine that calls `cloud.SetResult(env)` on completion.
- Rust: `cloud_detect::start_background_detection()` returns immediately after Phase 1a + 1b. Phase 2 launches via `tokio::spawn`. **If the SDK doesn't have a Tokio runtime at init time** (rust SDK init is sync), use `std::thread::spawn` for the probe and a blocking reqwest client with the 250ms timeout.
- TS: `startBackgroundDetection()` runs Phase 1a + 1b synchronously, fires Phase 2 as a fire-and-forget Promise: `void runProbeAsync()`.

**Wire into `init()`:**
- Go: `dexcost.Init(opts)` calls `cloud.StartBackgroundDetection()` if `opts.TrackNetwork` is enabled.
- Rust: `dexcost::init(...)` same.
- TS: `dexcost.init(opts)` same.

**TDD:** Port Python's `test_cloud_detect.py` (42 tests) faithfully — same fixtures, same provider names. The fixture pattern in TypeScript should mock `process.env` directly; Go via `t.Setenv`; Rust via `temp_env::with_var` (or a manual snapshot pattern). **Critical tests to include:**
- DMI rule-ordering: chassis_asset_tag canonical wins over sys_vendor backup (`test_dmi_canonical_field_wins_over_backup`)
- Phase 2 fanout limited to AWS/GCP/Azure (`test_phase2_runs_only_aws_gcp_azure_in_parallel`)
- OCI uses `canonicalRegionName` (`test_oci_probe_uses_canonical_region_name`)
- GCP prefers `/region` over `/zone` (`test_gcp_probe_prefers_region_endpoint`)
- init never blocks (`test_init_never_blocks_when_metadata_unreachable`)
- Negative case: laptop with `sys_vendor=LENOVO` returns `none` (`test_dmi_unknown_vendor_returns_none`)

---

### Task 10 — Task finalize — `network_cost_usd` + per-event back-fill

**Goal:** At task end, compute `task.network_cost_usd` from the accountant's canonical scalar, stamp `egress_cost_usd` into each `network_by_host` entry, and back-fill every `network` event for the task with its computed cost.

**Per-SDK files:**
- Go: extend `go/core/tracker.go` (find the existing `aggregateCosts` or equivalent — `grep -n "aggregateCosts\|_aggregate_costs" go/core/`)
- Rust: extend `rust/src/core/tracker.rs` similarly
- TS: extend `typescript/src/core/tracker.ts` similarly

**Pseudocode (language-idiomatic adaptation):**
```
def aggregate_costs(task):
    # ... existing LLM / external / compute aggregation ...
    snapshot = task._network.finalize()
    task.network_bytes_in = snapshot.bytes_in
    task.network_bytes_out = snapshot.bytes_out
    task.network_call_count = snapshot.call_count

    try:
        env = cloud_detect.get_cloud_env()
        rate = egress_pricing.resolve_rate(env.provider, env.region)
        external_gb = Decimal(snapshot.external_bytes_out) / Decimal("1000000000")
        task.network_cost_usd = external_gb * rate.rate_per_gb
        version = f"egress:{egress_pricing.catalog_version}"

        for host in snapshot.by_host.hosts:
            host_gb = Decimal(host.external_bytes_out) / Decimal("1000000000")
            host["egress_cost_usd"] = str(host_gb * rate.rate_per_gb)
        task.network_by_host = snapshot.by_host

        for event in events_for_task(task):
            if event.event_type != "network":
                continue
            billable_bytes = (
                0 if event.details.is_internal_traffic == true
                else int(event.details.request_bytes or 0) + int(event.details.response_bytes or 0)
            )
            event_gb = Decimal(billable_bytes) / Decimal("1000000000")
            event.cost_usd = event_gb * rate.rate_per_gb
            event.cost_confidence = (
                "exact" if event.details.is_internal_traffic == true
                else rate.cost_confidence
            )
            event.pricing_source = (
                "egress_catalog:internal" if event.details.is_internal_traffic == true
                else rate.pricing_source
            )
            event.pricing_version = version
            delete(event.details, "cost_pending")
            buffer.update_event(event)
            task.total_cost_usd += event.cost_usd  # network events were $0 in the LLM/external pass

        task.total_cost_usd += task.network_cost_usd
    except Exception as e:
        # Tier 5 — fail-silent, log warning, task still ships
        log.warning("egress cost computation failed for task %s: %s", task.task_id, e)
        task.network_cost_usd = Decimal("0")
        task.network_by_host = snapshot.by_host
```

**Per-SDK storage-update equivalent for "back-fill the network event":**
- Go: the in-memory buffer at `go/transport/` or the equivalent — find where events are stored and add an `UpdateEvent(event)` method that replaces by ID and re-marks sync state if applicable.
- Rust: same — extend `EventBuffer`.
- TS: same — extend `EventBuffer` in `typescript/src/transport/buffer.ts`.

**`total_cost_usd` arithmetic:** must include `network_cost_usd` AND the back-filled per-event network costs. The Python implementation adds them in two phases (per-event in the loop, then the task-level scalar) — replicate exactly.

**Tier 5 fail-silent:** if anything in the egress block throws, log a warning and ship the task with `network_cost_usd = 0`. The task's `llm_cost_usd` / `external_cost_usd` / `compute_cost_usd` MUST be unaffected.

**TDD:** Mirror Python's `test_network_cost_finalize.py` (8 tests) + `test_network_cost_invariants.py` (15 parametrized cases) + `test_network_cost_dual_invoice.py` (Decision #7 explicit test).

The **Decision #7 dual-invoice test is non-negotiable** for each SDK — it pins the spec's most subtle contract:
> A cataloged-vendor call MUST produce exactly ONE event (`external_cost`) AND populate both `task.external_cost_usd` (vendor charge) AND `task.network_cost_usd` (cloud egress on the same bytes). The event's own `cost_usd` stays unchanged at the vendor charge — no egress dollars stamped on it.

---

## 4. Cross-SDK consistency tests

After all three SDKs land, add a cross-SDK consistency test in CI:

**Catalog parity** — already covered in §1 (SHA-256 of bundled `egress_prices.json` matches Python).

**Schema parity** — the four task fields + the `network` event-type enum must appear in each SDK's `dexcost-task.v1.json` / `dexcost-event.v1.json`. A test in each SDK can hash those files and compare against the Python schemas.

**Property invariant parity** — each SDK's invariant suite uses the same parametrization (5 host counts × 3 classification modes). The numbers should match: `task.network_cost_usd == external_bytes_out / 10^9 × rate` for the same inputs.

---

## 5. Phased rollout

Recommended execution order:

1. **Phase A — Foundation (parallel across SDKs):** Tasks 1, 2, 4 — types, schema, helpers. Trivial ports; safe to do all three in parallel.

2. **Phase B — Accountant + adapter (per-SDK, sequential):** Tasks 5, 6, 7. Do Go first (fewest async surfaces), then Rust (verify the reqwest middleware integration), then TS (TransformStream interceptor — this is the most novel pattern).

3. **Phase C — Egress pricing (parallel across SDKs):** Tasks 8, 9. Both have clean boundaries; safe to parallelize.

4. **Phase D — Finalize (per-SDK, sequential):** Task 10 — pins the per-event update path which differs per SDK's buffer implementation.

5. **Phase E — Cross-SDK parity:** §4 above. After all three land.

---

## Self-Review — Spec coverage map

| Spec section | Where covered in this plan |
|---|---|
| v1 §4.1 Task fields | Task 2 |
| v1 §4.2 `network` event + `is_internal_traffic` | Task 1, 7 |
| v1 §4.3 uniform byte placement | Task 7 (the `byte_details` spread) |
| v1 §4.4 emission rule | Task 7 |
| v1 §4.5 schema changes | Task 1, 2 |
| v1 §5.1 NetworkAccountant + measure | Task 4, 5 |
| v1 §5.2 per-language thread-safety | Task 5 (per-SDK locking column) |
| v1 §5.3 ≤1-event invariant | Task 6 (suppression flag) + Task 7 (emission gate) |
| v1 §5.4 per-call flow | Task 7 |
| v1 §5.5 streaming | Task 7 (TS TransformStream; Go countingReadCloser; Rust documented limitation) |
| v1 §6.1 fail-silent | Task 7 (try/swallow around byte measurement) |
| v1 §6.2 no-task no-op | Task 7 (existing `_resolveTask` already returns nil) |
| v1 §6.3 double-count guard | Task 7 (regression matrix tests) |
| v1 §6.5 live cap | Task 5 (`LIVE_CAP=500`) |
| v1 §6.7 snapshot-and-freeze | Task 5 (`_frozen`) |
| v1 §7 tests | All tasks have TDD steps |
| v2 §2 Decision #1 (egress only) | Task 5 (external_bytes_out keys off `is_internal`) |
| v2 §2 Decision #2 (zero user config) | Task 9 (no new init knobs) |
| v2 §2 Decision #3 (no per-event egress on llm/external) | Task 10 (back-fill touches only `network` events) |
| v2 §2 Decision #4 (deferred per-event cost) | Task 7 (`cost_pending=true` at emission) + Task 10 (back-fill) |
| v2 §2 Decision #5 (4-valued confidence) | Task 8 (engine returns `computed`/`estimated`/`exact`) |
| v2 §2 Decision #6 (first-tier rates) | Task 8 (catalog `_meta.notes`) |
| v2 §2 Decision #7 (dual-invoice) | Task 10 (explicit pinning test) |
| v2 §3 measurement/pricing separation | Task 10 |
| v2 §4 catalog | §1 (shared file across SDKs) |
| v2 §5 cloud detection | Task 9 |
| v2 §6 cost computation | Task 10 |
| v2 §7 degradation ladder | Task 8 (Tier 1-4) + Task 10 (Tier 5 try/except) |
| v2 §8 schema/migration | Task 2 (schema only — no SQLite in Go/Rust/TS) |
| v2 §9 config interaction | Task 9 (track_network gate) |
| v2 §10.1-10.3 tests | Tasks 5, 8, 9, 10 |
| v2 §10.4 cross-language matrix | §4 (catalog SHA, schema SHA, invariant parity) |

**Known follow-ups (out of scope for this plan):**
- Rust streaming-body byte counting (Task 7) — see Decisions Log #2. The 1-hour spike inside the Rust port determines whether it ships in v2 or v2.1.
- IBM Cloud / Lambda Labs / Vast.ai / CoreWeave / Cloudflare / Replicate / Netlify env-var detection — these are in the catalog at $0 (or $0.05 for CoreWeave) but lack canonical env-var signals. Detection paths land if/when the provider documents them.
- DMI file reads are Linux-only across every SDK and every supported runtime. macOS/Windows hosts no-op silently and fall through to Phase 2 IMDS. No special-casing per runtime needed (per Decisions Log #3).

---

## Sources consulted (May 2026)

- [Go `http.RoundTripper`](https://pkg.go.dev/net/http) — Transport interception pattern
- [Go AWS SDK IMDS docs](https://pkg.go.dev/github.com/aws/aws-sdk-go-v2/feature/ec2/imds) — IMDSv2 timeout / context pattern
- [Rust `reqwest::Response`](https://docs.rs/reqwest/latest/reqwest/struct.Response.html) — `bytes_stream()` chunk iteration
- [Rust `reqwest-middleware`](https://crates.io/crates/reqwest-middleware) — middleware response interception
- [Rust `google-cloud-metadata`](https://crates.io/crates/google-cloud-metadata) — `on_gce()` precedent for IMDS detection
- [Node.js Web Streams API](https://nodejs.org/api/webstreams.html) — `TransformStream` reference for byte counting
- [Node.js `AsyncLocalStorage`](https://nodejs.org/api/async_context.html) — context propagation idiom (already used in TS SDK)
- [cloud-init `ds-identify`](https://raw.githubusercontent.com/canonical/cloud-init/main/tools/ds-identify) — canonical DMI fingerprints for OCI/Alibaba/DigitalOcean/Hetzner/Vultr
- [OCI metadata docs](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm) — `/canonicalRegionName` endpoint
- [Cloud Run container contract](https://docs.cloud.google.com/run/docs/container-contract) — `/instance/region` vs `/instance/zone` on Cloud Run
- [AWS Lambda envvars](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html) — `AWS_REGION` / `AWS_EXECUTION_ENV` definitive list
- All other env-var sources (Modal / RunPod / Render / Railway / Heroku / Koyeb / Fly / Vercel / Azure Container Apps) — documented in the Python `cloud_detect.py` module docstring and verified during the May 2026 audit on this branch.
