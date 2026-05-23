# Phase 2 — GPU Foundation — Research Report

**Date:** 2026-05-22
**Status:** Research input; binding decisions land in a separate decisions log.
**Scope:** Per-task GPU cost capture + attribution across the runtimes in the master capability table where GPUs are billed (EC2 g/p, GCE A/G, Azure NC/ND/NV, Modal, RunPod, CoreWeave, Lambda Labs Cloud, Replicate, K8s GPU node pools, Vast.ai / TensorDock / Hyperstack as v1.1 candidates).

This document gathers verified facts from official provider docs, NVIDIA developer docs, and the maintained Linux GPU bindings. Every measurement primitive, env-var name, file path, and pricing rate cited here has a source URL with the publication or "last updated" year noted. Where verification was thin or only an older source was available, that gap is called out explicitly — no quiet inference.

> **⚠️ Pricing-freshness caveat.** All dollar rates cited inline in §2 and §3 are point-in-time snapshots from provider pricing pages **as of 2026-05-20 ↔ 2026-05-22**. GPU rates swing meaningfully more than CPU rates — H100 on-demand fell ~40% across 2025 alone (Spheron, IntuitionLabs Feb-2026 surveys). The catalog refresh tooling described in §3 is what keeps `gpu_prices.json` current; **this document's rates are illustrative, not the source of truth.** Before any code lands, re-verify every rate against the live provider docs to catch quiet updates between research and spec — same discipline Phase 1 used for `compute_prices.json`.

> **Convention inheritance.** This subsystem (C in `conventions.md`) inherits the cross-subsystem conventions locked in Phase 1: one event type per subsystem with a discriminator (§1), four-value confidence enum (§2), `pricing_source` audit trail (§3), measurement-on-events / derived-dollars-on-task (§4), the ≤1-event-per-call vs ≥1-cost-category-per-call distinction (§5), source-measurement boundary (§9 — referenced inline in §4 below), fail-silent + log-once (§§9, 11). Decisions §6b in this doc surface where the GPU subsystem may need new convention text; none of the existing convention text is challenged.

---

## 1. Measurement primitives by stack

### 1.1 NVIDIA NVML — the canonical surface for per-PID accounting

**Source:** [NVIDIA NVML API Reference Guide — Device Queries](https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html) (NVIDIA, current; nvml.h header copyright reads "1993–2026, NVIDIA Corporation").

NVML (NVIDIA Management Library, ships in `libnvidia-ml.so` alongside every driver since the 410 series) is the C ABI behind `nvidia-smi`. The two functions dexcost needs:

