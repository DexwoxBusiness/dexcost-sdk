# Network / Egress Capture — Design Spec

**Date:** 2026-05-19
**Status:** Approved design — ready for implementation planning
**Sub-project:** A of 5 (network capture). The other four — B compute, C GPU,
D data-platform/warehouse, E catalog updates — are independent and each get
their own spec.

## 1. Summary

Network/egress is the #2 unexpected AI cost driver in 2025 industry research
(52% of companies), behind only data-platform usage. The dexcost HTTP adapter
already sees every outbound call from instrumented code; today it records a cost
only when the destination is in the vendor catalog.

This work extends the adapter to also record **bytes** — for every call, on
every task — and to emit a new `network` event for un-cataloged high-volume
calls. **v1 records bytes only**; the dollar egress cost (bytes × per-GB rate ×
region modifier) lands after subsystem B (cloud/runtime detection) exists and
can resolve the cloud and region.

It extends the **existing HTTP adapter** — which already monkey-patches the HTTP
transports, intercepts responses, and resolves the active task — so byte
accounting hooks in where that code already sits.

## 2. Context

**Current state (verified against `adapters/http.py`).** The SDKs capture LLM
costs, non-LLM vendor costs (163-service catalog), and MCP costs. The HTTP
adapter patches `requests`, `httpx`, `aiohttp`, `botocore`, and `urllib3`. It
emits an `external_cost` event for **every** instrumented call — a priced one
for service-catalog / domain-rate matches, and a placeholder
`cost_usd=0, pricing_source="none"` one for un-cataloged calls. It does **not**
measure bytes.

Two current behaviours this work corrects:

- **Un-cataloged calls emit a noise `external_cost $0` row.** This work re-types
  them: above the byte threshold they become `network` events; below it they
  drop to counters-only — removing meaningless $0 rows from vendor-spend queries.
- **LLM API calls double-emit.** An OpenAI call produces an `llm_call` event
  (LLM instrument) *and* an `external_cost $0` event (HTTP adapter —
  `api.openai.com` is un-cataloged). The "≤1 event per HTTP call" invariant
  (§5.3) eliminates this pre-existing duplicate.

**Why network.** In the Mavvrik/Benchmarkit *State of AI Cost Governance*
research, network/egress was the #2 unexpected AI cost driver (52%); LLM tokens
ranked 5th. The cost surface is broader than tokens — and the HTTP adapter
already sees every response, so byte capture is the highest value per unit of
effort.

## 3. Decisions

| Decision | Choice | Reason |
|---|---|---|
| Mechanism | Extend the existing HTTP adapter (Approach 1) | Adapter already intercepts responses and resolves the task; consistent coverage with current vendor capture |
| v1 deliverable | Bytes only; dollar egress cost deferred | Pricing egress needs cloud/region context — that is subsystem B |
| Rollout | One design; build + validate in Python first, then port Go / Rust / TS one at a time | De-risks the design before 4× replication |

**Coverage:** instrumented HTTP(S) traffic only — the same surface as today's
vendor capture. gRPC, raw sockets, and DB-driver traffic are out of scope for
v1 (see §8).

## 4. Data Model

### 4.1 Task — four new fields

| Field | Type | Meaning |
|---|---|---|
| `network_bytes_in` | int | Total response bytes received across the task's HTTP calls |
| `network_bytes_out` | int | Total request bytes sent |
| `network_call_count` | int | Number of instrumented HTTP calls |
| `network_by_host` | JSON (own column) | Per-host breakdown — see below |

`network_by_host` is an **array of objects** (not a map — arrays flatten better
for downstream columnar queries):

```json
{ "hosts": [
  { "host": "api.firecrawl.dev", "calls": 340, "bytes_in": 9_120_400, "bytes_out": 41_200 },
  { "host": "api.openai.com",   "calls": 12,  "bytes_in": 48_211,    "bytes_out": 3_204 },
  { "host": "_other",           "calls": 4,   "bytes_in": 2_104,     "bytes_out": 410 }
] }
```

- Capped to the **top 20 hosts by total bytes**; the rest fold into `_other`.
- `_other.calls` / `_other.bytes_*` are **sums** across all folded hosts — not a
  host count.
- Empty case serializes to `{"hosts": []}` — never `null`.

### 4.2 New `network` event type

Represents one notable HTTP call to an **un-cataloged** host.

