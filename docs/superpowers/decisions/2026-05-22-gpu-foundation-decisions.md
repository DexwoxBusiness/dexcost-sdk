# Phase 2 — GPU Foundation — Decisions Log

**Locked:** 2026-05-22
**Status:** Final. Subsequent specs and implementation reference this document; any change goes through a separate change request, not a quiet revision of this file.
**Reference research:** [`research/2026-05-22-gpu-foundation-research.md`](../research/2026-05-22-gpu-foundation-research.md)
**Lineage:** Inherits cross-subsystem conventions from [`conventions.md`](../conventions.md) (§§1, 2, 3, 4, 5, 9, 11). Phase 1 decisions ([`2026-05-20-compute-foundation-decisions.md`](2026-05-20-compute-foundation-decisions.md)) for source-measurement boundary, fail-silent discipline, and idle-is-invisible framing transfer to GPU without restatement — see Decision #6 below for the GPU-specific extension.

The eleven decisions that gate the Phase 2 — GPU Foundation spec, plus the strengthenings that emerged during the lock-in conversation. Each decision is recorded with: what was decided, the options considered, the chosen answer, the rationale, and any implementation sharpening that should land in the spec.

---

## Decisions 1–11

### Decision 1 — Multi-PID GPU attribution (the load-bearing one)

**Question:** When a customer's dexcost-instrumented Python process forks CUDA workers (PyTorch DataLoader, Ray actors, multiprocessing pools, vLLM worker processes), the child PIDs hold the GPU but the parent PID doesn't. NVML reports utilization scoped per-PID. How does dexcost-GPU attribute the forked workers' GPU time to the parent's task?

**Options:**
- (a) **Self-PID only.** dexcost reads NVML samples only for its own PID; misses everything in child processes. Permission-clean but undercounts distributed-training workloads dramatically.
- (b) **Self + descendants via cgroup membership.** Walk `/proc/<self>/cgroup`, find the parent task's cgroup, enumerate all PIDs in that cgroup (`cgroup.procs`), accumulate NVML samples for each.
- (c) **All PIDs with device access.** Call `nvmlDeviceGetComputeRunningProcesses` to list every PID touching the GPU, attribute everything not-otherwise-claimed to the calling task.

**Locked:** **(b) — self + cgroup-membership descendants**, with **(a) as the fail-silent fallback** when cgroup walk or NVML permissions fail.

**Rationale:**
1. PyTorch DDP, Ray actors, multiprocessing pools, and vLLM workers are the **most common GPU workload patterns in 2026**. Option (a) would silently undercount these by 4-8× (one parent PID, N-1 worker PIDs holding the actual compute). The first-install experience would be confusingly low numbers.
2. Cgroup membership is the right scoping boundary. The customer's task lives in a cgroup; their child processes inherit it; processes outside that cgroup (other customers' workloads on a shared GPU host) are correctly excluded.
3. Option (c) overshoots in shared environments — a Jupyter notebook on a multi-tenant GPU host would steal attribution from co-located workloads.
4. Permissions are the failure mode, not the design center. Inside an unprivileged container, the cgroup walk may fail (`/proc/<other_pid>/cgroup` returns EACCES) or NVML may return `NVML_ERROR_NO_PERMISSION` for non-self PIDs. Fail-silent + log-once + degrade to self-PID-only via convention §11 — the customer still gets a number, the log surfaces the degradation, dexcost keeps running.

**Implementation sharpening:**
- The GPU accountant reads `/proc/<self>/cgroup` once at task start to identify the cgroup path; caches it for the task lifetime.
- At each NVML sample (start, end), enumerate `cgroup.procs` (cgroup v2) or `tasks` (cgroup v1, future); call `nvmlDeviceGetProcessUtilization` for each PID in the set; accumulate.
- A PID that disappears between start and end (forked worker exited) is handled by reading its terminal `lastSeenTimeStamp` from the start snapshot — the end snapshot won't list it but the start snapshot captured its accumulated samples.
- Per-mode log-once tokens: `gpu_cgroup_walk_forbidden`, `gpu_nvml_pid_forbidden:<pid>`, `gpu_cgroup_v1_only` (future v1.1).
- Confidence labelling: full cgroup walk succeeded → `computed`; degraded to self-only → `estimated` + `pricing_source: "gpu_catalog:<provider>:<sku>:self_pid_only"`.

