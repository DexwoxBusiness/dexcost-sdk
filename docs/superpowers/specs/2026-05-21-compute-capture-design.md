# Compute Capture — Design Spec

**Date:** 2026-05-21
**Status:** Approved design — ready for implementation planning
**Sub-project:** B of 5 (compute capture). Sub-projects A (network — shipped) and B (compute) are independent and each get their own spec; C (GPU), D (storage), E (catalog updates) follow in Phases 2–3.
**Reference research:** [`research/2026-05-20-compute-foundation-research.md`](../research/2026-05-20-compute-foundation-research.md) — verified facts, sources
**Reference decisions:** [`decisions/2026-05-20-compute-foundation-decisions.md`](../decisions/2026-05-20-compute-foundation-decisions.md) — 10 locked decisions + sharpenings
**Cross-subsystem conventions:** [`conventions.md`](../conventions.md) — inherited patterns (event shape, confidence enum, source-measurement boundary, log-once discipline, etc.)

## 1. Summary

Compute is the largest unmeasured cost category in dexcost's current capture surface. LLM, vendor, MCP, and network egress are all live; **compute** — the dollars the customer pays for AWS Lambda invocations, Fargate vCPU-seconds, EC2 instance hours, Cloud Run vCPU/memory, Azure Functions GB-seconds, Vercel CPU-hours, and Kubernetes pod CPU/memory — is invisible to the SDK today.

This spec extends the existing per-task attribution surface with **runtime detection**, **cgroup-based measurement**, and a **`compute_cost` event type** that captures dollar attribution per task across the runtimes in the master capability table. The dollar layer ([`compute-cost-attribution-design.md`](2026-05-21-compute-cost-attribution-design.md), v2 sibling) ships alongside.

**Coverage matrix:**

| Runtime | Detection signal (existing) | Measurement primitive | Captured |
|---|---|---|---|
| AWS Lambda | `AWS_LAMBDA_FUNCTION_NAME` env | env (memory) + handler wrap (duration) | invocation + GB-seconds |
| AWS Fargate / ECS-on-Fargate | `ECS_CONTAINER_METADATA_URI_V4` env | task metadata endpoint + cgroup `cpu.stat` diff + `memory.peak` | vCPU-seconds + GiB-seconds |
| AWS EC2 / ECS-on-EC2 / EKS-on-EC2 | DMI `sys_vendor=Amazon EC2` + IMDS | IMDS instance-type + cgroup `cpu.stat` diff | per-task instance share |
| GCP Cloud Run / Cloud Functions Gen2 | `K_SERVICE` env | cgroup `cpu.max` / `memory.max` / `cpu.stat` diff + handler wrap | request + vCPU-seconds + GiB-seconds |
| GCP Compute Engine / GKE | DMI `product_name=Google Compute Engine` + IMDS | IMDS machine-type + cgroup `cpu.stat` diff | per-task instance share |
| Azure Functions Consumption | `FUNCTIONS_WORKER_RUNTIME` env | cgroup `memory.peak` + handler wrap | execution + GB-seconds |
| Azure VM / AKS | DMI `chassis_asset_tag=...` + IMDS | IMDS vmSize + cgroup `cpu.stat` diff | per-task instance share |
| Vercel Functions (Fluid) | `VERCEL` env | env (memory) + handler wrap (duration) | invocation + CPU-hours + GB-hours |
| Kubernetes pod (any cluster) | downward API `spec.nodeName` + cgroup | cgroup `cpu.max` / `memory.max` / `cpu.stat` diff | pod-limits × duration (v1; node-share when `k8s_node_aware: true` opted in) |

Out of scope for v1 (recorded for continuity in §11): Lambda provisioned-concurrency idle cost; non-Linux hosts (macOS/Windows dev — no cgroup); cgroup-v1-only kernels (older RHEL/CentOS).

## 2. Context

**Current state (verified against `tracker.py`, `tracker.go`, `tracker.rs`, `tracker.ts`).** Every SDK has a `compute_cost` event type defined in the EventType enum but **no producer**. The Task model has `compute_cost_usd` aggregated in `_aggregate_costs` / `aggregateCosts` / `finalize_network`, but the only path emitting `compute_cost` events today is the user-driven `record_cost(event_type="compute_cost", ...)` API. There is no automatic compute capture.

