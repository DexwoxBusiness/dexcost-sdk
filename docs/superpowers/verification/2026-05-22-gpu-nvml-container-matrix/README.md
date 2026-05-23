# GPU NVML × Container-Mode Verification Matrix

**Date:** 2026-05-22
**Purpose:** Empirically verify the cgroup-scope classification table from [Phase 2 Decision #1](../../decisions/2026-05-22-gpu-foundation-decisions.md#decision-1--multi-pid-gpu-attribution-the-load-bearing-one) before spec writing concludes. The decision is architecturally locked; this matrix confirms the implementation details (cgroup path prefixes, NVML permission behavior per environment) match the table.

**Gates:** Decision #1 of the GPU decisions log explicitly flags itself as a "verification gate — the spec MUST include the cgroup-scope classification table, the bare-metal-no-container handling, and the multi-container K8s pod limitation." This directory is where that gate gets cleared.

**Why this matters:** the naive "walk `/proc/self/cgroup`" implementation produces silent over-attribution (bare-metal-no-container hits the systemd user slice) or silent under-attribution (multi-container K8s pods miss sidecar PIDs) depending on environment. The classification table only works if real cgroup paths match the prefixes the table expects. This matrix verifies that.

---

## What to capture per environment

Run [`capture.sh`](capture.sh) inside each target environment. It captures:

1. **`/proc/self/cgroup`** — the cgroup path the calling process lives in (cgroup v2 uses `0::/path`; cgroup v1 has multiple controllers)
2. **`/proc/self/mountinfo | grep cgroup`** — confirms cgroup v1 vs v2 vs unified hybrid
3. **`nvidia-smi -q`** — full NVML query: device list, driver version, MIG mode, `productName` per device
4. **`nvidia-smi pmon -c 1`** — per-process GPU utilization snapshot (the data NVML reports to dexcost)
5. **`cgroup.procs`** at the resolved cgroup path — the PID list the cgroup-walk would enumerate
6. **NVML init success/failure** — does `nvmlInit()` succeed in this container's NVIDIA driver config?
7. **`nvmlDeviceGetComputeRunningProcesses`** — does it return all PIDs or only same-cgroup PIDs?
8. **Permission failure mode** — if NVML denies access to other-cgroup PIDs, what error code?

Drop the captured outputs into the corresponding subdirectory's files. Each environment dir contains:
- `EXPECTED.md` — documentation-based hypothesis: what we PREDICT the cgroup path / NVML behavior will look like, with the source URL backing the prediction
- `proc-self-cgroup.txt` — actual contents of `/proc/self/cgroup` (TO FILL IN)
- `mountinfo-cgroup.txt` — actual `mountinfo | grep cgroup` (TO FILL IN)
- `nvidia-smi-query.txt` — actual `nvidia-smi -q` output (TO FILL IN)
- `nvidia-smi-pmon.txt` — actual `nvidia-smi pmon -c 1` output (TO FILL IN)
- `cgroup-procs.txt` — actual PIDs in the cgroup at the resolved path (TO FILL IN)
- `nvml-init.log` — output from a small NVML-init test program (TO FILL IN)
- `OBSERVED.md` — once captures are in, written summary: did EXPECTED match? If not, what's the actual cgroup path / NVML behavior and how does Decision #1's classification table need to adjust?

---

## Coverage matrix

| Environment | Captures the case | Priority | Where to run |
|---|---|---|---|
| [`docker-gpus-all/`](docker-gpus-all/) | Standard Docker GPU container (`docker run --gpus all`) | P0 | Any GPU-equipped Linux host with `nvidia-container-toolkit` |
| [`docker-no-gpus/`](docker-no-gpus/) | Docker container WITHOUT `--gpus` flag (NVML init should fail) | P1 | Same host as above |
| [`k8s-nvidia-device-plugin/`](k8s-nvidia-device-plugin/) | Kubernetes pod with NVIDIA Device Plugin requesting `nvidia.com/gpu: 1` | P0 | Any K8s cluster with GPU nodes (GKE / EKS / on-prem) |
| [`non-root-container/`](non-root-container/) | Standard Docker GPU container running as `USER 1000:1000` (NVML permission case) | P0 | Same Docker host |
| [`modal-function/`](modal-function/) | Modal `@app.function(gpu="A10G")` decorator — captures Modal's container runtime | P0 | Modal account ($1-2 free credit covers this) |
| [`runpod-function/`](runpod-function/) | RunPod pod with one GPU attached — captures RunPod's container runtime | P1 | RunPod account ($10 minimum top-up covers ~10 minutes of T4) |
| [`coreweave-k8s/`](coreweave-k8s/) | CoreWeave K8s pod with GPU — verifies CoreWeave's node-label namespace ([research §6b-5](../../research/2026-05-22-gpu-foundation-research.md#6b-coreweave-node-label-namespace)) | P1 | CoreWeave trial account |
| [`azure-nvadsa10-v5-vgpu/`](azure-nvadsa10-v5-vgpu/) | Azure `Standard_NV6ads_A10_v5` — verifies vGPU profile distinction in NVML ([Decision #10](../../decisions/2026-05-22-gpu-foundation-decisions.md#decision-10--vgpu-profile-resolution-azure-nvadsa10-v5)) | P0 | Azure subscription, ~$1-2 for 30 minutes |
| [`bare-metal-host/`](bare-metal-host/) | Python running directly on a GPU host (no container) — verifies the systemd user slice case that should degrade to self-PID-only | P0 | Any GPU host where you can `ssh` and run Python without a container |

P0 = blocks spec writing; P1 = informs spec but spec can ship with documented limitations.

---

## Verification gate exit criteria

The spec can declare Decision #1 implemented when ALL of:

1. Every P0 environment has `OBSERVED.md` populated with actual capture results
2. Each P0 `OBSERVED.md` either confirms its `EXPECTED.md` hypothesis OR documents the deviation and updates the cgroup-scope classification table in the spec
3. The cgroup-scope classification table in `gpu-capture-design.md` lists every cgroup-path prefix observed in the matrix, plus the `:no_container_scope` and `:multi_container_pod_partial` cases from the decisions log
4. The NVML permission behavior in `non-root-container/` is documented: does NVML return `NVML_ERROR_NO_PERMISSION` for other-PID queries, or does it silently return empty? This determines whether the spec uses `try-with-fail-silent` or `if-can-query-then-query` flow control.

---

## How to use this directory

**If you're running the spikes:** start with `bare-metal-host/` (no cloud spend, just SSH to any GPU host you have access to). Then `docker-gpus-all/` and `non-root-container/` (same host, different `docker run` flags). Then the P0 cloud spikes (`modal-function/`, `azure-nvadsa10-v5-vgpu/`, `k8s-nvidia-device-plugin/`). Drop the capture outputs into each dir's files; write the `OBSERVED.md` once you have the data.

**If you're writing the spec:** read each environment's `EXPECTED.md` for the documented hypothesis. Where `OBSERVED.md` exists, treat it as the binding fact. Where it doesn't yet exist, cite the hypothesis with `(unverified — pending spike capture)` and note the spec section needs a revision pass when the capture lands.

**If you're a future maintainer:** if a customer reports "dexcost-GPU misattributes on environment X," compare their environment against this matrix. If X isn't covered, add a new directory and run the capture script there.

---

## Artifact discipline

This is the same pattern as `docs/superpowers/research/` and `docs/superpowers/decisions/`. The verification matrix is an artifact: it's checked in, version-controlled, references binding decisions, and outlives any individual implementer. Subsystem D (storage) and the AMD/Intel v1.1 GPU extension will inherit this directory shape for their own verification phases.

---

## Pricing refresh (parallel workstream)

A separate background agent is verifying GPU rates from live cloud sources (Modal / RunPod / Lambda Labs / CoreWeave / Replicate / AWS Price List / Azure Retail Prices / GCP). Output: `python/src/dexcost/data/gpu_prices.json` + the catalog sync script. That's mechanical; this directory is for the architectural verifications.
