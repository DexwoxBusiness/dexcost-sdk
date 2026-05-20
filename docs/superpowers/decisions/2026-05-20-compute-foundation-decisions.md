# Phase 1 — Compute Foundation — Decisions Log

**Locked:** 2026-05-20
**Status:** Final. Subsequent specs and implementation reference this document; any change goes through a separate change request, not a quiet revision of this file.
**Reference research:** [`docs/superpowers/research/2026-05-20-compute-foundation-research.md`](../research/2026-05-20-compute-foundation-research.md)

The ten decisions that gate the Phase 1 — Compute Foundation spec, plus the strengthenings that emerged during the lock-in conversation. Each decision is recorded with: what was decided, the options considered, the chosen answer, the rationale, and any implementation sharpening that should land in the spec.

---

## Decisions 1–10

### Decision 1 — Cloud Run billing mode default

**Question:** Cloud Run has two billing modes (request-based vs instance-based) that the customer picks at deploy time. The container has no way to discover which mode is active. How does the SDK resolve it?

**Options:**
- (a) Default to request-based, label `estimated` confidence + `pricing_source: "compute_catalog:cloud_run:request_based_default"`
- (b) Customer config knob to override
- (c) Probe Cloud Run Admin API at init (requires RBAC)

**Locked:** **(a) + (b).** Default request-based with the `estimated` confidence labeling; offer an override knob.

**Rationale:** Mirrors v2 §7.1 Tier-3 (default rates → `estimated` confidence; `pricing_source` carries the detail). Request-based is the statistical default. Customer who picked instance-based opts in. Path (c) breaks zero-config positioning.

**Implementation sharpening:** Make the config knob a *general* compute-override channel from day one — `compute_billing_overrides: {cloud_run: "instance", ...}` — rather than a Cloud-Run-specific knob. Decision #5 (provisioned-concurrency) and future cases will reuse the same shape. A single override channel is cleaner than accumulating per-runtime knobs.

---

### Decision 2 — Vercel active-CPU approximation

**Question:** Vercel "active CPU" billing pauses during I/O wait. The SDK can't measure I/O pauses without instrumenting every `await` (prohibitive). How do we approximate?

**Locked:** **Bill `active_CPU ≈ wall_duration`, label `computed` confidence.**

**Rationale:** Math is exact given the inputs; the inputs themselves are the approximation. Over-attributes for I/O-heavy code, exact for CPU-bound. The over-attribution direction is *toward* the customer's actual cloud bill, which is the safe direction — customers complain about under-attribution (looks like dexcost is hiding cost), not over-attribution.

**Spec note:** Document the approximation direction explicitly so customers understand they may see a slightly higher dexcost number than their Vercel invoice on I/O-heavy functions. The discrepancy is honest; the spec should call it out.

---

### Decision 3 — EC2/GCE/Azure-VM instance type from IMDS at init

**Locked:** **IMDS endpoint hit at init, cache result in `CloudEnv`.**

**Rationale:** Already a pattern from cloud_detect Phase 2. One extra IMDS endpoint hit is cheap; result caches for the process lifetime. The instance type lives alongside provider/region/source in the resolved `CloudEnv`, available at finalize time.

**Implementation sharpening:** The IMDS call to `/latest/meta-data/instance-type` runs in the **same Phase 2 background thread** that already probes for region — one probe, two values extracted. Do NOT introduce a second probe lifecycle. Same pattern applies to GCP (`/computeMetadata/v1/instance/machine-type`) and Azure (`/metadata/instance/compute/vmSize`).

---

### Decision 4 — K8s node info source

**Question:** The per-pod-hour math wants the node's CPU count and instance type. The K8s downward API exposes node *name* but NOT node CPU count or instance type. Getting node spec requires either `/api/v1/nodes` (RBAC) or mounted `/host/proc/cpuinfo` (customer manages the mount).

**Locked:** **Path (c) — bill pod limits × duration directly (no node-share term)**, `computed` confidence. Path (b) — K8s `/api/v1/nodes` API call — is **opt-in via config flag `k8s_node_aware: true`**, NOT auto-tried.

**Rationale (sharpened from original recommendation):** The original draft recommended "try path (b) automatically, fall through to (c) on 403." Strengthening: the SDK should NOT probe `/api/v1/nodes` on every pod init by default. Reason — pods can be short-lived (CI runners, batch jobs), and probing `/api/v1/nodes` on each one is a meaningful API server load across the customer's cluster. Default to (c); customers who want the precision opt in once they've granted the ServiceAccount the `get nodes` role.

This still keeps "zero config" — the default works without RBAC. The strengthening avoids the failure mode where a customer with thousands of short-lived pods accidentally DDoSes their own API server because the SDK is "trying to be smart."

