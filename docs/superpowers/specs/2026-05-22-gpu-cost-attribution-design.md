# GPU Cost (v2) — Attribution & Pricing — Design

**Date:** 2026-05-22
**Status:** Approved design — ready for implementation planning. Catalog rates already live-verified (`79c8745`); spec sections referencing the catalog cite verified facts.
**Scope:** Python SDK first; Go / Rust / TypeScript ports follow as separate plans.
**Sibling spec (v1, capture):** [`2026-05-22-gpu-capture-design.md`](2026-05-22-gpu-capture-design.md) — NVML cgroup-walk, runtime cascade, `gpu_cost` + `gpu_utilization_signal` event shapes.
**Reference decisions:** [`decisions/2026-05-22-gpu-foundation-decisions.md`](../decisions/2026-05-22-gpu-foundation-decisions.md) — 11 locked decisions + 11 strengthenings.
**Reference research:** [`research/2026-05-22-gpu-foundation-research.md`](../research/2026-05-22-gpu-foundation-research.md)
**Reference catalog:** `python/src/dexcost/data/gpu_prices.json` (v1.0.0, 32 SKUs, 8 providers, 243 entries — live-verified `79c8745`).
**Cross-subsystem conventions:** [`conventions.md`](../conventions.md) — inherits §§1, 2, 3, 4, 5, 6, 7, 9, 11; extends §1 (signal-event carve-out) and §8 (NVML cgroup-walk primitive).

---

## 1. Purpose & Scope

The v1 capture spec ([`2026-05-22-gpu-capture-design.md`](2026-05-22-gpu-capture-design.md)) defines **what** the SDK measures for GPU — runtime resolution, NVML cgroup-walk, `gpu_cost` event with `cost_pending: true`, `gpu_utilization_signal` events with no cost. It deliberately stops at measurement.

This v2 spec turns those measurements into dollars: a `gpu_cost_usd` figure per task, attributed alongside `llm_cost_usd` / `external_cost_usd` / `compute_cost_usd` / `network_cost_usd`. **Total cost** becomes `llm + external + compute + network + gpu`.