```
event_type:      "network"
service_name:    "api.somerandom.com"          # the host
cost_usd:        0                              # v1 — bytes only
cost_confidence: "unknown"                      # dollars not yet known
details: {
  protocol:            "https",                # future: "grpc", "postgres", ...
  url:                 "https://api.somerandom.com/v1/...",
  method:              "POST",
  request_bytes:       1204,
  response_bytes:      48201,
  status_code:         200,
  is_internal_traffic: false                    # true | false | null
}
```

- `protocol` ships in v1 (only `"http"`/`"https"` occur today). Future non-HTTP
  capture becomes a purely additive change — no schema rev.
- `is_internal_traffic` — three-valued:
  - `true` — confirmed RFC1918 / localhost / link-local destination
  - `false` — confirmed public IP
  - `null` — could not be determined (named host, and the SDK declined an extra
    DNS lookup to find out)

  Set in v1 (free now) so the future dollar layer can price it with no
  migration: `true` → $0, `false` → public egress rate, `null` → public egress
  rate at `cost_confidence: "estimated"` (conservative — see §11).

### 4.3 Byte placement is uniform across event types

`details.request_bytes`, `details.response_bytes`, and `details.protocol` appear
on **both** `network` events **and** `external_cost` events for HTTP calls — so
the byte-aggregation logic is one function regardless of event type.

### 4.4 Emission rule

Every call always feeds the task counters. What *else* happens:

| HTTP call | Task counters + `by_host` | Per-call record |
|---|---|---|
| Cataloged host (any size) | ✅ | bytes stamped into the `external_cost` event it already emits — no new event |
| Un-cataloged, **above** threshold | ✅ | new `network` event |
| Un-cataloged, **below** threshold | ✅ | none — counters only |
| LLM-provider host | ✅ | none — the `llm_call` event already represents it (see §5.3) |

The byte threshold gates **only** new `network` events. Cataloged calls always
get bytes stamped (the event exists anyway, regardless of size).

> The "above threshold" row is a simplification. An un-cataloged call **below**
> the byte threshold still emits a `network` event when it errors
> (`status >= 400`) or trips the latency trigger — see the full rule in §5.4.

> **Behaviour change.** Today un-cataloged calls emit an `external_cost $0`
> event. Under this design they emit a `network` event instead (or
> counters-only when below threshold and not an error). `test_http_adapter.py`
> cases asserting un-cataloged → `external_cost` are updated to expect
> `network`. This is deliberate — it removes $0 noise from vendor-spend.

### 4.5 Schema changes

- `tasks` table — 4 new columns + a migration (same pattern as the recent
  `sync_status` migration).
- `dexcost-task.v1.json` — gains the 4 fields.
- `dexcost-event.v1.json` — the `event_type` enum gains `"network"`.

## 5. Components & Flow (SDK side)

### 5.1 Components

1. **`NetworkAccountant`** — a per-task in-process accumulator living on the
   `TrackedTask` (lifecycle, context resolution, and the aggregate-pattern all
   already align there). Holds bytes in/out, call count, and a
   `host → {calls, bytes_in, bytes_out}` map. `record(host, in, out)` updates it;
   `finalize()` applies the top-20 + `_other` cap and returns the array.
2. **Byte measurement** — `measure_request_bytes` (request line + headers + body
   length) and `measure_response_bytes` (status line + headers + body;
   `Content-Length` when present, else a counting reader wrapper).
3. **Emission logic** — runs once per call, inside the adapter's existing
   request+response interception point.
4. **Config** — `track_network` (default on; only meaningful when `track_http`
   is on), `network_event_threshold_bytes` (default `102400` — i.e. 100 KiB;
   compared against **combined `request_bytes + response_bytes`**),
   `network_event_on_error`
   (default true — always emit on `status >= 400`), `network_event_latency_ms`
   (default 0 = disabled; when > 0, emit when call latency exceeds it).

### 5.2 Per-language thread-safety

The `by_host` map can be written by concurrently-completing HTTP calls within
one task. Locking strategy per SDK:

| SDK | Strategy |
|---|---|
| Python | `threading.Lock` around map updates (covers mixed sync/threadpool + async) |
| TypeScript | none — single-threaded event loop |
| Go | `sync.Mutex` around the map |
| Rust | `tokio::sync::Mutex` (consistent with the rest of the SDK) |

Contention is microsecond-scale (writes only; no reads until `finalize`).

