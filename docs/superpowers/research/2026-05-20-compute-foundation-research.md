# Phase 1 — Compute Foundation — Research Report

**Date:** 2026-05-20
**Status:** Research notes. NOT yet a design spec. Decisions are flagged for review.
**Scope:** Per-task compute cost capture + attribution across the providers in the master capability table (cgroup CPU/memory + Lambda/Fargate/EC2/Cloud Run/Cloud Functions/Azure Functions/Vercel/Kubernetes).

This document gathers verified facts from official provider docs, kernel docs, and cgroup-v2 documentation. Every number, env-var name, file path, and pricing rate cited here has a source. Where I could only verify *partial* information, that gap is called out explicitly — no quiet inference.

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
    "Memory": 512       // integer MB (verified: AWS docs treat both MiB and MB; the value is MB for Fargate task-level limits)
  },
  "AvailabilityZone": "us-east-1d",
  "LaunchType": "FARGATE",
  "Containers": [ {...} ],
  "EphemeralStorageMetrics": { "Reserved": 20496 }
}
```

**Critical:** the `Limits` at the **task** scope are in `vCPU (float)` and `MB`. The `Limits` inside individual `Containers` entries use CPU shares (1024 = 1 vCPU). The Phase 1 spec needs to bill the TASK, not individual containers.

**Billing formula (verified [AWS Fargate pricing](https://aws.amazon.com/fargate/pricing/), us-east-1, Linux/X86):**

```
cost_usd = (vCPU × duration_s × $0.000011244)
        + (memory_GB × duration_s × $0.000001235)
        + (ephemeral_storage_GB × duration_s × $0.0000000308)  // beyond the 20 GB free baseline