**Customer-facing framing (mandatory):**
> dexcost-GPU attributes GPU time across your task's cgroup — both the parent process and any worker processes it spawned (PyTorch DataLoader, Ray, multiprocessing, vLLM workers). In unprivileged containers where the cgroup walk or NVML lookup is denied, dexcost falls back to attributing only the calling process's GPU usage; this surfaces as `cost_confidence: estimated` and a per-process log warning. To get full attribution, grant the dexcost-running container `--cap-add=SYS_PTRACE` or run NVML with `nvidia-container-toolkit --gpus all` (the default on modern Kubernetes GPU operators).

---

### Decision 2 — MIG slice billing scope in v1

**Question:** NVIDIA MIG (Multi-Instance GPU) partitions a single A100 or H100 into up to 7 isolated slices. Each slice appears as its own GPU under NVML. Some clouds (CoreWeave) expose per-slice rentals; most clouds bill the full physical GPU and let the customer carve it up. How does dexcost-GPU model billing for MIG?

**Options:**
- (a) **v1 = full physical GPU rate.** Bill the customer the full SKU rate regardless of MIG configuration. Defer per-slice billing to v1.1 if any cloud ever bills that way at scale.
- (b) **v1 = per-slice fractional rate.** Treat each MIG slice as 1/7 (or 1/N) of the physical GPU's rate.
- (c) **Detect-and-defer.** Capture MIG configuration in event details for future analysis but use full-GPU rate for the dollar.

**Locked:** **(a) + (c) — full-GPU rate, MIG configuration captured in `details.mig_profile`** for the future per-slice billing case.

**Rationale:** Per research, every cloud surveyed in 2026 bills the full physical GPU even when the customer carves it into MIG slices. CoreWeave's per-slice pricing was preview-only and discontinued in 2025. Modeling fractional billing in v1 would systematically under-attribute against the actual cloud invoice — the wrong direction (customers complain about under-attribution).

**Schema-forward-compatible:** `details.mig_profile` (e.g. `"1g.5gb"`, `"2g.10gb"`, `"3g.20gb"`, `null` if not MIG) is captured for v1 even though v1 ignores it for the math. v1.1 adds the fractional math additively when needed; no schema migration.

---

### Decision 3 — `gpu_utilization_signal` event type (the 380× idle gap call)

**Question:** Decision #9 from Phase 1 (idle compute is invisible to dexcost) holds for GPU too, but the magnitude is 380× larger ($55/hr `p5.48xlarge` idle vs $0.145/hr `c7g.xlarge` idle). A customer installing dexcost-GPU and seeing `$850 attributed vs. $35,000 cloud bill` without explanation files a "dexcost is undercounting" bug on day one. Does v1 ship a side-channel utilization signal to surface the gap, or wait for the future server-side reconciliation surface?

**Options:**
- (a) **Defer — same as Phase 1 compute.** The gap is a known feature; reconciliation surface explains it server-side eventually.
- (b) **Ship a new `gpu_utilization_signal` event type in v1** with no `cost_usd` field, carrying `gpu_util_pct`, `vram_used_bytes`, `vram_total_bytes`, sampled at task-finalize time. The signal surfaces the gap immediately as actionable data.

**Locked:** **(b) — ship `gpu_utilization_signal` in v1.**

**Rationale:** The 380× magnitude is the differentiator. A customer who sees the gap explained as a real utilization number (`"35% GPU utilization across your H100 cluster"`) gets actionable signal immediately and stays. A customer who sees only the bare attributed number files a bug. The cost to ship is one new event type; the cost to defer is customer trust on first install.

