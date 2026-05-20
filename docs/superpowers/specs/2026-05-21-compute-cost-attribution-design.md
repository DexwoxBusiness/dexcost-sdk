# Compute Cost (v2) — Attribution & Pricing — Design

**Date:** 2026-05-21
**Status:** Approved design — ready for implementation planning
**Scope:** Python SDK first; Go / Rust / TypeScript ports follow as separate plans.
**Sibling spec (v1, capture):** [`2026-05-21-compute-capture-design.md`](2026-05-21-compute-capture-design.md) — runtime detection, cgroup measurement, `compute_cost` event shape.
**Reference decisions:** [`decisions/2026-05-20-compute-foundation-decisions.md`](../decisions/2026-05-20-compute-foundation-decisions.md) — 10 locked decisions + sharpenings.
**Reference research:** [`research/2026-05-20-compute-foundation-research.md`](../research/2026-05-20-compute-foundation-research.md) — verified facts (env vars, metadata endpoints, cgroup file formats, billing formulas).
**Cross-subsystem conventions:** [`conventions.md`](../conventions.md) — inherited patterns.

---

## 1. Purpose & Scope

The v1 capture spec ([`2026-05-21-compute-capture-design.md`](2026-05-21-compute-capture-design.md)) defines **what** the SDK measures for compute — runtime resolution, cgroup snapshots, the `compute_cost` event shape with `details.billing_model` discriminator — and emits each event with `cost_pending: true` (no dollar amount). It deliberately stops at measurement.

This v2 spec turns those measurements into dollars: a `compute_cost_usd` figure per task, attributed alongside `llm_cost_usd`, `external_cost_usd`, and `network_cost_usd`.

**Core principle — same as v2 egress.** dexcost is automatic and catalog-driven: no `init()` knobs for pricing; a bundled, versioned, community-maintainable catalog (`compute_prices.json`) plus the existing automatic cloud-environment detection. The single new config field — `compute_billing_overrides` — is for billing-*mode* disambiguation (Cloud Run request-vs-instance), NOT for rate overrides.

