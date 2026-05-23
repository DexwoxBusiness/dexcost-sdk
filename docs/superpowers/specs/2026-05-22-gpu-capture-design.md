# GPU Capture — Design Spec

**Date:** 2026-05-22
**Status:** Approved design — ready for implementation planning. **Subject to revision when the verification-matrix P0 captures land** ([`verification/2026-05-22-gpu-nvml-container-matrix/`](../verification/2026-05-22-gpu-nvml-container-matrix/)); spec sections that depend on those captures are flagged inline with `(pending spike capture)`.
**Sub-project:** C of 5 (GPU foundation). Builds on subsystems A (network — shipped) and B (compute foundation — shipped). Subsystems D (storage) and E (catalog updates) follow.
**Reference research:** [`research/2026-05-22-gpu-foundation-research.md`](../research/2026-05-22-gpu-foundation-research.md)
**Reference decisions:** [`decisions/2026-05-22-gpu-foundation-decisions.md`](../decisions/2026-05-22-gpu-foundation-decisions.md) — 11 locked decisions + 11 strengthenings
**Verification artifacts:** [`verification/2026-05-22-gpu-nvml-container-matrix/`](../verification/2026-05-22-gpu-nvml-container-matrix/) — 9-environment empirical confirmation of Decision #1 cgroup-scope classification
**Cross-subsystem conventions:** [`conventions.md`](../conventions.md) — inherits §§1, 2, 3, 4, 5, 9, 11; extends §1 (signal-event carve-out) and §8 (NVML cgroup-walk primitive)

## 1. Summary

GPU is the highest unit-cost capture surface dexcost addresses. A single `p5.48xlarge` runs $55/hr (380× the equivalent `c7g.xlarge`), and idle periods on long-running GPU runtimes dominate customer bills. Phase 2 GPU foundation captures **NVIDIA GPU usage per task** across the eight providers in the catalog, attributes dollars via the four-billing-model dispatch, and surfaces idle utilization as a side-channel `gpu_utilization_signal` event so the inevitable "$X attributed vs $Y invoiced" gap is actionable on first install instead of a trust-erosion bug.

This spec covers the **capture** side — runtime detection, NVML cgroup-walk primitive, per-task GPU accountant, single `gpu_cost` event per task, single `gpu_utilization_signal` event per task per GPU. The dollar layer ([`gpu-cost-attribution-design.md`](2026-05-22-gpu-cost-attribution-design.md)) ships alongside.

**Coverage matrix:**