This is **the only convention-text update Phase 2 introduces** — `conventions.md §1` (one event type per subsystem) gains an explicit carve-out: *"A subsystem MAY introduce a secondary 'signal' event type alongside its primary cost event, provided the signal event has no `cost_usd` field and is documented as observability-only. Phase 2 GPU's `gpu_utilization_signal` is the reference example."*

**Implementation sharpening:**
- Event shape:
  ```
  event_type:     "gpu_utilization_signal"
  task_id:        <UUID>
  timestamp:      <ISO-8601>
  details: {
    gpu_index:        0,
    gpu_sku:          "h100-80gb-sxm5",
    sm_util_pct:      35.0,         # NVML smUtil — kernel-time percentage (NOT SM-occupancy)
    mem_util_pct:     22.0,         # NVML memUtil
    vram_used_bytes:  21474836480,
    vram_total_bytes: 85899345920,
    process_count:    4,            # NVML compute-processes count on this device
    sampled_at:       <ISO-8601>,
  }
  ```
- NO `cost_usd`, NO `pricing_source`, NO `cost_confidence`, NO `pricing_version`. Pure observability.
- Emitted exactly once per task per GPU at task finalize, mirroring `compute_cost`'s emission discipline.
- The `sm_util_pct` field documentation MUST say: *"NVML smUtil — percent of time the GPU's SMs had ≥1 kernel running. NOT fractional SM occupancy. A single-block kernel pegging the GPU reads as 100% even if it uses 1/108 SMs."* (per research §1.1)

---

### Decision 4 — NVML `productName` → catalog-key mapping

**Question:** Per research §6b-1, neither Modal nor RunPod sets a GPU SKU env var; dexcost-GPU must read NVML's `productName` (e.g. `"NVIDIA H100 80GB HBM3"`) and map it to a catalog key (`"h100-80gb-sxm5"`). The mapping is fuzzy — same physical GPU has slightly different `productName` strings across driver versions, OEMs, and form factors. How is this mapping maintained?

**Options:**
- (a) **Alias array on each catalog entry.** `gpu_prices.json` carries `"aliases": ["NVIDIA H100 80GB HBM3", "NVIDIA H100 SXM5 80GB", "H100-SXM5-80GB"]` per SKU.
- (b) **Separate `nvml_aliases.json` file** mapping `productName` → catalog key.
- (c) **Regex match in code** (`if "H100" in name and "80GB" in name: → "h100-80gb-sxm5"`).

**Locked:** **(a) — aliases inline on each catalog entry.**

**Rationale:** Keeps the mapping close to the rate (one PR updates both rate AND aliases when a new variant ships). Separate file (option b) creates a drift surface where aliases get added without rate updates. Code regex (option c) requires a release to add a new variant — defeats the catalog's community-PR model.

**Implementation sharpening:**
- Aliases are matched case-insensitively after collapsing whitespace runs.
- A `productName` that matches multiple aliases (rare; would indicate catalog overlap) logs `gpu_alias_ambiguous:<productName>` once and uses the first match.
- No match found → `gpu_sku_unknown:<productName>` once + fall through to the GPU's `device_class` (`"hopper"`, `"ampere"`, `"ada-lovelace"`) which carries a coarse-default rate.
- A `device_class` default rate covers the long tail of new NVIDIA SKUs that ship between catalog refreshes — the customer gets `estimated` confidence and a rate within ~30% of true, rather than $0.

---

### Decision 5 — Vendor scope in v1

**Question:** NVIDIA / AMD / Intel GPUs all exist in 2026. Which does v1 cover, and how is the design forward-compatible to add others?

**Locked:** **v1 = NVIDIA only.** AMD ROCm and Intel oneAPI are v1.1 scope. Detection cascade and event schema are vendor-agnostic from day one.

