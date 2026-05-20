# Network Cost (v2) — Egress Pricing — Design

**Date:** 2026-05-20
**Status:** Approved design — ready for implementation planning
**Scope:** Python SDK first; Go / Rust / TypeScript ports follow as separate plans.
**Builds on:** `2026-05-19-network-capture-design.md` (v1 — bytes-only network capture, shipped).

---

## 1. Purpose & Scope

v1 of network capture measures request/response **bytes** for every instrumented
HTTP call and attributes them to the active task (`network_bytes_in/out`,
`network_call_count`, `network_by_host`). It deliberately stopped at bytes — no
dollar cost.

v2 turns external **egress** bytes into dollars: a `network_cost_usd` figure per
task, attributed alongside `llm_cost_usd`, `external_cost_usd`, and
`compute_cost_usd`.

**Core principle — stay true to the SDK's nature.** dexcost is automatic and
catalog-driven: LLM cost comes from the bundled `model_cost_map.json`, non-LLM
vendor cost from `service_prices.json`, with **zero user configuration**. Egress
cost works the same way — a bundled, versioned, community-maintainable catalog
plus automatic cloud-environment detection. There are **no new `init()` knobs**
for pricing.

**In scope (Python, this spec):**
- A bundled egress price catalog (`data/egress_prices.json`).
- An egress pricing engine (`egress_pricing.py`).
- Automatic cloud-environment detection (`cloud_detect.py`).
- `Task.network_cost_usd`, computed at task finalize.
- Per-host egress cost in `network_by_host`.
- `network` event `cost_usd` filled with the call's egress cost.
- SQLite schema migration v4 → v5.

**Out of scope (see §11):** Go/Rust/TS ports; monthly-tier pricing; a
reconciliation/dashboard surface; destination-geolocation heuristics.

---

## 2. Decisions (rationale preserved)

These decisions were settled during design review. They are recorded here so a
future maintainer sees *why* without reconstructing the reasoning.

1. **Egress only.** v2 prices outbound bytes (`bytes_out`) only. AWS/GCP/Azure
   charge for data leaving their network; ingress (data-in) is free everywhere.
   Pricing `bytes_in` would not match any real cloud bill.

2. **Catalog-driven, zero user config.** No `egress_cost_per_gb` or
   `cloud_provider` `init()` parameters. The rate comes from a bundled catalog;
   the cloud environment is auto-detected. This preserves the SDK's "one-line
   install, automatic" nature.

3. **No per-event egress on `llm_call` / `external_cost` events.** Egress dollars
   live in exactly one place per task: `Task.network_cost_usd`. Events carry the
   raw **measurement** (bytes, status, latency — already present from v1);
   the task carries the derived **attribution** (dollars). Stamping a separate
   `egress_cost_usd` onto `llm_call` events would create a reconciliation trap:
   `llm_call.cost_usd` would mean "the LLM API charge but not the egress part,
   which is over here and also in the task rollup" — the same dollars with two
   meanings depending on which field you read. Measurement on events, derived
   dollars on the task, keeps each field single-meaning.

4. **Per-event cost is deferred to task finalize**, not computed at emission.
   This keeps the invariant in #3 pure and eliminates per-event-vs-task rate
   drift (see §6.4).

5. **Confidence enum stays four-valued.** The existing `cost_confidence`
   vocabulary is `exact` / `computed` / `estimated` / `unknown`. The egress
   "universal default rate" case maps to `estimated`; the `pricing_source`
   string carries the resolution-step detail (`egress_catalog:aws:us-east-1`
   vs `egress_catalog:aws:default` vs `egress_catalog:default`).

6. **First-tier rates, no monthly tiering.** Cloud egress is billed in
   descending monthly-volume tiers. The SDK has no workspace-wide monthly view —
   only per-task bytes — so the catalog uses the **first (highest) tier** rate.
   This produces conservative *over*-attribution, never a hidden undercount
   (see §4).

7. **Cataloged vendor calls contribute to both `external_cost_usd` and
   `network_cost_usd` — by design, not double-counting.** A single HTTP call to
   a cataloged vendor (Pinecone, Firecrawl, an LLM provider, etc.) generates
   **two distinct real-world invoices** that the customer actually pays:

   - The **vendor's** per-request / per-token / per-unit charge — captured on
     the `external_cost` (or `llm_call`) event's `cost_usd` and aggregated into
     `external_cost_usd` (or `llm_cost_usd`).
   - The **cloud's** egress charge for the bytes that traversed the customer's
     VPC boundary to reach that vendor — aggregated into `network_cost_usd`.

   Pinecone's bill and AWS's egress bill do not refund each other; a 50 MB
   Firecrawl scrape generates both a Firecrawl per-request charge *and* an AWS
   egress charge on the response bytes. Surfacing both is the whole point —
   *not* surfacing the egress half would silently hide the #2 industry-research
   cost driver on exactly the traffic the SDK already sees most clearly. The
   dual attribution is therefore the **structurally correct** outcome, and
   `total_cost_usd = llm + external + compute + network` adds the two invoices
   the customer truly pays. See §3.3 for the per-call event-vs-cost invariants
   that make this work without record duplication, and §10.2 for the test that
   pins this contract.