**Confidence labeling:** `computed`. The over-attribution surfaces in reconciliation as "you're paying for capacity you're not using" — useful signal, not a bug.

---

### Decision 5 — Lambda provisioned-concurrency

**Locked:** **Track `AWS_LAMBDA_INITIALIZATION_TYPE` env var in v1 (across `on-demand`, `provisioned-concurrency`, `snap-start`, `lambda-managed-instances` values). Defer PC idle-cost billing to v1.1.**

**Rationale:** Most workloads are on-demand. Tracking the env var means the event schema is forward-compatible; adding PC billing in v1.1 is purely additive. Tracking it even for on-demand workloads enables future analysis ("how much of our v1 customer base was on PC?") that sizes the v1.1 work.

---

### Decision 6 — `compute_cost` event type

**Locked:** **Single `compute_cost` event type with `details.billing_model` discriminator** (`lambda` / `fargate` / `ec2` / `cloud_run` / `cloud_functions` / `azure_functions` / `vercel` / `k8s_pod`). NOT one event type per billing model.

**Rationale:** Matches v2's `network` event pattern (one event type, `pricing_source` carries the runtime detail) and v1's `external_cost` pattern (one event type, `service_name` carries the vendor detail). Adding N parallel event types per subsystem would explode the schema and force every downstream consumer (Cost Intelligence, Reconciliation) to handle them in parallel.

**Cross-subsystem implication:** This pattern is now established across three subsystems (network, external_cost, compute_cost). Documented in `docs/superpowers/conventions.md` so subsystems C (GPU), D (storage), E (catalog updates) inherit it.

---

### Decision 7 — Memory units

**Locked:** **Bytes everywhere internally, convert only at the catalog-lookup boundary.**

**Rationale:** Mirrors the v2 egress decision (Decimal arithmetic, divisor is `Decimal("1000000000")`, never the `1e9` literal). Conversion happens once, at the catalog lookup. Eliminates unit-confusion bugs.

**Implementation sharpening — pin the per-runtime conversion table explicitly in the spec:**

| Runtime | Catalog rate is per... | Conversion at lookup |
|---|---|---|
| Lambda | GB (10⁹ bytes) | `memory_bytes / Decimal("1000000000")` |
| **Fargate** | **GiB (2³⁰ bytes) — AWS uses "GB" colloquially but means GiB** | `memory_bytes / Decimal(1024*1024*1024)` |
| Cloud Run | GiB | `memory_bytes / Decimal(1024*1024*1024)` |
| Azure Functions | GB (10⁹) | `memory_bytes / Decimal("1000000000")` |
| Vercel | GB (10⁹) | `memory_bytes / Decimal("1000000000")` |
| K8s/EC2/etc. instance-hourly | per-instance, no per-byte conversion | — |

The Fargate row is the one that was wrong in the original research draft (~4.86% silent over-attribution). Pinning the per-runtime table explicitly prevents the same error in any other runtime. The spec MUST include this table.

---

### Decision 8 — Per-task share window

**Locked:** **Use existing `Task.started_at` / `Task.ended_at`.**
`window_seconds = (ended_at - started_at).total_seconds()`. Zero new schema.

---

### Decision 9 — Idle EC2/GCE/Azure-VM time between tasks

**Question:** A long-running VM might run 1000 dexcost tasks over its lifetime AND have idle periods. The current `task_cpu_share × hourly_rate × window` math divides occupied hours across tasks — but unaccounted idle hours never get billed to any dexcost task. Does dexcost's compute total match the customer's cloud invoice, or accept a systematic gap?

**Options:**
- (a) Idle is invisible to dexcost. Total runs lower than cloud bill by the idle delta.
- (b) Daily synthetic "idle pseudo-task" emits a `compute_cost` event for `idle_hours × hourly_rate`. Total matches cloud bill.
- (c) Compute "instance utilization" metric without billing it.

**Locked:** **(a) — idle is invisible to dexcost.**

**Rationale (this is the most important decision in the table):**

1. **It matches dexcost's actual positioning.** The boundary-enforcement section of the master plan explicitly says "no cloud bill / CUR ingestion — source-measurement boundary; the entire FinOps positioning depends on never reading the bill." Synthetic idle pseudo-tasks would either require reading the cloud bill (to know what idle was) or fabricating it (which is worse).
2. **The gap IS the signal.** A customer whose `dexcost_compute_total = $850/month` and `cloud_bill = $1,200/month` has a useful number: `$350 of unaccounted compute`. That's actionable — right-size instances, kill idle nodes, move to serverless. A customer whose `dexcost_compute_total ≈ cloud_bill` has a *less* useful number because the gap-to-action signal is gone.
3. **Pseudo-tasks invite synthetic-attribution arguments better solved server-side.** Cost Intelligence in the Control Layer is the right place to compute "instance utilization" and explain the gap. The SDK should stay honest about what it measured.