| Function | Returns | Sample model | Permission |
|---|---|---|---|
| `nvmlDeviceGetComputeRunningProcesses` (v3 as of CUDA 12) | Array of `{pid, used_gpu_memory}` for processes with an active CUDA context on the device | Snapshot; no time integration | `NVML_ERROR_NO_PERMISSION` if the calling process lacks visibility into the target PID (typical: non-root container can't see processes outside its PID namespace) — verified from NVML reference doc |
| `nvmlDeviceGetProcessUtilization` | Array of `nvmlProcessUtilizationSample_t {pid, timeStamp_us, smUtil%, memUtil%, encUtil%, decUtil%}` | **Time-windowed samples since `lastSeenTimeStamp` argument**, not instantaneous (verified from API reference). Caller passes the last timestamp seen; NVML returns every sample collected after that timestamp. Sample period is driver-controlled, typically 1 s. | Same PID-namespace permission requirement |

**`smUtil` semantic** — "percent of time over the past sample period during which one or more kernels was executing on the GPU" attributed to that process. **Not** fractional SM occupancy. A process that ran a single-block kernel for the full sample window will report `smUtil=100` even though it used 1/108 of the SMs. ([NVIDIA Developer Forums — "Questions on per-process GPU utilization"](https://forums.developer.nvidia.com/t/questions-on-per-process-gpu-utilization/265460), confirms the kernel-time semantics; also discussed in [Lei Mao's NVML notes](https://leimao.github.io/blog/NVIDIA-NVML-GPU-Statistics/), 2024 but technical content unchanged.)

**Implication for dexcost attribution math:** `smUtil%` × wall-clock duration ≈ "GPU-time used" for billing only if the bill is per-active-second and `smUtil` is collected continuously. For per-GPU-hour billing (EC2/Azure VM/GCE/Lambda Labs/CoreWeave), `smUtil` is a *signal* not a *measure*; the dollar comes from `(hours_attributable) × hourly_rate`, where hours are wall-clock the task held the GPU.

**Container behaviour — load-bearing for dexcost dispatch:**

- NVML requires the NVIDIA driver to be loaded in the host kernel (it does NOT run inside a container without GPU device files). [NVIDIA Container Toolkit — Troubleshooting](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/troubleshooting.html) (NVIDIA docs, last updated 2025) confirms: NVML calls fail with `Failed to initialize NVML: Unknown Error` or `Insufficient Permissions` if `/dev/nvidia*` device nodes are not mapped in.
- Mapping is done via `docker --gpus all` (or Kubernetes `nvidia.com/gpu` resource → NVIDIA device-plugin), which the NVIDIA Container Toolkit translates into device-node bind-mounts + cgroup device allowlist.
- **dexcost MUST fail-silent when NVML init fails.** Most customers don't have GPUs. NVML init failure is the *common* case, not an error condition. Convention §9 (fail-silent) applies.

**NVML version & ABI compat** — NVML uses versioned struct entry points (`_v2`, `_v3`); recent driver releases keep older entry points stable. Both `go-nvml` and `nvml-wrapper` advertise this backwards-compat property in their READMEs (see §7).

### 1.2 DCGM exporter — Prometheus scrape, not a programmatic library

**Source:** [NVIDIA/dcgm-exporter GitHub README](https://github.com/NVIDIA/dcgm-exporter) (NVIDIA, actively maintained 2025-2026); [DCGM-Exporter docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/dcgm-exporter.html) (NVIDIA, 2025).

| Property | Value |
|---|---|
| Default port | **9400** |
| Metrics path | **`/metrics`** |
| Output format | Prometheus exposition (Counter / Gauge text format) |
| Backing library | libDCGM (a higher-level layer over NVML, with Hopper/Blackwell-specific counters NVML doesn't expose directly) |

Key metrics relevant to per-task attribution:

| Metric | Meaning |
|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | Compute (SM) utilization, percent |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | Memory-copy engine utilization, percent |
| `DCGM_FI_DEV_FB_USED` / `DCGM_FI_DEV_FB_FREE` | Frame-buffer (VRAM) used / free, in MiB |
| `DCGM_FI_DEV_POWER_USAGE` | Power draw, Watts |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | Tensor-core pipe active %, Hopper+ |
| `DCGM_FI_PROF_SM_OCCUPANCY` | Fractional SM occupancy (the "true" utilization signal NVML doesn't expose at process granularity) |

**Confirmed default port** from the [Setting up Prometheus with DCGM-Exporter docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/kube-prometheus.html) (NVIDIA, 2025). The complete metric list is configurable via a CSV file (`/etc/dcgm-exporter/default-counters.csv`); the full reference is in the [DCGM API Reference](https://docs.nvidia.com/datacenter/dcgm/latest/dcgm-api/group__dcgmFieldIdentifiers.html).

**Where this matters for dexcost:** DCGM exporter is a daemon, not a library. It's the standard observability surface in K8s GPU clusters, but it's **outside the SDK's process** — scraping it is a sidecar pattern, not a direct measurement. v1 of dexcost-GPU should NOT take a dependency on DCGM running; treat its presence as a v1.1 "richer signal if available" enhancement.

### 1.3 `/proc/driver/nvidia/gpus/*/information` — sysfs fallback

**Source:** [NVIDIA — Using the /proc File System Interface](https://download.nvidia.com/XFree86/Linux-x86_64/435.17/README/procinterface.html) (driver-version-specific page, last revised by NVIDIA at 435.x driver release but the schema is stable through current drivers — verified per [libnvidia-container issue #105](https://github.com/NVIDIA/libnvidia-container/issues/105)). **⚠️ Older than 2025 source.**

Each detected GPU exposes `/proc/driver/nvidia/gpus/<bus>:<device>.<function>/information` with the model name, IRQ, BIOS version, PCI device IDs. **This file works without loading NVML** — the SDK can detect "NVIDIA GPU present, here is its model name" by reading sysfs even when NVML init would fail (e.g. container with device file mounted but no NVML library).

Use case for dexcost: **detection-only**. The file does NOT expose per-PID or utilization data. Useful as a "GPU exists; provider+SKU can be guessed from the model string; fall back to catalog default rate at `estimated` confidence" path.

### 1.4 AMD ROCm

**Source:** [AMD SMI CLI tool usage — ROCm docs](https://rocm.docs.amd.com/projects/amdsmi/en/latest/how-to/amdsmi-cli-tool.html) (AMD, 2025); [ROCm/amdsmi GitHub](https://github.com/ROCm/amdsmi); [amdgpu_top GitHub](https://github.com/Umio-Yasuno/amdgpu_top) (community, active 2025-2026).

| Tool | Per-PID? | Stable library binding? |
|---|---|---|
| `amd-smi` | **Yes** — `amd-smi process` shows per-PID PID, memory, GPU util | Python (`amdsmi` PyPI package, AMD-maintained) and C library shipped with ROCm |
| `rocm-smi` (older) | SDMA per-PID yes; compute per-PID limited | Being superseded by `amd-smi` per AMD's own docs |
| `amdgpu_top` | Yes (via fdinfo + GRBM/GRBM2 perf counters) | Rust binary; no library binding |

**Status for dexcost:** AMD support is meaningfully thinner than NVIDIA. `amd-smi` Python binding works; Go / Rust / Node bindings effectively don't exist as maintained packages. Realistic v1 path: shell out to `amd-smi process --json` for AMD detection, accept the cost on a less-common code path.

### 1.5 Intel oneAPI / Level Zero Sysman

**Source:** [Intel® XPU Manager](https://www.intel.com/content/www/us/en/software/xpu-manager.html) (Intel, 2025); [pti-gpu Level Zero Sysman chapter](https://github.com/intel/pti-gpu/blob/master/chapters/system_management/LevelZero.md) (Intel, 2024 last commit — **⚠️ older source**).

- Intel Data Center GPUs (Flex, Max) — `xpumanager` daemon + Level Zero Sysman API are the stable path. Sysman exposes per-process utilization on Max-series via the L0 ABI.
- Intel Arc consumer GPUs — `intel_gpu_top` does NOT expose memory usage; `xpu-smi` has partial Arc coverage per Intel community thread ([level1techs forum, 2024](https://forum.level1techs.com/t/is-there-a-monitoring-tool-that-shows-which-processes-are-using-intel-arc-gpu/248757) — **⚠️ older**).

**Status for dexcost:** Intel GPUs in cloud (Intel Gaudi 2/3 are HabanaLabs not GPU-class; Flex/Max appear on Intel Tiber AI Cloud and on-prem) are out-of-scope for v1. Detection path can light up later via the L0 Sysman C library (no Python/Go/Rust ecosystem to lean on yet).

### 1.6 Inference-server `/metrics` scrape (vLLM, Triton, TGI)

These are **application-level** signals, not per-PID OS signals. Useful as a complementary measurement for inference workloads, not as the primary attribution surface.

| Server | Default port | Endpoint | GPU-related metrics |
|---|---|---|---|
| **vLLM** | 8000 (same as API) | `/metrics` | `vllm:gpu_cache_usage_perc` (KV-cache fraction; not GPU SM util), `vllm:num_requests_running`, `vllm:num_requests_waiting`. **GPU SM/memory util NOT exposed natively** — vLLM expects DCGM exporter for that. ([vLLM Metrics docs](https://docs.vllm.ai/en/stable/design/metrics/), 2025; [NVIDIA Dynamo vLLM Prometheus integration page](https://docs.nvidia.com/dynamo/latest/backends/vllm/prometheus.html), 2026.) |
| **Triton** | **8002** (configurable via `--metrics-port`) | `/metrics` | GPU memory + utilization metrics included by default, toggle with `--allow-gpu-metrics`. ([Triton Inference Server Metrics doc](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/metrics.html), NVIDIA 2026.) |
| **TGI** (HuggingFace Text Generation Inference) | 80 / 8080 (configurable) | `/metrics` | Request-level metrics; GPU stats not exposed natively, scraped alongside DCGM exporter per [glukhov.org "Monitor LLM Inference in Production (2026)"](https://www.glukhov.org/observability/monitoring-llm-inference-prometheus-grafana/). |

**Implication for dexcost:** Phase 2 should NOT scrape inference servers in v1. The per-PID NVML path is upstream of every inference server and works whether the customer runs vLLM, Triton, raw PyTorch, or `llama.cpp`. Inference-server metrics are a v1.1 enrichment.

---

## 2. Runtime / cloud detection signals

### 2.1 AWS EC2 GPU instances

**Detection:** IMDS `/latest/meta-data/instance-type` (already wired in Phase 1 Decision #3 background probe). Instance type → GPU SKU via static catalog mapping. No special GPU IMDS endpoint.

| Family | GPU SKU | Common sizes | Source |
|---|---|---|---|
| g4dn | NVIDIA T4 (16 GB) | xlarge → 12xlarge / metal | [AWS EC2 GPU instance docs](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-types.html) |
| g5 | NVIDIA A10G (24 GB) | xlarge → 48xlarge | (same) |
| g6 | NVIDIA L4 (24 GB) | xlarge → 48xlarge — g6.xlarge from $0.8048/hr ([Wring AWS GPU pricing 2026](https://wring.co/blog/aws-gpu-instance-pricing-guide)) |
| g6e | NVIDIA L40S (48 GB) | xlarge → 48xlarge |
| p3 | NVIDIA V100 (16/32 GB) | 2xlarge → 16xlarge |
| p4d / p4de | NVIDIA A100 (40 / 80 GB), 8-GPU only | 24xlarge |
| p5 / p5e | NVIDIA H100 (80 GB) / H200, 8-GPU | p5.48xlarge ≈ $55.04/hr on-demand ([Vantage p5.48xlarge](https://instances.vantage.sh/aws/ec2/p5.48xlarge), 2026; [AWS P5 product page](https://aws.amazon.com/ec2/instance-types/p5/)) |

**Billing model:** **Per-instance-hour** (AWS bills per-second after 1-minute minimum on Linux on-demand). GPU cost is bundled into the instance rate — the SDK should NOT model GPU separately from CPU+RAM for EC2 GPU instances. The whole instance rate IS the GPU rate.

**Driver / IMDS interaction:** [AWS install NVIDIA gaming drivers docs](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nvidia-gaming-driver.html) confirms IMDSv2 requires NVIDIA driver ≥ 495 (gaming) / ≥ 14.0 (GRID). Driver version impacts whether the SDK's NVML probe succeeds, not whether IMDS responds.

### 2.2 GCP Compute Engine GPU instances

**Detection:** Metadata server at `http://metadata.google.internal/computeMetadata/v1/instance/machine-type` (header `Metadata-Flavor: Google`). Returns e.g. `projects/<num>/machineTypes/a3-highgpu-8g`. The machine-type string encodes the GPU class for A2/A3/A4/G2/G4.

Two attachment models per [GCP GPU machine types](https://docs.cloud.google.com/compute/docs/gpus) (Google, 2026):

| Family | GPU SKU | Attachment |
|---|---|---|
| A4X (Max) | GB300 Grace Blackwell Ultra Superchip | Pre-attached, machine-type implies SKU |
| A4X | GB200 Grace Blackwell | Pre-attached |
| A4 | B200 Blackwell | Pre-attached |
| A3 | H100 80 GB / H200 | Pre-attached |
| A2 | A100 40 GB / 80 GB | Pre-attached |
| G4 | RTX PRO 6000 Blackwell | Pre-attached |
| G2 | L4 | Pre-attached |
| N1 | T4 / V100 / P100 / P4 | Customer attaches manually via `acceleratorType` field |

**Metadata path for accelerators:** `/computeMetadata/v1/instance/scheduling/` covers maintenance hints but does NOT list accelerators directly per the public docs. For N1 + manually-attached GPUs, the SDK must rely on **NVML detection** to know what's actually attached — the metadata server only reveals machine-type. ⚠️ **Verification gap surfaced:** I could not find an authoritative metadata endpoint that lists attached `acceleratorTypes` from inside an N1 VM. Surface as Open Question §6b-2 below.

**Pricing references (2026, per-GPU-hour on-demand):**

| GPU SKU | GCP on-demand $/GPU-hr |
|---|---|
| T4 | ~$0.35 |
| L4 | ~$0.71 (G2) |
| A100 40GB | ~$3.67 (a2-highgpu-1g per [SynpixCloud Cloud GPU Pricing 2026](https://www.synpixcloud.com/blog/cloud-gpu-pricing-comparison-2026)) |
| A100 80GB | ~$3.93 (a2-ultragpu-1g per same source) |
| H100 80GB | ~$10.60 (a3-highgpu-1g per [Spheron GPU Cloud Pricing 2026](https://www.spheron.network/blog/gpu-cloud-pricing-comparison-2026/)) |

GCE GPU billing is **per-second after 1-minute minimum**, same as base GCE compute ([WebFetch of cloud.google.com/compute/gpus-pricing](https://cloud.google.com/compute/gpus-pricing), 2026 — confirmed billing model; specific dollar table partially truncated and falls under freshness caveat).

### 2.3 Azure VM GPU sizes

**Detection:** Azure IMDS `/metadata/instance/compute/vmSize?api-version=2021-02-01` (already used by Phase 1 cloud_detect). Header `Metadata: true` required.

| Series | GPU | Status |
|---|---|---|
| NC | K80 (deprecated) | EOL |
| NCv3 | V100 | Older, present |
| NCas T4 v3 | T4 | Present |
| NCa100 v4 | A100 40 GB | Present |
| ND A100 v4 | A100 80 GB, 8-GPU | Present |
| ND H100 v5 | H100 80 GB, 8-GPU, `Standard_ND96isr_H100_v5` | **Active flagship** — [Azure ND-H100-v5 size series doc](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndh100v5-series), updated 2026-04-02 |
| ND H200 v5 | H200, 8-GPU | Present (Azure docs site, 2026) |
| NDH200 v5 | H200 | Present |
| NV / NVv3 | M60 (vGPU) | Retiring 2026-09-30 per Azure docs ⚠️ |
| NVadsA10 v5 | A10 partial (1/6 → full) | **Active**, supports fractional GPU billing per the [NVadsA10v5 size series](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nvadsa10v5-series) |

Pricing references: Azure ND H100 v5 (8× H100) ≈ $98.32/hr in US East ([Vantage ND96isr H100 v5](https://instances.vantage.sh/azure/vm/nd96isrh100-v5), 2026; cross-checked [IntuitionLabs H100 Rental Prices 2026](https://intuitionlabs.ai/articles/h100-rental-prices-cloud-comparison)). Azure GPU VM billing is **per-second** with no minimum on Linux on-demand.

### 2.4 Modal

**Detection signal:** `MODAL_TASK_ID` env var is set inside every Modal container ([Modal — Environment variables guide](https://modal.com/docs/guide/environment_variables), 2026). Companion env vars confirmed in the same source:

- `MODAL_CLOUD_PROVIDER` — which underlying cloud the Modal container is on (Modal runs on multiple)
- `MODAL_IMAGE_ID` — the image hash
- `MODAL_REGION` — region identifier

**GPU SKU discovery from inside a Modal container — verification gap.** Modal's pricing page lists per-second rates by SKU, but the customer picks the SKU at function-definition time via the Python `gpu="H100"` decorator argument; Modal docs do NOT document an env var like `MODAL_GPU` exposing the chosen SKU at runtime. The SDK's options:
1. Use NVML (which DOES work inside Modal containers — Modal exposes the full driver) to read the GPU device name string, then map `"NVIDIA H100 80GB HBM3"` → `"h100-80gb"` in the catalog.
2. Require customer config: `dexcost.init(modal_gpu_sku="h100")`.

Path 1 is the zero-config option; needs the SKU-name → catalog-key mapping to be exhaustive (surface as Open Question §6b-1).

**Pricing — verified directly from [modal.com/pricing](https://modal.com/pricing) (2026-05):**

| GPU | Modal $/sec |
|---|---|
| B200 | 0.001736 |
| H200 | 0.001261 |
| H100 | 0.001097 |
| RTX PRO 6000 | 0.000842 |
| A100 80GB | 0.000694 |
| A100 40GB | 0.000583 |
| L40S | 0.000542 |
| A10 | 0.000306 |
| L4 | 0.000222 |
| T4 | 0.000164 |

**Billing model: per-GPU-second active** (only when the function holds a GPU; cold-start container time before GPU attach is billed differently per [Modal pricing](https://modal.com/pricing) — flagged as v1.1 detail).

### 2.5 RunPod

**Detection signal:** `RUNPOD_POD_ID` env var, verified from [RunPod environment variables docs](https://docs.runpod.io/pods/templates/environment-variables) (2026).

Confirmed RunPod-set env vars (from same doc, WebFetched):

- `RUNPOD_POD_ID`, `RUNPOD_DC_ID`, `RUNPOD_POD_HOSTNAME`
- `RUNPOD_GPU_COUNT` — number of GPUs (count only; **no SKU env var in the documented list**)
- `RUNPOD_CPU_COUNT`, `RUNPOD_VOLUME_ID`, `RUNPOD_API_KEY`
- `CUDA_VERSION`, `PYTORCH_VERSION`

**Important gap:** `RUNPOD_GPU_SIZE` (sometimes mentioned in older third-party blog posts and search-engine excerpts) is **NOT in the current official documented env-var list**. The doc lists `RUNPOD_GPU_COUNT` but not a SKU env var. Same conclusion as Modal: the SDK reads the SKU via NVML and maps the device-name string to the catalog.

**Billing:** per-second on-demand, GPU-SKU-priced; spot also per-second. RunPod publishes per-hour rates on the website (e.g. H100 PCIe ~$2.39/hr on-demand per [GetDeploying H100 Cloud Pricing 2026](https://getdeploying.com/gpus/nvidia-h100)).

### 2.6 CoreWeave

**Detection:** CoreWeave is **pure Kubernetes** — Managed CKS on bare metal ([CoreWeave docs portal](https://docs.coreweave.com/), 2026 — confirmed they offer "Managed Kubernetes on bare metal"). No documented CoreWeave-specific env vars. The SDK should detect via the K8s pattern (Phase 1 §1.8) and read the GPU node label `nvidia.com/gpu.product` (set by the NVIDIA GPU Operator), or fall back to NVML.

**Pricing — per-GPU-hour reserved/on-demand** (CoreWeave does not have per-second spot in the public price list):

| GPU | CoreWeave on-demand $/GPU-hr (May 2026) | Source |
|---|---|---|
| H100 HGX | ~$6.16 | [Thundercompute CoreWeave Pricing Guide May 2026](https://www.thundercompute.com/blog/coreweave-gpu-pricing-review) |
| H100 PCIe | ~$4.25 | (same) |
| A100 80GB | ~$2.70 | (same) |
| A100 PCIe | ~$2.21 | (same) |
| L40S | listed but no public per-hour | [computeprices.com CoreWeave](https://computeprices.com/providers/coreweave) |

CoreWeave Classic vs CoreWeave (post-IPO branding) have different price tables ([CoreWeave Cloud Pricing](https://www.coreweave.com/pricing), [CoreWeave Classic Pricing](https://www.coreweave.com/pricing/classic), both 2026).

### 2.7 Lambda Labs Cloud

**Detection signal:** Lambda Labs gives the customer a raw VM with no Lambda-specific env vars (verification: no env-var section in [Lambda pricing page](https://lambda.ai/pricing) and no Lambda docs page advertising container env vars). The SDK detects via NVML + a Lambda-specific signal — the cloud-detect chain in Phase 1 may already cover this via hostname pattern; otherwise it falls through to "GPU present, provider unknown → catalog default rate at `estimated` confidence" per the v2 egress Tier-3 ladder.

**Pricing — verified directly from [lambda.ai/pricing](https://lambda.ai/pricing) (2026-05 WebFetch):**

| GPU | Lambda $/GPU-hr on-demand |
|---|---|
| H100 SXM | $3.99 – $4.29 |
| A100 40GB | $1.99 |
| A100 80GB | $2.79 |
| B200 SXM6 | $6.69 – $6.99 |
| GH200 | $2.29 |
| H200 | not listed at WebFetch time — verify before catalog freeze |

Billing: **per-hour reserved or per-minute on-demand** (Lambda historically billed by the minute on on-demand; reserved is hourly committed). No spot.

### 2.8 Replicate

**Detection signal:** Replicate runs predictions inside Cog containers. Verified via [Replicate billing docs](https://replicate.com/docs/topics/billing) (2026): "you only pay when instances are actively processing requests" for public models; private models bill setup+idle+active. No Replicate-specific env-var detection list documented. Realistic detection: use the customer's API key context + NVML.

**Billing model: per-second active** (public models) or per-second always (private deployments). SKU is chosen at deploy time. Replicate was acquired by Cloudflare in 2026 per [WaveSpeedAI 2026 review](https://wavespeed.ai/blog/posts/replicate-review-2026/) — surfaces a v1.1 risk that detection signals may shift post-acquisition.

### 2.9 Vast.ai, TensorDock, Hyperstack — flag for v1.1

**Vast.ai** sets these env vars in instances ([Vast.ai Docker Execution Environment](https://docs.vast.ai/documentation/instances/templates/docker-environment), 2026):

- `VAST_CONTAINERLABEL` — unique instance ID
- `CONTAINER_API_KEY`, `JUPYTER_TOKEN`, `PUBLIC_IPADDR`
- `VAST_TCP_PORT_*` — port mappings

No documented `VAST_GPU_*` env var. SDK detects via NVML.

**TensorDock** and **Hyperstack** — research thin in public docs; both offer per-hour or per-minute marketplace pricing. ([Hyperstack pricing](https://www.hyperstack.cloud/blog/case-study/affordable-cloud-gpu-providers), 2026; [TensorDock vs Vast.ai](https://getdeploying.com/tensordock-vs-vast-ai), 2026). **Defer to v1.1** — catalog can include them with `estimated` confidence under a generic "GPU present, provider unknown" rule.

---

## 3. Billing models (the discriminator)

This shapes the dispatch table — direct analog of Phase 1's `billing_model` discriminator. The GPU subsystem inherits the convention from `conventions.md` §1: **one `gpu_cost` event type, `details.billing_model` discriminates**.

| Billing model | Providers | Rate shape | Confidence default |
|---|---|---|---|
| `per_instance_hour` (GPU bundled in VM rate) | AWS EC2 g*/p*, GCP A2/A3/G2/G4/N1, Azure NC/ND/NV | `instance_$/hr × hours × task_share` | `computed` (same math as Phase 1 EC2; share-of-instance over the task window) |
| `per_gpu_second_active` | Modal, RunPod, Replicate (public models) | `gpu_$/s × active_seconds × num_gpus` | `computed` (NVML or env-var tells us active time; rate is exact from catalog) |
| `per_gpu_hour_reserved` | Lambda Labs, CoreWeave, Vast.ai (some plans) | `gpu_$/hr × hours_held × num_gpus` | `computed` |
| `per_vgpu_hour` | Azure NVadsA10 v5 (fractional A10), NVIDIA vGPU on-prem | `vgpu_profile_$/hr × hours` | `computed` once the vGPU profile is detected via NVML device name string |
| `per_mig_slice_active` | Self-hosted A100/H100 with MIG enabled | varies — see §6 | `computed` to `estimated` depending on whether the underlying cluster has a published per-slice rate |

**Critical framing:** for `per_instance_hour` (EC2/GCE/Azure VM), the GPU is bundled — there is no separate GPU dollar amount the SDK can pull apart from the CPU+RAM dollar. The instance rate IS the GPU rate; the right model is to attribute the *whole instance hour* to the GPU-bearing task using the same share math as Phase 1 §1.3. Confidence is `computed`, source `gpu_catalog:aws:ec2:<instance_type>`.

For `per_gpu_second_active` (Modal/RunPod/Replicate), the dexcost dollar can match the provider invoice exactly if active time is measured cleanly. This is the **highest precision** regime for the GPU subsystem.

---

## 4. Memory / VRAM unit question (the GPU Decision #7)

**Question:** Does the GPU catalog need a per-SKU `vram_gb` field, or do prices already encode the tier (e.g. "A100-80GB" vs "A100-40GB" are distinct catalog SKUs)?

**Verified survey of 2026 GPU price lists:**

| Provider | VRAM-tiered SKUs in published pricing? |
|---|---|
| Modal | **Yes — `A100-40GB` and `A100-80GB` are separate price entries** with separate per-second rates ([modal.com/pricing](https://modal.com/pricing), 2026) |
| Lambda Labs | Yes — `A100 (40GB)` $1.99/hr and `A100 (80GB)` $2.79/hr listed separately ([lambda.ai/pricing](https://lambda.ai/pricing), 2026) |
| CoreWeave | Yes — separate H100 PCIe / SXM / HGX entries; A100 40 / 80 listed separately ([computeprices.com/providers/coreweave](https://computeprices.com/providers/coreweave), 2026) |
| AWS EC2 | N/A — GPU bundled into instance type, no VRAM line item |
| GCP | A100 40 / 80 are distinct machine types (a2-highgpu vs a2-ultragpu) per [GCP GPU machine types](https://docs.cloud.google.com/compute/docs/gpus), 2026 |
| Azure | NDv4 (A100 40) and NCa100 v4 are distinct sizes; H100 / H200 are distinct series |
| RunPod | Listed per-SKU on the marketplace; VRAM appears in the SKU label |

**Conclusion:** No provider in this survey bills **per-VRAM-GiB-second** (the way egress bills per-byte). VRAM tier is **baked into the SKU rate**, and the catalog only needs a SKU key like `h100-80gb` or `a100-40gb-pcie`. A `vram_gb` field on the catalog entry is useful for **display + customer reconciliation** (and for the future Cost-Intelligence surface), but it's not load-bearing for the dollar math.

**Therefore: no GPU Decision #7-sibling binary-vs-decimal divisor needed.** This is a meaningful simplification vs. Phase 1's memory-units table. The decisions log should still pin "VRAM-tier is part of the SKU key; no per-byte VRAM math" explicitly to prevent future drift.

**One edge case to verify in the spec:** NVIDIA vGPU profiles (Azure NVadsA10 v5 — 1/6th, 1/3rd, 1/2, full A10 per the Azure docs) DO have fractional-VRAM billing in the sense that a 1/6-A10 vGPU has 4 GiB framebuffer and a different per-hour rate from full A10. But the rate is **per vGPU profile**, not per-GiB — same conclusion: profile is part of the SKU key.

---

## 5. Idle GPU positioning

Phase 1 Decision #9 / #10 established: **idle compute time is invisible to dexcost; the gap surfaces in reconciliation, not in synthetic pseudo-tasks** (source-measurement boundary, locked in `conventions.md` §9).

**For GPUs, the same answer holds, but the gap is materially larger:**

- An EC2 c7g.xlarge sits idle at ~$0.145/hr. An EC2 p5.48xlarge sits idle at **~$55/hr** ([Vantage p5.48xlarge](https://instances.vantage.sh/aws/ec2/p5.48xlarge), 2026). The idle gap is 380× larger in absolute dollars.
- A typical training-cluster utilization study (referenced in [Luca Berton, "FinOps for AI: Control GPU Costs Without Killing Innovation" 2026](https://lucaberton.com/blog/finops-ai-gpu-workloads-cost-optimization-2026/)) puts industry-average GPU utilization at 30-50%. Half the GPU-hours on a long-running cluster will be "idle from dexcost's point of view."
- This makes the positioning conversation around Phase 1 Decision #9/#10 *more* load-bearing in the GPU subsystem: dexcost-GPU's total will run **substantially** below the cloud invoice for any customer on long-running GPU instances.

**Implications surfaced (not decided here — feed into decisions log):**

1. The reconciliation surface (future subsystem) MUST handle "GPU instance idle" as a first-class category, not as a residual.
2. The customer-facing positioning that worked for Phase 1 ("we only see tasks; the gap IS the right-sizing signal") needs sharpening for GPUs because the magnitude can be alarming on first view. Suggest a per-instance utilization signal as a side-channel (Phase 1 Decision #9 option (c)) without billing it — surfaces in the dashboard as "your H100 cluster is 35% utilized" without violating the source-measurement boundary.
3. **Stronger:** the SDK could optionally emit a `gpu_utilization_signal` event-type (Counter, no dollar) that reports `gpu_seconds_attributed / gpu_seconds_held` per task window. Stays in the four-value confidence enum (this event has no `cost_usd` field at all). Feed into decisions log.

**Critical: idle does NOT become a pseudo-task in v1. Source-measurement boundary holds.**

---

## 6. MIG / vGPU edge cases

### 6.1 NVIDIA MIG (Multi-Instance GPU)

**Source:** [NVIDIA MIG User Guide — Getting Started](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/getting-started-with-mig.html) (NVIDIA, last revised R580 driver release, 2025-2026); [NVML MIG Management API](https://docs.nvidia.com/deploy/nvml-api/group__nvmlMultiInstanceGPU.html) (NVIDIA, current).

**Verified behaviour:**

- A100 / H100 / H200 can be partitioned into **up to 7 MIG instances** per physical GPU.
- Each MIG instance has its own `nvmlDevice_t` handle in NVML — they appear as separate "devices" to the API.
- **`nvmlDeviceGetComputeRunningProcesses` works per-MIG-instance** (confirmed via WebFetch of the MIG User Guide and per [MIG NVML reference](https://docs.nvidia.com/deploy/nvml-api/group__nvmlMultiInstanceGPU.html)). Each MIG instance has a `GI ID` (GPU Instance) and `CI ID` (Compute Instance); `nvidia-smi` shows processes tagged with both. The verified output format from the MIG User Guide WebFetch: process listing has separate `GI ID` and `CI ID` columns.
- `CUDA_VISIBLE_DEVICES` uses **UUID format** under MIG, e.g. `MIG-GPU-8932f937-d72c-4106-c12f-20bd9faed9f6/1/2` (pre-R470 hierarchical) or `MIG-<UUID>` (R470+, simplified) per [NVIDIA Developer Forums MIG CUDA_VISIBLE_DEVICES thread](https://forums.developer.nvidia.com/t/how-to-use-cuda-visible-devices-for-mig-instances/195069) and [CUDA Programming Guide environment variables doc](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html), 2026.
- CUDA 12 / driver R570 limitation: a single CUDA process can enumerate across multiple GPU instances, but **only one Compute Instance per GPU Instance** — per the same CUDA programming guide source.

**Pricing for MIG slices:** No public cloud provider publishes per-MIG-slice pricing as of this research date. MIG is primarily a **self-managed** feature on customer-controlled A100/H100 instances. The customer is paying for the full GPU; MIG is how *they* partition it. dexcost should:

- **v1: bill the full GPU, attribute the full-GPU rate to whichever task held it during the window.** If the customer runs 7 MIG slices for 7 tasks simultaneously, each task gets 1/7 share via the share math (analog of Phase 1's pod-share-of-node).
- **v1.1 / future:** read MIG slice handles from NVML and attribute per-slice. Useful only if a public cloud ever bills per-MIG-slice (none do today).

### 6.2 NVIDIA vGPU (GRID)

**Source:** [Azure NVadsA10 v5 size series](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nvadsa10v5-series) (2026), [NVIDIA vGPU pricing overview](https://www.nvidia.com/en-us/data-center/buy-grid/).

- Azure NV / NVv3 / NV-series uses NVIDIA GRID vGPU technology. NVv3 (M60) is retiring 2026-09-30 per Azure docs (⚠️ deprecation in the v1 window).
- Azure NVadsA10 v5 is the active vGPU family: partial A10 GPUs from 1/6 (4 GiB framebuffer) → full A10 (24 GiB). Each profile is its own VM size with its own hourly rate.
- **Detection:** Azure IMDS vmSize string identifies the profile. NVML inside the VM reports the vGPU profile.

**Status for dexcost:** vGPU is just another SKU in the catalog (the profile name is part of the catalog key). No special billing-model treatment beyond what `per_instance_hour` already does.

---

## 7. Cross-language native bindings inventory

For each SDK target — Python / Go / Rust / TypeScript — verified the actual import path and maintenance state:

| Language | NVML binding | Status (May 2026) | Source |
|---|---|---|---|
| **Python** | `nvidia-ml-py` (PyPI) — **NVIDIA's official binding** | Actively maintained by NVIDIA Corporation. Copyright header reads "2011–2025, NVIDIA Corporation." Available at [pypi.org/project/nvidia-ml-py/](https://pypi.org/project/nvidia-ml-py/). | [PyPI `nvidia-ml-py`](https://pypi.org/project/nvidia-ml-py/) |
| Python (deprecated forks) | `pynvml` (gpuopenanalytics), `nvidia-ml-py3`, `py3nvml` | **All deprecated** — README of `nvidia-ml-py3` (nicolargo fork) and `pynvml` (gpuopenanalytics) explicitly redirect to `nvidia-ml-py`. | [pypi.org/project/pynvml/](https://pypi.org/project/pynvml/), [github.com/wookayin/nvidia-ml-py](https://github.com/wookayin/nvidia-ml-py) |
| **Rust** | `nvml-wrapper` (crate) | **Actively maintained**. Latest release v0.12.0 on 2026-03-04 (verified via WebFetch). Supports NVML version 12. Uses `libloading` so the wrapper can be a compile-time dependency even on systems without the NVIDIA driver — the load is runtime-gated. | [crates.io/crates/nvml-wrapper](https://crates.io/crates/nvml-wrapper), [github.com/rust-nvml/nvml-wrapper](https://github.com/rust-nvml/nvml-wrapper) |
| **Go** | `github.com/NVIDIA/go-nvml` — **NVIDIA-official** | Actively maintained. Supports NVML API version 13. Linux-only. Backwards-compatible with older `libnvidia-ml.so` versions. | [pkg.go.dev/github.com/NVIDIA/go-nvml/pkg/nvml](https://pkg.go.dev/github.com/NVIDIA/go-nvml/pkg/nvml), [github.com/NVIDIA/go-nvml](https://github.com/NVIDIA/go-nvml) |
| Go (deprecated) | `github.com/mindprince/gonvml`, `github.com/hotpxl/nvml`, `github.com/davidr/go-nvml` | Davidr's repo is explicitly marked "[Abandoned]"; mindprince/hotpxl forks lag. | [github.com/NVIDIA/go-nvml releases](https://github.com/NVIDIA/go-nvml/releases) |
| **TypeScript / Node** | **No actively maintained native binding** | Common pattern: shell out to `nvidia-smi --query-gpu=... --format=csv,noheader` and parse CSV. Packages like `node-nvidia-smi` (npm) wrap `nvidia-smi -q -x` (XML output, JSON-parseable). No npm package wrapping `libnvidia-ml.so` directly via N-API as of May 2026 research. | [npmjs.com/package/node-nvidia-smi](https://www.npmjs.com/package/node-nvidia-smi) |

**Implications for the TypeScript SDK:**

- dexcost-TS must **shell out to `nvidia-smi`** (or read sysfs `/proc/driver/nvidia/`) for NVIDIA GPU detection on Node.js — there is no maintained native binding.
- This has cost implications: `nvidia-smi` fork + CSV parse adds ~50 ms per probe. dexcost-TS should call once at init and cache, not per-task.
- Fail-silent discipline (convention §9): if `nvidia-smi` is not on PATH, NodeJS dexcost should return "no GPU" silently, not throw.

**AMD bindings:** Python `amdsmi` (PyPI, AMD-maintained) is the only first-party binding. Go/Rust/Node: no maintained AMD bindings. Same shell-out pattern applies for non-Python.

**Intel bindings:** Level Zero Sysman C API is callable via FFI from any language, but no high-level Python/Go/Rust/TS wrappers as of this research. Intel scope is v1.1+.

---

## 8. Prior-art survey

Brief survey of FinOps / GPU-aware libraries dexcost can position against:

- **OpenCost + Kubecost** ([opencost.io](https://opencost.io/), CNCF Incubation since 2024). Kubernetes-only; daemon-based (runs as a pod); attributes node cost down to pods / namespaces / labels using cluster-wide cost queries. GPU attribution exists but requires the customer to feed in per-GPU-node prices. **Differs from dexcost:** OpenCost is a cluster operator's tool (one deployment per cluster), not an SDK; it does not run inside the application process.

- **NVIDIA DCGM exporter** ([github.com/NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter)). Prometheus exporter daemon; rich GPU metrics; no cost layer at all. **Differs from dexcost:** measurement-only, no dollars, no SDK surface.

- **Kepler** ([github.com/sustainable-computing-io/kepler](https://github.com/sustainable-computing-io/kepler), CNCF). eBPF-based per-container energy attribution; uses NVML for the GPU energy term. Energy → $ requires a separate electricity-price input. **Differs from dexcost:** measures *energy*, not *cloud bill*; daemon, not SDK; sustainability framing, not unit-economics framing.

- **InferCost** ([github.com/defilantech/infercost](https://github.com/defilantech/infercost), 2026). Kubernetes-native cost-per-token from GPU amortization + electricity. Per-namespace attribution. **Differs from dexcost:** on-premises-AI-focused; daemon; computes amortized cost from purchase price, not from cloud invoice. Useful prior art for the per-token framing dexcost will need at the Cost Intelligence layer.

- **NVIDIA GPU Operator** (K8s). Manages NVIDIA driver/toolkit lifecycle in K8s clusters; sets `nvidia.com/gpu.product` and `nvidia.com/gpu.memory` node labels. Not a cost tool, but the **source of truth for GPU SKU detection** that dexcost on K8s should consult via the downward API path established in Phase 1 §1.8.

**Where dexcost differs across the field:** SDK-only (no daemon), four-language coverage (Python / Go / Rust / TS), event-shape consistency with the rest of unit-economics (LLM + external + network + compute), source-measurement boundary (we don't read the cloud invoice; we attribute what the customer's process actually held). The closest competitor in shape is **none** — every prior-art entry above is either a daemon or a single-cluster tool. dexcost-GPU lives where the customer's code lives, attributes the GPU dollars to the same task object that already carries LLM and compute dollars.

---

## 6b. Open questions to resolve before spec writing

Ordered by load-bearing-ness (mirrors compute research §6b). Items higher up should be resolved earlier in the spec phase.

1. **SKU-name → catalog-key mapping for NVML device-name strings.** Highest load-bearing question. When NVML reports `productName = "NVIDIA H100 80GB HBM3"`, the SDK must map that to a catalog key like `h100-80gb-sxm5`. The catalog needs an exhaustive (or fail-silent-default) mapping. Surface: should we ship the catalog with `nvml_product_name` as an alias field, or maintain a separate `nvml_aliases.json`? Both Modal and RunPod rely on this path (§2.4, §2.5).

2. **GCP N1-with-attached-accelerators detection from inside the VM.** Verification gap (§2.2): I could not confirm a metadata server endpoint that lists `acceleratorType` for a customer-attached N1 GPU. The SDK must rely on NVML for N1, but if NVML init fails, the fallback path is unclear. Resolve before spec: either confirm an `/computeMetadata/v1/instance/scheduling/` or `/instance/attributes/` path that lists accelerators, or accept that N1 detection requires NVML.

3. **vGPU profile detection accuracy.** Azure NVadsA10 v5 fractional profiles (1/6 A10 vs full A10) must be distinguishable from NVML — NVML *should* report the profile in `productName`, but I have no 2026 verified output from inside a NVadsA10 v5 VM. Worth a Azure-credit-based verification spike before the spec freezes the catalog schema.

4. **Replicate post-Cloudflare-acquisition detection signals.** Replicate was acquired by Cloudflare in 2026 ([WaveSpeedAI review](https://wavespeed.ai/blog/posts/replicate-review-2026/)); container env-var surface may shift. Mark Replicate as "v1, but expect a refresh by end of 2026."

5. **CoreWeave detection beyond "K8s with nvidia.com/gpu.product label".** No CoreWeave-specific env vars documented (§2.6). Confirm via running pod what the actual node labels look like — CoreWeave may set its own label namespace (`coreweave.cloud/*`) the SDK can use as a positive identification signal vs. generic K8s.

6. **NVML permission inside non-root containers.** §1.1 noted `NVML_ERROR_NO_PERMISSION` for `nvmlDeviceGetComputeRunningProcesses` outside the container's PID namespace. **The current PID is always visible to itself** — but cross-process attribution (multiple processes in the same container sharing a GPU) is the unverified case. Spec must specify: dexcost reads NVML samples scoped to the calling PID, not the device's full process list, to avoid the permission failure mode.

7. **Idle-GPU positioning signal — emit `gpu_utilization_signal` event?** Per §5, the idle gap on H100s is 380× larger than CPU. Decisions log should consider whether to add a signal event type that surfaces utilization without billing — keeps source-measurement boundary, gives the dashboard a story. **No new convention text needed if the event has no `cost_usd` field** (it's pure measurement; events without dollars don't trigger convention §2's confidence-enum rule).

8. **MIG slice attribution scope in v1.** Per §6.1, MIG slices appear as separate NVML handles. v1 recommendation is to bill the full GPU (since no cloud bills per-slice) and use the share math. Decisions log should explicitly pin v1 = full GPU attribution, v1.1 = per-slice if a cloud ever bills that way.

9. **DCGM exporter as enrichment signal.** Some customers already run DCGM exporter for observability. Should dexcost-GPU optionally scrape `localhost:9400/metrics` when available, to enrich the per-task GPU attribution? Bounded post-spec work — does not block v1.

10. **Pricing re-verification cadence.** GPU on-demand rates moved ~40% in 2025 across H100 alone. The catalog refresh tooling needs a cadence (weekly? monthly?) — Phase 1's "weekly CI cron" model probably needs to tighten for GPUs. Bounded.

---

## 7. Recommended scope for v1

Based on the research, my opinion on the v1 boundary (subject to decisions log override):

### In scope for v1

- **NVIDIA GPU detection** via NVML on Python / Go / Rust; via `nvidia-smi` shell-out on TS.
- **Provider coverage** for `per_instance_hour` (AWS EC2 g*/p*/g6/g6e, GCP A2/A3/A4/G2/G4, Azure NC/ND/NDH100v5/NDH200v5), `per_gpu_second_active` (Modal, RunPod, Replicate), `per_gpu_hour_reserved` (Lambda Labs, CoreWeave).
- **Per-task attribution math:** full-GPU rate × time-the-task-held-the-GPU, with the share math from Phase 1 §1.3 for multi-task instances.
- **Catalog schema:** SKU key includes VRAM tier (`a100-40gb`, `a100-80gb`, `h100-80gb-sxm5`); `vram_gb` and `gpu_count` as display fields, NOT used in the dollar math.
- **K8s GPU pods** via the same pattern as Phase 1 §1.8: read `nvidia.com/gpu.product` from node label (opt-in node-RBAC), fall through to NVML inside the pod.
- **Fail-silent NVML init** when no GPU is present (most dexcost customers don't have GPUs).
- **MIG awareness as detection-only:** if the SDK detects it's running inside a MIG slice, log once and attribute to the full parent GPU rate (no per-slice math in v1).

### Out of scope for v1 (defer to v1.1)

- **AMD ROCm and Intel GPU support** — bindings ecosystem thin outside Python; defer.
- **DCGM exporter scrape** for enrichment metrics — useful but not load-bearing.
- **Vast.ai / TensorDock / Hyperstack** detection — research thin; catalog default rate at `estimated` confidence is the fallback.
- **vGPU profile-level attribution** beyond what NVML's `productName` gives — Azure NVv3 retires 2026-09-30 anyway.
- **Per-MIG-slice billing** — no cloud bills per-slice today.
- **Inference-server `/metrics` scrape (vLLM/Triton/TGI)** — adds enrichment, not the dollar.
- **Replicate detection post-Cloudflare-merger** — refresh by end of 2026 once the dust settles.
- **`gpu_utilization_signal` event-type** — separate decision; v1 ships only `gpu_cost` with the four-runtime billing-model discriminator.

### Cross-subsystem invariants this v1 inherits

From `conventions.md`:

- One event type `gpu_cost` with `details.billing_model` discriminator (§1).
- Four-value confidence enum, no GPU-specific extension (§2).
- `pricing_source` strings prefixed `gpu_catalog:<provider>:<sku>` (§3).
- Measurement (active seconds, NVML smUtil samples, num_gpus) on events; derived dollar (`gpu_cost_usd`) on the task (§4).
- ≤ 1 event per task-window-per-GPU (record dedup) (§5).
- Source-measurement boundary: SDK measures, does not read cloud invoice. Idle GPU gap is honest (§9; reinforced in §5 above given the 380× magnitude).
- Fail-silent + log-once for NVML init, missing driver, missing `nvidia-smi`, NVML permission errors (§§9, 11).

---

## Sources

### NVIDIA NVML / DCGM / MIG (primary measurement docs)
- [NVML API Reference Guide — Device Queries](https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html) — `nvmlDeviceGetComputeRunningProcesses`, `nvmlDeviceGetProcessUtilization` signatures and permission semantics
- [NVML MIG Management API](https://docs.nvidia.com/deploy/nvml-api/group__nvmlMultiInstanceGPU.html) — MIG device handle semantics
- [NVIDIA MIG User Guide — Getting Started](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/getting-started-with-mig.html) — per-MIG-slice process attribution, GI/CI IDs, R580 release
- [NVIDIA MIG Device Names](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/mig-device-names.html) — `MIG-<UUID>` format under R470+
- [CUDA Programming Guide — Environment Variables](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html) — `CUDA_VISIBLE_DEVICES` with MIG, CUDA 12 / R570 single-CI-per-GI constraint
- [NVIDIA Developer Forum: CUDA_VISIBLE_DEVICES for MIG](https://forums.developer.nvidia.com/t/how-to-use-cuda-visible-devices-for-mig-instances/195069) — UUID enumeration ordering
- [NVIDIA Developer Forum: per-process GPU utilization](https://forums.developer.nvidia.com/t/questions-on-per-process-gpu-utilization/265460) — `smUtil` is kernel-time percent, not SM-fraction
- [Lei Mao — NVIDIA NVML GPU Statistics](https://leimao.github.io/blog/NVIDIA-NVML-GPU-Statistics/) — `nvmlProcessUtilizationSample_t` structure (⚠️ 2024 community source; technical content unchanged through 2026)
- [NVIDIA/dcgm-exporter GitHub](https://github.com/NVIDIA/dcgm-exporter) — port 9400, /metrics path, metrics list
- [DCGM-Exporter docs (NVIDIA Cloud Native)](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/dcgm-exporter.html) — full deployment model, 2025
- [Setting up Prometheus with DCGM-Exporter](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/kube-prometheus.html) — kube-prometheus integration, 2025
- [NVIDIA — Using the /proc File System Interface](https://download.nvidia.com/XFree86/Linux-x86_64/435.17/README/procinterface.html) — `/proc/driver/nvidia/gpus/*/information` file format (⚠️ schema verified stable; page itself is driver-435 era)
- [NVIDIA libnvidia-container issue #105](https://github.com/NVIDIA/libnvidia-container/issues/105) — sysfs availability inside containers
- [NVIDIA Container Toolkit Troubleshooting](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/troubleshooting.html) — NVML container permission errors, 2025

### AMD ROCm
- [AMD SMI CLI tool usage — ROCm docs](https://rocm.docs.amd.com/projects/amdsmi/en/latest/how-to/amdsmi-cli-tool.html) — `amd-smi process` per-PID, 2025
- [ROCm/amdsmi GitHub](https://github.com/ROCm/amdsmi) — official AMD SMI bindings (Python + C)
- [Umio-Yasuno/amdgpu_top](https://github.com/Umio-Yasuno/amdgpu_top) — community Rust binary for AMDGPU usage, active 2025-2026
- [Getting to Know Your GPU: A Deep Dive into AMD SMI — ROCm Blogs](https://rocm.blogs.amd.com/software-tools-optimization/amd-smi-overview/README.html) — amd-smi as rocm-smi successor

### Intel
- [Intel® XPU Manager](https://www.intel.com/content/www/us/en/software/xpu-manager.html) — XPUM uses Level Zero Sysman + Prometheus
- [pti-gpu Level Zero Sysman chapter](https://github.com/intel/pti-gpu/blob/master/chapters/system_management/LevelZero.md) — Sysman API surface (⚠️ 2024 last commit)
- [Level1Techs forum: Intel Arc GPU per-process monitoring](https://forum.level1techs.com/t/is-there-a-monitoring-tool-that-shows-which-processes-are-using-intel-arc-gpu/248757) — Arc memory-monitoring gap (⚠️ 2024)

### Inference-server metrics
- [vLLM Metrics docs](https://docs.vllm.ai/en/stable/design/metrics/) — port 8000, `vllm:` prefix, GPU util NOT exposed natively, 2025-2026
- [NVIDIA Dynamo: vLLM Prometheus Metrics](https://docs.nvidia.com/dynamo/latest/backends/vllm/prometheus.html) — DCGM exporter for actual GPU util, 2026
- [Triton Inference Server Metrics](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/metrics.html) — port 8002 default, --allow-gpu-metrics, 2026
- [glukhov.org — Monitor LLM Inference in Production (2026)](https://www.glukhov.org/observability/monitoring-llm-inference-prometheus-grafana/) — TGI + DCGM pattern

### AWS
- [AWS EC2 P5 product page](https://aws.amazon.com/ec2/instance-types/p5/) — H100 8-GPU, p5.48xlarge specs
- [AWS EC2 On-Demand Pricing](https://aws.amazon.com/ec2/pricing/on-demand/) — pricing model (1-min minimum, per-second after) — ⚠️ live table content was not extractable via WebFetch; cross-referenced via Vantage
- [Vantage p5.48xlarge instance page](https://instances.vantage.sh/aws/ec2/p5.48xlarge) — $55.04/hr us-east-1 May 2026
- [AWS install NVIDIA gaming drivers](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nvidia-gaming-driver.html) — IMDS + driver version requirements
- [AWS install NVIDIA GRID drivers](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nvidia-GRID-driver.html) — IMDSv2 ≥ driver 14.0 for GRID
- [Wring AWS GPU Instance Pricing Guide 2026](https://wring.co/blog/aws-gpu-instance-pricing-guide) — g6.xlarge $0.8048/hr
- [DoiT GPU Instance Pricing](https://compute.doit.com/gpu) — cross-reference for AWS GPU rates, 2026

### GCP
- [GCP GPU machine types](https://docs.cloud.google.com/compute/docs/gpus) — A4X/A4/A3/A2/G4/G2/N1 family GPU mapping, 2026
- [GCP Accelerator-optimized machine family](https://docs.cloud.google.com/compute/docs/accelerator-optimized-machines) — pre-attached vs N1 customer-attached
- [GCP GPU pricing](https://cloud.google.com/compute/gpus-pricing) — per-second billing, 1-minute minimum (live pricing table partially behind dynamic content; values cross-referenced from Spheron / Synpix)
- [GCP About VM Metadata](https://docs.cloud.google.com/compute/docs/metadata/overview) — metadata server endpoint, header requirement
- [GCP View and query VM metadata](https://docs.cloud.google.com/compute/docs/metadata/querying-metadata) — `/computeMetadata/v1/instance/machine-type` semantics
- [Spheron GPU Cloud Pricing 2026](https://www.spheron.network/blog/gpu-cloud-pricing-comparison-2026/) — H100 rates across providers
- [SynpixCloud Cloud GPU Pricing 2026](https://www.synpixcloud.com/blog/cloud-gpu-pricing-comparison-2026) — GCP A100 40GB/80GB per-GPU rates

### Azure
- [Azure ND-H100-v5 size series](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndh100v5-series) — Standard_ND96isr_H100_v5, 8× H100 80GB, updated 2026-04-02
- [Azure NVadsA10 v5 size series](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nvadsa10v5-series) — vGPU 1/6 → full A10 profiles
- [Azure NV size series](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nv-series) — M60 vGPU + 2026-09-30 retirement
- [Azure NVv3 size series](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nvv3-series) — retirement
- [Vantage ND96isr H100 v5](https://instances.vantage.sh/azure/vm/nd96isrh100-v5) — $98.32/hr US East
- [IntuitionLabs H100 Rental Prices 2026](https://intuitionlabs.ai/articles/h100-rental-prices-cloud-comparison) — Azure/AWS/GCP H100 cross-reference

### Modal / RunPod / CoreWeave / Lambda Labs / Replicate
- [modal.com/pricing](https://modal.com/pricing) — per-second GPU rates, May 2026 (B200, H200, H100, A100 40/80, L40S, A10, L4, T4, RTX PRO 6000)
- [Modal — Environment variables guide](https://modal.com/docs/guide/environment_variables) — `MODAL_TASK_ID`, `MODAL_CLOUD_PROVIDER`, `MODAL_IMAGE_ID`, `MODAL_REGION`
- [Modal — GPU acceleration guide](https://modal.com/docs/guide/gpu)
- [RunPod environment variables docs](https://docs.runpod.io/pods/templates/environment-variables) — `RUNPOD_POD_ID`, `RUNPOD_GPU_COUNT`, etc.
- [CoreWeave docs portal](https://docs.coreweave.com/) — Managed Kubernetes on bare metal positioning
- [CoreWeave Cloud Pricing](https://www.coreweave.com/pricing) — current pricing tier
- [CoreWeave Classic Pricing](https://www.coreweave.com/pricing/classic) — legacy pricing tier
- [Thundercompute CoreWeave Pricing Guide May 2026](https://www.thundercompute.com/blog/coreweave-gpu-pricing-review) — H100/A100 per-GPU-hour normalization
- [computeprices.com — CoreWeave](https://computeprices.com/providers/coreweave) — multi-GPU price comparison
- [lambda.ai/pricing](https://lambda.ai/pricing) — H100 SXM $3.99–4.29, A100 80GB $2.79, B200 SXM6 $6.69–6.99, GH200 $2.29
- [Replicate billing docs](https://replicate.com/docs/topics/billing) — per-second public-models active billing, private deployments all-time
- [Replicate Pricing](https://replicate.com/pricing)
- [WaveSpeedAI: Replicate Review 2026](https://wavespeed.ai/blog/posts/replicate-review-2026/) — Cloudflare acquisition

### Vast.ai / TensorDock / Hyperstack
- [Vast.ai Docker Execution Environment](https://docs.vast.ai/documentation/instances/templates/docker-environment) — `VAST_CONTAINERLABEL`, `CONTAINER_API_KEY`
- [Vast.ai pricing](https://vast.ai/pricing) — live marketplace rates
- [Hyperstack pricing study](https://www.hyperstack.cloud/blog/case-study/affordable-cloud-gpu-providers) — 2026 rates
- [TensorDock vs Vast.ai](https://getdeploying.com/tensordock-vs-vast-ai) — 2026 comparison

### Cross-language bindings
- [PyPI `nvidia-ml-py`](https://pypi.org/project/nvidia-ml-py/) — NVIDIA-official Python binding, copyright 2011-2025
- [PyPI `pynvml`](https://pypi.org/project/pynvml/) — deprecated, redirects to `nvidia-ml-py`
- [wookayin/nvidia-ml-py mirror](https://github.com/wookayin/nvidia-ml-py) — community mirror reference
- [crates.io `nvml-wrapper`](https://crates.io/crates/nvml-wrapper) — Rust crate, v0.12.0 March 2026
- [rust-nvml/nvml-wrapper GitHub](https://github.com/rust-nvml/nvml-wrapper) — README, NVML version 12 support
- [docs.rs `nvml_wrapper`](https://docs.rs/nvml-wrapper/0.4.1/nvml_wrapper/) — API reference
- [NVIDIA/go-nvml GitHub](https://github.com/NVIDIA/go-nvml) — NVIDIA-official Go binding, NVML API v13
- [pkg.go.dev `github.com/NVIDIA/go-nvml/pkg/nvml`](https://pkg.go.dev/github.com/NVIDIA/go-nvml/pkg/nvml) — Go package docs
- [NVIDIA/go-nvml releases](https://github.com/NVIDIA/go-nvml/releases) — release history
- [npm `node-nvidia-smi`](https://www.npmjs.com/package/node-nvidia-smi) — shell-out wrapper for Node, the only "binding" available

### Prior art
- [OpenCost — Introducing KubeModel](https://opencost.io/blog/introducing-kubemodel/) — OpenCost data model, CNCF Incubation 2024
- [Kubecost vs. OpenCost 2026 comparison](https://www.cloudzero.com/blog/kubecost-vs-opencost/) — relationship and divergence
- [Sustainable Computing IO — Kepler](https://github.com/sustainable-computing-io/kepler) — eBPF + NVML energy attribution
- [Red Hat — Introducing Kepler](https://next.redhat.com/2023/08/22/introducing-kepler-efficient-power-monitoring-for-kubernetes/) — Kepler architecture (⚠️ 2023 source; project ongoing)
- [defilantech/infercost GitHub](https://github.com/defilantech/infercost) — on-premises AI cost-per-token, 2026
- [Luca Berton — FinOps for AI 2026](https://lucaberton.com/blog/finops-ai-gpu-workloads-cost-optimization-2026/) — industry GPU utilization observations
- [NVIDIA GPU Operator with Google GKE](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/google-gke.html) — `nvidia.com/gpu.product` node-label source

### Phase-1 reference docs (this report inherits from)
- [`docs/superpowers/research/2026-05-20-compute-foundation-research.md`](2026-05-20-compute-foundation-research.md)
- [`docs/superpowers/decisions/2026-05-20-compute-foundation-decisions.md`](../decisions/2026-05-20-compute-foundation-decisions.md)
- [`docs/superpowers/conventions.md`](../conventions.md)