**Rationale:**
- NVIDIA has 92%+ data-center GPU market share in 2026; the 80/20 ROI for v1 lands here.
- AMD `amdsmi` has a Python binding (`amdsmi-bindings`) but no maintained Go/Rust/TS equivalent — would create a Python-only feature gap.
- Intel oneAPI's GPU coverage is essentially non-existent across the four target SDKs.
- Schema forward-compatibility: `details.gpu_vendor` (`"nvidia"`, `"amd"`, `"intel"`) captured on every event in v1, even though all v1 values are `"nvidia"`. v1.1 adds the AMD/Intel measurement primitives additively.

**Implementation sharpening:** the runtime resolver (sibling of `compute_runtime`) returns a `GpuStack` enum: `Nvidia`, `Amd`, `Intel`, `None`. v1 emits events only when `Nvidia` resolves; the other two paths exist as `RuntimeKind` constants but their measurement primitives are stubs that return `None`. v1.1 fills them in.

---

### Decision 6 — Source-measurement boundary + idle GPU framing

**Question:** Phase 1 Decisions #9 + #10 locked "idle is invisible to dexcost" with mandatory customer-facing framing. Does Phase 2 inherit the same posture given the 380× larger magnitude?

**Locked:** **Same posture, sharpened framing.** Decision #3 (the `gpu_utilization_signal` event) does the heavy lifting; Decision #6 is the explicit inheritance + framing-amplification statement.

**Customer-facing framing (mandatory — must appear in README, Cost Intelligence dashboard, marketing site):**

> dexcost-GPU's compute total will run lower than your cloud bill on long-running GPU runtimes. On a `p5.48xlarge` ($55/hr) sitting idle 60% of the time, your `dexcost_gpu_total` will read 40% of your cloud GPU spend. **This is by design** — dexcost measures what your tasks actually consumed. The `gpu_utilization_signal` events (Decision #3) surface the idle gap explicitly so you can act on it: right-size to smaller GPUs, move to a serverless GPU cloud (Modal, RunPod, Replicate), or schedule batch workloads to fill idle capacity.

The reconciliation surface (future Cost Intelligence feature) is where the per-cloud "$X attributed vs. $Y invoiced" variance gets explained as a first-class line item.

---

### Decision 7 — VRAM units (the Decision #7 sibling that isn't)

**Question:** Phase 1 Decision #7 pinned the binary-vs-decimal divisor table to prevent the silent ~4.86% Fargate over-attribution. Does GPU have an equivalent?

**Locked:** **No. VRAM tier is encoded into the SKU key, not as a multiplier.**

**Rationale:** Per research §4, no provider surveyed in 2026 bills per-VRAM-GiB-second. `a100-40gb` and `a100-80gb-sxm5` are distinct catalog SKUs with distinct per-hour rates. There is no divisor question.