Cloud detection is already shipped via `cloud_detect` (per the v2 egress work) — provider + region resolution from env vars / DMI / IMDS exists, and the resolved `CloudEnv` is consumed at task finalize. This spec extends the same `CloudEnv` to carry **instance-type** (per Decision #3) and reuses the same detection cascade.

**Why compute is hard:** unlike LLM and vendor cost (which come from vendor responses) and unlike network egress (which is dollars-per-GB on bytes the SDK already counts), compute cost depends on the **billing model of the underlying runtime**, which varies dramatically:

- **Lambda / Vercel / Azure Functions Consumption** bill per-invocation + per-GB-second (durable resource × time)
- **Cloud Run** bills per-request + per-active-vCPU-second + per-active-GiB-second OR per-instance-second depending on customer's deploy-time choice
- **Fargate** bills per-task vCPU-second + per-task GiB-second
- **EC2 / GCE / Azure VM** bill per-instance-hour regardless of how many tasks ran
- **Kubernetes pod** inherits the underlying VM's hourly rate, prorated across pods

The SDK needs ONE event type (`compute_cost`) that all of these emit into, with a discriminator field (`details.billing_model`) carrying which math applies. The math itself lives in the v2 sibling spec.

## 3. Decisions

The full 10-decision log lives at [`decisions/2026-05-20-compute-foundation-decisions.md`](../decisions/2026-05-20-compute-foundation-decisions.md). This spec restates the capture-relevant ones in narrative form; for the cost-math-relevant decisions see the v2 sibling spec.

| # | Decision | Reason |
|---|---|---|
| Detection cascade | Reuse existing `cloud_detect` (env → DMI → IMDS); extend Phase 2 IMDS probe to extract `instance_type` in the SAME thread that resolves region | One probe, two extractions — cheaper than a second probe lifecycle |
| Measurement primitive | **Cgroup v2 reads as primary**; env vars + IMDS metadata as supplemental signal | Convention §8 — CPU/memory enforcement is at the cgroup level; env vars only describe the platform |
| Event type | Single `compute_cost` event, `details.billing_model` discriminator (`lambda` / `fargate` / `cloud_run` / `ec2` / `azure_functions` / `vercel` / `k8s_pod`) | Convention §1 — one event type per subsystem with a discriminator |
| K8s node info | v1 bills pod-limits × duration (no node-share term); `k8s_node_aware: true` opts in to the API path | Decision #4 — keeps dexcost "zero config" on K8s; over-attribution surfaces as "you're paying for capacity you're not using" in reconciliation |
| Provisioned concurrency | Track `AWS_LAMBDA_INITIALIZATION_TYPE` across ALL values; defer PC idle-cost billing to v1.1 | Decision #5 — schema is forward-compatible |
| Idle attribution | Idle compute is invisible to dexcost (EC2 between tasks, Fargate container post-task tail) | Decisions #9 + #10 — matches source-measurement boundary (convention §10); the gap IS the signal |
| Rollout | One design; Python first, then Go / Rust / TS one at a time | Same de-risking pattern as network capture |

## 4. Data Model

### 4.1 Task — one new field

| Field | Type | Meaning |
|---|---|---|
| `compute_cost_usd` | Decimal | Already exists; populated by the new producers — not just by user-driven `record_cost` calls |

No NEW Task field for compute v1 — the existing `compute_cost_usd` aggregate is the home. Compute *measurement* fields (CPU-seconds used, peak memory, etc.) live on the event's `details`, not on the Task. v2 may add a `compute_by_runtime` JSON blob analogous to `network_by_host` if dashboards need per-billing-model breakdown — deferred to v2 spec.

### 4.2 New `compute_cost` event type

Represents one notable compute-cost unit. For serverless runtimes (Lambda / Vercel / Azure Functions / Cloud Functions / Cloud Run request-based) this is **per invocation**. For long-running runtimes (Fargate / EC2 / GCE / K8s pod / Cloud Run instance-based) this is **per task** (one event per dexcost task, computed at task finalize from the cgroup diff).

```
event_type:      "compute_cost"
service_name:    "aws.lambda" | "aws.fargate" | "aws.ec2.c7g.xlarge" | "gcp.cloud_run" |
                 "gcp.cloud_functions" | "gcp.gce.n2-standard-2" | "azure.functions_consumption" |
                 "azure.vm.Standard_D2s_v3" | "vercel.fluid" | "k8s.pod"
cost_usd:        <Decimal>     # v2 — back-filled at task finalize per cost-attribution design
cost_confidence: "computed"    # most common; "estimated" for default rates; "exact" never
                               # (compute has no exact-receipt path — always derived)
pricing_source:  "compute_catalog:<runtime>:<region>" | "compute_catalog:<runtime>:default" |
                 "compute_catalog:default"
pricing_version: "compute:<catalog_version>"  # e.g. "compute:1.0.0"
details: {
  billing_model:       "lambda" | "fargate" | "ec2" | "cloud_run_request" | "cloud_run_instance" |
                       "cloud_functions" | "azure_functions" | "vercel_fluid" | "k8s_pod",
  # Per-runtime measurement fields — exactly what billing math consumed:
  duration_ms:         <int>         # wall-clock task / invocation duration
  memory_bytes_peak:   <int>         # cgroup memory.peak at task end (or env-declared memory for Lambda)
  memory_bytes_limit:  <int>         # cgroup memory.max OR env-declared (Lambda) — billing dimension
  vcpu_count:          <float>       # cgroup cpu.max parsed (quota / period) OR env-declared
  vcpu_seconds_used:   <float>       # cpu.stat usage_usec diff / 1_000_000 (long-running runtimes only)
  invocation_count:    <int>         # serverless runtimes only — always 1 per event in v1
  # Runtime-specific signals retained for reconciliation:
  region:              "us-east-1" | "eu-west-2" | ...   # from CloudEnv
  architecture:        "x86_64" | "arm64"                # for Lambda / Fargate / EC2 pricing tiers
  initialization_type: "on-demand" | "provisioned-concurrency" | "snap-start" | "lambda-managed-instances"
                                                          # Lambda only; null otherwise
  cost_pending:        true                              # v2 §6.4 marker (stripped at finalize)
}
```

**Field rules:**
- All byte fields are **bytes** (not MB or GB) per Decision #7. Conversion happens at the catalog-lookup boundary using the per-runtime conversion table pinned in the v2 spec.
- `vcpu_count` is a float (`0.25` for a 256-shares Fargate task; `2.0` for a `n2-standard-2` GCE instance).
- `vcpu_seconds_used` is the canonical CPU-time consumed by the task; for serverless runtimes (where billing is per-GB-second of wall clock, not per-vCPU-second of used CPU) this field may be 0 — the billing model uses `duration_ms` instead.
- `architecture` is detected via `os.uname().machine` (Python/Rust/Go) / `process.arch` (TS). Required for Lambda and Fargate pricing (ARM is ~20% cheaper).
- `initialization_type` is captured for Lambda even when `"on-demand"` to enable future PC sizing analysis without a schema migration (Decision #5).
- `cost_pending: true` follows the v2 §6.4 deferred-cost pattern: cost is `0` at emission, back-filled at task finalize from the catalog. Marker is stripped after back-fill.

### 4.3 Emission rule

**One `compute_cost` event per task per runtime context.** Serverless runtimes (one task ≈ one invocation) emit at the END of the handler wrap. Long-running runtimes (one task spans some portion of a container/VM lifecycle) emit at task finalize, with `vcpu_seconds_used` computed from the cgroup `cpu.stat` diff between task start and task end.

**Exactly one event** — multiple `compute_cost` events per task would force downstream consumers to sum-and-dedup. If a single task somehow spans two runtimes (rare; mid-task migration is exotic), the SDK emits the event for whichever runtime was active at task end.

**Idle is not emitted** (Decisions #9 + #10). Time between dexcost tasks on a long-running VM, and time between dexcost tasks on a Fargate container, do NOT produce synthetic events. The gap-to-cloud-bill is the customer's "unaccounted capacity" signal.

### 4.4 Schema changes

- No new Task columns (existing `compute_cost_usd` is the home).
- `dexcost-event.v1.json` — the `event_type` enum already includes `"compute_cost"`. No schema bump for v1 capture.
- Per-SDK serializer code that maps `Event.Details` already accepts arbitrary keys (matches v1 `network` event pattern). No schema rev.

## 5. Components & Flow (SDK side)

### 5.1 Components

1. **`ComputeRuntimeResolver`** — resolves the active runtime at task start. Cascade per Decision #1:
   - Phase 1a (env, sub-ms): if `AWS_LAMBDA_FUNCTION_NAME` → `lambda`; `ECS_CONTAINER_METADATA_URI_V4` → `fargate`; `K_SERVICE` → `cloud_run`; `FUNCTIONS_WORKER_RUNTIME` → `azure_functions`; `VERCEL` → `vercel_fluid`; `KUBERNETES_SERVICE_HOST` → `k8s_pod`
   - Phase 1b (DMI, ~1ms): falls through to existing `cloud_detect` for `ec2` / `gce` / `azure_vm`
   - Phase 2 (background): instance-type extraction in the same IMDS thread that already runs for `cloud_detect`'s region probe
2. **`CgroupReader`** — pure helpers that read `/sys/fs/cgroup/{cpu.stat, cpu.max, memory.peak, memory.current, memory.max}` and parse them into typed values. Fail-silent on non-Linux (returns `null`/`None`/`nil`); fail-silent on cgroup-v1 (returns the same).
3. **`FargateTaskMetadata`** — HTTP client wrapper that hits `${ECS_CONTAINER_METADATA_URI_V4}/task` once at runtime resolution, caches the response, exposes `vcpu_count` (float) and `memory_bytes_limit` (MiB → bytes).
4. **`HandlerWrap`** — per-runtime decorator/helper that records task start time + start cgroup snapshot, runs the customer handler, records end time + end cgroup snapshot, emits the `compute_cost` event with `cost_pending: true`. One implementation per runtime family (serverless handler-style vs long-running task-style).
5. **`ComputeAccountant`** — per-task in-process accumulator analogous to `NetworkAccountant`. Lives on Task as an Arc / pointer field, registered in the cross-SDK registry. Holds the cgroup start snapshot, the runtime context, and the emitted-events list. At task finalize: computes the event, stamps `cost_pending: true`, hands off to the v2 cost-attribution layer for the dollar back-fill.

### 5.2 Per-language thread-safety

The compute accountant is mutated by exactly one writer per task (the task's owning goroutine / async-task / event-loop iteration), so the locking story is lighter than `NetworkAccountant`:

| SDK | Strategy |
|---|---|
| Python | `threading.Lock` around the start/end snapshot pair (covers mixed sync/threadpool + async) |
| TypeScript | none — single-threaded event loop |
| Go | `sync.Mutex` around the snapshot pair |
| Rust | `std::sync::Mutex` (consistent with `NetworkAccountant`, not `tokio::sync::Mutex` — accountant is called from sync contexts) |

### 5.3 The "one event per task per runtime" invariant

A single dexcost task produces **at most one** `compute_cost` event. Implementation:
- `ComputeAccountant.record_compute_event(...)` is idempotent — second call no-ops.
- The accountant is registered at task start (mirrors `NetworkAccountant` registry pattern) and unregistered at task finalize (after the event is emitted).

**Why not "≤ 1 event per HTTP call" like network?** Compute doesn't have a per-call shape — Lambda invocations are tasks (one invocation = one task = one `compute_cost` event), and long-running runtimes attribute the whole task window in one event. No suppression flag needed.

### 5.4 Flow

**Serverless runtimes (Lambda / Vercel / Azure Functions / Cloud Run request-based / Cloud Functions):**

1. Handler wrap captures `start_time = monotonic_now()`, `start_memory = cgroup.memory_current()` (or env-declared limit for Lambda).
2. Handler runs.
3. On handler return (or exception): `end_time = monotonic_now()`, `peak_memory = cgroup.memory_peak()`.
4. Emit `compute_cost` event with `cost_pending: true`, `details.duration_ms = (end - start) * 1000`, `details.memory_bytes_peak = peak`, `details.invocation_count = 1`, `details.billing_model = "lambda"` (etc.).
5. Task finalize → v2 cost-attribution layer back-fills `cost_usd` from the catalog.

**Long-running runtimes (Fargate / EC2 / GCE / Azure VM / K8s pod / Cloud Run instance-based):**

1. Task start: `ComputeAccountant.snapshot_start()` reads `cgroup.cpu_stat()` (`usage_usec`), `cgroup.memory_current()`, current monotonic time.
2. Task runs.
3. Task end (in `_aggregate_costs` / equivalent): `snapshot_end()` reads the same files; `vcpu_seconds_used = (cpu_usec_end - cpu_usec_start) / 1_000_000`, `memory_bytes_peak = cgroup.memory_peak()`.
4. Emit single `compute_cost` event with the diffs, `cost_pending: true`.
5. Same back-fill at finalize.

**Idle path (Decisions #9 + #10):** no event emitted between dexcost tasks. The cgroup keeps counting (it's the kernel's counter), but the SDK only reads at task boundaries.

### 5.5 Detection priority and runtime overlap

Some runtimes are layered (Lambda runs on AWS infrastructure; Vercel runs on AWS Lambda; Cloud Run Gen2 IS Cloud Run; K8s on EC2 is BOTH a k8s_pod and runs on EC2 hardware). The SDK picks ONE billing model per task based on detection priority, mirroring the existing `cloud_detect` order:

```
modal, runpod, render, railway, heroku, koyeb, fly, vercel  →  (these set provider but compute capture for them is v1.1+)
aws (Lambda first via AWS_LAMBDA_FUNCTION_NAME)
aws (ECS/Fargate via ECS_CONTAINER_METADATA_URI_V4)
aws (EC2 via DMI/IMDS fallback)
azure (Functions via FUNCTIONS_WORKER_RUNTIME)
azure (VM via DMI fallback)
gcp (Cloud Run via K_SERVICE)
gcp (GCE via DMI fallback)
kubernetes (via KUBERNETES_SERVICE_HOST + cgroup)  →  takes precedence over the underlying VM when both detected
```

The k8s-takes-precedence rule matters: a pod on an EC2 instance is billed as `k8s_pod` (pod-limits × duration on the EC2 hourly rate), not as `ec2` (instance-share). Otherwise the same compute hour would get double-counted.

## 6. Error Handling & Edge Cases

1. **Fail-silent, always.** Convention §9. All resolver / cgroup / metadata / handler-wrap operations are wrapped so an exception never breaks the customer's code. Each swallowed exception bumps an in-memory error counter exposed via `dexcost status` (same pattern as the network adapter's `get_network_error_count`).
2. **No active task → no-op.** Same rule as the network adapter: anonymous compute (no task in context) never creates orphan events. The compute accountant returns immediately when no task is registered.
3. **Non-Linux hosts (macOS / Windows dev).** Cgroup files don't exist. `CgroupReader.read_cpu_stat()` returns `null`/`None`/`nil`. Long-running runtimes can't measure `vcpu_seconds_used` → event emits with `vcpu_seconds_used: 0` + `cost_confidence: "estimated"` (the cost-attribution layer falls through to per-instance-hour billing without the share math). Serverless runtimes are unaffected (they don't depend on cgroup CPU diff).
4. **Cgroup v1 only (older RHEL 7 / CentOS 7 hosts).** Same treatment as non-Linux — `CgroupReader` returns null. Out of scope for v1 to add cgroup-v1 readers.
5. **Cgroup `cpu.max` returns the literal string `"max"`** (no limit set). The reader parses this as `vcpu_count = nproc` (or `os.cpu_count()` / equivalent). Documented in the per-runtime conversion table in the v2 spec.
6. **Memory.peak unavailable (kernel < 5.19).** Falls back to `memory.current` at task end + a one-shot warning logged via convention §11. Approximates peak as end-of-task current; over-attributes for memory-spikey workloads, under-attributes for memory-stable ones.
7. **Handler exception in the wrap.** Event still emits (cost is incurred regardless of customer-code success), with `details.task_status = "failed"` if the wrap can propagate it. Lambda et al. bill failed invocations the same as successful ones.
8. **Lambda cold start.** The init phase IS billed (init duration counts toward the invocation total). The handler wrap measures from the FIRST instrumentation call (which is typically the handler entry — init has already happened). Init-phase tracking is v1.1 scope (see §11).
9. **K8s pod with `k8s_node_aware: true` but RBAC missing.** Per Decision #4 sharpening: log-once warning via convention §11 + fall through to limit-based billing. SDK keeps running.
10. **Fargate metadata endpoint unreachable.** One-shot warning + fall back to cgroup-only measurement. The event emits with `vcpu_count: null` + `memory_bytes_limit: null`, and the cost-attribution layer downgrades to Tier 3 (universal default rate) — matches the v2 egress §7.1 ladder.
11. **`AWS_LAMBDA_FUNCTION_MEMORY_SIZE` is unparseable.** Defaults to `128` MB (Lambda's minimum tier) + one-shot warning. Over-attributes for misconfigured-but-larger functions; under-attributes for misconfigured-but-smaller (unlikely).
12. **Snapshot-and-freeze at task end.** Like `NetworkAccountant`, the compute accountant freezes at finalize. Late `record_compute_event` calls no-op. No late-arriving CPU time mutates already-shipped events.

## 7. Testing (per SDK — Python first)

**Unit**
- `CgroupReader` — known fixture files (cpu.stat with `usage_usec 12345`, cpu.max with `100000 100000` / `max 100000`, memory.peak with `2147483648`) parse to expected values. Missing files → `None`. Cgroup v1 layout → `None`.
- Per-runtime conversion table (v2 spec) — exact arithmetic from `(bytes, vcpu, duration_ms)` inputs to `cost_usd` outputs for each billing model. Pin AT LEAST one canonical case per runtime: Lambda 1 GB × 100 ms = ..., Fargate 0.5 vCPU × 1 GiB × 60 s = ..., Cloud Run request × 0.5 vCPU × 256 MiB × 250 ms = ...
- Handler wrap — start/end snapshots capture correctly across sync/async/exception paths.
- Runtime resolver — fixture env-var matrices for each runtime; assertion that `lambda` wins over `aws` (just AWS_REGION); that `k8s_pod` wins over `ec2` when both DMI and `KUBERNETES_SERVICE_HOST` are set.
- Idle behavior — task ends, second task starts without container/VM restart; the SDK reads the cgroup at the SECOND task's start (not at first task's end + epsilon). Pin no idle-event-emission between tasks.

**Integration**
- Mock Lambda runtime: env vars set, handler invoked via wrap, event emitted with correct `duration_ms` / `memory_bytes_peak` / `invocation_count = 1`. Asserts `cost_pending: true` at emission, dollar back-filled at finalize.
- Mock Fargate: ECS metadata endpoint served from a local HTTP server (matches our Phase 2 IMDS probe test pattern), runtime resolver picks `fargate`, accountant snapshots cgroup at start/end, event emits at finalize with `vcpu_seconds_used > 0`.
- Mock K8s pod: `KUBERNETES_SERVICE_HOST` set, cgroup readers return realistic values, event emits with `billing_model: "k8s_pod"`. Separate test with `k8s_node_aware: true` but mocked 403 from the API server → log-once warning + event still emits at `computed` confidence.
- Failed task: handler raises → event still emits with the duration up to the throw.
- Zero-task period: long-running runtime with no dexcost tasks → no events emitted (idle invisible).

**Regression**
- Existing user-driven `record_cost(event_type="compute_cost", ...)` API still works unchanged; the new automatic capture coexists.
- Existing `compute_cost_usd` aggregation in `_aggregate_costs` correctly sums the new auto-emitted events alongside any user-driven ones.

**Property invariants (v2 spec):** the per-runtime conversion math + the canonical-scalar invariants for any future `compute_by_runtime` breakdown.

## 8. Boundaries / Non-Goals (v1)

- **No Lambda provisioned-concurrency idle billing.** Track `AWS_LAMBDA_INITIALIZATION_TYPE` for ALL values (Decision #5) so v1.1 adds PC math without a schema migration; the math itself is deferred.
- **No cgroup-v1 support.** Older RHEL 7 / CentOS 7 hosts return null cgroup reads → estimated confidence at the cost-attribution layer. Adding cgroup-v1 readers is v1.1 scope.
- **No Lambda init-phase tracking.** Cold-start init duration is billable but happens before the handler wrap. v1.1 adds an init hook (custom runtime / extension API).
- **No idle / unaccounted compute pseudo-tasks.** Decisions #9 + #10. dexcost compute total < cloud invoice on long-running runtimes IS the design. Customer-facing positioning (README, dashboard, marketing) is mandatory per the decisions log.
- **No Modal / RunPod / Replicate / Render / Railway / Heroku / Koyeb compute capture in v1.** These are detected by `cloud_detect` (so the provider attribution lights up), but their compute billing models aren't covered. v1.1 scope.
- **No AMD ROCm / Intel oneAPI compute capture.** GPU is subsystem C entirely.
- **No per-container compute breakdown on multi-container Fargate tasks.** v1 bills at the task scope. Per-container breakdown is v1.1+ scope and would extend the event's `details` with a container array.

## 9. Control Layer Dependencies

This spec covers the **SDK capture side**. The Control Layer (ingest, ClickHouse aggregation, reconciliation) is a separate repo and a companion workstream.

**SDK → Control Layer contract** (what the SDK guarantees):

1. Every `compute_cost` event has a unique `event_id` (UUIDv4).
2. The `details.billing_model` field is one of the documented discriminator values (`lambda` / `fargate` / `ec2` / `cloud_run_request` / `cloud_run_instance` / `cloud_functions` / `azure_functions` / `vercel_fluid` / `k8s_pod`). Adding new values is additive — Control Layer treats unknown values as `unknown_compute` and ships them to a dead-letter table for catalog updates.
3. `details.region` and `details.architecture` are populated when known; both are nullable.
4. `cost_pending: true` events are temporary state; they get an UpdateEvent re-push within the same task finalize cycle. The Control Layer dedups by `event_id` per the v2 §6.4 contract.
5. Per-task `compute_cost_usd` is the sum of all `compute_cost.cost_usd` for that task (which in v1 is at most one event per task; the sum invariant still holds).
6. The "dexcost compute total < cloud invoice on long-running runtimes" gap is **expected, not a bug**. Convention §10. Reconciliation explains it.

Everything downstream of the ingest endpoint — idle-aware aggregation, instance-utilization metrics, the compute-totals-vs-cloud-bill reconciliation surface — is Control Layer scope and not implemented here.

## 10. Pre-Requisite

The cross-subsystem **registry pattern** (task_id → accountant pointer) used by `NetworkAccountant` is the same pattern `ComputeAccountant` uses. Already shipped in the network capture v1+v2 work across all four SDKs; no prerequisite to land first. The compute accountant slots into the same registry shape.

The cross-subsystem **conventions doc** ([`conventions.md`](../conventions.md)) is the prerequisite for any subsystem-spec reviewer: the event-shape, confidence enum, source-measurement boundary, log-once discipline, and fail-silent rules referenced throughout this spec all derive from there.

## 11. Future (out of scope here, recorded for continuity)

- **Lambda provisioned-concurrency idle cost** (v1.1): the env var is captured in v1; the math extends to bill the idle PC period at its separate `$/GB-s` rate. Schema-additive.
- **Lambda init-phase tracking** (v1.1): custom runtime / Lambda Extensions API hook to measure init duration; emits a second `compute_cost` event OR extends `details` with `init_duration_ms`. Decision deferred.
- **Cgroup v1 readers** (v1.1): RHEL 7 / CentOS 7 are end-of-life in mid-2024 but some customers will be on them. Adding cgroup-v1 readers if customer demand surfaces.
- **GPU runtimes (Modal / RunPod / CoreWeave / Lambda Labs)** compute capture (v1.1 OR subsystem C): these are detected today; their billing models combine compute + GPU which is subsystem C scope.
- **Cloud Run Admin API for billing-mode discovery** (v1.1): if the default-request-based assumption misses a meaningful customer segment, switch to probing the Admin API at init when service-account RBAC permits.
- **`compute_by_runtime` task-level breakdown** (v2 if dashboards need it): analog to `network_by_host`; each runtime contributes per-runtime sub-totals on the Task. Requires the v2 cost-attribution spec to extend.
- **Multi-container Fargate task per-container breakdown** (v1.1+): bill each container in a Fargate task separately if customer surface shows them as distinct workloads.