**Customer-facing framing (mandatory — must appear in user-facing docs, not just internal specs):**

> dexcost's compute total will systematically run lower than your cloud bill on long-running runtimes (EC2, Fargate, K8s). The gap is your unaccounted capacity — idle hours, background daemons, scheduling overhead. This is by design: dexcost measures what your tasks actually consumed. The gap-to-cloud-bill is your "unutilized capacity" line item.

This sentence (or its equivalent) must appear in:
- The SDK README
- The Cost Intelligence dashboard surface (when it ships)
- The marketing site's "what dexcost measures" page

Without it explicit, the first customer who notices the gap will file a "dexcost is undercounting" bug. With it explicit, the gap becomes a feature.

---

### Decision 10 — Fargate container-idle-between-tasks

**Question:** A Fargate container starts, runs 3 dexcost tasks back-to-back over 10 minutes, then idles 50 minutes before shutdown. The 50-minute tail is billable Fargate time not attributed to any task. Same shape as #9 but at the container scope.

**Locked:** **(a) — idle Fargate time is invisible**, for consistency with #9.

**Rationale:** A Fargate container idling 50 minutes is a customer-configuration issue (task lifecycle ≠ container lifecycle); the SDK shouldn't paper over it. The gap surfaces a real problem the customer should fix.

**Spec note:** Include an explicit sentence — **"the reconciliation surface (future) is where #9 and #10 gaps both get explained as line items."** This avoids the "but our number is lower than the cloud bill" customer support question becoming perpetual; it has a planned answer.

---

## Strengthenings — implementation polish that landed during decision review

These aren't decision overrides; they're sharpenings that emerged during the lock-in conversation and need to be reflected in the spec.

1. **Decision #1 — generalize the config knob.** Instead of `cloud_run_billing_mode`, use `compute_billing_overrides: { cloud_run: "instance", lambda: "...", ... }` from day one. Single override channel; #5 and future cases reuse it.

2. **Decision #3 — single Phase 2 probe.** IMDS instance-type extraction shares the same background thread as region detection. One probe, two extractions.

3. **Decision #4 — `k8s_node_aware` opt-in.** Path (b) `/api/v1/nodes` probe is opt-in via config flag, NOT auto-tried. Default (c) works zero-config; opt-in (b) once RBAC granted.

4. **Decision #5 — track `AWS_LAMBDA_INITIALIZATION_TYPE` even for `on-demand`.** Enables future PC sizing analysis without v1.1 schema migration.

5. **Decision #7 — pin the per-runtime memory-unit conversion table explicitly in the spec.** Listed above under Decision #7. The Fargate MiB row is the one that was wrong in research draft.

6. **Decisions #9 + #10 — customer-facing framing is mandatory** (not just internal spec text). Listed above under Decision #9.

---

## Cross-subsystem conventions extracted during this decision pass

The decisions above plus v2's network capture have surfaced a coherent set of patterns. Captured separately in [`docs/superpowers/conventions.md`](../conventions.md):

- One event type per subsystem with a discriminator field
- Four-value confidence enum across all subsystems (`exact` / `computed` / `estimated` / `unknown`)
- `pricing_source` strings carry resolution-step detail
- "Measurement on events, derived dollars on task" invariant
- Catalog distribution pattern (Python canonical + sync script + per-SDK bundling)
- Cgroup-first for compute resource measurement; HTTP body interception for network

Subsystems C (GPU), D (storage), E (catalog updates) inherit these. New subsystems propose conventions adjacent to these or update `conventions.md` explicitly.

---

## What happens next

1. **§6b research follow-ups (in parallel with this lock):** GCP Cloud Functions Gen1 deprecation status confirm; Azure Functions duration approach per language binding; Cloud Run Admin API field path (only if Decision #1 changes from option (a), which it has not).
2. **Pricing re-verification:** Every rate cited in the research doc gets re-checked against the live provider page before the spec is locked.
3. **Spec writing:** `docs/superpowers/specs/2026-05-XX-compute-capture-design.md` (capture design) + `docs/superpowers/specs/2026-05-XX-compute-cost-attribution-design.md` (cost math + catalog).
4. **Plan + implementation:** Python first (mirrors the network capture rollout), then cross-SDK port.

---

## Verdict

All 10 approved. Three strengthenings folded in. Two customer-facing artifacts to produce (#9 framing + conventions doc). Ready to move to spec.