### 5.3 The "at most one event per HTTP call" invariant

A single HTTP call must produce **at most one** of `{llm_call, external_cost,
network}`. This is a structural invariant, not an LLM-host list.

Implementation — a **context-scoped per-call suppression flag**:
- The catalog matcher (inside the HTTP adapter) sets the flag when it emits an
  `external_cost` event — same scope, a local check suffices.
- The **LLM instruments** set the flag around their HTTP call. The LLM patcher
  (`openai.chat.completions.create`, etc.) and the HTTP adapter
  (`httpx.Client.send`) are **different monkey-patch layers** — outer and inner
  — so the flag must be context-scoped (contextvar / AsyncLocalStorage /
  context value / task-local), not an adapter local. This is a ~2-line addition
  to each LLM instrument.
- The `network`-event decision is gated on
  `not other_event_emitted AND (bytes > threshold OR status >= 400 OR latency trigger)`.

LLM-host bytes still count toward the task counters and `by_host` — only the
standalone `network` *event* is suppressed.

### 5.4 Flow (per HTTP call)

1. Adapter intercepts → does its existing catalog/cost work (may emit an
   `external_cost` event and set the suppression flag).
2. Measure request + response bytes.
3. Resolve the active task → `accountant.record(host, in, out)` (always —
   lossless counters).
4. **If** an `external_cost` event was emitted for this call → stamp
   `request_bytes`/`response_bytes`/`protocol` into its `details`.
5. **Else if** the suppression flag is unset **and**
   ( `request_bytes + response_bytes > network_event_threshold_bytes`
   OR `status >= 400`
   OR ( `network_event_latency_ms > 0` AND `latency_ms > network_event_latency_ms` ) )
   → insert a `network` event.
6. On `task.end()` → the existing aggregation step calls `accountant.finalize()`
   and writes the 4 fields onto the Task.
7. The existing pusher syncs the `network` / `external_cost` events and the
   task-with-aggregates.

### 5.5 Streaming responses

The counting reader wraps the **transport stream** — between the HTTP transport
and the SDK's iterator — so the count is accurate even if the caller never
drains the stream. On **early-abort** (timeout / cancel), the counter holds the
bytes *actually received*, which may differ from what the vendor bills; this is
documented so reconciliation accounts for it.

## 6. Error Handling & Edge Cases

1. **Fail-silent, always.** All accounting/measurement/emission is wrapped so an
   exception never breaks the customer's HTTP call. Each swallowed exception
   bumps an in-memory error counter exposed via `dexcost status` — so silent
   capture failure becomes observable instead of hidden.
2. **No active task** → no-op: no counters, no event. Same rule as today's
   adapter; anonymous traffic never creates orphan rows.
3. **Ride the existing double-count guard.** The adapter already patches
   `urllib3` *and* `requests`/`botocore` (which use urllib3 internally) and
   guards double-counting with the `_in_patched_call` flag. Byte measurement is
   invoked from the **same code path, at the same outermost gate** where cost
   recording happens today — never per nested layer. (This guard is a
   pre-existing adapter invariant network accounting depends on — see §10.)
4. **Streaming counter ≠ the 1 MB body-parse cap.** The adapter caps body
   *parsing* at 1 MB for cost extraction; the byte *counter* counts the full
   body beyond 1 MB as a streaming increment, never buffering it. Separate
   concerns.
5. **Live `by_host` memory bound.** The 20-entry cap applies at task end. To
   bound memory mid-task, the in-process map has a generous **live cap of ~500
   hosts**; the 501st and later unique hosts fold into `_other` continuously
   (hard cap — not LRU eviction; LRU is a possible later refinement, and the
   data model is forward-compatible with it).
6. **Live-cap / per-call-emission are decoupled.** A call to the 501st host
   still gets its own `network` event if it crosses the threshold — the live
   cap affects `by_host` aggregation only, never the per-call emission decision.
7. **Snapshot-and-freeze at `task.end()`.** Task end snapshots the accumulator
   into the task aggregates and freezes it; `record()` on an ended task no-ops.
   No late-arriving bytes mutate already-shipped aggregates.
8. **Process exit mid-task.** The accumulator is in-memory; a crashed task loses
   its network counters along with the rest of its state — exactly as
   `llm_cost_usd` / `total_input_tokens` already behave. Not a new failure mode.

## 7. Testing (per SDK — Python first)

