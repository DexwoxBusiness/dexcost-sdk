# Cross-subsystem conventions

**Status:** Live document. Patterns adopted across multiple dexcost subsystems live here. New subsystem designs inherit these conventions by default; a subsystem that proposes diverging from one of them MUST update this document explicitly with the rationale.

**What belongs here, what does not.** Conventions in this doc are **cross-subsystem invariants** — patterns that constrain how multiple subsystems are designed (event-model shape, confidence enum, fail-silent discipline, source-measurement boundary). They are NOT style preferences, naming choices, or formatting decisions. A repeated pattern is a convention only if changing it in one subsystem would break a downstream consumer (dashboard, reconciliation, Cost Intelligence) or violate a positioning principle. When in doubt: if the pattern only matters inside one subsystem's code, it lives in that subsystem's spec; if it shapes how subsystems interoperate or how customers experience the product, it lives here.

**Subsystem index** (master capability table reference):
- **A** — Network / egress capture *(v1 bytes shipped; v2 pricing shipped — see specs/2026-05-19 and 2026-05-20)*
- **B** — Compute foundation *(Phase 1 — in design; decisions locked 2026-05-20)*
- **C** — GPU *(Phase 2 — future)*
- **D** — Storage / data-platform *(Phase 3 — future)*
- **E** — Catalog updates *(Phase 3 — future)*

Conventions in this document have been validated against subsystems A and B. C/D/E inherit them.

---

## 1. Event model — one type per subsystem with a discriminator

| Subsystem | Event type | Discriminator field | Example values |
|---|---|---|---|
| LLM | `llm_call` | `provider` + `model` | `openai` + `gpt-4o`, `anthropic` + `claude-sonnet-4` |
| External (non-LLM) | `external_cost` | `service_name` | `firecrawl`, `pinecone`, `tavily` |
| Network egress | `network` | `pricing_source` | `egress_catalog:aws:us-east-1`, `egress_catalog:internal` |
| Compute | `compute_cost` | `details.billing_model` | `lambda`, `fargate`, `cloud_run`, `ec2`, `k8s_pod` |
| GPU | `gpu_cost` | `details.billing_model` | `per_gpu_second_active`, `per_instance_hour`, `per_gpu_hour_reserved`, `per_vgpu_hour` |
| Compute waste (future) | `retry_marker` | `retry_reason` | `rate_limit`, `timeout`, `5xx` |

**Rule:** A new subsystem MUST NOT introduce N parallel event types where one event type + a discriminator field would do. Downstream consumers (Cost Intelligence, Reconciliation, the dashboard) should iterate over a small fixed set of event types and discriminate inside.