---

## 3. Architecture & Data Model

### 3.1 New modules

| File | Responsibility |
|---|---|
| `python/src/dexcost/data/egress_prices.json` | Bundled, versioned egress-rate catalog. Community-maintainable by PR, mirroring `service_prices.json`. |
| `python/src/dexcost/egress_pricing.py` | Loads the catalog, resolves a rate from `(provider, region)`, returns rate + `pricing_source` + `cost_confidence`. Mirrors `pricing.py`. |
| `python/src/dexcost/cloud_detect.py` | Detects `(provider, region)` of the host environment. Synchronous env-var + DMI detection at `init()`; background metadata probe. |

### 3.2 Data model changes

- **`Task.network_cost_usd: Decimal`** — new field, sibling of `llm_cost_usd` /
  `external_cost_usd` / `compute_cost_usd`. Default `Decimal("0")`.
- **`Task.total_cost_usd`** becomes `llm + external + compute + network`.
- **`network` event `cost_usd`** — the call's egress cost (no longer always
  `$0`). Computed at task finalize (§6.4), not at emission.
- **`network_by_host[]` entries** gain `egress_cost_usd` — a per-host rollup.
  Lives inside the existing `network_by_host` JSON blob (no new column). For a
  host whose traffic was all internal, this is `0`.
- **`llm_call` / `external_cost` events are unchanged.** Their `cost_usd` keeps
  meaning only the LLM/vendor charge. They already carry `request_bytes` /
  `response_bytes` in `details` from v1 — that is the measurement, and it is
  enough; egress dollars for those calls are attributed via the task rollup and
  `network_by_host`.

### 3.3 The measurement/pricing separation (the central invariant)

> **Events carry raw measurement (bytes, status, latency). The task carries
> derived attribution (the dollar rollup). Per-host dollar attribution lives on
> `network_by_host`, which is itself a task-level aggregate.**

The only event-level dollar amount for egress is on the `network` event — and a
`network` event *is* purely an egress record, so its `cost_usd` is
unambiguous. `llm_call` / `external_cost` events never carry egress dollars.

**Two invariants — distinguish them carefully.** It is easy to read the
"one event per call" rule and slide into a stricter, *wrong* sibling rule.
Only the first is a design invariant; the second is explicitly rejected:

- ✅ **One event per call (correct invariant).** A single HTTP call produces
  **at most one** of `{llm_call, external_cost, network}` (the v1 §5.3
  structural invariant, enforced by the context-scoped suppression flag).
  This prevents record duplication — the call appears as exactly one row in
  the event stream, so downstream `COUNT(*)`/dedup queries are honest.