| Provider / runtime | Detection signal (existing or new) | Billing model | NVML primitive | Captured |
|---|---|---|---|---|
| AWS EC2 GPU (g4/g5/g6/p3/p4/p5) | IMDS `instance-type` (Phase 1) → GPU SKU via catalog | `per_instance_hour` | NVML cgroup-walk | task share of instance hour |
| GCP GCE GPU bundled (A2/A3/G2) | GCE machine-type via IMDS (Phase 1) → GPU SKU via catalog | `per_instance_hour` | NVML cgroup-walk | task share of instance hour |
| GCP GCE GPU N1+accelerator | NVML-only fallback (Decision #9) | `per_gpu_hour_reserved` (accelerator) | NVML cgroup-walk | task share of attached-accelerator hour |
| Azure VM GPU (NC/ND series) | IMDS `vmSize` (Phase 1) → GPU SKU via catalog | `per_instance_hour` | NVML cgroup-walk | task share of instance hour |
| Azure NVadsA10 v5 (vGPU) | IMDS `vmSize` (Phase 1) + NVML `productName` (Decision #10) | `per_vgpu_hour` | NVML cgroup-walk | task share of vGPU hour |
| Modal | `MODAL_TASK_ID` env (Phase 1) + NVML `productName` → SKU alias | `per_gpu_second_active` | NVML cgroup-walk | active GPU-seconds |
| RunPod | `RUNPOD_POD_ID` env (Phase 1) + NVML `productName` → SKU alias | `per_gpu_second_active` | NVML cgroup-walk | active GPU-seconds |
| Lambda Labs | DMI (Phase 1) + NVML `productName` → SKU alias | `per_gpu_hour_reserved` | NVML cgroup-walk | task share of GPU hour |
| CoreWeave | K8s + CoreWeave node label `(pending spike capture)` | `per_gpu_hour_reserved` | NVML cgroup-walk | task share of GPU hour |
| Replicate | `REPLICATE_*` env (TBD per Phase 1) + NVML SKU alias | `per_gpu_second_active` | NVML cgroup-walk | active GPU-seconds |

**Out of scope for v1** (recorded for continuity in §11): AMD ROCm and Intel oneAPI capture (Decision #5); DCGM exporter scrape (deferred); inference-server `/metrics` enrichment (vLLM, Triton — deferred); MIG per-slice billing (Decision #2 — full-GPU rate in v1); cgroup-v1 hosts (degrade to self-PID-only); non-Linux hosts (no NVML, no events).

## 2. Context

**Current state (post-Phase-1).** Every SDK has `compute_cost` event capture for the runtimes in subsystem B. GPU has **zero capture today** — no `gpu_cost` or `gpu_utilization_signal` event type exists in any SDK's `EventType` enum. `Task.gpu_cost_usd` does not exist as a field; the network/llm/external/compute four-aggregate model from Phase 1 needs extending to five.

Cloud detection (`cloud_detect.py` + ports) already resolves provider + region + instance type for AWS/Azure/GCP/Modal/RunPod/Lambda/CoreWeave/Replicate (Phase 1). The Phase 1 `CloudEnv.instance_type` field is the discovery signal for the `per_instance_hour` billing model; the NVML productName cascade (Decision #4) is the discovery signal for the `per_gpu_second_active` / `per_gpu_hour_reserved` / `per_vgpu_hour` models where IMDS doesn't disclose the GPU SKU.

**Why GPU is harder than compute:**

1. **Measurement primitive is a native library, not a file read.** Compute reads `/sys/fs/cgroup/{cpu.stat, memory.peak, memory.max}` — pure POSIX file I/O. GPU reads NVML via `pynvml` / `nvml-wrapper` / `NVIDIA/go-nvml` — a C library that must be present, loadable, and runtime-driver-compatible. **TypeScript has no maintained native binding** (research §7), so `dexcost-ts` must shell out to `nvidia-smi --query-gpu=...` and parse CSV.

2. **Per-PID attribution requires cgroup-walking.** Compute attributes the entire dexcost-task wall-window. GPU attributes per-process GPU time, and customer workloads (PyTorch DDP, Ray actors, vLLM workers) fork worker processes that hold the GPU while the dexcost-instrumented parent holds nothing. Decision #1's cgroup-walk is the architectural answer; the verification matrix is the empirical confirmation.

3. **Idle gap is 380× larger than CPU's.** Decision #6 inherits Phase 1's source-measurement boundary but amplifies the customer-facing framing. Decision #3's `gpu_utilization_signal` is the new event type that surfaces the gap as actionable signal at install time.

4. **VRAM tier is in the SKU key, not a multiplier** (Decision #7). Eliminates the binary-vs-decimal divisor headache that bit Fargate. `vram_gb` is a display field.

## 3. Decisions (narrative summary)

The full 11-decision log lives at [`decisions/2026-05-22-gpu-foundation-decisions.md`](../decisions/2026-05-22-gpu-foundation-decisions.md). This spec restates the capture-relevant decisions in narrative form; the cost-math-relevant ones live in the v2 sibling spec.

| # | Decision | Reason |
|---|---|---|
| #1 (load-bearing) | Multi-PID via cgroup-membership walk + self-PID-only fallback on permission failure | PyTorch DDP / Ray / vLLM workers fork; naive self-PID-only undercounts 4-8× |
| #1 sharpening | Cgroup-scope classification table (kubepods/docker/containerd/crio/user.slice/root) + bare-metal degradation + multi-container K8s limitation | Spec-MUST-clear verification gate; silent overcount/undercount cases otherwise |
| #2 | MIG slices billed at full-GPU rate in v1; `mig_profile` captured in details for v1.1 forward-compat | No cloud surveyed bills per-slice in 2026 |
| #2 sharpening | Log-once `gpu_mig_detected_full_billing_applied` for transparency | 7 tasks on 7 MIG slices look mysteriously like 7× without the log |
| #3 | Ship `gpu_utilization_signal` event in v1 — no `cost_usd`, observability-only | 380× idle gap magnitude; surfaces the gap as actionable signal at install |
| #3 sharpening | Emission is **task-window-averaged**, NOT point-sampled at finalize | Point-sample misses any task with a quiet tail; window-averaged math reuses Decision #8's accumulators |
| #4 | NVML productName → catalog-key via inline aliases array per SKU; device_class fallback for cold start | Modal/RunPod/Lambda don't set SKU env var; aliases inline keep mapping close to rate |
| #4 sharpening | NFC Unicode normalization in alias matching | Driver versions vary on non-breaking spaces; without NFC two visually-identical strings differ at byte level |
| #5 | NVIDIA only in v1; AMD/Intel deferred via `details.gpu_vendor` forward-compat | NVIDIA has 92%+ data-center share; AMD/Intel binding inconsistency across 4 SDKs |
| #6 | Source-measurement boundary inherited; framing amplified for 380× magnitude | Mandatory customer-facing language at v1 launch |
| #7 | No GPU Decision-#7 sibling; VRAM tier in SKU key, `vram_gb` display-only | Eliminates binary-vs-decimal divisor question |
| #8 | NVML `lastSeenTimeStamp` persisted per-PID across snapshot calls | Naive impl misses samples between calls |
| #9 | GCP N1+accelerator via NVML-only fallback (no metadata-server endpoint) | GCP doesn't expose `acceleratorType` from inside the VM |
| #10 | Azure NVadsA10 v5 vGPU: best-effort distinction via NVML productName; full-A10 assumption fallback | Verification spike pending; spec falls back per Decision #10's two-branch design |
| #11 | Weekly GPU catalog refresh, 90/365-day soft-warn/hard-fail | GPU rates moved 40%/year across 2025 |

## 4. Data Model

### 4.1 Task — one new field

| Field | Type | Meaning |
|---|---|---|
| `gpu_cost_usd` | Decimal | New field; sibling of `compute_cost_usd` / `network_cost_usd`. Default `Decimal("0")`. |

Aggregation: `total_cost_usd = llm + external + compute + network + gpu`.

### 4.2 New `gpu_cost` event type

Represents one task's dollar GPU attribution. **At most one per task** (capture §5.3 invariant), regardless of how many GPUs the task touched. Multi-GPU usage is summed into the single `gpu_cost` event's `cost_usd`; per-GPU breakdown lives on the `gpu_utilization_signal` events (one per GPU per task).

```
event_type:      "gpu_cost"
service_name:    "aws.ec2.p5.48xlarge" | "gcp.gce.a3-highgpu-8g" | "azure.vm.Standard_ND96isr_H100_v5" |
                 "modal.h100" | "runpod.h100-sxm" | "lambda.h100-sxm5" | "coreweave.h100-hgx-sxm5" |
                 "replicate.h100" | "gcp.gce.n1+nvidia-h100-80gb"
cost_usd:        <Decimal>
cost_confidence: "computed" | "estimated"   # "exact" never (always derived); "unknown" only on Tier-5 failure
pricing_source:  "gpu_catalog:<provider>:<sku>:<region>"                       # exact match
                 "gpu_catalog:<provider>:<sku>:default"                        # provider known, region missing
                 "gpu_catalog:<provider>:<sku>:device_class_fallback"          # SKU unknown, device_class default applied
                 "gpu_catalog:default:<billing_model>"                         # universal _meta default
                 "gpu_catalog:hardcoded:<billing_model>"                       # Tier-4
                 "gpu_catalog:<provider>:<sku>:self_pid_only"                  # Decision #1 fallback
                 "gpu_catalog:<provider>:<sku>:no_container_scope"             # bare-metal degradation
                 "gpu_catalog:<provider>:<sku>:multi_container_pod_partial"    # K8s sidecar case
pricing_version: "gpu:<catalog_version>"
details: {
  billing_model:      "per_instance_hour" | "per_gpu_second_active" |
                      "per_gpu_hour_reserved" | "per_vgpu_hour",
  gpu_vendor:         "nvidia",                            # Decision #5 forward-compat (all v1 = "nvidia")
  gpu_sku:            "h100-80gb-sxm5",                    # canonical catalog key
  gpu_count:          <int>,                                # number of GPUs the task touched
  region:             "us-east-1" | "us-central1" | null,
  instance_type:      "p5.48xlarge" | "Standard_ND96..." | null,    # set when per_instance_hour
  vgpu_profile:       "1/6 A10" | "full A10" | null,        # set when per_vgpu_hour; Decision #10
  mig_profile:        "1g.5gb" | null,                      # Decision #2 — captured but ignored for v1 math
  gpu_seconds_used:   <float>,                              # total active GPU-seconds across all PIDs
  duration_ms:        <int>,                                # task wall-clock
  cost_pending:       true                                  # deferred-cost marker (Phase 1 §6.4 pattern)
}
```

### 4.3 New `gpu_utilization_signal` event type (Decision #3)

Observability-only — **no `cost_usd`, no `pricing_source`, no `cost_confidence`, no `pricing_version`**. One per task per GPU (multi-GPU tasks emit N signal events alongside the single `gpu_cost`).

```
event_type:      "gpu_utilization_signal"
task_id:         <UUID>
timestamp:       <ISO-8601>
details: {
  gpu_index:             0,
  gpu_sku:               "h100-80gb-sxm5",
  sm_util_pct:           35.0,           # task-window-averaged kernel-time %; Decision #3 sharpening
  mem_util_pct:          22.0,           # task-window-averaged NVML memUtil
  vram_used_peak_bytes:  21474836480,    # peak across the task window (NOT point sample)
  vram_total_bytes:      85899345920,
  process_count:         4,              # cgroup-membership PIDs that held GPU during window
  sample_count:          53,             # number of NVML samples accumulated
  task_duration_ms:      312500,
}
```

**Field rules:**
- `sm_util_pct` is NVML `smUtil` averaged across the task window. Per research §1.1, smUtil is **percent of time the GPU's SMs had ≥1 kernel running, NOT fractional SM occupancy.** A single-block kernel pegging the GPU reads as 100% even if it uses 1/108 SMs. Documentation MUST say this.
- `sm_util_pct == null` when `task_duration_ms == 0` (sub-100ms degenerate tasks).
- `vram_used_peak_bytes` is a high-water mark (peak), not an average — peak-vs-limit is the right-sizing signal.
- All byte fields are bytes (Decision #7 — VRAM is display-only; no per-byte multiplier).

### 4.4 Emission rules

**One `gpu_cost` event per task per dexcost-task lifecycle.** If a task touches GPUs on multiple devices, the `cost_usd` is the sum and `gpu_count` is the count. Per-device breakdown via the parallel `gpu_utilization_signal` events.

**One `gpu_utilization_signal` event per task per GPU.** A task using 4 devices emits 1 `gpu_cost` + 4 signal events.

**Skipped if NVML init fails / no GPU detected / runtime is `unknown`.** No empty events.

**`cost_pending: true` at emission for `gpu_cost`** — back-filled at task finalize via the deferred-cost pattern (Phase 1 §6.4). `gpu_utilization_signal` events have no cost; they emit with final values directly.

### 4.5 Schema changes

- `dexcost-event.v1.json` event_type enum gains `"gpu_cost"` AND `"gpu_utilization_signal"`. Two values.
- Task model gains `gpu_cost_usd: Decimal`.
- Per-SDK event serializer code accepts arbitrary `details` keys (matches Phase 1 pattern).
- No new Task columns beyond `gpu_cost_usd`; per-GPU breakdown lives on the signal events.

## 5. Components & Flow (SDK side)

### 5.1 Components

1. **`GpuRuntimeResolver`** — sibling of `compute_runtime`. Cascade:
   - Phase 1a: env-var resolution (Modal/RunPod/Replicate explicit env vars + Phase 1's `cloud_detect` provider).
   - Phase 1b: NVML init probe (`nvmlInit()` returns success/`NVML_ERROR_DRIVER_NOT_LOADED`).
   - Phase 2 (background, never blocks init): NVML device enumeration + per-device `productName` capture → SKU alias resolution (Decision #4).
   - Returns `GpuRuntimeKind` enum: `Nvidia`, `Amd` (stub), `Intel` (stub), `None`.

2. **`NvmlReader`** — pure helpers wrapping NVML calls:
   - `init() / shutdown()` — fail-silent on `NVML_ERROR_DRIVER_NOT_LOADED` / `NVML_ERROR_LIBRARY_NOT_FOUND`.
   - `get_device_count() -> int`
   - `get_device_handle(index) -> handle`
   - `get_product_name(handle) -> str` — UTF-8 decoded, NFC-normalized (Decision #4 sharpening).
   - `get_compute_running_processes(handle) -> list[ProcessInfo]` — fail-silent on permission denied.
   - `get_process_utilization(handle, last_seen_timestamp_per_pid: dict) -> dict[pid, UtilSample]` — Decision #8 stateful API.
   - `get_memory_info(handle) -> MemInfo` — VRAM total/used.
   - `get_mig_mode(handle) -> bool` — MIG detection for Decision #2 log.
   - **TypeScript exception:** shells out to `nvidia-smi --query-gpu=...` and `--query-compute-apps=...` with CSV parsing. Same Python/Go/Rust function signatures via different implementation.

3. **`CgroupWalker`** — Decision #1's container-scope classifier:
   - `classify_scope() -> CgroupScope` — reads `/proc/self/cgroup`, matches prefixes from Decision #1's table (kubepods.slice/docker/containerd/crio/user.slice/root), returns one of `{Container(path), BareMetalUserSlice, RootCgroup, Unknown}`.
   - `enumerate_pids(scope) -> list[int]` — reads `cgroup.procs` for Container scope; returns `[os.getpid()]` for any non-Container scope (the bare-metal-no-container degradation).
   - Per-mode log-once tokens: `gpu_cgroup_walk_forbidden`, `gpu_no_container_scope`, `gpu_multi_container_pod_partial`, `gpu_cgroup_v1_only` (future v1.1).

4. **`GpuAccountant`** — per-task in-process accumulator. Lives on Task as `_gpu` (mirrors `_compute` / `_network`). Holds:
   - The start cgroup snapshot (PID set at task start)
   - The NVML start snapshot (per-PID `lastSeenTimeStamp` from initial `get_process_utilization` call — Decision #8)
   - The per-device memory peak tracker
   - The emission-frozen flag (Decision #3 + capture §5.3 invariant)
   - **At task finalize:** calls `NvmlReader.get_process_utilization` with the persisted timestamps, accumulates `total_sm_us_per_pid`, sums across the cgroup PIDs, computes `gpu_seconds_used` AND `sm_util_pct` (window-averaged per Decision #3 sharpening), emits one `gpu_cost` event with `cost_pending: true` + one `gpu_utilization_signal` event per device.

5. **`HandlerWrap` (serverless GPU)** — Modal / RunPod / Replicate per-invocation handler decorator. Mirrors Phase 1's `compute_wrap`:
   - Reads runtime-specific env vars (`MODAL_TASK_ID`, `RUNPOD_POD_ID`, etc.).
   - Constructs `GpuAccountant`, attaches to `task._gpu`.
   - Times the handler with monotonic clock.
   - On exit (success OR exception), persists the events with `cost_pending: true` for the `gpu_cost`. Re-raises handler exceptions AFTER persisting (handler exceptions don't refund the GPU-seconds — Modal et al. bill the failed invocation).

### 5.2 Per-language thread-safety

The GPU accountant is mutated by exactly one writer per task (same shape as compute):

| SDK | Strategy |
|---|---|
| Python | `threading.Lock` around the freeze flag + snapshot pair |
| TypeScript | none — single-threaded event loop; freeze flag is sufficient |
| Go | `sync.Mutex` around the snapshot pair; out-of-band registry attaches accountant to task (Phase 1 Go-SDK pattern) |
| Rust | `std::sync::Mutex` (matches `NetworkAccountant` and `ComputeAccountant` — sync contexts) |

### 5.3 The "≤1 `gpu_cost` event per task" + "≤1 `gpu_utilization_signal` per task per GPU" invariants

A single dexcost task produces:
- **At most one `gpu_cost` event.** `GpuAccountant.emit_cost_event(...)` is idempotent — second call no-ops via the freeze flag.
- **At most N `gpu_utilization_signal` events**, one per NVML device the task's cgroup touched during the window. If a task touched only device 0, exactly one signal event; if it touched devices 0 and 2, exactly two signal events; if NVML reports devices but the cgroup walk never finds PIDs holding them, **zero signal events** (no idle pollution).

This is the convention §1 carve-out from Decision #3 in practice: the primary cost event obeys the "one per task" rule; the secondary signal events are observability-only and may emit multiple per task per device — but never speculatively, only when measurement detected actual usage.

### 5.4 Flow

**Serverless GPU runtimes (Modal / RunPod / Replicate — `per_gpu_second_active`):**

1. Handler wrap: read env vars → resolve `RuntimeKind::Modal` (or similar) → `NvmlReader.init()` → enumerate cgroup PIDs at task start → `NvmlReader.get_process_utilization(handle, {})` for the baseline `lastSeenTimeStamp` snapshot.
2. Handler runs (the customer's function with CUDA workload).
3. On return (or exception): re-enumerate cgroup PIDs → `get_process_utilization(handle, persisted_timestamps)` → accumulate per-PID samples → compute `gpu_seconds_used = sum(sm_us) / 1_000_000` and window-averaged `sm_util_pct`.
4. Emit `gpu_cost` event with `cost_pending: true`, `billing_model: "per_gpu_second_active"`, `gpu_seconds_used`, etc.
5. Emit `gpu_utilization_signal` event per GPU touched.
6. Task finalize → v2 cost-attribution layer back-fills `cost_usd` for the `gpu_cost` event using `gpu_seconds_used × rate_per_gpu_second`.

**Long-running GPU runtimes (EC2/GCE/Azure VM/Lambda Labs/CoreWeave — `per_instance_hour` or `per_gpu_hour_reserved`):**

1. Task start (in tracker / auto-task): `GpuAccountant.snapshot_start()` — same NVML + cgroup snapshot as above.
2. Task runs.
3. Task finalize (in `_aggregate_costs` / equivalent — alongside the existing compute finalize block): `snapshot_end_and_build()` does the diff, builds the event with `details.duration_ms = task_window_ms` and `details.gpu_seconds_used = <window-active GPU seconds>`.
4. Same back-fill at finalize. Cost math is `share_factor × hourly_rate` (similar shape to compute EC2 share math).

**vGPU runtimes (Azure NVadsA10 v5 — `per_vgpu_hour`):**

Same as long-running; `vgpu_profile` field set per Decision #10's verification result (currently `(pending spike capture)`).

**Idle path (Decision #6):** no event emitted between dexcost tasks. The cgroup keeps counting, NVML keeps sampling, but the SDK only reads at task boundaries. Per-device idle-vs-active is surfaced via `gpu_utilization_signal.sm_util_pct` at the next task finalize — the customer sees "your task ran with 35% GPU utilization" and learns the gap directly.

### 5.5 Detection priority & runtime overlap

GPU runtime resolution layers on top of Phase 1's compute runtime resolution. The `cloud_detect` provider answer (e.g. `aws`) gates the GPU resolver but doesn't itself emit GPU events — that's NVML's job.

Priority (after Phase 1 compute resolver runs and identifies the provider):

```
NVML init succeeds → enumerate devices → assert at least 1 device → GpuRuntime = Nvidia
NVML init fails → GpuRuntime = None → no GPU capture (silent no-op)
```

Independently, the `billing_model` is selected from the provider:
- `modal` / `runpod` / `replicate` → `per_gpu_second_active`
- `lambda_labs` / `coreweave` → `per_gpu_hour_reserved`
- `aws` / `gcp_bundled` / `azure_vm_gpu` → `per_instance_hour` (catalog has instance-type → GPU SKU mapping)
- `gcp_n1+accelerator` → `per_gpu_hour_reserved` (Decision #9)
- `azure_nvads_a10_v5` → `per_vgpu_hour` (Decision #10)

A pod-on-EC2-GPU follows compute's same precedence: K8s wins over the underlying VM (avoids double-counting compute hours). But GPU billing is per-attached-device — both Decision #1's cgroup walk AND the K8s/EC2 resolver land on the same answer: the customer's task pays for its share of the GPU it actually touched.

## 6. Error Handling & Edge Cases

1. **NVML not installed / driver not loaded.** `nvmlInit()` returns `NVML_ERROR_DRIVER_NOT_LOADED` or `NVML_ERROR_LIBRARY_NOT_FOUND` → log-once `gpu_no_driver_in_container` → no GPU events. The customer's container without GPU access has zero GPU capture — silent and correct.
2. **NVML init succeeds but no devices** (`nvmlDeviceGetCount() == 0`). Same as above; no events.
3. **NVML permission denied for `nvmlDeviceGetComputeRunningProcesses`** (research §1.1 calls out non-root containers as the failure case). Per Decision #1, the cgroup-walk fallback ladder kicks in. The exact failure mode (`NVML_ERROR_NO_PERMISSION` vs silent empty list vs returning everything) is `(pending spike capture in non-root-container/)` — the spec's `try/except` choice depends on this.
4. **Cgroup walk denied** (`/proc/<other_pid>/cgroup` returns EACCES). Decision #1's degradation: self-PID-only at `estimated` confidence, `pricing_source: ":self_pid_only"`, log-once `gpu_cgroup_walk_forbidden`.
5. **Bare-metal-no-container detected** (cgroup scope is `user.slice/...` or root). Decision #1 degradation: self-PID-only at `estimated` confidence, log-once `gpu_no_container_scope`. The cgroup-scope classification table in Decision #1's sharpening is the authoritative list of prefixes; `(pending spike capture in bare-metal-host/ for empirical confirmation)`.
6. **Multi-container K8s pod** (NVML reports compute processes whose PIDs are NOT in dexcost's container cgroup). Decision #1 degradation: `:multi_container_pod_partial` confidence, log-once `gpu_multi_container_pod_partial`. The capture excludes the foreign PIDs — over-attribution is impossible, under-attribution is the surfaced limitation.
7. **MIG mode detected.** Decision #2: full-GPU rate used; `details.mig_profile` captured; log-once `gpu_mig_detected_full_billing_applied`.
8. **vGPU profile detected (Azure NVadsA10 v5).** Decision #10's two-branch: if NVML productName distinguishes the profile (case A or B), catalog SKU resolution succeeds at `computed` confidence. If not (case C), full-A10 assumption at `estimated` confidence with `pricing_source: "gpu_catalog:azure:nvads_a10_v5:full_a10_assumption"`. **`(pending spike capture in azure-nvadsa10-v5-vgpu/)`**.
9. **NVML productName unknown to catalog.** Decision #4 device_class fallback: derive `device_class` (hopper/ampere/ada-lovelace/blackwell) from productName substring matching → use class default rate → `estimated` confidence, `pricing_source: ":device_class_fallback"`. If even the device class can't be resolved, Tier-5 fail-silent with `cost_usd = 0`.
10. **Cgroup v1 host.** Out of scope for v1; degrade to self-PID-only with log-once `gpu_cgroup_v1_only`.
11. **TypeScript SDK on a GPU host.** `nvidia-smi` shell-out replaces NVML library calls. Same `NvmlReader` interface; different implementation. CSV parsing is the failure surface; fail-silent on parse errors → no events.
12. **Sub-100ms tasks (degenerate window).** `gpu_utilization_signal.sm_util_pct = null` per Decision #3 sharpening; `gpu_cost.gpu_seconds_used` may also be 0 if the task ended within one NVML sample period; cost_usd correctly evaluates to ~0.
13. **Buffer overflow on long-running tasks** (`nvmlDeviceGetProcessUtilization` sample buffer wraps before the next snapshot). Decision #8 sharpening: log-once `gpu_nvml_buffer_overflow` — flags v1.1 enhancement (background sample-buffer flushing).
14. **Snapshot-and-freeze at task end.** Like NetworkAccountant + ComputeAccountant, the GPU accountant freezes at finalize. Late `emit_cost_event` calls no-op. No late-arriving samples mutate already-shipped events.
15. **Handler exception in serverless wrap.** Event still emits (the GPU-seconds were consumed; Modal et al. bill regardless). Handler exception re-raised AFTER event persisted.

## 7. Testing (per SDK — Python first)

**Unit**
- `NvmlReader` — mock NVML responses for: device-count, productName per device, compute-running-processes list, process-utilization samples with persisted timestamps. Verify NFC normalization on productName matching.
- `CgroupWalker` — fixture files for each scope prefix in Decision #1's table (kubepods.slice/docker/containerd/crio/user.slice/root). Assert correct classification → correct `enumerate_pids` behavior.
- `GpuAccountant` — start/end snapshot pair across all 4 billing models. Verify `gpu_seconds_used` math against hand-computed expected.
- Per-billing-model cost math (in v2 sibling spec) — pin canonical case per model (e.g. Modal H100 × 1.234 sec, Lambda H100 × 60 sec window, AWS p4d × share-factor math).
- Decision #3 emission cadence — pin that `sm_util_pct` is window-averaged, NOT point-sampled. Test with a 5-second simulated task that runs at 80% for 4 seconds and 0% for 1 second → assert ~64% average, NOT 0%.
- Decision #4 alias matching — feed `productName = "NVIDIA  H100  80GB  HBM3"` (multiple spaces) → assert matches `"NVIDIA H100 80GB HBM3"` alias post-normalization. Feed Unicode non-breaking space U+00A0 → assert matches.
- Decision #2 MIG transparency — fixture with NVML reporting MIG UUIDs → assert log-once `gpu_mig_detected_full_billing_applied` AND `details.mig_profile` populated AND full-GPU rate used.
- Tier-5 fail-silent — corrupt catalog → no exception, `cost_usd = 0`.

**Integration**
- Mock Modal runtime: env vars set, handler invoked via wrap, NVML mock returns plausible H100 productName + samples → `gpu_cost` event emits at `per_gpu_second_active`, `gpu_utilization_signal` event emits, cost back-fills at finalize.
- Mock Lambda Labs: long-running task on a mocked H100 SXM5 productName → `gpu_cost` at `per_gpu_hour_reserved`, share-factor math correct.
- Mock K8s pod: `KUBERNETES_SERVICE_HOST` set, cgroup walk returns `kubepods.slice/...` → `gpu_cost` at `computed` confidence.
- Mock non-root container: NVML returns permission error → fall through to self-PID-only, `pricing_source: ":self_pid_only"`, log-once.
- Mock bare-metal: `/proc/self/cgroup` = `user.slice/...` → degrade to self-PID-only, log-once `gpu_no_container_scope`.

**Regression**
- Existing `compute_cost` and `network` events unaffected by the new GPU capture path.
- `task.total_cost_usd == llm + external + compute + network + gpu` invariant holds across mixed-cost tasks.

**Property invariants (v2 spec):** per-billing-model conversion math + the "one `gpu_cost` per task" / "≤N `gpu_utilization_signal` per task" invariants.

## 8. Boundaries / Non-Goals (v1)

- **No AMD ROCm / Intel oneAPI capture.** Decision #5 — `details.gpu_vendor` forward-compat for v1.1.
- **No MIG per-slice billing.** Decision #2 — full-GPU rate; `mig_profile` captured for v1.1.
- **No DCGM exporter scrape.** Daemon dependency; deferred to v1.1 as enrichment if available.
- **No inference-server `/metrics` scrape** (vLLM, Triton, TGI). Same daemon-dependency reasoning; v1.1.
- **No cgroup-v1 cgroup-walk.** Older RHEL 7 / CentOS 7 hosts degrade to self-PID-only at `estimated` confidence. v1.1 adds cgroup-v1 walking.
- **No cross-container K8s pod attribution.** Decision #1 sharpening — sidecar containers in the same pod can't be walked; surface as `:multi_container_pod_partial`. v1.1 explores Downward API path.
- **No GPU-on-non-Linux hosts.** macOS / Windows have no cgroup; NVML on those platforms isn't a dexcost-supported scenario.
- **No fractional VRAM billing.** Decision #7 — no provider bills per-VRAM-GiB-second in 2026; `vram_used_peak_bytes` is display-only.

## 9. Control Layer Dependencies

This spec covers the **SDK capture side**. The Control Layer (ingest, ClickHouse aggregation, reconciliation, Cost Intelligence dashboard) is a separate workstream.

**SDK → Control Layer contract:**

1. Every `gpu_cost` event has a unique `event_id` (UUIDv4); same for `gpu_utilization_signal`.
2. `details.billing_model` is one of the four documented discriminator values. Unknown values → Control Layer's dead-letter table for catalog updates.
3. `details.gpu_vendor` is `"nvidia"` in v1; `"amd"` / `"intel"` reserved for v1.1.
4. `details.gpu_sku` is the canonical catalog key (e.g. `"h100-80gb-sxm5"`) — same key across providers (a Modal H100 and an AWS p5's H100 share `gpu_sku`).
5. `cost_pending: true` events get an UpdateEvent re-push within the same task finalize cycle; Control Layer dedups by `event_id`.
6. `gpu_utilization_signal` events have NO `cost_usd`. Control Layer must not include them in any cost aggregate; they're observability-only.
7. The "dexcost gpu total < cloud GPU invoice on long-running runtimes" gap is **expected, not a bug**. Decision #6. Reconciliation surface explains it server-side using the `gpu_utilization_signal` events.

Per-task `gpu_cost_usd` is the sum of all `gpu_cost.cost_usd` for that task (which in v1 is at most one event per task; the sum invariant still holds).

## 10. Pre-Requisite

The cross-subsystem **registry pattern** (task_id → accountant pointer) used by `NetworkAccountant` + `ComputeAccountant` is the same pattern `GpuAccountant` uses. Already shipped across all four SDKs; no prerequisite to land first.

The cross-subsystem **conventions doc** ([`conventions.md`](../conventions.md)) is the prerequisite reading. One convention update lands as part of v1 implementation: §1 carve-out for observability-only signal events (Decision #3 reference example).

The **NVML cgroup-walk verification matrix** ([`verification/2026-05-22-gpu-nvml-container-matrix/`](../verification/2026-05-22-gpu-nvml-container-matrix/)) is the prerequisite for declaring Decision #1 implemented. The spec is drafted on the documented hypotheses; sections marked `(pending spike capture)` need revision when the captures land.

## 11. Future (out of scope here, recorded for continuity)

- **AMD ROCm capture (v1.1):** `amdsmi` Python binding + `amd-smi` shell-out for Go/Rust/TS. `details.gpu_vendor = "amd"` already in the schema.
- **Intel oneAPI capture (v1.1):** sysman / level-zero bindings; same forward-compat path.
- **DCGM exporter scrape enrichment (v1.1):** if available at `localhost:9400/metrics`, enrich `gpu_utilization_signal` with extra fields (GPU temperature, power draw, ECC errors). Optional; never required.
- **Inference-server `/metrics` enrichment (v1.1):** detect vLLM / Triton / TGI Prometheus endpoints; surface model-name, request-rate, prompt-token-throughput as enrichment on `gpu_utilization_signal`.
- **MIG per-slice billing (v1.1):** when (if) a cloud ever ships per-slice pricing. `details.mig_profile` already captured.
- **Cgroup-v1 walker (v1.1):** RHEL 7 / CentOS 7 long-tail support.
- **K8s sidecar attribution via Downward API (v1.1):** read `spec.nodeName` + cross-container cgroup access patterns.
- **GPU-aware reconciliation surface (Control Layer):** uses `gpu_utilization_signal` to render the per-cloud "$X attributed vs $Y invoiced" variance as a first-class dashboard line.
- **`gpu_by_device` per-task aggregate (v2):** analog of `network_by_host`; per-device sub-totals on Task if dashboards need it.
- **Modal/RunPod region-aware pricing:** today their catalog blocks are global; if either ever ships per-region rates, the catalog adds the region nesting (no spec change).