**Carve-out for observability-only signal events** (added with Phase 2 GPU, Decision #3): a subsystem MAY introduce a SECONDARY "signal" event type alongside its primary cost event, provided the signal event has **no `cost_usd` field, no `pricing_source`, no `pricing_version`, no `cost_confidence`** and is documented as observability-only. The Control Layer MUST NOT aggregate signal events into any dollar field; their entire purpose is to surface measurement (utilization, gap-to-cloud-bill) without violating the convention §4 measurement/dollar separation.

The Phase 2 GPU `gpu_utilization_signal` event is the reference example — emitted alongside `gpu_cost` events with `sm_util_pct` (task-window-averaged), `vram_used_peak_bytes`, `process_count`, etc. The 380× CPU magnitude of the idle-GPU gap made first-install trust depend on surfacing utilization explicitly, and the convention §4 invariant ("dollars on task only") prohibited stamping util onto `gpu_cost` events. The carve-out resolves both. Future subsystems may use the pattern when the same trade-off applies; the test surface is the executable contract that `gpu_utilization_signal` events have `cost_usd=0` AND are never aggregated into task totals.

**Counter-pattern (rejected):** `lambda_cost`, `fargate_cost`, `ec2_cost` as separate event types. Explodes the schema; every consumer has to handle N parallel "compute" event types as if they were unrelated.

---

## 2. Cost confidence — four-value enum, no per-subsystem extensions

Confidence label on every `cost_usd` field:

| Value | Meaning |
|---|---|
| `exact` | Vendor provided the exact dollar amount (e.g. response-header billing, internal-traffic = $0 free) |
| `computed` | SDK computed from inputs known with certainty (e.g. token-count × catalog rate, pod-limit × duration × hourly rate) |
| `estimated` | Catalog fallback applied (default rate; tier-3+ in the v2 egress ladder; default billing-mode in compute) |
| `unknown` | Cost is $0 because we could not compute it; the event is recorded so the call surfaces, but the dollar is a placeholder |

**Rule:** A new subsystem MUST stay within these four values. Introducing a fifth like `inferred` or `partial` forces the dashboard to explain two enums per cost line. The `pricing_source` field is where resolution-step detail goes — see §3.

**Why this matters:** v2 egress had a moment during decision review where `inferred` was proposed for Cloud Run's default-billing-mode case. It was rejected in favour of `estimated` + a richer `pricing_source` string. The convention here pins that choice for all future subsystems.

---

## 3. `pricing_source` strings — resolution-step audit trail

The `pricing_source` field on every event (and on the per-host entries in `network_by_host`) is a structured string that records *how* the rate was resolved. Format: colon-delimited steps.

| Pattern | Example | Meaning |
|---|---|---|
| `<catalog>:<provider>:<region>` | `egress_catalog:aws:us-east-1` | Catalog exact-match on (provider, region) |
| `<catalog>:<provider>:default` | `egress_catalog:aws:default` | Catalog had the provider but not the region → provider default |
| `<catalog>:default` | `egress_catalog:default` | Catalog had neither → universal default |
| `<catalog>:internal` | `egress_catalog:internal` | Internal traffic, rate is $0 |
| `<catalog>:<provider>:<billing_mode>_default` | `compute_catalog:cloud_run:request_based_default` | Catalog had the provider but the billing mode was inferred |
| `gpu_catalog:<provider>:<runtime>:<region>:<sku>` | `gpu_catalog:aws:ec2_gpu:us-east-1:p5.48xlarge` | GPU SKU+region exact match |
| `gpu_catalog:<provider>:<runtime>:<sku>` | `gpu_catalog:modal:per_gpu_second_active:h100` | GPU SKU match on non-regional provider (Modal/Lambda Labs/etc.) |
| `gpu_catalog:device_class_fallback:<class>:<billing_model>` | `gpu_catalog:device_class_fallback:hopper:per_gpu_second_active` | Phase 2 Decision #4 — cold-start fallback when productName isn't in any catalog alias array |
| `<source>:<sku>:self_pid_only` | `gpu_catalog:modal:per_gpu_second_active:h100:self_pid_only` | Phase 2 Decision #1 — cgroup walk failed; attribution degraded to dexcost's own PID |
| `<source>:<sku>:no_container_scope` | `<...>:no_container_scope` | Phase 2 Decision #1 — bare-metal-no-container (systemd user slice / root cgroup); degraded to self-PID-only |
| `<source>:<sku>:multi_container_pod_partial` | `<...>:multi_container_pod_partial` | Phase 2 Decision #1 — K8s sidecar PIDs outside dexcost's cgroup; partial attribution |
| `rate_registry` | `rate_registry` | User-registered domain rate (not from a bundled catalog) |
| `service_catalog` | `service_catalog` | Bundled service catalog match (non-LLM vendor pricing) |
| `litellm` | `litellm` | LLM pricing from the bundled LiteLLM map |

**Rule:** Subsystems use a `<catalog>` prefix that identifies their pricing surface (`egress_catalog`, `compute_catalog`, `gpu_catalog`, etc.). Within that prefix, the colon-delimited steps trace the lookup ladder, ending at the most specific match found.

This is what enables reconciliation: a customer asking "why did dexcost charge me $X for this task?" gets a precise answer by reading the `pricing_source` field.

---

## 4. Measurement on events, derived dollars on the task

**The central invariant** (from v2 egress §3.3, extended to every cost-attributing subsystem):

> Events carry raw measurement (bytes, tokens, vCPU-seconds, peak memory). The task carries derived attribution (the dollar rollups). Per-host / per-service breakdowns live on task-level aggregate fields, not on the events.

Examples:

| Subsystem | Event-level measurement | Task-level derived dollar |
|---|---|---|
| Network | `network` event `details.request_bytes` / `response_bytes` / `is_internal_traffic` | `task.network_cost_usd` + `task.network_by_host[].egress_cost_usd` |
| Compute | `compute_cost` event `details.{vcpu_seconds, memory_bytes_peak, duration_ms, billing_model}` | `task.compute_cost_usd` |
| LLM | `llm_call` event `input_tokens` / `output_tokens` / `cached_tokens` | `task.llm_cost_usd` |

**Rule:** Events do NOT carry dollar amounts from a SECOND subsystem. A `network` event carries network bytes only; it does NOT also carry the compute cost of the request that triggered it. The dual-invoice attribution from v2 Decision #7 — that a single HTTP call to a cataloged vendor produces ONE event (`external_cost`) but populates TWO task-level cost categories (`external_cost_usd` + `network_cost_usd`) — is the canonical example.

A future subsystem MUST NOT propose "stamp the compute portion of this LLM call onto the llm_call event." Compute attribution lives on `task.compute_cost_usd`; events stay single-meaning.

---

## 5. ≤ 1 event per call (record dedup) ≠ 1 cost category per call (dollar dedup)

From v2 Decision #7 — the distinction has bitten enough designs to be a convention:

- **Correct invariant:** A single HTTP call / LLM call / compute invocation produces AT MOST ONE event (`llm_call` OR `external_cost` OR `network` OR `compute_cost`). Enforced by the per-call suppression flag.
- **Rejected invariant:** A single HTTP call / LLM call / compute invocation contributes to AT MOST ONE task-level cost category. This would silently undercount (a cataloged-vendor HTTP call generates BOTH a vendor invoice AND a cloud egress charge).

**Rule:** The suppression flag dedups *records*, never *dollars*. Records describe what happened; dollars describe what the customer owes. One call can owe money on multiple bills.

---

## 6. Catalog distribution — Python canonical, sync script, per-SDK bundling

Pricing catalogs (`model_cost_map.json`, `service_prices.json`, `egress_prices.json`, future `compute_prices.json` / `gpu_prices.json` / `storage_prices.json`) follow this distribution pattern:

1. **Canonical file lives in the Python SDK** at `python/src/dexcost/data/<catalog>.json`.
2. **A sync script at the repo root** (`scripts/sync_<catalog>.sh`) copies the canonical file into the other three SDKs at their bundled location.
3. **CI runs the script in `--check` mode** on every PR. Drift fails the build.
4. **Each SDK bundles its local copy** in the published artifact via the language-native mechanism:
   - Python wheel — `[tool.hatch.build.targets.wheel]`
   - Rust crate — `include_str!`
   - Go module — `//go:embed`
   - TypeScript package — `package.json files` array

**Why not a single shared file at the repo root:** `pip install` / `cargo add` / `npm install` / `go get` only ship the SDK's own tarball. A shared file at the repo root would be invisible to installed packages. The four-copies-plus-sync-script is the standard monorepo pattern.

**Catalog content schema:** Each catalog has a `_meta` block at top (`version`, `last_updated`, `currency`, `default_rate_*`, `description`, `notes`, `sources`) and provider blocks each with `_last_verified` (ISO-8601 date) + a regions/SKUs map. See `egress_prices.json` for the reference shape.

---

## 7. Five-tier degradation ladder for pricing resolution

Established in v2 §7.1, applied to every cost-attributing subsystem:

| Tier | Condition | `cost_confidence` |
|---|---|---|
| 1 | Exact (provider, region/SKU) match in catalog | `computed` |
| 2 | Provider known, region/SKU missing | `estimated` + provider default rate |
| 3 | Provider not detected / not in catalog | `estimated` + universal `_meta.default_rate_*` |
| 4 | Catalog unreadable / malformed / `_meta.default_*` itself missing | `estimated` + hardcoded constant + WARN_ONCE |
| 5 | Computation raises at finalize | `cost_usd = 0`, task still ships, log warning |

**Rule:** Every subsystem's pricing engine implements this ladder. Tiers 1–4 live in the rate resolver; Tier 5 is the `try/except` shell around the per-event back-fill step in the task-finalize path.

**Scope note on Tier 5:** Tier 5 applies to subsystems where dollar attribution happens at **task finalize** (network egress, compute, future GPU/storage). Subsystems that compute cost at **event-emission time** (LLM — token counts known at the response moment) handle computation failure differently: the event itself ships with `cost_usd=0` + `cost_confidence="unknown"` + a logged warning, with no finalize-time back-fill step to wrap. The five-tier ladder is the right shape for derived-at-finalize subsystems; event-emission subsystems implement Tiers 1–4 plus their own "computation failed at emission" handler. See each subsystem spec for its specific behaviour.

The warn-once-per-failure-mode discipline is captured separately as convention §11.

---

## 8. Measurement primitives by subsystem

| Subsystem | Primary measurement primitive | Why |
|---|---|---|
| LLM | Provider SDK monkey-patching / wrapping | Token counts come from the LLM provider's response |
| External (non-LLM) | HTTP adapter monkey-patching + 163-service catalog | Vendor charges come from response headers / bodies |
| Network egress | HTTP adapter byte counting + cgroup net counters (future) | Bytes are what matter |
| **Compute** | **Cgroup v2 file reads as primary; env vars + IMDS metadata as supplemental signal** | **CPU/memory enforcement is at the cgroup; env vars only tell us about platform/runtime** |
| **GPU** | **NVML native bindings (`pynvml` / `nvml-wrapper` / `go-nvml`) + cgroup-membership PID walk (Phase 2 Decision #1); `nvidia-smi --query-gpu=...` shell-out for TypeScript** | **Per-PID GPU accounting comes from the NVIDIA driver; cgroup walk scopes attribution to the dexcost task's container** |
| Storage (future) | Observed HTTP traffic proration | We only see what the customer's code does |

**Rule for compute specifically:** `/sys/fs/cgroup/{cpu.stat, memory.peak, memory.max, cpu.max}` is the source of truth for what was used and what the limit is. Env vars (`AWS_LAMBDA_FUNCTION_MEMORY_SIZE`, `MODAL_REGION`, etc.) tell us about the runtime; HTTP metadata endpoints (Fargate, IMDS) supplement. Reads happen at task start (snapshot) and task end (diff or peak).

**Rule for GPU specifically:** `nvmlDeviceGetProcessUtilization` per-PID is the source of GPU-seconds-used (Decision #8 — persistent `lastSeenTimeStamp` state across snapshot calls; naive impl misses samples). The cgroup walk (Decision #1) is the scoping boundary that filters NVML samples to the dexcost task's PIDs only — without it, attribution silently overcounts (bare-metal-no-container hits the systemd user slice) or undercounts (multi-container K8s pods miss sidecar PIDs). The cgroup-scope classification table (7 prefixes: kubepods.slice/kubepods/docker/system.slice/docker-/containerd/system.slice/containerd-/crio/system.slice/crio-) is the implementation contract. TypeScript shells out to `nvidia-smi` because no maintained NVML binding exists for Node in 2026.

---

## 9. Fail-silent discipline — capture errors must never break customer code

Established in v1 §6.1, extended to every subsystem:

- Every monkey-patch / instrument / measurement call is wrapped in a try/except that swallows the error and increments an in-memory error counter
- The counter is surfaced via `dexcost status` (CLI) so silent capture failure becomes observable, not hidden
- A capture failure NEVER raises into the customer's HTTP call, function handler, or compute task

**Rule:** A new subsystem's code path that touches customer execution (RoundTripper, fetch patch, handler wrap) MUST be fail-silent. Capture failure that breaks customer code is the worst possible bug class — it means installing dexcost is observably worse than not installing it.

---

## 10. Source-measurement boundary — what dexcost does NOT do

From the master plan's boundary-enforcement section, restated here because it shapes capture decisions:

- dexcost MUST NOT read the cloud bill / CUR
- dexcost MUST NOT ingest invoices from any source
- Dollar amounts come from: (1) what the SDK observed + (2) bundled pricing catalogs
- The SDK's total may LEGITIMATELY run lower than the customer's cloud bill on long-running runtimes (idle/unaccounted capacity). The gap is the signal — see Phase 1 decisions #9 + #10
- Reconciliation (future, server-side feature) is where bill ↔ dexcost-total gaps get explained as line items

**Rule:** A new subsystem that proposes reading the cloud bill, ingesting an invoice, or fabricating a "missing" cost line to make totals match the bill is rejected by default. The source-measurement boundary is what defines dexcost's category — diverging from it changes what the product is.

---

## 11. Log-once per failure mode per process

Established in v2 §7.3 (egress pricing engine warnings). Promoted here because the discipline has three parts that are easy to get wrong, and "log once" without the per-mode nuance is the default an implementer will reach for unless the convention spells it out.

**The three parts:**

1. **Per distinct failure *mode*, not once globally.** A transition from one failure mode to another produces a second log. Example: a missing catalog file (mode `catalog_missing`) and later a malformed catalog file (mode `catalog_malformed`) each produce their own one-time log. Logging once globally would hide the mode change after the first warning fires.
2. **A module-level set tracks which modes have already fired.** Each subsystem owns its own tracking set keyed by mode-token strings (e.g. `catalog_missing`, `meta_default_missing`, `region_rate_malformed:<provider>:<region>`). Check-then-insert under a mutex.
3. **The set is resettable in tests.** Each subsystem exports a `_reset_warning_state_for_tests()` (Python) / `reset_warning_state_for_tests()` (Rust) / `ResetWarningStateForTests()` (Go) / `_resetWarningStateForTests()` (TS) helper. Tests for the warning behaviour reset the set between cases so they can deterministically verify "first call logs, second call does not."

**Counter-pattern (rejected):** Logging globally on first warning of any kind. After the first `catalog_missing` log, a subsequent `catalog_malformed` failure would silently swallow — and the customer never learns that their catalog file went from "absent" to "present but broken."

**Rule:** Every subsystem that emits warning-class logs from a recoverable failure path implements the three-part discipline. The tracking set is per-subsystem, not shared, so a network warning doesn't suppress a compute warning.

---

## Adopting a new convention

If you're designing a new subsystem and find a pattern that's worth promoting to a convention:

1. Implement it in your subsystem.
2. Open a PR to this document with the proposed convention + the rationale from your subsystem.
3. Get review from owners of the OTHER live subsystems — if the convention is supposed to be cross-subsystem, others need to inherit it without disruption.
4. Once landed here, all NEW subsystem designs reference this document; existing subsystems may continue their pre-convention pattern until a planned refactor.

If you're starting a new subsystem, this document is required reading before the design spec is written.