**Unit**
- Byte measurement — known request/response → exact counts; `Content-Length`
  present vs. absent (counting wrapper).
- Emission triggers — just-below / just-above the byte threshold;
  `status >= 400` always-emit; latency trigger when enabled.
- `by_host` finalize — top-20 selection, `_other` summation, empty →
  `{"hosts": []}`.
- Live cap — 501st host folds into `_other`; **a heavy 501st-host call still
  emits a `network` event** (the §6.6 decoupling).
- `is_internal_traffic` — private-IP / localhost / link-local → `true`; public →
  `false`; named host with no peer IP → `null`.
- Streaming — full body counted beyond 1 MB; early-abort → bytes-actually-received.
- Lifecycle — `record()` after `task.end()` no-ops.
- Zero-call task — a task with no HTTP calls ships the four network fields as
  `0 / 0 / 0 / {"hosts": []}`: present, never absent/`null` (locks §9 contract
  bullet 5 — some serializers omit zero-value fields).

**Double-count regression matrix (critical).** One test each for `requests`,
`httpx`, `aiohttp`, `botocore` — assert **exactly one** byte-count update per
call. Locks out the urllib3-nesting 2× bug permanently.

**Integration** — a call through the adapter against a mock server: task
counters correct; un-cataloged above-threshold → `network` event with correct
`details`; below-threshold → counters only; cataloged → bytes in the
`external_cost` event, no `network` event; LLM call → bytes counted, **no
`network` event** (the §5.3 invariant); ended task ships with the 4 network
fields populated.

**Regression** — existing **cataloged** cost-capture tests unchanged.
Un-cataloged-call tests are updated to expect a `network` event instead of an
`external_cost $0` event (the deliberate behaviour change — see §4.4).

## 8. Boundaries / Non-Goals (v1)

- **No dollar egress cost.** Bytes only; `network` events are `cost_usd: 0` /
  `cost_confidence: "unknown"`. The dollar layer lands after subsystem B.
- **HTTP(S) only.** No gRPC, raw sockets, or DB-driver traffic — the same
  coverage as today's vendor capture. The `protocol` field makes that extension
  purely additive later.
- **No process-level / cgroup network totals** — that belongs to subsystem B.
- **No cross-region detection** — part of the deferred dollar layer.
- **SDK side only** — see §9.

## 9. Control Layer Dependencies

This spec covers the **SDK capture side**. The Control Layer (ingest, ClickHouse
aggregation, reconciliation) is a separate repo and a companion workstream.

**SDK → Control Layer contract** (what the SDK guarantees):

1. Every `network` event has a unique `event_id` (UUIDv4).
2. Task aggregates (`network_bytes_in/out`, `network_call_count`,
   `network_by_host`) are computed in-process at task end and shipped on the
   task upsert.
3. The SDK never re-sends a `network` event with a different `event_id` for the
   same underlying HTTP call — so dedup by `event_id` is safe.
4. `network_by_host` arrives pre-capped at 20 entries plus `_other`.
5. `network_by_host` is always an array — never `null`/absent; `{"hosts": []}`
   for the empty case.
6. For any HTTP call, **at most one** of `{llm_call, external_cost, network}`
   events is emitted.

Everything downstream of the ingest endpoint — incremental-aggregation
idempotency under retry, the `network_by_host` JSON-merge, columnar storage
layout — is Control Layer scope and not implemented here.

## 10. Pre-Requisite

The double-count guard (§6.3) is a **pre-existing adapter invariant**, not new
work. Before this lands, audit it across all four SDKs and confirm it exists and
behaves consistently (Python thread-local, TypeScript `AsyncLocalStorage`, Go
context value, Rust `tokio::task_local`). If any SDK lacks the guard, that is a
latent **cost-data** double-count bug to fix first — surfaced by this spec, but
fixed as its own change, not bundled into network capture.

## 11. Future (out of scope here, recorded for continuity)

- **Dollar egress layer** (after subsystem B): `network` events get a real
  `cost_usd` from an egress-rate catalog × destination region; `cost_confidence`
  moves to `computed`/`estimated`. `is_internal_traffic` pricing: `true` → $0,
  `false` → public egress rate, `null` → public egress rate at `estimated`
  confidence (conservative default — see §4.2).
- **Non-HTTP protocols**: gRPC / DB-driver capture reuses the `network` event
  type with a new `protocol` value — additive, no schema rev.
