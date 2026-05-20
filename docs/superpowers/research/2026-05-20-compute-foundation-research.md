# Phase 1 — Compute Foundation — Research Report

**Date:** 2026-05-20 (initial pass) → revised 2026-05-20 after critique-driven re-verification
**Status:** Research notes with three verification gaps closed (K8s node info, Fargate units, Lambda price-list schema). NOT yet a design spec. Decisions §5 are flagged for review; §5.1 adds two newly-surfaced decisions on idle attribution.
**Scope:** Per-task compute cost capture + attribution across the providers in the master capability table (cgroup CPU/memory + Lambda/Fargate/EC2/Cloud Run/Cloud Functions/Azure Functions/Vercel/Kubernetes).

This document gathers verified facts from official provider docs, kernel docs, and cgroup-v2 documentation. Every number, env-var name, file path, and pricing rate cited here has a source. Where I could only verify *partial* information, that gap is called out explicitly — no quiet inference.

> **⚠️ Pricing-freshness caveat.** All dollar rates cited inline in §1 and §3 are point-in-time snapshots from provider docs **as of 2026-05-20**. AWS, GCP, Azure, and Vercel all quietly adjust per-second / per-GB rates more often than this document is dated. The catalog refresh tooling described in §3 is what keeps `compute_prices.json` current; **this document's rates are illustrative, not the source of truth.** Before any code lands, re-verify every rate against the live provider docs to catch quiet updates between research and spec.

---

## 1. Detection signals — verified per-runtime

### 1.1 AWS Lambda
**Source:** [docs.aws.amazon.com Lambda envvars](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html)

Every Lambda invocation has these env vars set automatically:

| Env var | Format | Use |
|---|---|---|
| `AWS_LAMBDA_FUNCTION_NAME` | string | provider detection (existing) |
| `AWS_LAMBDA_FUNCTION_MEMORY_SIZE` | string of int in MB (e.g. `"1024"`) | **memory dimension for cost** |
| `AWS_LAMBDA_FUNCTION_VERSION` | string | attribution |
| `AWS_LAMBDA_INITIALIZATION_TYPE` | `on-demand` \| `provisioned-concurrency` \| `snap-start` \| `lambda-managed-instances` | billing-tier signal |
| `AWS_REGION` | region code | region modifier in catalog lookup |
| `AWS_LAMBDA_RUNTIME_API` | host:port | (custom runtimes only — irrelevant for cost) |

**Per-invocation runtime data** — NOT in env, comes from the handler context object:
- `context.awsRequestId` — unique per invocation (event_id source)
- `context.memoryLimitInMB` — matches `AWS_LAMBDA_FUNCTION_MEMORY_SIZE`
- `context.getRemainingTimeInMillis()` — remaining budget; we want **billed_duration_ms** which is end - start of the handler body

**Billing formula (verified [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/)):**

```
cost_usd = (invocations × per_invocation_rate)
        + (memory_GB × duration_seconds × per_GB_second_rate)
```