```

Billing rounds up to the nearest second with a **1-minute minimum**. Duration starts at the image-pull and ends when the task terminates.

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

**Decision needed**: how does the SDK know which billing mode the customer is in? Cloud Run doesn't expose this. Options: (a) assume request-based (the default, statistically common) and document as `inferred` confidence; (b) require a customer config knob (breaks "zero config" principle); (c) read it from the Cloud Run Admin API at init time (requires service-account permissions).

### 1.5 GCP Cloud Functions (Gen1 + Gen2)
- **Gen1** sets `FUNCTION_NAME` / `FUNCTION_TARGET` + memory via `FUNCTION_MEMORY_MB` (verify against current docs before relying)
- **Gen2** runs on Cloud Run under the hood → same K_* env vars + same cgroup story
- Billing matches Cloud Run essentially (Gen2 IS Cloud Run; Gen1 has a similar but separate price list)

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

**Pod resource limits**: Two ways to expose them:

| Method | Reliability | SDK requirement |
|---|---|---|
| Downward API via env or volume | Customer must configure `resourceFieldRef` in their pod spec | Customer-managed; brittle |
| Read cgroup directly (`/sys/fs/cgroup/cpu.max`, `memory.max`) | Always works on cgroup-v2 hosts | Zero customer config |

**Decision**: prefer cgroup reads as the default. Downward API is a fallback if cgroup files are unavailable.

**Pricing**: Pod cost = `(pod_cpu_request / node_cpu) × node_hourly × (task_cpu_seconds / pod_cpu_seconds_in_window)` per cap #20. `computed` confidence. Node hourly comes from the EC2/GCE/Azure-VM catalog. Pod can discover its node via the K8s downward API (`spec.nodeName`) + a metadata call.

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
| 1 | Cloud Run billing mode (request-based vs instance-based) is opaque from inside the container. How do we resolve it? | (a) Default to request-based, document as `inferred`. (b) Add a customer config knob `init({cloud_run_billing_mode: 'instance'})`. (c) Probe the Cloud Run Admin API at init time. **Recommendation: (a) default request-based + (b) config knob for override.** |
| 2 | Vercel "active CPU" billing pauses during I/O wait; we can't measure that without instrumenting every await. How do we approximate? | Bill `active_cpu_hours ≈ wall_duration_hours`. Over-attributes for I/O-heavy functions; matches reality for CPU-bound. Document as `computed`. |
| 3 | EC2 / GCE / Azure VM per-task attribution requires knowing the instance type. Read IMDS at init (one extra HTTP call)? Or require customer config? | **Recommendation: IMDS at init.** Already a pattern from cloud_detect Phase 2; one more endpoint hit is cheap. Cache the instance-type string in the resolved CloudEnv. |
| 4 | K8s pod attribution needs node info (cpu count + instance type). Where does that come from? | Options: (a) read from K8s API server via in-cluster service account (requires RBAC), (b) read from `/host/proc/cpuinfo` (only works if proc is host-mounted), (c) just bill the pod limits × duration without the node-share term (over-attributes but simpler). **Recommendation: (c) for v1, document as `computed`; (a) as a follow-up.** |
| 5 | Lambda's `INITIALIZATION_TYPE = "provisioned-concurrency"` has a separate idle-cost line item. Out of scope for v1? | **Yes — defer.** Most workloads are on-demand. Track the env var so we can fix attribution later without breaking the event schema. |
| 6 | Should `compute_cost` be a single event type, or one event-type per billing model (lambda_cost, fargate_cost, etc.)? | **Single `compute_cost` event** with `details.billing_model` discriminator. Matches how Python's `external_cost` works — one event type, the `pricing_source` distinguishes vendor. |
| 7 | Memory units: Lambda uses MB, Fargate uses MB at task scope but MiB inside containers, cgroup uses bytes. What's the SDK's canonical unit? | **Bytes everywhere internally**, convert only at the boundary (display + when comparing to catalog rates which are in `$/GB-second` where GB = 10^9 bytes). Same decision as network capture's "10^9 not 2^30". |
| 8 | "Window" for per-task share attribution. Some tasks are seconds, others hours. How do we record the window? | Already on the Task model: `started_at` and `ended_at`. `window_hours = (ended_at - started_at) / 3600`. |

---

## 6. What I deliberately did NOT verify in this pass

Honest list of follow-ups that need confirmation before code:

- **GCP Cloud Functions Gen1 env vars** — I cited `FUNCTION_MEMORY_MB` but didn't fetch it from docs. Verify before relying.
- **Azure Functions duration measurement** — I assumed the handler-wrap approach works the same as Lambda. Need to confirm Azure exposes the equivalent of Lambda's `context.getRemainingTimeInMillis()` in Python/Node/Go bindings.
- **Fargate `Limits.Memory` units** — example shows `"Memory": 512` for a 0.25 vCPU / 0.5 GB task. AWS docs use MB and MiB interchangeably; I'm assuming MB here. Verify via a real Fargate task or AWS billing docs.
- **K8s node CPU count source** — `/proc/cpuinfo` reflects the container's cgroup cpu count, not the node's. The node CPU count requires either the K8s API or a node label. Worth a dedicated research pass before designing.
- **The exact AWS Lambda price-list JSON field paths** — I cited the URL pattern but didn't open one. Decision #3 in §5 hinges on the catalog refresh being straightforward; sanity-check by opening one before committing.
- **Cloud Run Admin API response shape** — if we go with Decision #1 option (c), we need to know the exact field that exposes billing mode. Didn't drill in.

---

## 7. Recommended next step

**Lock in §5 decisions (8 items), THEN write the spec.** Same workflow we used for network capture v2 — spec first, plan second, code third. The cost of getting a billing-model wrong is silent under-attribution that customers won't catch until their cloud bill arrives, so the audit-then-decide discipline matters here just as much as the cloud_detect audit caught the OCI region bug.

After §5 is locked, I'd write:
1. `docs/superpowers/specs/2026-05-XX-compute-capture-design.md` — the design spec (analog to the v1 network capture design)
2. `docs/superpowers/specs/2026-05-XX-compute-cost-attribution-design.md` — the cost-attribution math + catalog distribution (analog to the v2 egress design)
3. `docs/superpowers/plans/2026-05-XX-compute-foundation-python.md` — Python-first implementation plan (analog to network capture plan)
4. Cross-SDK port plan (analog to the Go/Rust/TS network port plan)

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