**Implementation sharpening (the corollary):**
- `vram_total_bytes` and `vram_used_bytes` ARE captured on every `gpu_utilization_signal` event (Decision #3) — but as **display-only fields**, not load-bearing for the dollar math.
- The pricing engine MUST NOT use `vram_used_bytes / vram_total_bytes` as a multiplier on the per-hour rate. A future implementer reading the spec might be tempted to "fractionally bill based on VRAM utilization" — pin in the spec: *"VRAM fields are display-only. dexcost-GPU does NOT compute fractional-VRAM billing. If a provider ever ships per-VRAM-GiB-second pricing, the catalog adds a new SKU entry, not a multiplier."*

---

### Decision 8 — NVML timestamp state across samples

**Question:** `nvmlDeviceGetProcessUtilization` returns samples since the `lastSeenTimeStamp` argument. A naive implementation that always passes `lastSeenTimeStamp=0` would either return ALL samples since device boot (huge over-attribution) or fail with `NVML_ERROR_NOT_FOUND` (silent zero-attribution depending on driver version). How does dexcost manage this state?

**Locked:** **The per-task GPU accountant persists `lastSeenTimeStamp` between snapshot calls.**

**Rationale:** NVML's sample buffer has finite size (typically 100 samples per device per process). Without persistent timestamp state, a long-running task that calls between snapshot intervals longer than the buffer can hold misses intermediate samples. The accountant maintains a `last_seen_timestamp_per_pid: HashMap<u32, u64>` (or language-equivalent) and passes the per-PID timestamp on each call.

**Implementation sharpening:**
- At `snapshot_start()`, the accountant calls `nvmlDeviceGetProcessUtilization` once with `lastSeenTimeStamp=0` to capture the initial baseline and stores the returned timestamps per PID.
- At `snapshot_end_and_build()`, the accountant calls again with the stored per-PID timestamps, accumulates the returned samples into `total_sm_us_per_pid`, sums across PIDs in the cgroup membership set (Decision #1), and reports `gpu_seconds_used = sum(total_sm_us) / 1_000_000`.
- A PID that exited between snapshots returns empty samples from NVML on the end-call; its accumulated start-call samples are still summed (they represent real GPU time that the PID consumed).
- Per convention §11: log-once `gpu_nvml_buffer_overflow` if a returned sample set indicates the buffer wrapped (rare on tasks <1 minute; possible on long-running batch jobs).

---

### Decision 9 — GCP N1-with-attached-accelerators detection

**Question:** Per research §6b-2, GCP N1 instances with attached GPUs (`n1-standard-4` + `nvidia-tesla-v100` accelerator) cannot be detected from inside the VM via metadata server endpoint — there's no documented endpoint listing `acceleratorType`. Modal-on-N1 and similar customers would mis-route to "no GPU detected." How does dexcost-GPU handle N1?

**Locked:** **NVML-only fallback for N1.** When `cloud_detect` resolves provider=`gcp` AND `instance_type` is an N1 family AND NVML reports devices present, classify as `gce_n1_gpu` with the SKU resolved purely from NVML `productName` (Decision #4 alias mapping).

**Rationale:** The metadata-server limitation is GCP's, not dexcost's to fix. NVML presence is a strong positive signal — if there's an NVIDIA device on a GCP N1 VM, it's an attached accelerator. The catalog carries per-region per-accelerator-SKU pricing for N1; resolution lands at `computed` confidence.

**Implementation sharpening:** the runtime resolver returns `RuntimeKind::GceN1Gpu` (distinct from `Gce` so the dispatch table can apply per-accelerator rates rather than per-instance-hour rates). The catalog entry shape mirrors EC2 with an extra `accelerator_types` map per region.

---

### Decision 10 — vGPU profile resolution (Azure NVadsA10 v5)

**Question:** Per research §6b-3, Azure NVadsA10 v5 sells fractional A10 profiles (1/6, 1/3, full A10). NVML may or may not distinguish these via `productName`. How does v1 handle vGPU?

**Locked:** **Best-effort detection in v1; document the limitation.** If NVML's `productName` distinguishes the fractional profile (verification spike during spec writing), the catalog carries per-profile SKUs. If not, v1 attributes at the full-A10 rate with `cost_confidence: estimated` + `pricing_source: "gpu_catalog:azure:nvads_a10_v5:full_a10_assumption"`.

**Rationale:** vGPU is a thin-edge case (Azure's NV series is the only major cloud surface). Verification can resolve during spec writing via a $1-2 Azure credit and a 5-minute `nvidia-smi -q` capture. If verification confirms NVML doesn't distinguish profiles, v1.1 adds an `AzureInstanceMetadata` API path that does. v1 ships with the conservative over-attribution.

**Implementation sharpening:** the spec-writing task includes a "verification spike: confirm whether `nvidia-smi -q` on an Azure NVadsA10 v5 6Q profile reports `productName` that differs from a full A10." Pin the answer in the spec.

---

### Decision 11 — Pricing-refresh cadence

**Question:** Per research §6b-10, GPU rates swing materially more than CPU (H100 on-demand fell ~40% across 2025 alone). The Phase 1 catalog-refresh pattern is human-triggered with a 180-day soft-warn. Does GPU need a tighter cadence?

**Locked:** **Weekly refresh cadence for GPU catalog; 90-day soft-warn freshness threshold.** Compute catalog stays on its current 180-day soft-warn.

**Rationale:** GPU rate volatility is real and material (40% / year > 5% / month). A 180-day-stale GPU catalog would carry rates 30-50% off true. The catalog-refresh workflow (deferred to a separate task per Decision #10 of Phase 1 — "catalog refresh automation") covers GPU at weekly cadence; CPU continues at monthly.

**Implementation sharpening:**
- `gpu_prices.json _meta.notes` field explicitly states the weekly refresh expectation so a community contributor opening a PR sees the discipline.
- The integrity test soft-warns at 90 days for GPU providers; fails the build at 365 days (vs. compute's 180-day soft-warn, 730-day fail).
- The refresh workflow itself (cron job, MCP, GitHub Action, or human) is out of scope for the v1 spec — it's the same artifact that Phase 1 Decision #10 deferred. Both subsystems will share the same refresh infrastructure when it lands.

---

## Strengthenings — implementation polish that landed during decision review

These aren't decision overrides; they're sharpenings that emerged during the lock-in conversation and need to be reflected in the spec.

1. **Decision #1 — cgroup walk is the primary path.** The fail-silent fallback to self-PID-only is explicit but secondary. Confidence labelling (`computed` vs `estimated`) is the customer-visible signal that the fallback kicked in.

2. **Decision #3 — `gpu_utilization_signal` is the convention-text-update.** This is the ONLY cross-subsystem convention change Phase 2 introduces — `conventions.md §1` gains the carve-out for observability-only signal events. Document this in the conventions file as part of spec writing, not as a separate change request.

3. **Decision #4 — `device_class` fallback prevents the cold-start zero-attribution case.** When a new NVIDIA SKU (e.g. B100 "Blackwell" launching in 2026-Q4) ships before the catalog updates, the customer still gets a rate via the device-class default (~30% accuracy band) instead of `$0`. The cost-confidence is `estimated`, the pricing_source carries `:device_class_fallback`.

4. **Decision #5 — `details.gpu_vendor` captured for ALL events** even when all v1 values are `"nvidia"`. Enables v1.1 AMD/Intel without a schema migration.

5. **Decision #6 — the customer-facing framing language must ship in product surfaces** (README, dashboard, marketing site) AT v1 launch, not "later." The Phase 1 lesson was that without the framing, the first customer files a bug and the framing has to be retrofitted under support pressure. Don't repeat.

6. **Decision #8 — NVML buffer-overflow detection.** A `gpu_nvml_buffer_overflow` log-once is the early signal that tasks are running longer than NVML's sample buffer can retain (per-device, per-process). This is documentation-only in v1 (the customer can't fix it from outside dexcost) but flags a future v1.1 enhancement: sample buffer flushing on a background thread for long-running batch jobs.

7. **Decision #11 — soft-warn vs hard-fail thresholds.** 90-day soft-warn / 365-day hard-fail for GPU mirrors compute's 180/730 but at half the duration. The integrity test must explicitly distinguish the two thresholds and emit different log levels (WARN vs ERROR) so CI doesn't fail on a 91-day-stale catalog.

---

## Cross-subsystem conventions delta

Phase 2 introduces ONE convention update vs. inherits:

| Convention | Status | Change |
|---|---|---|
| §1 (one event type per subsystem) | **EXTENDED** | Adds carve-out for observability-only "signal" event types with no `cost_usd` (Decision #3 reference example) |
| §2 (four-value confidence enum) | INHERITED | No change |
| §3 (`pricing_source` audit trail) | INHERITED | GPU uses `gpu_catalog:<provider>:<sku>:<region>` and `gpu_catalog:<provider>:<sku>:self_pid_only` and `gpu_catalog:<provider>:<sku>:device_class_fallback` |
| §4 (measurement on events, dollars on task) | INHERITED | `gpu_utilization_signal` event details are pure measurement; `task.gpu_cost_usd` carries the dollar |
| §5 (≤1 event per call ≠ 1 cost category per call) | INHERITED | GPU has no per-call shape; ≤1 `gpu_cost` event per task, ≤1 `gpu_utilization_signal` event per task per GPU |
| §6 (catalog distribution — Python canonical + sync script) | INHERITED | New `gpu_prices.json` follows the same pattern; new `scripts/sync_gpu_catalog.sh` |
| §7 (five-tier degradation ladder) | INHERITED | GPU's ladder: per-region SKU → per-SKU default → device-class default (Decision #4) → universal `_meta` default → hardcoded |
| §8 (measurement primitives per subsystem) | EXTENDED | Adds: "GPU = NVML per-PID with cgroup-membership walk; nvidia-smi shell-out for TypeScript SDK; fallback to self-PID-only on permission failure" |
| §9 (fail-silent discipline) | INHERITED | No change |
| §10 (source-measurement boundary) | INHERITED | No change |
| §11 (log-once per failure mode) | INHERITED | New GPU-specific tokens listed in Decision #1 + Decision #8 sharpenings |

---

## What happens next

§6b research follow-ups are ordered by load-bearing-ness, same disciplined ordering as Phase 1's "What happens next."

1. **Verification spikes** that resolve during spec writing (cheap, bounded):
   - vGPU profile distinction on Azure NVadsA10 v5 (Decision #10) — needs an Azure NVadsA10 v5 6Q credit (~$1-2/hr).
   - CoreWeave node-label namespace (research §6b-5) — needs a CoreWeave trial account or a customer who runs there.
   - NVML container-mode behaviour matrix — needs Docker + nvidia-container-toolkit + a non-root container test.

2. **Pricing re-verification** (same workflow as Phase 1's `fb5d0a0` refresh — live verification against provider sources):
   - Modal: published per-second rates at modal.com/pricing (static; web-fetchable)
   - RunPod: per-second on-demand and spot at runpod.io/pricing (static)
   - Lambda Labs Cloud: per-hour rates at lambdalabs.com/service/gpu-cloud (static)
   - CoreWeave: per-hour rates (HTML; verify static-vs-JS-rendered)
   - Replicate: per-second public-model rates at replicate.com/pricing
   - AWS GPU EC2: same Price List Bulk API path as Phase 1
   - GCP A100/H100/L4/T4 accelerators: same SKUs-public-via-HTML path as Phase 1's GCE verification
   - Azure NCv3/ND/NDA100_v4/NDH100_v5: same Azure Retail Prices REST API as Phase 1

3. **Spec writing:** `docs/superpowers/specs/2026-05-22-gpu-capture-design.md` (capture: NVML cascade, cgroup walk, event shapes, `gpu_utilization_signal`) + `docs/superpowers/specs/2026-05-22-gpu-cost-attribution-design.md` (per-billing-model math, catalog distribution, pricing engine, refresh cadence).

4. **Plan + implementation:** Python first (mirrors Phase 1 rollout), then cross-SDK ports. The Python plan includes the specific task: **"populate `gpu_prices.json` with rates verified against live provider sources"** — same pattern as Phase 1's compute catalog refresh.

5. **Convention update:** `conventions.md §1` gains the observability-only signal carve-out (Decision #3 reference). This is a one-paragraph addition committed alongside the spec.

---

## Verdict

**All 11 approved. Seven strengthenings folded in. Three customer-facing artifacts to produce (Decision #1 cgroup-walk fallback framing + Decision #3 `gpu_utilization_signal` documentation + Decision #6 idle-gap framing). One convention text update to land (§1 observability-signal carve-out). Ready to move to spec.**

The most important decision is #1 — multi-PID attribution via cgroup walk. Getting this right is what makes dexcost-GPU's numbers match customer intuition for the most common 2026 distributed-training workloads (PyTorch DDP, Ray, vLLM workers). Getting it wrong silently under-attributes by 4-8× on first install and triggers the trust-erosion failure mode.

The discipline pattern (research → critique → decisions → spec → plan → implementation) that produced Phase 1's quality holds for Phase 2.