**In scope (Python, this spec):**
- A bundled compute price catalog (`data/compute_prices.json`).
- A compute pricing engine (`compute_pricing.py`).
- Per-billing-model cost math — Lambda / Fargate / Cloud Run (request and instance) / Cloud Functions / Azure Functions / Vercel / EC2-style instance share / K8s pod-limits.
- Per-runtime memory-unit conversion table pinned at the catalog-lookup boundary (Decision #7).
- `Task.compute_cost_usd` populated from auto-emitted `compute_cost` events (alongside any user-driven `record_cost(...)` entries — they sum).
- Per-event `cost_usd` back-fill at task finalize (the v2 §6.4 deferred-cost pattern, applied to compute).
- `compute_billing_overrides` config knob (Decision #1).
- SQLite schema: `compute_cost_usd` already exists; no migration.

**Out of scope (see §11):** Go/Rust/TS ports; monthly-tier pricing; instance-utilization reconciliation surface; PC idle billing; cgroup-v1; GPU runtimes (subsystem C); a `compute_by_runtime` per-task aggregate (deferred — added only if dashboards need it).

---

## 2. Decisions (rationale preserved)

The full 10-decision log lives at [`decisions/2026-05-20-compute-foundation-decisions.md`](../decisions/2026-05-20-compute-foundation-decisions.md). This spec restates the cost-math-relevant decisions in narrative form; the capture spec (sibling) restates the measurement-relevant ones.

1. **Cloud Run defaults to request-based** (Decision #1). The container has no way to discover which billing mode is active; request-based is the statistical default. Labelled `estimated` + `pricing_source: "compute_catalog:cloud_run:request_based_default"`. Customers on instance-based opt in via `compute_billing_overrides: { cloud_run: "instance" }` — a *general* compute-override channel from day one (not a Cloud-Run-specific knob), so Decision #5 (PC) and any future case reuse the same shape.

2. **Vercel active-CPU ≈ wall duration** (Decision #2). Vercel's "active CPU" billing pauses during I/O wait; the SDK cannot measure I/O pauses without instrumenting every `await`. Approximation: bill `active_CPU = wall_duration`. Math is exact; the inputs are the approximation. `cost_confidence: "computed"`. Over-attributes on I/O-heavy code — the safe direction (customers complain about under-attribution, not over-attribution). The README must call this out so the discrepancy is honest, not hidden.

3. **Instance type from IMDS at init, cached** (Decision #3). The instance type is extracted in the same Phase 2 background thread that already runs for `cloud_detect`'s region probe — one probe, two values extracted. EC2 (`/latest/meta-data/instance-type`), GCE (`/computeMetadata/v1/instance/machine-type`), Azure VM (`/metadata/instance/compute/vmSize`). Cached for the process lifetime in `CloudEnv`.

4. **K8s bills pod-limits × duration by default** (Decision #4). The downward API exposes node *name* but NOT node CPU count. v1 bills `pod_cpu_limit × duration_hours × hourly_rate_per_vcpu` (no node-share term), `cost_confidence: "computed"`. The `k8s_node_aware: true` opt-in enables an `/api/v1/nodes` API call to compute the precise node-share; default zero-config does not probe the API server. Failure mode when `k8s_node_aware: true` is set but RBAC is missing: fail-silent + log-once + fall through to limit-based (NOT fail-loud on init).

5. **Track `AWS_LAMBDA_INITIALIZATION_TYPE` for all values** (Decision #5). v1 captures it on every Lambda event (`on-demand` / `provisioned-concurrency` / `snap-start` / `lambda-managed-instances`); v1 bills ALL of these at the standard per-invocation + per-GB-second Lambda rate. PC idle billing is v1.1 — the math extends additively because the schema is already forward-compatible.

6. **Single `compute_cost` event with `details.billing_model` discriminator** (Decision #6, convention §1). v2 cost-attribution dispatches on `details.billing_model` to pick the math. New billing models are additive to the dispatch table; the event schema does not change.

7. **Bytes everywhere internally; convert only at the catalog-lookup boundary** (Decision #7). The per-runtime conversion table is pinned in §6.2 below. The Fargate row (`memory_bytes / Decimal(1024*1024*1024)`, NOT `/ Decimal("1000000000")`) is the one that was wrong in the research draft (~4.86% silent over-attribution); pinning the table explicitly prevents the same error elsewhere.

8. **Per-task share window uses existing `Task.started_at` / `Task.ended_at`** (Decision #8). `window_seconds = (ended_at - started_at).total_seconds()`. Zero new schema.

9. **Idle EC2/GCE/Azure-VM time is invisible** (Decision #9). The SDK does NOT emit synthetic "idle pseudo-tasks." `dexcost_compute_total < cloud_invoice` on long-running runtimes IS expected — the gap is the customer's "unaccounted capacity" signal, surfaced by the future Cost Intelligence / reconciliation server-side feature. Customer-facing framing in README, dashboard, and marketing site is MANDATORY (not just internal spec text).

10. **Idle Fargate container time is invisible** (Decision #10), for consistency with #9. The reconciliation surface (future) is where #9 and #10 gaps both get explained as line items.

---

## 3. Architecture & Data Model

### 3.1 New modules

| File | Responsibility |
|---|---|
| `python/src/dexcost/data/compute_prices.json` | Bundled, versioned compute-rate catalog. Community-maintainable by PR, mirroring `egress_prices.json`. |
| `python/src/dexcost/compute_pricing.py` | Loads the catalog, dispatches on `billing_model`, applies the per-runtime math from §6, returns `cost_usd` + `pricing_source` + `cost_confidence`. Mirrors `egress_pricing.py`. |

`cloud_detect.py` (shipped in v2 egress) is **extended in v1 capture** to expose `instance_type` alongside `provider` / `region` / `source` on `CloudEnv`. The pricing engine reads `instance_type` from `CloudEnv` to resolve EC2/GCE/Azure-VM SKU rates.

### 3.2 Data model changes

- **`Task.compute_cost_usd`** — already exists; v1 made it the aggregation home for auto-emitted `compute_cost` events. v2 populates `cost_usd` on each event at task finalize so the aggregation produces a real dollar.
- **`Task.total_cost_usd`** invariant restated for completeness — `llm + external + compute + network` — unchanged from v2 egress.
- **`compute_cost` event `cost_usd`** — back-filled at task finalize from the catalog. The event's `details.cost_pending: true` marker (set at emission in v1 capture) is stripped after back-fill.
- **No new schema fields, no new columns, no new JSON-blob aggregates in v1.** `compute_by_runtime` (analog of `network_by_host`) is deferred to a later spec only if dashboards need a per-billing-model breakdown.

### 3.3 The measurement/pricing separation (extends convention §4)

> Events carry raw measurement (`vcpu_seconds_used`, `memory_bytes_peak`, `duration_ms`, `invocation_count`, `billing_model`). The task carries derived attribution (`task.compute_cost_usd`).

For compute v1 there is at most one `compute_cost` event per task (capture §5.3), so `task.compute_cost_usd = sum(compute_cost.cost_usd for that task)` is trivially the event's own `cost_usd`. The "sum is the truth" framing still holds — and v2 of *this* spec (a `compute_by_runtime` breakdown) would extend it the same way v2 egress extends `network_by_host`.

### 3.4 LLM / vendor instruments need no changes

Compute cost does NOT get stamped onto `llm_call` or `external_cost` events. An LLM call whose handler runs on a Cloud Run instance generates ONE `llm_call` event (LLM dollars) AND contributes its share to ONE `compute_cost` event (compute dollars). The convention §5 distinction — "≤ 1 event per call (record dedup) ≠ 1 cost category per call (dollar dedup)" — applies the same way it does for egress.

---

## 4. The compute catalog (`data/compute_prices.json`)

### 4.1 Structure

Mirrors `egress_prices.json` conventions — `_meta` block, then provider keys with `_last_verified`. Where egress has a single rate per region, compute has multiple rates per region (one per billing model / instance type), so the per-provider blocks nest deeper.

```json
{
  "_meta": {
    "version": "1.0.0",
    "last_updated": "2026-05-21",
    "currency": "USD",
    "default_lambda_request_usd": "0.0000002",
    "default_lambda_gb_second_usd": "0.0000166667",
    "default_fargate_vcpu_second_usd": "0.0000111111",
    "default_fargate_gib_second_usd": "0.0000012222",
    "default_cloud_run_request_usd": "0.0000004",
    "default_cloud_run_vcpu_second_usd": "0.000024",
    "default_cloud_run_gib_second_usd": "0.0000025",
    "default_azure_functions_execution_usd": "0.0000002",
    "default_azure_functions_gb_second_usd": "0.000016",
    "default_vercel_cpu_hour_usd": "0.128",
    "default_vercel_memory_gb_hour_usd": "0.0106",
    "default_ec2_vcpu_hour_usd": "0.0464",
    "default_k8s_pod_vcpu_hour_usd": "0.0464",
    "description": "Dexcost compute catalog — per-billing-model rates by cloud provider/region. Community-maintained; submit PRs to add or refresh rates.",
    "notes": "Rates are standard on-demand pricing, FIRST tier only. Sustained-use discounts (GCE), Savings Plans (AWS), and Reserved Instances are not modelled — the SDK does not know the customer's commitment posture. Lambda/Azure Functions/Vercel rates are in DECIMAL GB (10^9 bytes); Fargate/Cloud Run rates are in BINARY GiB (2^30 bytes) — see §6.2 of the design spec. The default_* keys at this _meta level are the universal fallbacks used when neither a region match nor a provider default is available."
  },
  "aws": {
    "_last_verified": "2026-05-21",
    "lambda": {
      "default": {
        "x86_64": { "request_usd": "0.0000002", "gb_second_usd": "0.0000166667" },
        "arm64":  { "request_usd": "0.0000002", "gb_second_usd": "0.0000133334" }
      },
      "regions": {
        "us-east-1":      { "x86_64": { "request_usd": "0.0000002", "gb_second_usd": "0.0000166667" }, "arm64": { "request_usd": "0.0000002", "gb_second_usd": "0.0000133334" } },
        "ap-south-1":     { "x86_64": { "request_usd": "0.00000022", "gb_second_usd": "0.0000183334" }, "arm64": { "request_usd": "0.00000022", "gb_second_usd": "0.0000146667" } }
      }
    },
    "fargate": {
      "default": {
        "x86_64": { "vcpu_second_usd": "0.0000111111", "gib_second_usd": "0.0000012222" },
        "arm64":  { "vcpu_second_usd": "0.00000888888", "gib_second_usd": "0.00000097777" }
      },
      "regions": { "us-east-1": { "x86_64": { "vcpu_second_usd": "0.0000111111", "gib_second_usd": "0.0000012222" }, "arm64": { "vcpu_second_usd": "0.00000888888", "gib_second_usd": "0.00000097777" } } }
    },
    "ec2": {
      "default_vcpu_hour_usd": "0.0464",
      "regions": {
        "us-east-1": {
          "instance_types": {
            "c7g.xlarge":     { "hourly_usd": "0.1450", "vcpu_count": "4" },
            "m7i.large":      { "hourly_usd": "0.1008", "vcpu_count": "2" },
            "t3.medium":      { "hourly_usd": "0.0416", "vcpu_count": "2" }
          }
        }
      }
    }
  },
  "gcp": {
    "_last_verified": "2026-05-21",
    "cloud_run": {
      "default": { "request_usd": "0.0000004", "vcpu_second_usd": "0.000024", "gib_second_usd": "0.0000025" },
      "regions": { "us-central1": { "request_usd": "0.0000004", "vcpu_second_usd": "0.000024", "gib_second_usd": "0.0000025" } }
    },
    "cloud_functions": {
      "default": { "request_usd": "0.0000004", "vcpu_second_usd": "0.0000100", "gib_second_usd": "0.0000025" }
    },
    "gce": {
      "default_vcpu_hour_usd": "0.0475",
      "regions": {
        "us-central1": {
          "instance_types": {
            "n2-standard-2":  { "hourly_usd": "0.0971", "vcpu_count": "2" },
            "e2-standard-4":  { "hourly_usd": "0.1340", "vcpu_count": "4" }
          }
        }
      }
    }
  },
  "azure": {
    "_last_verified": "2026-05-21",
    "functions_consumption": {
      "default": { "execution_usd": "0.0000002", "gb_second_usd": "0.000016" }
    },
    "vm": {
      "default_vcpu_hour_usd": "0.046",
      "regions": {
        "eastus": {
          "instance_types": {
            "Standard_D2s_v3": { "hourly_usd": "0.096", "vcpu_count": "2" },
            "Standard_B2ms":   { "hourly_usd": "0.0832", "vcpu_count": "2" }
          }
        }
      }
    }
  },
  "vercel": {
    "_last_verified": "2026-05-21",
    "fluid": {
      "default": { "active_cpu_hour_usd": "0.128", "memory_gb_hour_usd": "0.0106", "invocation_usd": "0.000000600" }
    }
  }
}
```

### 4.2 Encoding & precision

- **All rates are string-encoded Decimals**, parsed via `Decimal(...)` — never `float(...)`. Pinned by `test_decimal_no_float_drift` (§10), reusing the v2 egress pattern.
- **`currency: "USD"`** for v1; the field exists so a future non-USD catalog has a place to land.
- **Per-architecture nesting for Lambda and Fargate.** ARM-vs-x86 is ~20% on those runtimes; the catalog encodes both. The pricing engine reads `event.details.architecture` to pick the right SKU.
- **Per-instance-type nesting for EC2 / GCE / Azure VM.** Each region has an `instance_types` map keyed by the SKU returned by IMDS (e.g. `c7g.xlarge`, `n2-standard-2`, `Standard_D2s_v3`). The map carries `hourly_usd` and `vcpu_count` so the engine can compute the per-vCPU rate and the pod-share math without needing a second probe.

### 4.3 Per-provider freshness

Each provider carries `_last_verified` (ISO-8601). Catalog-integrity test (§10) asserts every provider has a parseable date and soft-warns if any is older than 180 days. Same pattern as v2 egress §4.3.

### 4.4 No sustained-use / Savings Plans / RI modelling

The catalog holds standard on-demand rates. Sustained-use discounts (GCE), Savings Plans (AWS), and Reserved Instances are not modelled — the SDK does not know the customer's commitment posture. A customer with heavy RI coverage will see `dexcost_compute_total` higher than their AWS invoice; the future reconciliation surface explains this as a separate variance line (analogous to v2 egress's first-tier over-attribution).

### 4.5 No monthly-tier pricing

Cloud egress was billed in descending monthly-volume tiers; compute is not (the runtimes here are flat per-unit). The "no monthly view in the SDK" structural limitation does not bite compute pricing; the SDK has no need for a workspace-scoped cumulative view for compute.

### 4.6 Launch coverage (prerequisite, not "grows over time")

For v2 to ship credibly, `compute_prices.json` MUST cover, at launch:
- Lambda x86_64 + arm64 in all commercial AWS regions
- Fargate x86_64 + arm64 in all commercial AWS regions
- Cloud Run + Cloud Functions in all commercial GCP regions
- Azure Functions Consumption in all commercial Azure regions
- Vercel Fluid (single global rate)
- The TOP ~50 most-deployed EC2 / GCE / Azure VM instance types (covering ~95% of compute usage by AWS's own breakdown)

Each cloud publishes per-region pricing publicly; populating the catalog is a one-time, manual data-entry job for v2 — read each provider's public pricing page, transcribe rates, human-review pass per provider. **Tracked as an explicit launch-prerequisite task in the v2 implementation plan** (`docs/superpowers/plans/`), the same way v2 egress §4.5 tracks its catalog-population gate.

An uncovered instance type silently degrades to the provider's `default_vcpu_hour_usd` × `vcpu_count` (one confidence step worse: `estimated`); an uncovered region degrades the same way. Customers in long-tail regions or on exotic instance types get a useful number, not a crash.

### 4.7 Rate resolution order

The five-tier degradation ladder (convention §7) applied per billing model:

1. `(provider, runtime, region, [architecture | instance_type])` exact match → region/SKU rate, `computed`.
2. Provider+runtime known, region/SKU absent → provider+runtime `default` block, `estimated`.
3. Provider not in catalog / `billing_model` not supported in this provider → `_meta.default_<billing_model>_*` rates, `estimated`.
4. Catalog file missing / unreadable / malformed → hardcoded per-billing-model constants in code (mirroring v2 egress Tier 4 hardcoded `Decimal("0.09")`), `estimated` + warning.
5. Computation raises at finalize → `cost_usd = 0`, task still ships, warning logged.

---

## 5. The pricing engine (`compute_pricing.py`)

```
resolve_compute_cost(event_details: dict, cloud_env: CloudEnv, overrides: dict)
  → ComputeCost(cost_usd: Decimal, pricing_source: str, cost_confidence: str)
```

### 5.1 Dispatch

1. Read `details.billing_model` from the event.
2. If `cloud_run` and `overrides.get("cloud_run") == "instance"`, switch the math to instance-based; record `pricing_source: "compute_catalog:cloud_run:instance_override"` (`computed`, not `estimated`, because the customer told us).
3. Look up the rate block per §4.7 ladder.
4. Apply the per-billing-model math from §6 using the per-runtime memory conversion from §6.2.
5. Return `ComputeCost` for the task-finalize back-fill.

### 5.2 Configuration interaction

| Knob | Source | Default | Effect |
|---|---|---|---|
| `compute_billing_overrides: dict` | `init()` kwarg | `{}` | Per-runtime billing-mode override (Decision #1 sharpening — general channel from day one) |
| `k8s_node_aware: bool` | `init()` kwarg | `False` | Opts into the K8s `/api/v1/nodes` probe at task finalize for node-share math (Decision #4) |

Both are **disambiguation knobs**, not **rate-override knobs.** There is intentionally no `compute_cost_per_vcpu_hour` parameter, matching the v2 egress zero-config positioning.

### 5.3 Confidence labelling contract

| Situation | `cost_confidence` | `pricing_source` (example) |
|---|---|---|
| Region + SKU + architecture exact match | `computed` | `compute_catalog:aws:lambda:us-east-1:arm64` |
| Region exact, SKU missing → provider+runtime default | `estimated` | `compute_catalog:aws:lambda:us-east-1:arm64:default` |
| Provider+runtime missing → `_meta` default | `estimated` | `compute_catalog:default:lambda` |
| Cloud Run defaulted to request-based (no override) | `estimated` | `compute_catalog:cloud_run:request_based_default` |
| Cloud Run instance-based via override | `computed` | `compute_catalog:cloud_run:instance_override` |
| K8s without `k8s_node_aware` | `computed` | `compute_catalog:k8s_pod:limits` |
| K8s with `k8s_node_aware: true` + RBAC granted | `computed` | `compute_catalog:k8s_pod:node_share:<instance_type>` |
| K8s with `k8s_node_aware: true` + RBAC missing → fall through to limits | `computed` | `compute_catalog:k8s_pod:limits` (one-shot warning per §7.3) |
| Vercel active-CPU approximation | `computed` | `compute_catalog:vercel:fluid` (Decision #2 — math is exact, inputs approximate) |
| Catalog malformed → hardcoded constants | `estimated` + warning | `compute_catalog:hardcoded:<billing_model>` |

`unknown` is reserved for the Tier-5 "computation raised" case and any event where `details.region` / `details.architecture` are both null AND no fallback path produces a number — which should be empirically zero in v1.

---

## 6. Cost computation — per billing model

This is the heart of the spec. Each subsection is the exact arithmetic for one `details.billing_model` value, with `cost = …` written as a Decimal expression so the implementation translates one-for-one.

### 6.1 Universal preliminaries

```
duration_s     = Decimal(details.duration_ms) / Decimal("1000")
memory_gb      = Decimal(details.memory_bytes_limit) / Decimal("1000000000")   # decimal GB; see §6.2
memory_gib     = Decimal(details.memory_bytes_limit) / Decimal(1024*1024*1024) # binary GiB; see §6.2
vcpu_seconds   = Decimal(details.vcpu_seconds_used)
vcpu_count     = Decimal(details.vcpu_count)
invocations    = Decimal(details.invocation_count)
window_s       = Decimal((task.ended_at - task.started_at).total_seconds())   # Decision #8
```

`window_s` is used only by long-running billing models (Fargate, EC2, GCE, Azure VM, K8s pod, Cloud Run instance-based). Serverless billing models use `duration_s` (per-invocation wall-clock).

### 6.2 The per-runtime memory-unit conversion table (Decision #7 — pinned)

Memory rates differ across providers in the **unit** they're quoted in. Bytes are the internal currency; conversion happens ONCE, at the catalog-lookup boundary.

| Billing model | Catalog rate unit | Divisor at conversion | Reason |
|---|---|---|---|
| `lambda` | `gb_second_usd` (decimal GB) | `Decimal("1000000000")` | AWS Lambda Pricing page quotes per "GB" = 10^9 bytes |
| `fargate` | `gib_second_usd` (binary GiB) | `Decimal(1024*1024*1024)` | AWS Fargate spec is in MiB; the page colloquially says "GB" but the binary divisor is what the bill uses — the silent ~4.86% over-attribution bug if confused |
| `cloud_run` | `gib_second_usd` (binary GiB) | `Decimal(1024*1024*1024)` | GCP Cloud Run pricing is per "GiB" explicitly |
| `cloud_functions` | `gib_second_usd` (binary GiB) | `Decimal(1024*1024*1024)` | Same |
| `azure_functions` | `gb_second_usd` (decimal GB) | `Decimal("1000000000")` | Azure docs use "GB-s" decimal |
| `vercel_fluid` | `memory_gb_hour_usd` (decimal GB) | `Decimal("1000000000")` | Vercel docs use "GB" decimal |
| `ec2` / `gce` / `azure_vm` / `k8s_pod` (instance-hourly path) | no per-byte conversion | — | Billed per-instance-hour; memory is bundled |

**Rule:** the divisor MUST be a Decimal literal (`Decimal("1000000000")` or `Decimal(1024*1024*1024)`), NEVER the floating literals `1e9` or `1024**3` evaluated as float. Pinned by `test_decimal_no_float_drift` (§10).

### 6.3 `lambda`

```
gb_seconds = memory_gb * duration_s
cost = invocations * rate.request_usd + gb_seconds * rate.gb_second_usd
```

- `memory_gb` from `details.memory_bytes_limit` (the env-declared `AWS_LAMBDA_FUNCTION_MEMORY_SIZE`, converted to bytes by the v1 capture layer; here it converts back at the decimal-GB boundary).
- `rate` selected by `(region, architecture)`; ARM (`arm64`) is ~20% cheaper.
- `details.initialization_type` is captured but does not change the math in v1 (Decision #5 — PC idle is v1.1).

### 6.4 `fargate`

```
gib_seconds  = memory_gib * window_s
vcpu_seconds_billed = vcpu_count * window_s   # Fargate bills CPU CONFIG × duration, not CPU USED
cost = vcpu_seconds_billed * rate.vcpu_second_usd + gib_seconds * rate.gib_second_usd
```

- `vcpu_count` and `memory_bytes_limit` come from the ECS task metadata endpoint (`${ECS_CONTAINER_METADATA_URI_V4}/task`), cached in the `FargateTaskMetadata` helper (v1 capture §5.1.3).
- **Fargate bills task configuration × duration**, NOT used CPU. `details.vcpu_seconds_used` (from the cgroup diff) is captured for utilization analysis but is NOT in the cost math.
- `window_s` is the full task window (Decision #8). Fargate idle-tail between dexcost tasks is invisible (Decision #10).

### 6.5 `cloud_run_request` (default — Decision #1 path (a))

```
gib_seconds   = memory_gib * duration_s        # request-based: only ACTIVE container time is billed
vcpu_seconds  = vcpu_count * duration_s
cost = invocations * rate.request_usd
     + vcpu_seconds * rate.vcpu_second_usd
     + gib_seconds  * rate.gib_second_usd
```

- `duration_s` is per-invocation wall-clock (the handler wrap measures it).
- Cloud Run request-based bills only the time the container is actively serving requests — `duration_s` here matches that semantically.
- `vcpu_count` and `memory_bytes_limit` come from the cgroup at task start (`cpu.max`, `memory.max`).

### 6.6 `cloud_run_instance` (override — Decision #1 path (b))

```
gib_seconds   = memory_gib * window_s        # instance-based: the WHOLE container window is billed
vcpu_seconds  = vcpu_count * window_s
cost = vcpu_seconds * rate.vcpu_second_usd + gib_seconds * rate.gib_second_usd
```

- Same rate keys, swapped to instance-based math. No `request_usd` term — instance-based has no per-request charge.
- Activated by `compute_billing_overrides: { cloud_run: "instance" }`. `cost_confidence: "computed"` (the customer told us the billing mode).
- The "one event per task" rule (capture §5.3) means this fires once per dexcost task; idle time between tasks is invisible (Decision #10's logic, applied to instance-based Cloud Run by analogy).

### 6.7 `cloud_functions` (Gen2 — Cloud Run pricing under the hood)

Cloud Functions Gen2 IS Cloud Run with a function wrapper; the math is identical to `cloud_run_request`. `cloud_functions` is kept as a distinct `billing_model` discriminator so dashboards can break out function-vs-service usage if dashboards eventually need that — the dispatch in §5.1 routes both to the same arithmetic. Gen1 is deprecated (full sunset Sep 2026 per research §6b); the SDK falls through to the same `cloud_run_request` math when the detector resolves a Gen1 environment.

### 6.8 `azure_functions` (Consumption plan)

```
gb_seconds = memory_gb * duration_s
cost = invocations * rate.execution_usd + gb_seconds * rate.gb_second_usd
```

- Identical SHAPE to Lambda (per-execution + per-GB-second).
- Azure Functions ROUNDS billed memory to the nearest 128 MB tier; v1 reports raw `memory_bytes_peak` and lets the catalog rate absorb the rounding ambiguity. A small under/over-attribution on the rounding boundary surfaces as a reconciliation line — same posture as Decision #9.
- Premium plan + Dedicated plan are NOT covered in v1 (Premium has its own pricing model; Dedicated is just an App Service VM). v1.1 scope.

### 6.9 `vercel_fluid`

```
active_cpu_hours = duration_s / Decimal("3600")           # Decision #2 — active_CPU ≈ wall_duration
memory_gb_hours  = memory_gb * (duration_s / Decimal("3600"))
cost = invocations * rate.invocation_usd
     + active_cpu_hours * rate.active_cpu_hour_usd
     + memory_gb_hours  * rate.memory_gb_hour_usd
```

- The active-CPU-≈-wall-duration approximation over-attributes I/O-heavy code (safe direction per Decision #2).
- `vcpu_count` is read from the cgroup; Vercel Fluid pricing is per-active-CPU-hour, not per-vCPU — the SDK has no way to measure fractional active CPU. v1 charges 1.0 active CPU per wall hour as the approximation; multi-CPU Vercel Fluid sandboxes are v1.1.

### 6.10 `ec2` (and `gce`, `azure_vm` — same shape)

```
window_hours       = window_s / Decimal("3600")
share_factor       = vcpu_seconds / (vcpu_count * window_s)   # cgroup-measured CPU consumed by THIS task / total CPU available across the window
task_instance_hours = share_factor * window_hours
cost = task_instance_hours * rate.hourly_usd
```

- `rate.hourly_usd` comes from the per-region `instance_types` block (Decision #3 — IMDS extracts the instance type once).
- `share_factor` lets multiple dexcost tasks on the SAME instance fairly divide the instance hour. If the task used 50% of one of 4 vCPUs for 60 seconds, `share_factor = (0.5 * 60) / (4 * 60) = 0.125`, and `task_instance_hours = 0.125 * (60/3600)`.
- **Idle time between dexcost tasks on the same VM is invisible** (Decision #9). The cgroup keeps counting kernel-side, but the SDK only reads at task boundaries. The "unaccounted" VM hours are what the future reconciliation surface explains.
- Non-Linux fallback (cgroup reads return null): `share_factor = 1.0`, `cost_confidence: "estimated"`. Over-attributes to compensate for missing share data — safe direction.

### 6.11 `k8s_pod` (default — Decision #4 path (c))

```
pod_vcpu_limit_hours = vcpu_count * (window_s / Decimal("3600"))
cost = pod_vcpu_limit_hours * rate.k8s_pod_vcpu_hour_usd
```

- `vcpu_count` is the pod's CPU LIMIT (cgroup `cpu.max` quota / period). NOT cgroup-measured used CPU.
- `rate.k8s_pod_vcpu_hour_usd` is sourced from the K8s catalog block; it's effectively the underlying VM's per-vCPU-hour rate at the cluster's typical mix (a coarse estimate documented in the catalog `_meta.notes`). v1 uses a single regional default; node-aware accuracy is the `k8s_node_aware: true` opt-in.
- `cost_confidence: "computed"` — the math is exact given the inputs.

### 6.12 `k8s_pod` (opt-in — Decision #4 path (b), `k8s_node_aware: true`)

```
node_share_factor    = pod_cpu_limit / node_vcpu_count          # how much of the node this pod owns
task_node_hours      = node_share_factor * (window_s / Decimal("3600"))
cost = task_node_hours * rate.node_hourly_usd                   # node instance-type rate from the EC2/GCE/Azure VM block
```

- Probes `/api/v1/nodes/<spec.nodeName>` at task finalize, reads `.status.capacity.cpu` and `.metadata.labels["node.kubernetes.io/instance-type"]`.
- Falls through to §6.11 limits-based math if the API call returns 403 (RBAC missing), times out, or returns malformed JSON. Logs once per failure mode (convention §11).
- `cost_confidence: "computed"`; `pricing_source: "compute_catalog:k8s_pod:node_share:<instance_type>"`.

---

## 7. Error handling & the degradation ladder

v2 compute extends v1 capture's fail-silent discipline (convention §9): pricing or computation MUST never break a customer's task. Inherits the five-tier degradation ladder (convention §7).

### 7.1 The five-tier ladder, instantiated for compute

| Tier | Condition | Result | `cost_confidence` |
|---|---|---|---|
| 1 | `(provider, runtime, region, [arch | instance_type])` exact match | region/SKU rate | `computed` |
| 2 | Provider+runtime known, region/SKU missing | provider+runtime `default` | `estimated` |
| 3 | Provider not in catalog / `billing_model` unknown | `_meta.default_<billing_model>_*` | `estimated` |
| 4 | Catalog file missing/unreadable/malformed OR `_meta.default_*` itself missing | hardcoded per-billing-model constants in code | `estimated` + WARN_ONCE |
| 5 | Computation raises at finalize | `cost_usd = 0`, task ships, log warning | warning |

Tier 4's hardcoded constants mirror v2 egress's hardcoded `Decimal("0.09")` — true last resort, fires only when the catalog cannot speak at all. Eliminates the drift surface where the catalog's documented default and a parallel hardcoded constant diverge over time.

### 7.2 Fail-silent specifics

- **Detection probe failure** (no IMDS, no DMI, no env var) → `CloudEnv` unresolved → Tier 3.
- **Catalog load failure** → Tier 4; warning logged per convention §11.
- **Computation failure at finalize** → Tier 5: the compute back-fill step in `_aggregate_costs` is wrapped so a pricing bug cannot break task finalization. Task ships with correct LLM/external/network costs and `compute_cost_usd` derived from whatever events succeeded.
- **Fargate metadata endpoint unreachable** (v1 capture §6 case 10) → event ships with `vcpu_count: null` + `memory_bytes_limit: null` → pricing dispatcher returns Tier 3 (universal default) for that event.
- **K8s `/api/v1/nodes` 403 with `k8s_node_aware: true`** → fall through to §6.11 limits-based math, WARN_ONCE per convention §11.

### 7.3 Warning logging — convention §11 (per failure mode per process)

The compute pricing engine owns its own module-level tracking set, distinct from the egress pricing engine's set. Failure modes:

- `catalog_missing`, `catalog_malformed`, `meta_default_missing:<billing_model>`
- `region_rate_malformed:<provider>:<runtime>:<region>`
- `fargate_metadata_unreachable`
- `k8s_node_probe_forbidden`, `k8s_node_probe_timeout`, `k8s_node_probe_malformed`
- `unsupported_billing_model:<billing_model>` (defensive — should never fire in production)

Each fires once per process per mode; `_reset_compute_warning_state_for_tests()` resets in tests.

---

## 8. Schema & migration

### 8.1 No SQLite migration

`compute_cost_usd` is already on the `tasks` table (shipped in v2 egress's v4→v5 migration, alongside `network_cost_usd`). v1 capture made it populated by auto-emitted events; v2 cost-attribution makes the population non-zero. **No new migration in v2 compute.**

### 8.2 No event schema change

`compute_cost` is already in the `event_type` enum (shipped in baseline). v1 capture defined the `details.*` shape; v2 cost-attribution only adds the back-fill of `cost_usd` / `pricing_source` / `cost_confidence` / `pricing_version` — all top-level event fields that already exist.

### 8.3 `update_event` re-mark-pending

The fix is already in v2 egress §8.2 — `update_event` re-marks `sync_status='pending'` so finalize-time `cost_usd` corrections re-sync. v2 compute relies on the SAME fix; no separate work.

### 8.4 Backward compatibility

- A new SDK on an existing v5 DB → no migration needed.
- Old `compute_cost` events written by the user-driven `record_cost(event_type="compute_cost", ...)` API are untouched. They never had `cost_pending: true`, so the back-fill walker (§5 of capture spec, §9.3 here) skips them.
- `pricing_version` strings on auto-emitted compute events use the prefix `compute:<catalog_version>` (e.g. `compute:1.0.0`) — a distinct namespace from `egress:` so reconciliation queries can identify the catalog version that produced any number.

---

## 9. Cost attribution flow at task finalize

### 9.1 Where this hooks in

`_aggregate_costs` (Python) / `aggregateCosts` (Go/TS) / `finalize_network` (Rust) is the existing task-finalize step that v2 egress already extended. v2 compute extends it further:

```
1. Existing: resolve cloud_env, resolve egress rate, back-fill network event cost_usd, compute task.network_cost_usd.
2. NEW: walk compute_cost events where details.cost_pending == True:
     a. For each event, call compute_pricing.resolve_compute_cost(event.details, cloud_env, init_overrides)
     b. Stamp event.cost_usd / pricing_source / cost_confidence / pricing_version
     c. Strip event.details.cost_pending
     d. storage.update_event(event)        # re-marks sync_status='pending' per §8.3
3. Recompute task.compute_cost_usd = sum(e.cost_usd for e in task.compute_cost_events)
4. Recompute task.total_cost_usd.
```

### 9.2 Per-event vs per-task

`task.compute_cost_usd` is computed from the event sum — the event's `cost_usd` IS the truth, not a separate task-level computation. v1 has at most one `compute_cost` event per task (capture §5.3), so the sum is trivially the event's value; the framing matters because v2-of-this-spec (per-runtime breakdown) would introduce multiple events per task and the sum-IS-truth pattern transfers cleanly.

### 9.3 User-driven `record_cost(event_type="compute_cost", ...)` coexistence

The user-driven API path (`record_cost(...)`) creates events with `cost_usd` already populated and NO `cost_pending: true` marker. The back-fill walker filters on the marker — user-driven entries are skipped, their `cost_usd` flows into the task sum unchanged. Automatic + manual compute events sum together.

---

## 10. Testing (Python first)

### 10.1 Unit tests

- **`compute_pricing.py` — per-billing-model math**, one canonical case per billing model, asserting BOTH `cost_usd` and `pricing_source`:
  - Lambda x86_64 1024 MiB × 100 ms in us-east-1 → expected = `Decimal("0.0000002") + Decimal("1.073741824") * Decimal("0.1") * Decimal("0.0000166667")` (hand-computed; pin to a fixed string).
  - Lambda arm64 same shape → ~20% cheaper (catches arch-keying regression).
  - Fargate 0.5 vCPU × 1 GiB × 60 s in us-east-1 → `(0.5 * 60) * 0.0000111111 + (1.0 * 60) * 0.0000012222`. **Specifically pin the `/Decimal(1024*1024*1024)` divisor**, not `/Decimal("1000000000")` — the test fails fast if a future refactor confuses GB/GiB on Fargate (the ~4.86% silent bug).
  - Cloud Run request-based 0.5 vCPU × 256 MiB × 250 ms in us-central1.
  - Cloud Run instance-based via override 0.5 vCPU × 256 MiB × 60 s window.
  - Azure Functions 512 MB × 200 ms.
  - Vercel Fluid 256 MB × 500 ms — confirms the active-CPU-≈-duration approximation.
  - EC2 c7g.xlarge (4 vCPU @ $0.1450/hr) with 1.0 vCPU-seconds used over 60s window → `share_factor = 1/(4*60) = 0.004166...`, `task_instance_hours = 0.004166 * (60/3600)`, `cost = task_instance_hours * 0.1450`. Hand-computed expected.
  - K8s pod 0.5 vCPU limit × 60 s window with `k8s_node_aware: false` → `0.5 * (60/3600) * default_k8s_pod_vcpu_hour_usd`.

- **`test_decimal_no_float_drift`** (extends v2 egress version): asserts every per-runtime conversion divisor in §6.2 stays Decimal. Specifically:
  - `Decimal(2147483648) / Decimal(1024*1024*1024) == Decimal("2")` (Fargate / Cloud Run).
  - `Decimal(1000000000) / Decimal("1000000000") == Decimal("1")` (Lambda / Azure Functions / Vercel).
  - A multiplication-step case per billing model against a hand-computed expected (catches float introduction at the multiply, not just the divide).

- **Per-billing-model conversion-table integrity** — a parametrized test that walks the §6.2 table and asserts the divisor each `billing_model` uses matches the table. Catches a future refactor that quietly switches Fargate to decimal GB.

- **Cloud Run override** — `compute_billing_overrides: { cloud_run: "instance" }` flips the math to §6.6; assert `cost_confidence == "computed"` and `pricing_source` ends `instance_override`.

- **K8s node-aware path** — mocked `/api/v1/nodes/<name>` returning a valid JSON `.status.capacity.cpu=8` + `.metadata.labels["node.kubernetes.io/instance-type"]="c7g.xlarge"` → §6.12 math. Mocked 403 → fall through to §6.11 + assert WARN_ONCE log.

- **Catalog integrity** — `compute_prices.json` parses; every rate is a valid Decimal string; `_meta` has all required default keys; every provider has a parseable ISO-8601 `_last_verified`; soft freshness check (180 days). Every billing model in the §6 dispatch has at least one rate path in the catalog (no orphan code paths).

- **Warning-once-per-failure-mode** — trigger `catalog_missing`, assert one log; trigger `meta_default_missing:lambda`, assert a second log; reset the tracking set between cases.

### 10.2 Integration tests

- Mock Lambda runtime: env vars set, handler invoked via wrap, event emits with `cost_pending: true`, task finalize back-fills the Lambda math, asserts `task.compute_cost_usd == event.cost_usd > 0`, asserts the `cost_pending` marker is gone, asserts the event row's `sync_status` is back to `pending`.

- Mock Fargate: ECS metadata endpoint served from a local HTTP server, runtime resolver picks `fargate`, accountant snapshots cgroup at task start/end, event emits at finalize, cost back-filled with the **GiB divisor** — and the test asserts the exact expected cost (catches a regression into GB).

- Mock Cloud Run (default): event emits with `billing_model: "cloud_run_request"`, asserts `pricing_source == "compute_catalog:cloud_run:request_based_default"`, `cost_confidence == "estimated"`.

- Mock Cloud Run (override): same shape with `compute_billing_overrides: { cloud_run: "instance" }`, asserts `pricing_source` ends `instance_override`, `cost_confidence == "computed"`, math matches §6.6.

- Mock EC2 with two back-to-back dexcost tasks on the same VM: assert each task gets its own `share_factor` based on its own cgroup CPU diff; idle time between tasks is invisible (no third event); the SUM of `cost_usd` across the two tasks is LESS than the corresponding cloud-bill share (the idle gap — pins Decision #9 explicitly).

- **Decision #9 explicit pin** — a long-running EC2 scenario: 2 tasks of 60s each, 600s idle between them, on a 4 vCPU @ $0.1450/hr instance. Assert `sum(task.compute_cost_usd) < 720s × $0.1450/3600s` (because idle 600s is excluded). The inequality IS the test — the gap is by design.

- **Decision #10 explicit pin** — same shape on Fargate: 3 dexcost tasks back-to-back, 50 minutes idle tail before container shutdown. Assert `sum(task.compute_cost_usd) < (total_container_lifetime × Fargate rate)`. Same gap, same design.

- **Per-event vs per-task consistency** — `task.compute_cost_usd == sum(event.cost_usd for event in task.compute_events)` for arbitrary task shapes.

- **`total_cost_usd` arithmetic** — `task.total_cost_usd == llm + external + compute + network` (the invariant restated for completeness).

- **Fail-silent** — corrupt `compute_prices.json` → SDK still runs, Tier 4 hardcoded fallback kicks in, the warning is logged once, the customer's task is unaffected.

- **User-driven `record_cost(event_type="compute_cost", ...)` coexistence** — manual event + auto event on the same task; both contribute to `task.compute_cost_usd`; the back-fill walker leaves the manual one untouched.

- **Cross-runtime regression matrix** — one task per `billing_model` value (lambda, fargate, ec2, cloud_run_request, cloud_run_instance, cloud_functions, azure_functions, vercel_fluid, k8s_pod), each emitted with a known fixture, each priced from the catalog. Catches a dispatch-table regression.

### 10.3 Property invariants

These hold across arbitrary task shapes. Parametrized property tests, generating scenarios over: billing model (all 9 values); region (covered + uncovered); architecture (x86 + arm64); duration (1 ms — 1 hour); memory (128 MiB — 64 GiB); vCPU count (0.25 — 64).

1. `task.compute_cost_usd >= Decimal("0")` — never negative.
2. `task.compute_cost_usd == sum(e.cost_usd for e in task.compute_cost_events)` — sum-IS-truth.
3. For any two tasks A, B on the same runtime/region/SKU with `A.duration == 2 × B.duration` and `A.memory == B.memory` and `A.vcpu == B.vcpu`, `A.cost_usd ≈ 2 × B.cost_usd` modulo per-request constants — linearity in the right axes.
4. ARM rate < x86 rate for the same Lambda / Fargate / EC2 SKU pair (~20% cheaper) — guards against an arch-keying regression that silently bills ARM at x86 rates.
5. For every successfully resolved event, `cost_confidence ∈ {"computed", "estimated"}` (NEVER `"unknown"` on a well-formed input).
6. `pricing_source` starts with `compute_catalog:` for every successfully resolved event.

### 10.4 Cross-language test matrix

Each SDK port (Go, Rust, TypeScript) implements the same test matrix — identical unit cases, integration scenarios, and the six property invariants — translated to that language's testing idioms. `compute_prices.json` is a single canonical file (Python) synced to the other three SDKs via `scripts/sync_compute_catalog.sh` (convention §6); the catalog-integrity test plus the assertion that every SDK reads the same file proves cross-language consistency.

### 10.5 Explicitly not tested

- **No performance benchmarks.** Pricing dispatch is O(1) per event; perf assertions are flaky in CI.
- **No real-cloud price assertions.** The catalog rates are point-in-time snapshots (per the decisions log "What happens next"); tests assert the MATH (`x * y = z`), not that the catalog rates match live provider docs. Re-verification lives in the implementation plan as an explicit task.
- **No instance-utilization assertions.** That's a Control Layer / reconciliation surface concern.

---

## 11. Future (out of scope for this spec)

- **Go / Rust / TypeScript ports** — each its own spec → plan → implementation cycle. The shared `compute_prices.json` is the cross-SDK contract.
- **`compute_by_runtime` per-task aggregate** — analog of `network_by_host`. Adds a JSON blob field on the task listing per-billing-model sub-totals. Deferred until a dashboard need surfaces.
- **Lambda provisioned-concurrency idle billing** — schema already forward-compatible (`details.initialization_type` captured for all values). Math extends additively: PC idle period × `gb_second_rate`.
- **Lambda init-phase tracking** — cold-start init duration is billable but happens before the handler wrap. v1.1 adds an init hook (custom runtime / Lambda Extensions API) or a second `compute_cost` event per invocation.
- **Cgroup-v1 support** — older RHEL 7 / CentOS 7 fall through to estimated confidence in v1 (no cgroup-v2 files). Adding cgroup-v1 readers extends the precision.
- **Azure Functions Premium + Dedicated plans** — v1 covers Consumption only. Premium plan has its own per-vCPU/per-GB-second model; Dedicated is just an App Service VM (same shape as `azure_vm`).
- **Multi-CPU Vercel Fluid sandboxes** — v1 approximates as 1.0 active CPU per wall hour.
- **Sustained-use discounts (GCE) / Savings Plans (AWS) / Reserved Instances** — the SDK does not know the customer's commitment posture. The future reconciliation surface explains the variance.
- **Per-container compute breakdown on multi-container Fargate tasks** — v1 bills at the task scope.
- **Cost Intelligence / reconciliation surface** — Control Layer scope. Where #9 and #10 idle gaps get explained as line items; where catalog-vs-invoice variance gets surfaced; where instance-utilization metrics live.
- **Catalog refresh tooling** — the SDK ships with a `compute_prices.json` snapshot. Ongoing per-provider re-verification is a repo-side workflow (cron + PR), not an SDK responsibility.
- **GPU runtimes (Modal / RunPod / CoreWeave / Lambda Labs / Replicate)** — these are *detected* by `cloud_detect`, but their compute+GPU combined billing models are subsystem C scope.

---

## 12. Non-goals

- **No reading the cloud bill, CUR, or any invoice format.** Convention §10 source-measurement boundary. dexcost computes from observed measurement + bundled catalogs.
- **No synthetic "idle" pseudo-tasks to make totals match the bill.** Decisions #9 + #10. The gap IS the signal.
- **No user-facing pricing-rate configuration.** No `compute_cost_per_vcpu_hour` `init()` parameter. The `compute_billing_overrides` knob disambiguates billing MODE (request-vs-instance for Cloud Run, future cases), not RATES.
- **No per-event compute cost on `llm_call` / `external_cost` / `network` events.** Convention §4. Compute attribution lives on `task.compute_cost_usd`; events carry single-meaning fields.
- **No "free" runtime detection for unsupported billing models in v1.** Modal / RunPod / Render / Railway / Heroku / Koyeb runtimes are detected by `cloud_detect` (provider attribution lights up) but their `compute_cost` events are not emitted in v1 — adding their billing models is a v1.1 additive change to the dispatch table.
- **No sub-second billing precision claims.** Lambda / Cloud Run / Azure Functions / Vercel bill at millisecond resolution; the SDK measures at monotonic-clock resolution which is per-language but always sub-ms. The catalog rates are exact; the timing inputs are precise enough that compute math reproduces the cloud bill to within the SDK's measurement precision (typically <0.1%).