us-east-1 rates: `$0.0000166667/GB-s`, `$0.20/M invocations`. Lambda bills in **1-ms increments** since 2020 (don't round duration up).

**ARM vs x86 architecture** — Lambda has TWO SKU sets per region. The price-list JSON exposes them via `attributes.usagetype`:
- `Request` / `Request-ARM` — for invocation rate (x86 vs ARM)
- `Lambda-GB-Second` / `Lambda-GB-Second-ARM` — for duration rate (x86 vs ARM)

ARM (Graviton) is ~20% cheaper. The SDK can detect architecture at init via `os.uname().machine == "aarch64"` (Python/Rust/Go) or `process.arch === "arm64"` (TS). Pin as a billing dimension in the catalog.

**Lambda price-list JSON schema — verified** ([offers/v1.0/aws/AWSLambda/current/us-east-1/index.json](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSLambda/current/us-east-1/index.json)):
- Top-level: `formatVersion`, `disclaimer`, `offerCode`, `version`, `publicationDate`, `products`, `terms`
- A Lambda invocation SKU has `productFamily: "Serverless"`, `attributes.usagetype: "Request"` (x86) or `"Request-ARM"` (ARM), `attributes.group: "AWS-Lambda-Requests"`
- A Lambda duration SKU has `attributes.usagetype: "Lambda-GB-Second"` / `"Lambda-GB-Second-ARM"`
- Ephemeral storage has its own SKU: `"Invocation duration weighted by ephemeral storage assigned to function, measured in GB-s"`
- The catalog-refresh script needs to filter products by `productFamily` + `usagetype` and join to the `OnDemand` term's `pricePerUnit.USD`

**SnapStart / Provisioned Concurrency add new line items**: provisioned-concurrency has a separate `$/GB-s` rate while idle. Out of scope for v1 of Phase 1.

### 1.2 AWS Fargate (ECS / EKS-on-Fargate)
**Source:** [docs.aws.amazon.com ECS task metadata v4 Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-metadata-endpoint-v4-fargate-examples.html)

Fargate sets `ECS_CONTAINER_METADATA_URI_V4` (e.g. `http://169.254.170.2/v4/...`). GET `${ECS_CONTAINER_METADATA_URI_V4}/task` returns task-level info including:

```json
{
  "Cluster": "arn:aws:ecs:us-east-1:...",
  "TaskARN": "arn:aws:ecs:...",
  "Family": "sample-fargate",
  "Revision": "5",
  "Limits": {
    "CPU": 0.25,        // float vCPU (NOT 1024-unit CPU shares — Fargate uses vCPU directly here)
    "Memory": 512       // integer MiB (verified — see "AWS Fargate units" note below)
  },
  "AvailabilityZone": "us-east-1d",
  "LaunchType": "FARGATE",
  "Containers": [ {...} ],
  "EphemeralStorageMetrics": { "Reserved": 20496 }
}
```

**Critical:** the `Limits` at the **task** scope are in `vCPU (float)` and `MiB (int)`. The `Limits` inside individual `Containers` entries use CPU shares (1024 = 1 vCPU). The Phase 1 spec needs to bill the TASK, not individual containers.

**AWS Fargate units — verified May-2026** ([ECS task definition docs](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html)): AWS uses **MiB (mebibytes) internally** but writes "GB" colloquially in marketing copy. So when the docs say "0.5 GB" for a `Memory: 512` task, the actual underlying value is 512 MiB = 0.5 GiB, NOT 500 MB. **Critical billing implication:** the per-GB-second Fargate price (`$0.000001235`) is per **GiB**-second under the hood. The right conversion is `mib_value / 1024 → "GB"` for billing, NOT `mib_value × (1024×1024) / 10^9 → MB`. Without this correction the SDK over-attributes by ~4.86% (the MB/MiB ratio at 512 MiB). Pin this in the spec.

**Billing formula (verified [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/), us-east-1, Linux/X86):**

```
cost_usd = (vCPU × duration_s × $0.000011244)
        + (memory_GiB × duration_s × $0.000001235)   // memory_GiB = task_limits.Memory_MiB / 1024
        + (ephemeral_storage_GiB × duration_s × $0.0000000308)  // beyond the 20 GiB free baseline
```

Billing rounds up to the nearest second with a **1-minute minimum**. Duration starts at the image-pull and ends when the task terminates.

**Architecture also matters for billing** — Linux/X86 vs Linux/ARM vs Windows have different per-vCPU-second rates. The SDK can detect ARM via `os.uname().machine == "aarch64"` (or `process.arch === "arm64"` in Node). Pin as a billing dimension in the catalog.

### 1.3 AWS EC2 (and ECS-on-EC2 / EKS-on-EC2)
**Source:** [pricing.us-east-1.amazonaws.com Price List Bulk API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-ppslong.html)

No specific env-var pattern. Detection signal is `chassis_asset_tag` / `sys_vendor` (already in our `cloud_detect`). To know the instance type, we need to either:
- Read the IMDS endpoint at `/latest/meta-data/instance-type` (returns e.g. `c7g.xlarge`)
- Look up its hourly rate from the EC2 price-list JSON

The EC2 hourly rate is per-instance, not per-task. Our attribution math from the master plan:
```
task_cost = max(task_cpu_share, task_mem_share) × hourly_rate × window_hours
```
where `task_cpu_share = task_cpu_seconds / instance_cpu_count / window_seconds`. Per cap #19 this is `computed` confidence (not `exact`).

### 1.4 GCP Cloud Run
**Source:** [docs.cloud.google.com Cloud Run container contract](https://docs.cloud.google.com/run/docs/container-contract), [cloud.google.com/run/pricing](https://cloud.google.com/run/pricing)

**Critical gap discovered**: Cloud Run **does NOT expose vCPU or memory limits via env vars or the metadata server.** The container contract guarantees only `K_SERVICE`, `K_REVISION`, `K_CONFIGURATION`, `PORT`. The CPU and memory allocations are visible only via cgroup files (`/sys/fs/cgroup/cpu.max` and `/sys/fs/cgroup/memory.max`).

**Billing formula** — Cloud Run has TWO billing modes the customer picks at deploy time:

| Mode | Formula | Notes |
|---|---|---|
| Request-based (default) | `requests × per_request + vCPU_s_active × per_s_active + GiB_s_active × per_s_active` | "active" = while a request is being processed; instance idle = no charge |
| Instance-based | `vCPU_s_full × per_s_full + GiB_s_full × per_s_full` | charges for the full instance lifetime; no per-request fee; lower per-second rates |

Rates differ per region (Tier 1 vs Tier 2). Granularity: 100ms.

**Decision needed**: how does the SDK know which billing mode the customer is in? Cloud Run doesn't expose this. Options: (a) assume request-based (the default, statistically common) and label `estimated` confidence with `pricing_source: "compute_catalog:cloud_run:request_based_default"` (matches v2 §7.1 Tier-3 pattern — `default` rates collapse to `estimated` confidence + the source string carries the resolution-step detail); (b) require a customer config knob (breaks "zero config" principle); (c) read it from the Cloud Run Admin API at init time (requires service-account permissions). **Confidence label note:** the v2 egress design intentionally restricted the confidence enum to four values (`exact`/`computed`/`estimated`/`unknown`); introducing a fifth value `inferred` for compute would force the dashboard to explain two enums. Stay in the four-value space.

### 1.5 GCP Cloud Functions (Gen1 + Gen2)
- **Gen2** runs on Cloud Run under the hood → same K_* env vars + same cgroup story + same Cloud Run billing model
- **Gen1** has been deprecated to new deployments since 2024 and exits sunset by Sep 2026 per GCP's migration timeline; verify before treating as a v1 target. **Recommendation: support if trivial via the Gen2/Cloud Run code path; explicitly skip Gen1-specific env-var detection unless customer demand surfaces.** This avoids investing in a path that will be cold by the time the SDK ships.

### 1.6 Azure Functions
**Source:** [azure.microsoft.com Functions pricing](https://azure.microsoft.com/en-us/pricing/details/functions/), [learn.microsoft.com consumption-costs](https://learn.microsoft.com/en-us/azure/azure-functions/functions-consumption-costs)

Detection: `FUNCTIONS_WORKER_RUNTIME` env var (already in our `cloud_detect`).

**Consumption plan billing**:
```
cost_usd = (executions × $0.20/M) + (GB_seconds × $0.000016)
```

Where `GB_seconds = avg_memory_GB × duration_seconds`. This is **nearly identical** to Lambda's formula. Memory is **observed peak** (Azure measures during execution; doesn't use a configured memory limit like Lambda does).

**Premium / Dedicated plans** bill differently (per vCPU-second + per GB-second of a reserved instance). Out of scope for v1.

### 1.7 Vercel Functions
**Source:** [vercel.com/docs/functions/usage-and-pricing](https://vercel.com/docs/functions/usage-and-pricing)

Detection: `VERCEL` / `VERCEL_REGION` env (already in our `cloud_detect`).

Vercel's "Fluid Compute" billing is **three-dimensional**:
```
cost_usd = (invocations × $0.60/M)
        + (active_cpu_hours × $0.128)
        + (provisioned_memory_GB_hours × $0.0106)
```

**Active CPU** = billed only while the CPU is actually running JS — pauses during I/O wait. **Provisioned memory** = billed continuously until the last in-flight request finishes.

**Gap**: we can't reliably measure "active CPU pauses during I/O" from the customer's JS code without instrumenting every promise/await. Realistic plan: bill as if `active_cpu_hours ≈ wall_duration_hours` (slight over-attribution; matches the customer's experience when their function is CPU-bound, undershoots for I/O-heavy functions). Document as `computed` confidence.

### 1.8 Kubernetes (any cluster — EKS / GKE / AKS / self-managed)

**Pod resource limits — two paths, same end:**

| Method | Reliability | SDK requirement |
|---|---|---|
| Downward API via env or volume | Customer must configure `resourceFieldRef` in their pod spec | Customer-managed; brittle |
| Read cgroup directly (`/sys/fs/cgroup/cpu.max`, `memory.max`) | Always works on cgroup-v2 hosts | Zero customer config |

**Decision**: prefer cgroup reads as the default. Downward API is a fallback if cgroup files are unavailable.

**Node info for the per-pod-hour math — verified May-2026** ([Kubernetes downward API](https://kubernetes.io/docs/concepts/workloads/pods/downward-api/)). The per-cap-#20 formula needs the node's CPU count and instance type. Three paths, ranked by zero-config friendliness:

| Path | What it gives | Cost / Risk |
|---|---|---|
| Downward API `spec.nodeName` + `status.hostIP` | Node NAME and IP only — NOT the CPU count or instance type | Always works (kubelet acts as secure intermediary; no RBAC). Caller can't bill from this alone. |
| K8s API call to `/api/v1/nodes/{nodeName}` via the in-cluster service account | Full node spec: `status.capacity.cpu`, `status.allocatable.cpu`, `metadata.labels["node.kubernetes.io/instance-type"]` | Requires the customer's pod's ServiceAccount to have `get` permission on `nodes` — many customers do NOT grant this by default. Best-effort: try the call, log 403 + fall through. |
| Bill the pod's *limits* directly without the node-share term | `pod_cpu_limit × duration × per-vCPU-second` from the EC2/GCE/Azure-VM catalog → over-attributes when the pod doesn't fully utilise its limits, undercounts the share-of-instance pattern | Zero config. Same shape as Fargate's per-task billing. **`computed` confidence.** |

**v1 recommendation:** path (c) — bill pod limits × duration directly. Path (b) lights up automatically when the customer has granted node-read RBAC; logs a one-shot "node-read denied; falling back to limit-based billing" warning otherwise. This keeps dexcost "zero config" on K8s — a positioning property worth more than the precision gain from the node-share math for most customers.

**Why path (c) is reasonable**: K8s scheduling already attempts to pack pods such that `sum(pod_limits) ≤ node_capacity`. When pods are right-sized (the common case), billing `pod_limit × hourly_per_vcpu_rate` is close to the true per-pod share. When pods are over-provisioned, the SDK over-attributes — but the customer's *actual* cloud bill is also over-attributing on their behalf (they're paying for the node regardless of pod utilization), so the gap is visible in reconciliation as "you're paying for capacity you're not using" rather than as a dexcost bug.

**Cluster-vendor detection** for the right hourly rate: EKS clusters can be detected by hitting the AWS IMDS from the pod (works if the pod's network policy allows the metadata IP). GKE via GCP metadata server. AKS via Azure IMDS. Same fanout as our existing cloud_detect Phase 2. If all three fail, the pod gets the universal `_meta.default_rate` from the catalog (matches v2's Tier-3 ladder).

---

## 2. Cgroup v2 — verified file formats

**Source:** [docs.kernel.org cgroup-v2.html](https://docs.kernel.org/admin-guide/cgroup-v2.html)

All these live at `/sys/fs/cgroup/<field>` (unified hierarchy; cgroup v2 only — Lambda still uses a restricted cgroup-v1ish layout, hence the env-var-based path for Lambda).

| File | Format | Unit | Example | Use |
|---|---|---|---|---|
| `cpu.stat` | flat key-value | microseconds | `usage_usec 12345` | CPU time used by this cgroup. Diff at task end - task start = task CPU-seconds (cap #5) |
| `cpu.max` | `<quota> <period>` or `max <period>` | microseconds | `100000 100000` (= 1 vCPU) OR `max 100000` (unlimited) | CPU limit / count for billing (Cloud Run, K8s, Fargate) |
| `memory.peak` | single int | bytes | `2147483648` | Peak memory used (cap #6). RW: tests can reset it via "max" write per kernel docs |
| `memory.current` | single int | bytes | `1234567890` | Current memory used (snapshot) |
| `memory.max` | single int or `max` | bytes | `536870912` OR `max` | Memory limit; used for billing dimension when not in env |
| `memory.pressure` | PSI nested | percent + microseconds | `some avg10=0.00 avg60=0.00 avg300=0.00 total=0` | Memory stall % (cap #8) |
| `cpu.pressure` | PSI nested | percent + microseconds | same shape as memory.pressure | CPU stall % (cap #7) |
| `io.stat` | nested keyed by `<maj>:<min>` | bytes & counts | `8:16 rbytes=1459200 wbytes=314773504 rios=192 wios=353 dbytes=0 dios=0` | Per-device IO bytes (cap #9, Phase 2) |

**Behaviour notes**:
- `cpu.stat` `usage_usec` is **monotonic** — read at task start, read again at task end, diff is the task's CPU-seconds.
- `memory.peak` is the **high-water mark** since creation OR last reset. Reading it at task end gives the task's peak (good enough for billing when one task ≈ one container lifecycle).
- `cpu.max` value `max <period>` (literal string "max") means **no quota** — the cgroup can use all CPUs. In that case we use `nproc` / `/sys/devices/system/cpu/online` for the count.
- PSI files are only present if the kernel has CONFIG_PSI=y (Linux 4.20+). Default on every major distro since 2020.

---

## 3. Pricing catalog — how to build it

### 3.1 AWS Price List Bulk API
**Source:** [docs.aws.amazon.com Price List Bulk API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-ppslong.html)

Same shape as the egress pricing we already use:
- Discovery: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSLambda/current/region_index.json` lists region → JSON file URL
- Per-region JSON files contain `products` (SKU dimensions) + `terms` (rate cards). Schema is documented; well-known.
- Services we need: `AWSLambda`, `AmazonECS` (for Fargate), `AmazonEC2`

These files are large (10–100s of MB per region for EC2). The Python SDK already has a precedent for "weekly CI cron pulls + bundled JSON" — we'd run a script in this repo that distills the AWS files into our catalog format (just the SKUs we need: per-vCPU-second, per-GB-second, per-invocation, per-instance-hour).

### 3.2 GCP Cloud Billing API
**Source:** [cloud.google.com/billing/v1/how-tos/catalog-api](https://cloud.google.com/billing/v1/how-tos/catalog-api)

Cloud Billing Catalog API at `https://cloudbilling.googleapis.com/v1/services/{serviceId}/skus`. Requires an API key. Services we need:
- Cloud Run: `services/152E-C115-5142`
- Cloud Functions: `services/29E7-DA93-CA13`
- Compute Engine: `services/6F81-5844-456A`

The script approach mirrors AWS — pull, filter, bundle.

### 3.3 Azure Retail Prices API
**Source:** [learn.microsoft.com Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)

Public, anonymous: `https://prices.azure.com/api/retail/prices?$filter=serviceName eq 'Functions' and armRegionName eq 'eastus'`. Returns paginated JSON. Need: Functions, Container Apps, VMs (App Service is its own service).

### 3.4 Vercel
**Source:** Vercel publishes regional rates only on docs pages — no API. Manual transcription, same as the egress catalog approach. Currently published rates:
- Default region (US East): `$0.60/M`, `$0.128/CPU-h`, `$0.0106/GB-h`
- Brazil South: `$0.221/CPU-h`, `$0.0183/GB-h`
- See [vercel.com/docs/functions/usage-and-pricing](https://vercel.com/docs/functions/usage-and-pricing) for the full table

### 3.5 Cross-vendor schema sketch

Same pattern as `egress_prices.json` — one bundled JSON, four SDKs read it via the catalog-sync script. Skeleton:

```json
{
  "_meta": {
    "version": "1.0.0",
    "last_updated": "2026-05-20",
    "currency": "USD",
    "description": "Compute pricing — per-invocation, per-vCPU-second, per-GB-second, per-instance-hour by provider + region."
  },
  "aws": {
    "lambda": {
      "us-east-1": {
        "per_invocation_usd": "0.0000002",
        "per_gb_second_usd": "0.0000166667"
      }
    },
    "fargate": {
      "us-east-1": {
        "per_vcpu_second_usd": "0.000011244",
        "per_gb_second_usd": "0.000001235",
        "per_storage_gb_second_usd": "0.0000000308"
      }
    },
    "ec2": {
      "us-east-1": {
        "c7g.xlarge": "0.1452",
        "...": "..."
      }
    }
  },
  "gcp": {
    "cloud_run_request_based": {
      "tier_1": {
        "per_request_usd": "0.00000040",
        "per_vcpu_second_active_usd": "0.000024",
        "per_gib_second_active_usd": "0.0000025"
      }
    }
  },
  "azure": {
    "functions_consumption": {
      "global": {
        "per_execution_usd": "0.0000002",
        "per_gb_second_usd": "0.000016"
      }
    }
  },
  "vercel": {
    "fluid": {
      "default": {
        "per_invocation_usd": "0.00000060",
        "per_cpu_hour_usd": "0.128",
        "per_gb_hour_usd": "0.0106"
      }
    }
  }
}
```

---

## 4. The attribution math per runtime — verified formulas

### 4.1 Per-invocation runtimes (serverless)

These bill **per call**. The SDK wraps the handler / request and records start + end timestamps + memory.

| Runtime | Cost formula | Inputs the SDK needs |
|---|---|---|
| AWS Lambda | `1 × $/M_invocations + (mem_MB/1024) × duration_s × $/GB-s` | memory from env, duration from handler instrumentation |
| Vercel Fluid | `1 × $/M + cpu_h × $/CPU-h + (mem_MB/1024) × duration_h × $/GB-h` | memory from env or cgroup, duration from handler |
| Azure Functions Consumption | `1 × $/M + (peak_mem_MB/1024) × duration_s × $/GB-s` | peak memory from cgroup `memory.peak`, duration from handler |
| GCP Cloud Functions Gen1 | `1 × $/M + (mem_MB/1024) × duration_s × $/GB-s + cpu_GHz_s × $/GHz-s` | memory from env, duration from handler, vCPU from env (`FUNCTION_MEMORY_MB` implies a CPU tier) |
| GCP Cloud Run (request-based) | `1 × $/M + active_vCPU_s × $/vCPU-s + active_GiB_s × $/GiB-s` | vCPU from cgroup `cpu.max`, mem from cgroup `memory.max`, duration from request lifecycle |

**Pattern**: wrap the entry point, snapshot start time, snapshot end time + bytes, look up the rate, emit a `compute_cost` event.

### 4.2 Per-task share runtimes (long-running)

These bill the **whole instance hour**; the SDK has to apportion across tasks.

| Runtime | Cost formula | Inputs |
|---|---|---|
| AWS EC2 / GCE / Azure VM | `max(cpu_share, mem_share) × hourly_rate × window_hours` where `cpu_share = task_cpu_seconds / instance_total_cpu_seconds_in_window`, same for mem | instance type from IMDS, task cpu/mem from cgroup diff, window = task start→end |
| K8s pod | `(pod_cpu_request / node_cpu) × node_hourly × (task_cpu_s / pod_cpu_s_in_window)` | pod limits from cgroup, node info from K8s downward API or node-label cascade |
| Fargate | `vCPU × duration_s × $/vCPU-s + mem_GB × duration_s × $/GB-s` (this is *almost* per-task already — billing is task-granular) | from task metadata endpoint |

**Pattern**: at task start, read `cpu.stat` `usage_usec`, `memory.peak` (reset if RW), `memory.current`. At task end, read again, diff, compute.

**Confidence**: `computed` (the resource shares are accurate; the assumption that the hourly rate divides cleanly across tasks is an approximation when multiple processes share the instance).

---

## 5. Open decisions — flag for review before any code lands

These came up during research and have no obvious answer. We should lock them in before the Phase 1 spec is written.

| # | Decision | Trade-off |
|---|---|---|
| 1 | Cloud Run billing mode (request-based vs instance-based) is opaque from inside the container. How do we resolve it? | (a) Default to request-based, label confidence `estimated` + `pricing_source: "compute_catalog:cloud_run:request_based_default"` (mirrors v2 §7.1 Tier-3 — `default` rates → `estimated` + source string carries the resolution-step detail; stays in the four-value confidence enum). (b) Add a customer config knob `init({cloud_run_billing_mode: 'instance'})`. (c) Probe the Cloud Run Admin API at init time. **Recommendation: (a) default request-based + (b) config knob for override.** |
| 2 | Vercel "active CPU" billing pauses during I/O wait; we can't measure that without instrumenting every await. How do we approximate? | Bill `active_cpu_hours ≈ wall_duration_hours`. Over-attributes for I/O-heavy functions; matches reality for CPU-bound. Document as `computed`. |
| 3 | EC2 / GCE / Azure VM per-task attribution requires knowing the instance type. Read IMDS at init (one extra HTTP call)? Or require customer config? | **Recommendation: IMDS at init.** Already a pattern from cloud_detect Phase 2; one more endpoint hit is cheap. Cache the instance-type string in the resolved CloudEnv. |
| 4 | K8s pod attribution needs node info (cpu count + instance type). Where does that come from? | Options: (a) read from K8s API server via in-cluster service account (requires RBAC), (b) read from `/host/proc/cpuinfo` (only works if proc is host-mounted), (c) just bill the pod limits × duration without the node-share term (over-attributes but simpler). **Recommendation: (c) for v1, document as `computed`; (a) as a follow-up.** |
| 5 | Lambda's `INITIALIZATION_TYPE = "provisioned-concurrency"` has a separate idle-cost line item. Out of scope for v1? | **Yes — defer.** Most workloads are on-demand. Track the env var so we can fix attribution later without breaking the event schema. |
| 6 | Should `compute_cost` be a single event type, or one event-type per billing model (lambda_cost, fargate_cost, etc.)? | **Single `compute_cost` event** with `details.billing_model` discriminator. Matches how Python's `external_cost` works — one event type, the `pricing_source` distinguishes vendor. |
| 7 | Memory units: Lambda uses MB, Fargate uses MB at task scope but MiB inside containers, cgroup uses bytes. What's the SDK's canonical unit? | **Bytes everywhere internally**, convert only at the boundary (display + when comparing to catalog rates which are in `$/GB-second` where GB = 10^9 bytes). Same decision as network capture's "10^9 not 2^30". |
| 8 | "Window" for per-task share attribution. Some tasks are seconds, others hours. How do we record the window? | Already on the Task model: `started_at` and `ended_at`. `window_hours = (ended_at - started_at) / 3600`. |

### 5.1 Idle / unattributed compute — newly surfaced

These two decisions came out of the critique pass and weren't in the original §5. Both shape **whether the dexcost compute total matches the customer's cloud invoice exactly or accepts a systematic gap for unaccounted time.** That's a positioning question, not a detail.

| # | Decision | Options & trade-off |
|---|---|---|
| 9 | **Idle EC2 / GCE / Azure-VM time between tasks.** A long-running VM might run 1000 dexcost tasks over its lifetime AND have idle periods (no task active, just kernel + background daemons). The current `task_cpu_share × hourly_rate × window` math divides the *occupied* hours across tasks proportionally — but unaccounted idle hours never get billed to any dexcost task. | (a) **Idle is invisible to dexcost** (matches "we only see tasks"). Customer's dexcost total runs lower than their cloud bill by the idle delta. Reconciliation surfaces the gap as "compute idle / unaccounted". (b) **Daily idle pseudo-task.** Emit a synthetic `compute_cost` event each day for `idle_hours × hourly_rate`, attributed to a workspace-level `__idle__` pseudo-task. Customer's total matches cloud bill. (c) **Compute "instance utilization" metric without billing it.** Track the ratio of `sum(task_cpu_seconds) / instance_cpu_seconds` per workspace and surface it as a side-channel signal. **Recommendation: (a) for v1** — the "we only see tasks" framing is the SDK's actual positioning; the reconciliation surface (future) is where idle gets explained. Idle pseudo-tasks invite synthetic attribution arguments better solved server-side. |
| 10 | **Fargate container idle between tasks.** A Fargate container starts, runs 3 dexcost tasks back-to-back over 10 minutes, then idles for 50 minutes before shutdown. The 50-minute tail is billable Fargate time that's not attributed to any task. Same shape as #9 but at the container scope (vs the VM scope). | (a) **Idle Fargate time is invisible** (matches the EC2 answer in #9). (b) **Auto-emit a "container idle" pseudo-task** for the post-task tail. (c) **Tie idle Fargate time to the LAST task that ran on the container** (the task "leaves the container hot"). **Recommendation: (a) for consistency with #9.** A Fargate container that idles for 50 minutes is a customer-configuration issue (task lifecycle ≠ container lifecycle); the SDK shouldn't paper over it. The pattern customers want fixed (right-size your tasks) is visible only when dexcost's total is lower than the cloud bill, so the gap IS the signal. |

**Critical framing for both decisions:** If we pick (a) for both — "idle is invisible" — then `dexcost compute total < cloud invoice` is *expected, not a bug*. The Cost Intelligence surface (future) and the reconciliation feature (future) are where the gap gets explained. The SDK's compute capture stays honest: it only attributes time the customer's own tasks actually ran. This matches the source-measurement boundary in the boundary-enforcement section of the master plan.

---

## 6. What I deliberately did NOT verify in this pass

Honest list of remaining follow-ups, separated by whether they gate the spec or are bounded post-spec verification.

### 6a. Resolved in this revision (initially flagged as gaps)

- ✅ **Fargate `Limits.Memory` units** — verified as **MiB, not MB.** AWS docs say "memory values are specified in MiB" and the colloquial "0.5 GB = 512" mapping is GiB-aliased GB. Pinned in §1.2 with the billing correction (`memory_GiB = mib / 1024`). Was a silent ~4.86% billing error if left unverified.
- ✅ **AWS Lambda price-list JSON schema** — verified the regional file at `pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSLambda/current/us-east-1/index.json`. Top-level keys (`products`, `terms`, etc), field paths (`productFamily: "Serverless"`, `attributes.usagetype: "Request"`/`"Lambda-GB-Second"`), and the ARM/x86 SKU split are now pinned in §1.1. Catalog-refresh tooling will work.
- ✅ **K8s node CPU count source** — verified via the K8s docs: downward API exposes node *name* + IP but NOT node CPU count. The full node spec requires `nodes` API access through the in-cluster service account (RBAC the customer must grant). Pinned in §1.8 with the three-path table and the v1 recommendation (bill pod limits × duration, no node-share term — keeps dexcost "zero config" on K8s). This was load-bearing for the K8s positioning; resolving it before the spec was the right call.

### 6b. Bounded post-spec follow-ups (don't gate architecture)

- **GCP Cloud Functions Gen1 env vars** — Gen1 is deprecated to new deployments since 2024 with full sunset by Sep 2026. §1.5 reframes this as a "support if trivial via the Gen2/Cloud Run code path; skip Gen1-specific detection" recommendation. If a customer surfaces Gen1 demand, verify the env vars at that point.
- **Azure Functions duration measurement** — assumed the handler-wrap approach works the same as Lambda. Confirm via the Azure Functions runtime docs for each language binding (Python, Node, Go) before the Python implementation; trivial to course-correct if wrong.
- **Cloud Run Admin API response shape** — only matters if Decision #1 lands on option (c). The §5 recommendation is (a) + (b), which doesn't depend on the Admin API.

### 6c. New verification gap surfaced in this revision

- **Pricing rates may have drifted between this research date (2026-05-20) and code time.** See the freshness caveat at the top. The spec phase should include a re-verification pass against each provider's pricing page. The values in this document are reference points for the design, not source-of-truth for the catalog.

---

## 7. Recommended next step

**Lock in §5 decisions (now 10 items with the idle/container-lifecycle additions) AND complete the §6b follow-ups in parallel — THEN write the spec.** The follow-ups are bounded enough that they don't gate decision-making, but waiting until post-spec to verify them invites the same kind of silent-error surface the Fargate MiB question exposed.

Order of operations:

1. **Now (parallel):**
   - User reviews + confirms/overrides the 10 decisions in §5 + §5.1
   - I verify the §6b items (GCP Functions Gen1 status confirm, Azure Functions duration approach for each language, Cloud Run Admin API field path if Decision #1 changes from option (a))
   - Re-verify all pricing rates against live provider docs (the freshness caveat at top)

2. **Then:**
   - `docs/superpowers/specs/2026-05-XX-compute-capture-design.md` — the design spec (analog to the v1 network capture design)
   - `docs/superpowers/specs/2026-05-XX-compute-cost-attribution-design.md` — the cost-attribution math + catalog distribution (analog to the v2 egress design)

3. **Then:**
   - `docs/superpowers/plans/2026-05-XX-compute-foundation-python.md` — Python-first implementation plan (analog to network capture plan)
   - Cross-SDK port plan (analog to the Go/Rust/TS network port plan)

Same workflow we used for network capture v2 — spec first, plan second, code third. The cost of getting a billing-model wrong is silent under-attribution that customers won't catch until their cloud bill arrives, so the audit-then-decide discipline matters here just as much as the cloud_detect audit caught the OCI region bug — and matters as much as the Fargate MiB unit error this critique pass caught before any code shipped.

---

## Sources

- [AWS Lambda envvars reference](https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html) — verified all `AWS_LAMBDA_*` env var names + the reserved list
- [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/) — `$0.0000166667/GB-s`, `$0.20/M invocations`, 1ms billing granularity
- [AWS Fargate task metadata v4 Fargate examples](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-metadata-endpoint-v4-fargate-examples.html) — JSON shape, `Limits.CPU` as vCPU float, `Limits.Memory` as MB int
- [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/) — `$0.000011244/vCPU-s`, `$0.000001235/GB-s`, `$0.0000000308/GB-s` ephemeral
- [AWS Price List Bulk API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-ppslong.html) — `pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSLambda/current/region_index.json` pattern
- [Cloud Run container contract](https://docs.cloud.google.com/run/docs/container-contract) — `K_*` env vars; NO CPU/memory env exposure
- [Cloud Run pricing](https://cloud.google.com/run/pricing) — request-based + instance-based modes, tier 1 / tier 2
- [Azure Functions Consumption pricing](https://azure.microsoft.com/en-us/pricing/details/functions/) — `$0.000016/GB-s`, `$0.20/M executions`
- [Azure Functions consumption-costs guide](https://learn.microsoft.com/en-us/azure/azure-functions/functions-consumption-costs) — peak-memory billing semantics
- [Vercel Functions usage and pricing](https://vercel.com/docs/functions/usage-and-pricing) — three-dimensional `$0.60/M + $0.128/CPU-h + $0.0106/GB-h`
- [Linux cgroup-v2 admin guide](https://docs.kernel.org/admin-guide/cgroup-v2.html) — `cpu.stat` flat-keyed usec, `memory.peak` bytes, `cpu.max` quota/period format
- [Kubernetes downward API — env exposure](https://kubernetes.io/docs/tasks/inject-data-application/environment-variable-expose-pod-information/) — `resourceFieldRef` for `limits.cpu`, `limits.memory`, `requests.cpu`, `requests.memory`
- [Kubernetes resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) — how kubelet sets `cpu.max` / `memory.max` from pod spec
- [Kubernetes downward API doc](https://kubernetes.io/docs/concepts/workloads/pods/downward-api/) — verified: downward API exposes `spec.nodeName` + `status.hostIP` but NOT node CPU count; node spec requires `/api/v1/nodes` access via service account
- [Amazon ECS task definition parameters](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html) — verified Fargate memory values are in MiB (AWS uses "GB" loosely to mean GiB)
- [AWS Lambda regional pricing JSON](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSLambda/current/us-east-1/index.json) — verified the price-list schema: `productFamily: "Serverless"`, `attributes.usagetype` (`Request` / `Request-ARM` / `Lambda-GB-Second` / `Lambda-GB-Second-ARM`), `terms.OnDemand[...].pricePerUnit.USD`