- ❌ **One cost category per call (rejected — would silently undercount).**
  A single HTTP call may legitimately contribute to **multiple** task-level
  cost aggregates. A cataloged-vendor call produces one `external_cost` event
  (the vendor's per-request charge → `external_cost_usd`) *and* feeds its
  bytes into `external_bytes_out` → `network_cost_usd` (the cloud's egress
  charge — Decision #7). An LLM call does the same with `llm_cost_usd` +
  `network_cost_usd`. Forcing "one cost category per call" would erase the
  cloud-egress half of every vendor-call invoice — exactly the silent
  undercount this work exists to fix.

Put another way: the suppression flag dedups **records**, never **dollars**.
Records describe what happened; dollars describe what the customer owes, and
one HTTP call to a vendor really does cost money on two separate bills.

### 3.4 Consequence: LLM instruments need no changes

Because egress is never stamped on `llm_call` events, there is no
adapter→instrument byte-handoff to build. v2 touches only the HTTP adapter, the
`NetworkAccountant`, the new catalog/pricing/detection modules, and the
task-finalize step. The LLM instrument files are untouched.

---

## 4. The egress catalog (`data/egress_prices.json`)

### 4.1 Structure

Mirrors `service_prices.json` conventions — `_meta` block, then provider keys.

```json
{
  "_meta": {
    "version": "1.0.0",
    "last_updated": "2026-05-20",
    "currency": "USD",
    "default_rate_usd_per_gb": "0.09",
    "description": "Dexcost egress catalog — per-GB internet data-transfer-out rates by cloud provider/region. Community-maintained; submit PRs to add or refresh rates.",
    "notes": "Rates are standard internet data-transfer-out, FIRST pricing tier only. Cloud egress is billed in descending monthly-volume tiers; the SDK has no monthly cumulative view, so it uses the first (highest) tier. Effect: customers exceeding ~10 TB/month of egress on a single cloud may see attributed cost up to ~45% above their actual invoice for their highest-volume tier; customers under the first tier (the majority) see no over-attribution. The universal default_rate_usd_per_gb is AWS us-east-1 first-tier ($0.09/GB) — the modal egress rate across hyperscalers; a deliberate conservative choice so undetected environments over-attribute slightly rather than undercount. Intra-region/internal traffic is free and never priced from this file."
  },
  "aws": {
    "_last_verified": "2026-05-20",
    "default_usd_per_gb": "0.09",
    "regions": { "us-east-1": "0.09", "ap-south-1": "0.1093", "...": "..." }
  },
  "gcp": {
    "_last_verified": "2026-05-20",
    "default_usd_per_gb": "0.12",
    "regions": { "us-central1": "0.12", "...": "..." }
  },
  "azure": {
    "_last_verified": "2026-05-20",
    "default_usd_per_gb": "0.087",
    "regions": { "eastus": "0.087", "...": "..." }
  }
}
```

### 4.2 Encoding & precision

- **All rates are string-encoded Decimals** (`"0.09"`, `"0.1093"`). The loader
  MUST parse them with `Decimal(...)` — never `float(...)`. A unit test
  (`test_decimal_no_float_drift`, §10) pins this.
- **`currency`** is `"USD"` for v1. The field exists so a future
  non-USD catalog has a place to land without a breaking change.

### 4.3 Per-provider freshness

Each provider carries `_last_verified` (ISO-8601 date), distinct from
`_meta.last_updated` (when the file was last edited). This lets a future catalog
refresh job emit per-provider freshness signals — one provider's staleness does
not contaminate trust in the others. The catalog integrity test (§10) asserts
every provider has a parseable `_last_verified` and soft-warns if any is older
than 180 days.

### 4.4 No monthly tiering

The catalog holds only first-tier rates (see Decision #6, §2). This is a
deliberate "honest beats clever" call: a monthly cumulative view would require
either workspace-scoped byte persistence (turning the task-scoped accountant
into a workspace-scoped one) or server-side tiering (breaking the "egress
dollars live on the task" invariant). Neither is worth it. First-tier rates
over-attribute conservatively; §4.1 `_meta.notes` documents the bound (~45%
worst case at very high volume; zero for the majority under the first tier).

### 4.5 Launch coverage (prerequisite, not "grows over time")

For v2 to ship credibly, `egress_prices.json` MUST cover, at launch, **all
commercial regions** of AWS, GCP, and Azure. Each cloud publishes per-region
egress pricing publicly; populating the catalog is a **one-time, manual
data-entry job** for v2 — read each provider's public pricing page, transcribe
rates, human-review pass per provider. No automated scrape/refresh tooling is
assumed to exist or is a dependency of v2; an ongoing catalog-refresh job is
explicitly out of scope (§11). Partial coverage silently degrades a customer in
an uncovered region to the provider-default rate (one confidence step worse)
when an exact match was achievable.

This is tracked as an explicit **launch-prerequisite task in the v2
implementation plan** (`docs/superpowers/plans/`), not deferred to "the catalog
grows as customers report gaps." The §10.1 catalog-integrity test (and its
180-day soft freshness check) is the *ongoing* maintenance signal; this
prerequisite is the *initial* population gate.

### 4.6 Rate resolution order

A discrete three-step ladder — no heuristic fourth step:

1. `(provider, region)` exact catalog match → region rate.
2. Provider known, region absent/unknown → provider `default_usd_per_gb`.
3. Neither → `_meta.default_rate_usd_per_gb`.

(§7 extends this into the full five-tier degradation ladder including catalog
load failure.)

---

## 5. Cloud detection (`cloud_detect.py`)

Resolves `(provider, region)` of the host environment. Must **never block
`init()`** and never hang.

### 5.1 Phase 1a — synchronous env-var detection (sub-millisecond)

| Provider | Detected from |
|---|---|
| AWS | **Provider:** `AWS_EXECUTION_ENV`, `AWS_LAMBDA_FUNCTION_NAME`. **Region:** `AWS_REGION` / `AWS_DEFAULT_REGION`. Lambda and Fargate resolve fully here (env vars guaranteed by the runtime). ECS-on-EC2 and bare EC2 do **not** set these automatically — region falls to Phase 2. |
| Azure | **Provider:** `WEBSITE_SITE_NAME` (App Service / Functions), `FUNCTIONS_WORKER_RUNTIME` (Functions), `CONTAINER_APP_NAME` (Container Apps). **Region:** `REGION_NAME` *if present* — best-effort only; it is set for some legacy App Service / Functions configurations but **not** for Container Apps, AKS, or VMs. Azure region is **usually a Phase 2 resolve.** |
| GCP | **Provider:** `K_SERVICE` (Cloud Run), `GAE_ENV` (App Engine), `FUNCTION_TARGET` (Cloud Functions). **Region:** not exposed via env vars on any GCP runtime *(verified as of 2026-05 — re-verify if GCP ships a region env var in a future runtime)* → always Phase 2. |

### 5.2 Phase 1b — DMI check (Linux only, ~1 ms)

A single read of `/sys/class/dmi/id/board_vendor` (or `sys_vendor`) definitively
identifies the cloud provider for any IaaS VM (EC2 → "Amazon EC2", GCE →
"Google", Azure VM → "Microsoft Corporation"). It gives **provider, not region**.
Runs only if Phase 1a did not already resolve the provider. On non-Linux or when
the file is absent, this phase is a silent no-op.

### 5.3 Phase 2 — background metadata probe (~250 ms budget, never blocks init)

Runs on a single daemon thread, spawned by `init()`, only if Phase 1a+1b left
**provider or region** unresolved.

| Provider | Endpoint |
|---|---|
| AWS | IMDSv2: `PUT http://169.254.169.254/latest/api/token` (`X-aws-ec2-metadata-token-ttl-seconds: 21600`), then `GET .../latest/meta-data/placement/region` with the token header. |
| GCP | `GET http://metadata.google.internal/computeMetadata/v1/instance/zone` (`Metadata-Flavor: Google`) → returns `projects/.../zones/us-central1-a`; **region = zone with the trailing `-<letter>` stripped** (string op: drop everything after the final `-`). |
| Azure | `GET http://169.254.169.254/metadata/instance?api-version=2021-02-01` (`Metadata: true`) → JSON `.compute.location`. |

- **Provider known** (from Phase 1a/1b) → probe only that provider's endpoint.
- **Provider unknown** → probe all three endpoints **in parallel**, each with a
  fresh client/session and a tight per-request timeout; first successful
  response wins. Worst case is bounded by the per-request timeout (~250 ms),
  not 3× serial.
- All failures are silent. If nothing resolves, the result stays "undetected".
- **Thread-termination guarantee:** the background daemon thread always
  terminates within the per-request timeout bound (~250 ms) regardless of which
  combination of probes succeed or fail — parallel probes do not sum, and a
  fully off-cloud host (laptop, bare metal) resolves to "undetected" in ~250 ms,
  not longer. There is no "thread still polling minutes later" surface.

### 5.4 Result & lifecycle

- Result type: `CloudEnv(provider: str | None, region: str | None, source: str)`
  where `source ∈ {"env", "dmi", "imds", "none"}` — the audit trail for "how did
  you know my region."
- Held in a lock-guarded module global, written once by the background thread,
  read at task finalize by `_aggregate_costs`.
- **Skipped entirely when `track_network=False`** — no cost will be computed, so
  there is nothing to detect; the SDK makes no metadata call.
- **Probe pending at task end:** if a task finalizes before the probe lands,
  pricing resolves with whatever is available (env/DMI partial, or nothing) →
  `estimated` confidence; the next task in the process picks up the resolved
  result. Self-healing.

### 5.5 The metadata probe does not contaminate egress cost

The metadata IPs (`169.254.169.254`, `metadata.google.internal` →
`169.254.169.254`) are link-local. `classify_destination` already marks
link-local as internal → `$0` egress, so even if the probe's own HTTP were
instrumented it contributes no cost (at most `+1` to `network_call_count`). The
probe additionally runs under the existing `_in_patched_call` / network-event
suppression machinery as belt-and-suspenders.

---

## 6. Cost computation & finalize flow

### 6.1 `NetworkAccountant` — external-byte split

The adapter already computes `is_internal_traffic = classify_destination(domain)`
per call (`True` / `False` / `None`). v2 extends `NetworkAccountant.record()` to
take it as a fourth argument:

- **Billable egress** for a call = `bytes_out`, unless `is_internal is True`.
  Both `False` (public IP literal) and `None` (named host, no peer IP — the
  common case) are treated as billable. The `None` case is conservative:
  over-attribute and let the customer notice, rather than under-attribute and
  hide cost. Heavy `None` rates are logged at debug level so a customer can
  investigate (e.g. missing DNS resolution).

The accountant accumulates:
- a **scalar `external_bytes_out`** total — the basis for `network_cost_usd`;
- **per-host `external_bytes_out`** on every host entry **and** on the `_other`
  and `_unknown` overflow buckets — so per-host cost survives the top-20 cap and
  `sum(per-host external) == scalar external` holds.

`finalize()` returns the external scalar and the per-host external bytes
alongside the v1 fields.

### 6.2 `egress_pricing.py` — rate resolution & confidence contract

`resolve_rate(provider, region) → EgressRate(rate_per_gb: Decimal,
pricing_source: str, cost_confidence: str)`.

| Situation | Rate | `pricing_source` | `cost_confidence` |
|---|---|---|---|
| Traffic classified internal | `Decimal("0")` | `egress_catalog:internal` | `exact` |
| `(provider, region)` exact catalog match | region rate | `egress_catalog:<prov>:<region>` | `computed` |
| Provider known, region absent/unknown | provider default | `egress_catalog:<prov>:default` | `estimated` |
| No provider detected (or probe still pending) | universal default | `egress_catalog:default` | `estimated` |

`unknown` is not used for egress — the SDK always produces a usable number.

### 6.3 Unit

Cloud providers bill egress per **GB = 10⁹ bytes** (decimal, **not** GiB =
2³⁰). Using GiB would systematically under-attribute by ~7.4%.

```
cost = external_bytes_out / Decimal("1000000000") * rate_per_gb
```

The divisor MUST be a Decimal literal `Decimal("1000000000")` (or the
per-language Decimal/BigDecimal equivalent) — **never** the floating literal
`1e9`, which silently coerces to float. This is pinned by
`test_decimal_no_float_drift` (§10).

### 6.4 Deferred per-event cost & the finalize flow

Per-event egress cost is computed at **task finalize**, not at emission. This
keeps the §3.3 invariant pure and removes any rate-drift surface (the
background probe could otherwise resolve mid-task, leaving early `network`
events priced at the default rate and the task rollup at the resolved rate).

**At emission** (HTTP adapter, `_handle_uncataloged`): the `network` event is
persisted with `cost_usd = Decimal("0")`, no pricing fields, and a free-form
`details["cost_pending"] = true` marker.

**At task finalize** (`_aggregate_costs`):
1. Read `cloud_detect.get_cloud_env()` → `egress_pricing.resolve_rate(...)`.
2. `NetworkAccountant.finalize()` → external scalar + per-host external bytes.
3. `task.network_cost_usd = scalar_external_bytes_out / Decimal("1000000000")
   * rate` — computed from the **canonical scalar**, not by summing events.
4. Each `network_by_host[]` entry: `egress_cost_usd =
   entry_external_bytes_out / Decimal("1000000000") * rate` (`0` for
   all-internal hosts).
5. Walk the task's `network` events; for each, stamp `cost_usd`,
   `pricing_source`, `cost_confidence`, and `pricing_version`, and remove the
   `cost_pending` marker — via `storage.update_event(...)`.

**Required storage fix:** `update_event` currently does **not** re-mark
`sync_status='pending'` (unlike `update_task`, which does). v2 MUST add
`sync_status='pending'` to the `update_event` UPDATE statement, so a
finalize-time cost correction actually re-syncs. Worst case: a `network` event
syncs once at `$0`, then re-syncs corrected — the same idempotent
update→re-sync pattern `update_task` already relies on (the Control Layer
upserts by `event_id`). Not a new failure mode.

`pricing_version` on `network` events = `egress:<egress_prices.json _meta.version>`
— so a future reconciliation surface can identify which catalog version produced
a number. This is one of several `pricing_version` prefixes across event types
(`llm_call` and `external_cost` events carry their own catalog/source markers);
the `egress:` prefix keeps the egress catalog version auditable distinctly from
the others. The implementer should confirm the `egress:` prefix does not collide
with an existing `pricing_version` convention and, if a `pricing_version`
format-by-event-type reference doc does not yet exist, note the three formats
together so reconciliation logic stays auditable.

### 6.5 `network_cost_usd` is computed from the canonical scalar

The task figure is computed from `external_bytes_out` (the scalar), and events
are stamped to be consistent with it by construction. Events are *derived*; the
scalar is the *truth*. Resulting invariants (asserted as tests, §10):
- `sum(per-host external bytes) == scalar external_bytes_out`
- `sum(network_by_host[].egress_cost_usd) == network_cost_usd`
- `sum(network event cost_usd) ≤ network_cost_usd` — intentional inequality:
  cataloged calls and below-threshold un-cataloged calls contribute bytes to the
  task aggregate but emit no `network` event.

---

## 7. Error handling & the degradation ladder

v2 extends v1's fail-silent discipline: egress pricing or detection must **never**
break a customer's HTTP call or prevent a task from being recorded.

### 7.1 The five-tier degradation ladder

Egress pricing always produces a number, degrading through these tiers in order:

| Tier | Condition | Result | `cost_confidence` |
|---|---|---|---|
| 1 | `(provider, region)` exact catalog match | region rate | `computed` |
| 2 | Provider known, region missing | provider `default_usd_per_gb` | `estimated` |
| 3 | Provider not in catalog / not detected | `_meta.default_rate_usd_per_gb` | `estimated` |
| 4 | Catalog file missing, unreadable, malformed JSON, or `_meta.default_rate_usd_per_gb` itself missing/malformed | hardcoded constant `Decimal("0.09")` | `estimated` + warning |
| 5 | Egress computation raises at finalize | `network_cost_usd = 0`, task still ships | warning |

Tier 4's hardcoded `Decimal("0.09")` is a **true last resort**: the loader first
attempts to read `_meta.default_rate_usd_per_gb` from the catalog, and only falls
through to the hardcoded literal if *that* read also fails. This eliminates the
drift surface where the catalog's documented default and a parallel hardcoded
constant diverge over time — the catalog's own `_meta` is the primary source of
the universal default; the literal only fires when the catalog cannot speak at
all.

### 7.2 Fail-silent specifics

- **Detection probe failure** → silent; `CloudEnv` stays "undetected" → Tier 3.
- **Catalog load failure** → Tier 4; warning logged (see §7.3).
- **Computation failure at finalize** → Tier 5: the egress step in
  `_aggregate_costs` is wrapped so a pricing bug cannot break task
  finalization. The task still ships with correct `llm` / `external` /
  `compute` costs and `network_cost_usd = 0`; the failure is logged.

### 7.3 Warning logging — once per failure mode per process

"Log once" means **once per distinct failure mode per process**, not once
globally. A module-level set tracks which conditions have fired
(`catalog_missing`, `catalog_malformed`, `meta_default_missing`, …) and each is
logged only on its first occurrence. This surfaces a *mode change* (e.g. a file
that was missing is now present-but-malformed) instead of swallowing it after
the first warning of any kind. The tracking set is resettable for tests.

---

## 8. Schema & migration

### 8.1 SQLite migration v4 → v5

v1 left the schema at version 4 (`TARGET_SCHEMA_VERSION = 4`). v2 adds one
migration, **v4 → v5**, following the exact pattern of v1's v3→v4:

- Idempotent `ALTER TABLE tasks ADD COLUMN network_cost_usd TEXT NOT NULL
  DEFAULT '0'` (Decimal stored as TEXT, consistent with the other
  `*_cost_usd` columns).
- `_CREATE_TASKS` fresh-create DDL gains the same column.
- `to_dict` / `from_dict` handle `network_cost_usd`; `from_dict` defaults it to
  `Decimal("0")` for old payloads that lack the key.
- Task JSON schema gains `network_cost_usd`; sync `_prepare_task_dict` includes
  it.
- `TARGET_SCHEMA_VERSION` → 5.

### 8.2 `update_event` re-mark-pending fix

Per §6.4: `update_event`'s UPDATE statement gains `sync_status='pending'` so a
finalize-time `network`-event cost correction re-syncs. One line; makes
`update_event` consistent with `update_task`.

### 8.3 Backward compatibility

- A new SDK opening an old v4 DB → migration adds `network_cost_usd` with
  default `0`. Old tasks read back `network_cost_usd == Decimal("0")` — truthful:
  those tasks predate egress capture.
- `network_by_host` entries persisted under v1 lack `egress_cost_usd`. Readers
  (`from_dict`, Control Layer) treat a missing per-host `egress_cost_usd` as
  `0`. **No JSON-blob backfill** — matches the top-level field's default
  treatment.
- `cost_pending` is a **free-form `details` field**, not part of the validated
  Event JSON schema. An un-finalized event still carrying `cost_pending: true`
  represents a task that crashed before finalize — a deliberate, honest crash
  signal the Control Layer can use for reconciliation diagnostics, not a bug.
  *Confirmed:* `dexcost-event.v1.json` sets `additionalProperties: false` at the
  event's top level, but the `details` property is an unconstrained
  `{"type": "object"}` — arbitrary keys inside `details` (as v1 already does
  with `request_bytes` / `url` / `protocol`) pass validation. `cost_pending`
  needs no schema change.

---

## 9. Configuration interaction

v2 adds **no new config fields**. It interacts with the existing
`track_network` flag (added in v1):

- `track_network = False` → no cloud detection, no metadata probe, no egress
  cost; `network_cost_usd` stays `0`. (Egress capture is part of network
  capture; turning network capture off turns egress cost off.)
- `track_network = True` (default) → detection runs at `init()`; egress cost is
  computed at every task finalize.
- The v1 `network_event_threshold_bytes` gates **only `network`-event
  emission** — never cost accounting. A below-threshold un-cataloged call emits
  no event but its `bytes_out` still flows into `external_bytes_out` and
  therefore into `network_cost_usd`. This invariant is asserted by a dedicated
  test (§10).

---

## 10. Testing (Python first)

### 10.1 Unit tests

- **`egress_pricing.py`** — every tier of the §7.1 ladder (region match →
  `computed`; provider default, `_meta` default, hardcoded fallback →
  `estimated`); the `pricing_source` string for each tier;
  `test_decimal_no_float_drift` — asserts `Decimal("0.1093") *
  Decimal("1000000000")` is exact, **and** a multiplication-step case
  (`Decimal("0.087") * Decimal("12345678")` against a hand-computed expected
  value) to catch float introduction at the multiply, not just the divide.
- **`cloud_detect.py`** — env-var detection per provider (Lambda, Azure App
  Service, Cloud Run fixtures); DMI check with a mocked `board_vendor`;
  metadata-probe response parsing (AWS IMDSv2 token+region, GCP zone→region
  string op, Azure IMDS JSON); probe timeout/failure → "undetected";
  `track_network=False` → no probe; **"init never blocks"** — a timing
  assertion with the metadata IP unreachable, asserting `init()` returns under a
  tight bound (e.g. < 10 ms).
- **`NetworkAccountant`** — external-byte split for `is_internal ∈ {True, False,
  None}`; scalar and per-host `external_bytes_out`; `_other` / `_unknown`
  buckets carry external bytes.
- **Catalog integrity** — `egress_prices.json` parses; every rate is a valid
  Decimal string; `_meta` has all required keys including `currency`; every
  provider has a parseable ISO-8601 `_last_verified`; a **soft freshness check**
  warns (does not fail the build) if any provider's `_last_verified` is older
  than 180 days.
- **Warning-once-per-failure-mode** — trigger one mode, assert one log; trigger
  a *different* mode, assert a second log; the test fixture explicitly resets
  the module-level tracking set.

### 10.2 Integration tests

- Adapter call → bytes classified → task finalize → `network_cost_usd` at the
  resolved rate; internal-host (RFC1918) traffic excluded and shown as
  `egress_cost_usd: 0` in `network_by_host`.
- **Deferred cost** — a `network` event is emitted with `cost_usd=0` and
  `cost_pending` set; after task finalize, assert (a) `cost_usd` is non-zero for
  external traffic, (b) the `cost_pending` marker is gone, (c) the event row's
  `sync_status` is back to `pending`.
- **Migration v4→v5 round-trip** — a v4 DB is created, a task written, the v5
  migration applied → the column exists with default `0`; an old v4 task reads
  back `network_cost_usd == Decimal("0")` (not `None`, not `0.0`, not `"0"`);
  a v5 task written with `network_cost_usd = Decimal("0.0042")` reads back as
  exactly `Decimal("0.0042")` (Decimal exactness preserved across SQLite TEXT
  storage); re-applying the migration is a no-op (idempotency).
- **Fail-silent** — a corrupt `egress_prices.json` → the SDK still runs, Tier 4
  hardcoded fallback is used, the warning is logged once.
- **Mid-task probe completion** — emit two `network` events, complete the
  background probe between them, emit two more, finalize; assert **all four**
  events carry the same resolved rate and the task aggregate matches (guards the
  deferred-cost design against a regression that stamps rate-at-emission).
- **Threshold-gates-emission-not-accounting** — one 50 KB un-cataloged call
  (below the default 100 KB threshold, no event) and one 200 KB call (above,
  event emitted) in one task; assert `external_bytes_out` and
  `network_cost_usd` cover **both**, while only **one** `network` event exists.
- **`is_internal_traffic = null` end-to-end** — a named host whose peer IP is
  unresolved → classification `None` → accountant treats the bytes as external
  → finalize includes them in `network_cost_usd`. Guards against silent
  regression into "`null` = `$0`".
- **End-to-end** — a task with LLM + cataloged-vendor + un-cataloged calls.
  Assert the **arithmetic explicitly**, not a weak non-zero check:
  `task.network_cost_usd == (llm_bytes_out + vendor_bytes_out +
  uncataloged_bytes_out) / Decimal("1000000000") * resolved_rate` (all three
  external), and `task.total_cost_usd == llm_cost + external_cost +
  compute_cost + network_cost_usd`.
- **Dual-invoice attribution for cataloged vendor calls (Decision #7)** —
  pins the §3.3 "one event per call, multiple cost categories per call"
  contract. Make one HTTP call to a cataloged vendor host (e.g. a registered
  domain rate of `$0.01/request` with a 50 KB response). After task
  finalize, assert all of:
  1. **Exactly one** event exists for that call, with
     `event_type == "external_cost"` (zero `network` events for it — the
     "one event per call" invariant holds).
  2. `task.external_cost_usd == Decimal("0.01")` — the vendor's per-request
     invoice is captured intact.
  3. `task.network_cost_usd > 0` and equals
     `vendor_call_bytes_out / Decimal("1000000000") * resolved_rate` — the
     cloud's egress invoice on those same bytes is captured *in addition*,
     not instead.
  4. `task.total_cost_usd == task.external_cost_usd + task.network_cost_usd`
     (plus zero for the unused categories) — the customer's true
     two-invoice total.
  5. The vendor's `external_cost` event's own `cost_usd` is unchanged from
     v1 (still `Decimal("0.01")`, no egress dollars stamped on it) — the
     §3.3 "events carry measurement, task carries derived attribution"
     separation holds.

  This test is the executable spec for Decision #7: it fails fast if a future
  refactor ever conflates the two invariants and silently strips the egress
  half of vendor-call cost. The same shape repeats once with an `llm_call`
  event in place of `external_cost` to lock the LLM equivalent.

### 10.3 Property invariants

The three structural invariants below MUST hold across **arbitrary** task
shapes. They are written as **parametrized property tests**, generating
scenarios over: host count `∈ {1, 5, 20, 100, 1000}`; mixed
internal/external/`null` classification; mixed cataloged/un-cataloged calls;
mixed above/below threshold. Each generated scenario asserts all three:

1. `sum(network_by_host[].external_bytes_out) == scalar external_bytes_out`
2. `sum(network_by_host[].egress_cost_usd) == network_cost_usd`
3. `sum(network event cost_usd) ≤ network_cost_usd`

### 10.4 Cross-language test matrix

Each SDK port (Go, Rust, TypeScript) implements the **same** test matrix —
identical unit cases, integration scenarios, and the three property invariants —
translated to that language's testing idioms. `egress_prices.json` is a single
shared file across all four SDKs; the catalog-integrity test plus the assertion
that every SDK reads the same file proves cross-language catalog consistency by
construction.

### 10.5 Explicitly not tested

- **No performance benchmarks.** Accountant operations are O(1) per call by
  design; perf assertions are flaky in CI. Code review guards against an
  architectural regression.
- **No real-cloud IMDS tests.** Metadata-probe tests use mocked responses; real
  AWS/GCP/Azure endpoints are flaky, slow, and credential-bound.

---

## 11. Future (out of scope for this spec)

- **Go / Rust / TypeScript ports** — each its own spec → plan → implementation
  cycle, inheriting this design and the §10.4 test matrix. The shared
  `egress_prices.json` is the cross-SDK contract.
- **Region-undetected dashboard hook** — when a provider is detected but the
  region is not, the task is priced at the provider-default rate with
  `pricing_source: "egress_catalog:<prov>:default"`. A future Cost Intelligence
  surface can read that source and tell the customer "egress priced at the
  provider default because we could not resolve your region" rather than
  silently baking it into the number.
- **Reconciliation surface** — when an invoice-reconciliation feature ships, the
  first-tier over-attribution (§4.4) becomes a visible, explainable variance
  line; `pricing_version` on `network` events identifies the catalog version
  that produced each number.
- **Monthly-tier pricing** — explicitly deferred (§4.4); would require a
  workspace-scoped cumulative view the SDK does not have.

---

## 12. Non-goals

- No per-event `egress_cost_usd` on `llm_call` / `external_cost` events
  (Decision #3).
- No user-facing pricing configuration — no `init()` rate or provider knobs
  (Decision #2).
- No destination-geolocation / IP-heuristic rate inference — the resolution
  ladder stays discrete (§4.6).
- No ingress (`bytes_in`) pricing (Decision #1).