**Core principle — same as Phase 1.** dexcost is automatic and catalog-driven. No `init()` knobs for GPU pricing rates. The bundled `gpu_prices.json` catalog + the auto-detected runtime ([`compute_runtime`](2026-05-22-gpu-capture-design.md#5-components--flow) extended for GPU) are the only inputs to the dollar math.

**In scope (Python, this spec):**
- A bundled GPU price catalog (`data/gpu_prices.json`) — already in place per `79c8745`.
- A GPU pricing engine (`gpu_pricing.py`) mirroring `compute_pricing.py` and `egress_pricing.py`.
- Per-billing-model cost math — `per_instance_hour`, `per_gpu_second_active`, `per_gpu_hour_reserved`, `per_vgpu_hour`.
- NVML productName → catalog-SKU alias resolution with NFC normalization (Decision #4) and device_class fallback.
- `Task.gpu_cost_usd` populated from auto-emitted `gpu_cost` events.
- Per-event `cost_usd` back-fill at task finalize (the Phase 1 §6.4 deferred-cost pattern).
- `gpu_utilization_signal` events untouched by pricing (observability-only — Decision #3).

**Out of scope (see §11):** Go/Rust/TS ports; AMD/Intel pricing (Decision #5); MIG per-slice billing (Decision #2); cgroup-v1 / non-Linux hosts (no events emitted, nothing to price); `gpu_by_device` per-task aggregate (v2 if dashboards need it); GPU-aware reconciliation (Control Layer scope).

---

## 2. Decisions (cost-math-relevant)

The full 11-decision log lives at [`decisions/2026-05-22-gpu-foundation-decisions.md`](../decisions/2026-05-22-gpu-foundation-decisions.md). This spec restates the cost-math-relevant ones in narrative form.

1. **Four billing models** (Decision #6 of the original compute spec equivalent — promoted in GPU because the fanout is smaller and cleaner):
   - `per_instance_hour` — AWS EC2 GPU, GCP GCE bundled, Azure VM GPU. Math: `share_factor × hourly_rate` (analog of compute's EC2 share math).
   - `per_gpu_second_active` — Modal, RunPod, Replicate. Math: `gpu_seconds_used × rate_per_gpu_second`.
   - `per_gpu_hour_reserved` — Lambda Labs, CoreWeave, GCP GCE N1+accelerator. Math: `share_factor × hourly_rate_per_gpu` (similar to instance-hour but per-GPU not per-instance).
   - `per_vgpu_hour` — Azure NVadsA10 v5 fractional. Math: `share_factor × vgpu_hourly_rate` against the fractional rate.

2. **No VRAM divisor** (Decision #7). VRAM tier is in the SKU key; `vram_gb` is display-only. The Phase 1 Decision #7 binary-vs-decimal headache that bit Fargate does NOT recur here.

3. **NVML productName → catalog-key via aliases** (Decision #4). Each SKU in the catalog carries an `aliases: [...]` array of NVML productName strings. Resolution: NFC-normalize the observed productName, lowercase, collapse whitespace, strip — match against the aliases. Device-class fallback (`hopper` / `ampere` / `ada-lovelace` / `blackwell`) for cold-start unknown SKUs.

4. **NVIDIA only in v1** (Decision #5). `details.gpu_vendor = "nvidia"` on every event for forward-compat with v1.1 AMD/Intel.

5. **MIG = full-GPU rate** (Decision #2). `details.mig_profile` captured but ignored for v1 math.

6. **Source-measurement boundary inherited** (Decision #6, Phase 2). The "$X attributed vs $Y invoiced" gap is by design; the `gpu_utilization_signal` events surface it. The reconciliation surface (future) explains the variance line-by-line.

7. **Weekly refresh cadence + 90/365-day soft-warn/hard-fail** for the GPU catalog (Decision #11). The integrity test emits WARN at 90 days, ERROR at 365.

---

## 3. Architecture & Data Model

### 3.1 New modules

| File | Responsibility |
|---|---|
| `python/src/dexcost/data/gpu_prices.json` | **Already shipped** — bundled, versioned GPU rate catalog. v1.0.0, 32 SKUs, 8 providers. |
| `python/src/dexcost/gpu_pricing.py` | Loads the catalog, dispatches on `details.billing_model`, applies the §6 math, returns `cost_usd` + `pricing_source` + `cost_confidence`. Mirrors `compute_pricing.py`. |
| `scripts/sync_gpu_catalog.sh` | **Already shipped** — syncs `gpu_prices.json` to Go / Rust / TS bundles. |

`compute_runtime.py` is extended (in capture spec) with the `GpuRuntimeKind` enum + NVML resolver. The pricing engine consumes events emitted by `GpuAccountant` (capture spec §5).

### 3.2 Data model changes

- **`Task.gpu_cost_usd: Decimal`** — new field, sibling of `compute_cost_usd` / `network_cost_usd`. Default `Decimal("0")`.
- **`Task.total_cost_usd`** invariant: `llm + external + compute + network + gpu`. Five aggregates.
- **`gpu_cost` event `cost_usd`** — back-filled at task finalize from the catalog. `cost_pending: true` marker stripped after back-fill.
- **`gpu_utilization_signal` events** are NEVER priced. The pricing engine's walk over events for back-fill filters on `event_type == "gpu_cost"` AND `details.cost_pending == true`. Signal events have no cost fields and are skipped.
- **No new schema columns beyond `gpu_cost_usd`** in v1. `gpu_by_device` (per-device breakdown analog of `network_by_host`) is deferred to v2.

### 3.3 The measurement/pricing separation (convention §4 — restated)

> Events carry raw measurement (`gpu_seconds_used`, `sm_util_pct`, `vram_used_peak_bytes`, `billing_model`). The task carries derived attribution (`task.gpu_cost_usd`).

For GPU v1 there is at most one `gpu_cost` event per task (capture §5.3), so `task.gpu_cost_usd = sum(gpu_cost.cost_usd for that task)` is trivially the event's own `cost_usd`. The "sum is the truth" framing holds; v2 of this spec (a `gpu_by_device` breakdown) would extend it the same way the network and compute v2 specs extended their per-aggregate breakdowns.

### 3.4 LLM / vendor / compute / network instruments need no changes

GPU cost does NOT get stamped onto any other event type. An LLM call whose handler runs on a Modal H100 generates ONE `llm_call` event (LLM dollars) AND contributes its share to ONE `compute_cost` event (compute dollars, the Modal CPU portion) AND contributes its share to ONE `gpu_cost` event (GPU dollars). Three event types, three single-meaning cost fields on the task. Convention §5 ("≤1 event per call ≠ 1 cost category per call") applies identically.

---

## 4. The GPU catalog (`data/gpu_prices.json`) — already shipped

### 4.1 Structure (live-verified — see commit `79c8745`)

Mirrors `compute_prices.json` shape: `_meta` block + per-provider blocks with `_last_verified`. The provider blocks carry billing-model-specific entries:

```json
{
  "_meta": {
    "version": "1.0.0",
    "last_updated": "2026-05-22",
    "currency": "USD",
    "default_per_instance_hour_usd": "55.04",
    "default_per_gpu_second_active_usd": "0.000694",
    "default_per_gpu_hour_reserved_usd": "3.99",
    "default_per_vgpu_hour_usd": "0.454",
    "description": "Dexcost GPU catalog — per-billing-model rates by cloud provider/SKU. Community-maintained...",
    "notes": "VRAM tier encoded in SKU key; no per-VRAM multiplier. NVIDIA only in v1. Refresh cadence: WEEKLY..."
  },
  "aws": {
    "_last_verified": "2026-05-22",
    "ec2_gpu": {
      "regions": {
        "us-east-1": {
          "instance_types": {
            "p5.48xlarge": {
              "hourly_usd": "98.32",
              "vcpu_count": "192",
              "memory_gb": "2048",
              "gpu_count": "8",
              "gpu_sku": "h100-80gb-sxm5",
              "gpu_vram_gb": "80",
              "aliases": ["NVIDIA H100 80GB HBM3"]
            },
            ...
          }
        }
      }
    }
  },
  "gcp": {
    "_last_verified": "2026-05-22",
    "gce_gpu_attached": { ... per-region accelerator_types map for N1 ... },
    "gce_gpu_bundled":  { ... per-region instance_types map for A2/A3/G2 ... }
  },
  "azure": {
    "_last_verified": "2026-05-22",
    "vm_gpu":  { ... per-region NCs_v3/ND/H100 instance_types ... },
    "vm_vgpu": { ... per-region NVadsA10 v5 fractional with vgpu_profile ... }
  },
  "modal":       { "_last_verified": "2026-05-22", "per_gpu_second_active": { "default": { "h100": { "gpu_second_usd": "0.001097", "gpu_sku": "h100-80gb-sxm5", "aliases": [...] }, ... } } },
  "runpod":      { "_last_verified": "2026-05-22", "per_gpu_second_active": { "default": { "on_demand": {...}, "community_cloud": {...} } } },
  "lambda_labs": { "_last_verified": "2026-05-22", "per_gpu_hour_reserved": { "default": { "h100-sxm5": { "gpu_hour_usd": "3.99", ... }, ... } } },
  "coreweave":   { "_last_verified": "2026-05-22", "per_gpu_hour_reserved": { "default": { ... } } },
  "replicate":   { "_last_verified": "2026-05-22", "per_gpu_second_active": { "default": { ... } } }
}
```

### 4.2 Encoding & precision

- **All rates are string-encoded Decimals** (`"3.99"`, `"0.001097"`). Loader parses with `Decimal(...)` — never `float(...)`. Test `test_decimal_no_float_drift` pins this (mirrors Phase 1).
- **`currency: "USD"`** for v1.
- **`aliases: [...]`** — list of NVML `productName` strings that map to the catalog SKU. Matched after NFC + lowercase + whitespace-collapse normalization (Decision #4 sharpening).
- **`gpu_sku`** — canonical key shared across providers. A Modal H100, an AWS p5's H100, and a Lambda Labs H100 SXM5 all share `gpu_sku: "h100-80gb-sxm5"`. This is what lets a customer compare "this workload on Modal = $X/hr-equivalent vs same workload on AWS p5 = $Y/hr-equivalent" through dexcost analytics.
- **`gpu_vram_gb`** — display-only per Decision #7. NEVER used as a multiplier in §6 math.

### 4.3 Per-provider freshness

Each provider has `_last_verified` (ISO-8601). Catalog-integrity test asserts every provider has a parseable date and **soft-warns at 90 days, hard-fails at 365 days** for GPU (vs 180/730 for compute per Decision #11).

### 4.4 No sustained-use / Savings Plans / Reserved Instance modelling

Inherited from Phase 1 compute. Standard on-demand rates only. Customers with heavy RI coverage on EC2 GPU will see `dexcost gpu total` higher than their AWS invoice; the future reconciliation surface explains the variance.

### 4.5 Launch coverage (already at adequate level — see `79c8745`)

`79c8745` lands with:
- AWS EC2 GPU: 119 entries across 6 regions
- GCP GCE GPU: 39 entries (bundled + attached-accelerator)
- Azure VM GPU: 20 entries across 4 regions (including 6 NVadsA10 v5 vGPU profiles)
- Modal: 10 SKUs (T4, L4, L40S, A10G, A100-40, A100-80, H100, H200, B200 if listed)
- RunPod: 29 entries (community + on-demand spreads)
- Lambda Labs: 13 SKUs including per-cluster-size variants
- CoreWeave: 9 SKUs
- Replicate: 4 SKUs (T4, A40, A100, H100)

**Known launch gaps** (from `79c8745` commit message):
- GCP A4 / A4X (B200 / GB200) per-accelerator rates — `cloud.google.com/compute/gpus-pricing` returned JS-rendered content; re-verify before declaring complete.
- Lambda Labs H200 — published but agent omitted; backfill in next refresh pass.
- Azure NVadsA10 v5 vGPU profile NVML alias strings populated from convention; verified by the `azure-nvadsa10-v5-vgpu/` spike when it lands.

### 4.6 Rate resolution order

Five-tier degradation ladder (convention §7) applied per billing model:

1. `(provider, billing_model, sku, region)` exact match → SKU rate at `computed` confidence.
2. Provider+billing_model+sku known, region absent (or non-regional providers like Modal/Lambda) → provider+SKU default → `computed`.
3. Provider+billing_model known, SKU unknown → device-class fallback (Decision #4) → `estimated` + `pricing_source: ":device_class_fallback"`.
4. Catalog file missing / unreadable / malformed → hardcoded per-billing-model constants in code → `estimated` + WARN_ONCE.
5. Computation raises at finalize → `cost_usd = 0`, task ships, warning logged.

---

## 5. The pricing engine (`gpu_pricing.py`)

```
resolve_gpu_cost(event_details: dict, cloud_env: CloudEnv) -> GpuCost(cost_usd, pricing_source, cost_confidence)
```

### 5.1 Dispatch

1. Read `details.billing_model` (one of four values).
2. Read `details.gpu_sku` if present (set by `GpuAccountant` from NVML alias resolution); else fall through to device-class.
3. Look up the rate per §4.6 ladder.
4. Apply per-billing-model math from §6.
5. Wrap the whole dispatch in `try/except` for Tier-5 fail-silent. Return `GpuCost(cost_usd=Decimal("0"), pricing_source="gpu_catalog:error:<billing_model>", cost_confidence="unknown")` on any exception.

### 5.2 Configuration interaction

**v1 introduces NO new init() knobs.** Compute's `compute_billing_overrides` knob has no GPU sibling — GPU billing models are unambiguous per provider (Modal is always `per_gpu_second_active`, AWS EC2 GPU is always `per_instance_hour`, etc.). No customer override needed.

Cloud Run-style override (compute Decision #1) doesn't recur because GPU providers don't expose customer-deploy-time billing-mode choices.

### 5.3 Confidence labelling contract

| Situation | `cost_confidence` | `pricing_source` (example) |
|---|---|---|
| SKU+region exact match in provider's billing-model block | `computed` | `gpu_catalog:aws:ec2_gpu:us-east-1:p5.48xlarge` |
| SKU exact, region non-applicable (Modal/Lambda Labs/etc.) | `computed` | `gpu_catalog:modal:per_gpu_second_active:h100` |
| Provider+SKU known, region missing → provider default | `computed` | `gpu_catalog:aws:ec2_gpu:default:p5.48xlarge` |
| SKU unknown → device-class fallback | `estimated` | `gpu_catalog:aws:ec2_gpu:device_class_fallback:hopper` |
| Provider not in catalog | `estimated` | `gpu_catalog:default:per_instance_hour` |
| Decision #1 self-PID-only fallback | `estimated` | `<provider>:<sku>:self_pid_only` |
| Decision #1 bare-metal-no-container | `estimated` | `<provider>:<sku>:no_container_scope` |
| Decision #1 multi-container K8s partial | `estimated` | `<provider>:<sku>:multi_container_pod_partial` |
| Decision #10 vGPU full-A10 assumption | `estimated` | `gpu_catalog:azure:nvads_a10_v5:full_a10_assumption` |
| Decision #2 MIG, full-GPU rate applied | `computed` | (normal SKU source, with log-once `gpu_mig_detected_full_billing_applied`) |
| Catalog malformed → hardcoded constants | `estimated` + warning | `gpu_catalog:hardcoded:<billing_model>` |
| Tier-5 computation failure | `unknown` | `gpu_catalog:error:<billing_model>` |

---

## 6. Cost computation — per billing model

The heart of the spec. Each subsection is the exact arithmetic for one `details.billing_model` value, written as Decimal expressions for one-for-one implementation.

### 6.1 Universal preliminaries

```
duration_s         = Decimal(details.duration_ms) / Decimal("1000")
gpu_seconds_used   = Decimal(details.gpu_seconds_used)
gpu_count          = Decimal(details.gpu_count)
window_s           = Decimal((task.ended_at - task.started_at).total_seconds())
HOUR_S             = Decimal("3600")
```

`window_s` is used by `per_instance_hour`, `per_gpu_hour_reserved`, `per_vgpu_hour` (the share-factor billing models). `gpu_seconds_used` is used by `per_gpu_second_active`.

### 6.2 No per-runtime conversion table (vs Phase 1's Decision #7)

Phase 1 compute Decision #7 required a per-runtime memory-unit conversion table (decimal GB for Lambda, binary GiB for Fargate, etc.) because providers price memory in different units. Phase 2 GPU has **no equivalent table** — VRAM is never priced per-byte, so there's no divisor question. The `gpu_vram_gb` field exists for display only.

### 6.3 `per_gpu_second_active` (Modal / RunPod / Replicate)

```
cost = gpu_seconds_used * rate_per_gpu_second_usd
```

- `gpu_seconds_used` is the NVML-measured active GPU-seconds across the task's cgroup PIDs (capture spec §5).
- `rate_per_gpu_second_usd` from the catalog (e.g. Modal H100 = `"0.001097"`).
- For multi-GPU tasks: `gpu_seconds_used` is the sum across devices; rate × sum = total cost (this is correct — `per_gpu_second_active` charges per device-second; doubling devices doubles the rate).
- `gpu_count` is captured in `details` for breakdown but not used in this math (already baked into `gpu_seconds_used`).

**Confidence:** `computed` when the rate and `gpu_seconds_used` are both exact (Modal / RunPod / Replicate publish exact per-second rates; NVML reports exact GPU-seconds). This is the highest-precision regime for the GPU subsystem — both sides of the multiplication are measured precisely.

### 6.4 `per_instance_hour` (AWS EC2 GPU / GCP GCE bundled / Azure VM GPU)

```
window_hours       = window_s / HOUR_S
share_factor       = gpu_seconds_used / (gpu_count * window_s)
task_instance_hours = share_factor * window_hours
cost = task_instance_hours * instance_hourly_usd
```

- `instance_hourly_usd` from the catalog's `ec2_gpu.regions[region].instance_types[sku].hourly_usd`.
- `share_factor` lets multiple dexcost tasks on the same instance fairly divide the instance hour. A task that used 1.0 GPU-second on a `p5.48xlarge` (8 GPUs) over a 60s window has `share_factor = 1/(8×60) = 0.002083`, `task_instance_hours = 0.002083 × (60/3600) = 0.0000347`, `cost = 0.0000347 × $98.32 = $0.00341`.
- **Idle time between dexcost tasks is invisible** (Decision #6). The cgroup keeps counting; the SDK only reads at task boundaries. The "unaccounted" instance-hours are what the future reconciliation surface explains using the parallel `gpu_utilization_signal` events.
- Non-cgroup-walking fallback (self-PID-only per Decision #1): `gpu_seconds_used` reflects only the dexcost process's contribution, which usually under-counts. `cost_confidence: estimated`, `pricing_source: ":self_pid_only"`.

### 6.5 `per_gpu_hour_reserved` (Lambda Labs / CoreWeave / GCP GCE N1+accelerator)

```
window_hours        = window_s / HOUR_S
share_factor        = gpu_seconds_used / (gpu_count * window_s)
task_gpu_hours      = share_factor * window_hours * gpu_count
cost = task_gpu_hours * gpu_hourly_usd
```

- Similar to `per_instance_hour` but the rate is per-GPU, not per-instance. For an 8x H100 Lambda Labs box at $3.99/hr/GPU, the customer pays $31.92/hr regardless of GPU count usage; dexcost attributes per-task share of each used GPU-hour.
- `gpu_hourly_usd` from `lambda_labs.per_gpu_hour_reserved.default.h100-sxm5.gpu_hour_usd` etc.
- Same idle-invisible semantics as `per_instance_hour`.

### 6.6 `per_vgpu_hour` (Azure NVadsA10 v5 fractional)

```
window_hours        = window_s / HOUR_S
share_factor        = gpu_seconds_used / window_s     # gpu_count = 1 for vGPU profiles
task_vgpu_hours     = share_factor * window_hours
cost = task_vgpu_hours * vgpu_hourly_usd
```

- `vgpu_hourly_usd` from `azure.vm_vgpu.regions[region].instance_types[sku].vgpu_hour_usd`.
- The fractional profile (1/6, 1/3, 1/2, full) is encoded into the SKU (`a10-vgpu-1of6` vs `a10`); the rate already reflects the fraction. No fractional multiplier in the math.
- Decision #10 fallback: if NVML productName doesn't distinguish the profile, the SKU defaults to `a10` (full) and confidence drops to `estimated` with `pricing_source: ":full_a10_assumption"`. This is a per-`(pending spike capture)` decision.

### 6.7 Multi-billing-model task scope

A task that touches multiple billing models in one dexcost-task window (e.g. a long-running Lambda Labs reservation that ALSO calls a Modal-hosted model mid-task) is conceptually possible but **does NOT happen in practice** — the dexcost task is a customer-defined unit, typically scoped to one runtime environment. v1 emits one `gpu_cost` event per task scoped to the host runtime; cross-runtime GPU usage (the Modal call from inside a Lambda Labs reservation) lands in the embedded Modal task's events.

If a future product surface needs to attribute cross-runtime GPU calls as part of one customer-task aggregate, that's a server-side rollup, not an SDK change.

---

## 7. Error handling & degradation ladder

GPU v2 extends Phase 1's fail-silent discipline (convention §9). The five-tier ladder (convention §7) instantiated for GPU:

| Tier | Condition | Result | `cost_confidence` |
|---|---|---|---|
| 1 | `(provider, billing_model, sku, region)` exact | SKU+region rate | `computed` |
| 2 | Provider+SKU known, region missing (or non-regional provider) | Provider's `default` block rate | `computed` (rates not differing across regions on Modal/Lambda) |
| 3 | SKU unknown → device-class fallback (Decision #4) | Device-class default rate | `estimated` |
| 4 | Catalog file missing/unreadable/malformed | Hardcoded per-billing-model constant in code | `estimated` + WARN_ONCE |
| 5 | Computation raises at finalize | `cost_usd = 0`, task ships | `unknown` + warning |

Plus the Decision #1 lineage that surfaces as `pricing_source` suffixes (`:self_pid_only`, `:no_container_scope`, `:multi_container_pod_partial`) — these are NOT separate ladder tiers; they're orthogonal to the rate-resolution ladder and signal that the **measurement** (not the rate) was degraded. Such events stay at `estimated` confidence regardless of whether the rate match was Tier 1, 2, or 3.

### 7.1 Tier-4 hardcoded constants

The Python `_HARDCODED` block in `gpu_pricing.py`:

```python
_HARDCODED = {
    "per_instance_hour":      {"hourly_usd":         Decimal("55.04")},      # ~p4d.24xlarge baseline
    "per_gpu_second_active":  {"gpu_second_usd":     Decimal("0.000694")},   # A100-40GB Modal rate
    "per_gpu_hour_reserved":  {"gpu_hour_usd":       Decimal("3.99")},       # Lambda H100 SXM
    "per_vgpu_hour":          {"vgpu_hour_usd":      Decimal("0.454")},      # Azure NV6 1/6 A10
}
```

Must mirror `_meta.default_*_usd` in the catalog. The catalog-integrity test asserts they match (mirrors Phase 1's pattern).

### 7.2 Warning logging — convention §11 per-mode tokens

GPU pricing engine owns its own warned-modes set:

- `catalog_missing`, `catalog_malformed`, `meta_default_missing:<billing_model>`
- `sku_unknown:<provider>:<productName>` (Decision #4 → device-class fallback fired)
- `device_class_unknown:<productName>` (Decision #4 → even class fallback failed, cost_usd=0)
- `gpu_pricing_failure:<billing_model>` (Tier-5 catch-all)

Resettable via `_reset_warning_state_for_tests()` (test helper).

---

## 8. Schema & migration

### 8.1 SQLite schema migration

v1 GPU adds `Task.gpu_cost_usd` as a new column. Mirrors the Phase 1 compute approach (which added `compute_cost_usd` to the existing schema). Per-SDK approach:

- **Python:** `ALTER TABLE tasks ADD COLUMN gpu_cost_usd TEXT NOT NULL DEFAULT '0';` (Decimal-as-TEXT, `Decimal("0")` default for existing rows).
- **Go / Rust / TypeScript:** equivalent ALTER + struct field addition.

The `total_cost_usd` column already exists; the application-level computation extends to include `gpu_cost_usd`.

### 8.2 Event schema additions

`dexcost-event.v1.json` gains two new enum values: `"gpu_cost"` and `"gpu_utilization_signal"`. Both have `details` objects as documented in capture spec §4.

The Control Layer accepts unknown future enum values gracefully (Phase 1 contract); old Control Layer versions that haven't been updated will route GPU events to dead-letter, which is the correct behavior.

### 8.3 `pricing_version` namespace

GPU events use `pricing_version: "gpu:<catalog_version>"` (e.g. `"gpu:1.0.0"`). Distinct from `compute:<version>` and `egress:<version>` so reconciliation queries can identify which catalog produced each number.

---

## 9. Cost attribution flow at task finalize

### 9.1 Where this hooks in

`_aggregate_costs` (Python tracker, similar in other SDKs) already extended in Phase 1 to handle compute back-fill after the existing egress back-fill. v2 GPU extends it further with a third back-fill block:

```
1. Existing: aggregate llm_cost / external_cost from events; aggregate network bytes.
2. Existing: resolve cloud_env, resolve egress rate, back-fill network event cost_usd, compute task.network_cost_usd.
3. Existing (Phase 1): finalize_compute() — emit + back-fill compute_cost events.
4. NEW (Phase 2): finalize_gpu() — walk gpu_cost events with cost_pending=true, back-fill via GpuPricingEngine.resolve_gpu_cost(). Update task.gpu_cost_usd by delta (NOT recompute).
5. NEW: walk gpu_utilization_signal events for the task — these don't need back-fill, just confirm they're persisted. (No cost involvement at all.)
6. Recompute task.total_cost_usd.
```

Same delta-vs-recompute discipline from Phase 1's compute integration (preserves retry_marker event costs already summed by the main aggregation loop).

### 9.2 Per-event vs per-task

`task.gpu_cost_usd` is computed from the event sum — the event's `cost_usd` IS the truth. v1 has at most one `gpu_cost` event per task; sum trivially equals event value. The framing matters for v2 (`gpu_by_device` breakdown) where multiple events per task would sum.

### 9.3 User-driven `record_cost(event_type="gpu_cost", ...)` coexistence

The user-driven path creates events with `cost_usd` already populated and NO `cost_pending` marker. The back-fill walker filters on the marker — user-driven entries are skipped, their `cost_usd` flows into the task sum unchanged. Automatic + manual GPU events sum together. Same pattern as Phase 1 compute.

---

## 10. Testing (Python first)

### 10.1 Unit tests

- **`gpu_pricing.py` — per-billing-model math**, one canonical case per billing model:
  - **Modal H100 × 1.234 seconds** → `Decimal("1.234") × Decimal("0.001097")`, exact Decimal equality.
  - **Lambda Labs H100 SXM 1.0 GPU × 60s window** → share-factor + hourly math.
  - **AWS p5.48xlarge 1.0 GPU-second over 60s window with 8 GPUs** → `(1 / (8 × 60)) × (60/3600) × $98.32` = ~$0.00341.
  - **Azure NVadsA10 v5 NV6 (1/6 A10) 1.0 GPU-second over 60s** → fractional-rate × share-factor math.
- **`test_decimal_no_float_drift`** — extends Phase 1's test. Asserts every catalog rate is Decimal-parseable; asserts multiplication examples don't go through float.
- **Decision #4 alias matching** — feed `productName = "NVIDIA H100 80GB HBM3"` (non-breaking spaces) → assert NFC-normalize + whitespace-collapse → matches `"NVIDIA H100 80GB HBM3"` alias. Feed unknown productName → device-class fallback applied → `estimated` confidence.
- **Decision #2 MIG transparency** — fixture event with `details.mig_profile = "1g.5gb"` → assert full-GPU rate applied AND log-once `gpu_mig_detected_full_billing_applied` fired.
- **Catalog integrity** — `gpu_prices.json` parses; every rate is a valid Decimal string; every provider has `_last_verified` ISO-8601; **soft-warn at 90 days, hard-fail at 365** (mirrors Phase 1 but tighter per Decision #11); every dispatched `billing_model` value has at least one rate path in the catalog.
- **Warning-once-per-failure-mode** — same pattern as Phase 1.

### 10.2 Integration tests

- Mock Modal runtime: env vars set, handler invoked via wrap, NVML mock returns plausible H100 productName + utilization samples → `gpu_cost` event emits with `cost_pending: true`, task finalize back-fills, asserts `task.gpu_cost_usd == event.cost_usd > 0`, asserts marker stripped, asserts `sync_status` re-marked pending.
- Mock long-running EC2 `p5.48xlarge` task: cgroup walk returns 8 PIDs across an 8-GPU instance, NVML mock returns 30 GPU-seconds across devices → `gpu_cost` event with `billing_model: per_instance_hour`, share-factor math correct against $98.32/hr.
- Mock non-root container: NVML returns permission error on `get_process_utilization` for non-self PIDs → degrade to self-PID-only, `pricing_source: ":self_pid_only"`, `cost_confidence: estimated`.
- Mock bare-metal-host: `/proc/self/cgroup = /user.slice/...` → degrade to self-PID-only, log-once `gpu_no_container_scope`.
- Mock MIG-enabled A100: NVML reports MIG UUIDs → log-once fires, full-A100 rate applied, `details.mig_profile = "1g.5gb"`.
- **Decision #6 explicit idle-gap test** — same shape as Phase 1's: two tasks on the same Lambda Labs H100 with 50-minute idle between them. Assert `sum(task.gpu_cost_usd) < (full_window_hours × $3.99)`. The inequality IS the test — idle invisible by design (Decision #6).
- Per-event vs per-task consistency: `task.gpu_cost_usd == sum(e.cost_usd for e in gpu_cost_events_for_task)`.
- `total_cost_usd` arithmetic: `task.total_cost_usd == llm + external + compute + network + gpu`.
- Fail-silent: corrupt `gpu_prices.json` → SDK runs, Tier-4 hardcoded fallback kicks in, warning logged once, customer's task unaffected.
- User-driven `record_cost(event_type="gpu_cost", ...)` coexistence — manual + auto on same task; both contribute to `task.gpu_cost_usd`.
- Cross-billing-model regression matrix — one task per billing_model value (`per_instance_hour`, `per_gpu_second_active`, `per_gpu_hour_reserved`, `per_vgpu_hour`), each priced from the catalog.

### 10.3 Property invariants

Across arbitrary task shapes. Parametrized:

1. `task.gpu_cost_usd >= Decimal("0")` always.
2. `task.gpu_cost_usd == sum(e.cost_usd for e in task.gpu_cost_events)`.
3. For any two tasks A, B on the same runtime/SKU with `A.gpu_seconds_used == 2 × B.gpu_seconds_used` and same window: `A.cost_usd ≈ 2 × B.cost_usd` (linearity in active-GPU axis).
4. H100 SKU rate > A100 SKU rate on the same provider (newer/faster more expensive).
5. Per-GPU-second rate × 3600 ≈ per-GPU-hour rate (within ~5-15% — Modal serverless markup is real and the gap is the point).
6. For every successfully resolved event, `cost_confidence ∈ {"computed", "estimated"}` (NEVER `"unknown"` on a well-formed input).
7. `pricing_source` starts with `gpu_catalog:` for every successfully resolved event.

### 10.4 Cross-language test matrix

Each SDK port (Go, Rust, TypeScript) implements the same test matrix — identical unit cases, integration scenarios, and the seven property invariants. `gpu_prices.json` is a single canonical file (Python) synced via `scripts/sync_gpu_catalog.sh`; the catalog-integrity test + the byte-equal drift check (mirroring Phase 1) proves cross-language consistency.

### 10.5 Explicitly NOT tested

- **No performance benchmarks.** Pricing dispatch is O(1) per event.
- **No real-cloud price assertions.** Catalog rates are point-in-time; tests assert the MATH (`x × y = z`), not that the catalog matches today's provider invoice.
- **No NVML live tests.** All NVML calls in tests are mocked. Real NVML runs happen in the verification matrix (separate artifact track).

---

## 11. Future (out of scope for this spec)

- **Go / Rust / TypeScript ports** — each its own spec → plan → implementation cycle. Shared `gpu_prices.json` is the cross-SDK contract.
- **`gpu_by_device` per-task aggregate** (v2 if dashboards need it) — analog of `network_by_host`; per-device sub-totals on the Task.
- **AMD ROCm + Intel oneAPI pricing** (v1.1) — `details.gpu_vendor` already in schema; catalog adds `amd` / `intel` provider blocks.
- **MIG per-slice billing** (v1.1 — if any cloud ever ships it) — `details.mig_profile` already captured.
- **Sustained-use discounts (GCE), Savings Plans (AWS), Reserved Instances** — customer commitment posture unknown to the SDK; reconciliation surface explains the variance.
- **Per-VRAM-GiB-second billing** — would require a Decision #7 sibling. None observed in 2026; the catalog adds a new SKU entry if it ever ships, not a multiplier (Decision #7 explicitly).
- **GPU-aware reconciliation surface** (Control Layer) — uses `gpu_utilization_signal` events to render the per-cloud "$X attributed vs $Y invoiced" variance as a first-class dashboard line.
- **Catalog refresh automation** — the SDK ships `gpu_prices.json` snapshots; ongoing weekly refresh is a repo-side workflow (cron + PR), not an SDK responsibility. Shared with Phase 1 compute catalog refresh infrastructure (whichever lands first).

---

## 12. Non-goals

- **No reading the cloud bill, CUR, or any invoice format.** Convention §10.
- **No synthetic "idle GPU" pseudo-tasks to make totals match the bill.** Decision #6.
- **No user-facing pricing-rate configuration.** No `gpu_cost_per_hour` `init()` parameter.
- **No per-VRAM-GiB-second fractional billing.** Decision #7 — `gpu_vram_gb` is display-only.
- **No per-event GPU cost on `llm_call` / `external_cost` / `network` / `compute_cost` events.** Convention §4. GPU attribution lives on `task.gpu_cost_usd`; events stay single-meaning.
- **No `gpu_utilization_signal` aggregation into any cost field.** Decision #3 — observability-only; Control Layer must not roll signal events into any dollar aggregate.
- **No detection of GPU runtimes whose billing model isn't one of the four discriminators.** Future runtimes that introduce new billing shapes get a Decision-log entry + an additional discriminator value.
- **No sub-millisecond billing precision claims.** Modal et al. bill at second resolution; the SDK measures at NVML sample resolution (~100ms typical); the math reproduces the cloud bill to within measurement precision.
