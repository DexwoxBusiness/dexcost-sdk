# Compute Foundation (v1 capture + v2 cost) — TypeScript SDK — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Port the Phase 1 compute capture + cost-attribution layer to the TypeScript SDK, mirroring the Python implementation (commits `a613924` … `bc3b45d` + the `3f5b327` price refresh).

**Reference implementations:**
- Python (source of truth): `python/src/dexcost/` — read the equivalent Python file BEFORE writing each TS module.
- Go plan: `docs/superpowers/plans/2026-05-22-compute-foundation-go.md` — same 11-task structure.
- Specs: `docs/superpowers/specs/2026-05-21-compute-{capture,cost-attribution}-design.md`
- Decisions: `docs/superpowers/decisions/2026-05-20-compute-foundation-decisions.md`

**Architecture:** five new files mirroring Python — `cloud-detect.ts` extended with `instanceType`, `core/cgroup-reader.ts`, `core/compute-runtime.ts`, `core/fargate-metadata.ts`, `pricing/compute-pricing.ts`, `core/compute-accountant.ts`. `core/tracker.ts::finalizeCosts` gains a compute back-fill step.

**Tech stack:** Node.js 18+, TypeScript 5.x, `Decimal.js` (already in `package.json`), Node's `fs.promises` / `node:fs` for cgroup reads, `node:http` / `globalThis.fetch` for IMDS. **Browser-safe:** the cgroup reader, Fargate metadata, and IMDS probes must be node-only — guard them with `typeof process !== 'undefined' && process.versions?.node` and no-op in browser bundles. **Runtime-agnostic:** works under Node, Bun, and Deno where `process.env` and `fs.promises` are available (Deno has them via `node:` specifiers).

**Run tests with:** `cd typescript && pnpm test`

**Pre-requisites already landed on this branch:**
- v2 network capture: `cloud-detect.ts`, `pricing/egress-pricing.ts`, `core/network-accountant.ts` + deferred-cost finalize pattern.
- `EventType.ComputeCost` already in `core/models.ts`.
- `Task.computeCostUsd: Decimal` already on the Task type.
- `typescript/src/data/compute_prices.json` already synced from Python canonical by `scripts/sync_compute_catalog.sh`.

---

### Task 1: Extend `CloudEnv` with `instanceType` + IMDS probe extensions

**Files:**
- Modify: `typescript/src/cloud-detect.ts`
- Test: `typescript/test/cloud-detect-instance-type.test.ts`

**Python sibling:** `python/src/dexcost/cloud_detect.py` (commit `a613924`).

- [ ] Extend `interface CloudEnv` with `instanceType: string | null` (default `null`). Audit all `CloudEnv` literals across the codebase and add the field.
- [ ] `_probeAws`: after the region fetch, GET `/latest/meta-data/instance-type` with the same IMDSv2 token, wrapped in its own `try/catch` so a failure here doesn't lose the region.
- [ ] `_probeGcp`: GET `/computeMetadata/v1/instance/machine-type` with `Metadata-Flavor: Google`; strip `projects/.../machineTypes/` prefix via `path.split('/').pop()`.
- [ ] `_probeAzure`: read `compute.vmSize` from the existing JSON payload.
- [ ] `_background` (async detection block): preserve `instanceType` when stitching env+IMDS results.
- [ ] Tests — 8 cases mirroring Python. Use `nock` or `vitest`'s `vi.spyOn(global, 'fetch')` to mock IMDS responses (check how existing `cloud-detect.test.ts` does it).
- [ ] Commit — `feat(compute-ts): extend CloudEnv with instanceType from IMDS (Decision #3)`.

---

### Task 2: `core/cgroup-reader.ts` — cgroup v2 file readers (node-only)

**Python sibling:** `cgroup_reader.py` (commit `1609faf`).

- [ ] Module-level `let cgroupRoot = '/sys/fs/cgroup'` with a test helper `export function _setCgroupRootForTests(p: string)`.
- [ ] Types:
  ```ts
  export interface CpuStat { usageUsec: number }
  export interface CpuMax { quotaUs: number | null, periodUs: number, vcpuCount: number }
  ```
- [ ] Functions return `null` (TS idiom for Python `None`):
  - `readCpuStat(): CpuStat | null`
  - `readCpuMax(): CpuMax | null`
  - `readMemoryPeak() / readMemoryMax() / readMemoryCurrent(): number | null`
- [ ] **Browser guard:** at the top of every public function, `if (typeof process === 'undefined' || !process.versions?.node) return null;`. The module never throws in a browser bundle.
- [ ] `readCpuMax`: when literal is `"max"`, fall back to `os.cpus().length`.
- [ ] All reads use `fs.readFileSync` (sync — cgroup reads are local file I/O, sub-millisecond; matches Python which uses sync `read_text()`).
- [ ] Tests — 12 cases mirror Python. Use `tmp.dirSync()` + write fixture files + override `cgroupRoot` per test.
- [ ] Commit — `feat(compute-ts): cgroup v2 file readers (node-only, browser-safe)`.

---

### Task 3: `core/compute-runtime.ts` — runtime cascade

**Python sibling:** `compute_runtime.py` (commit `afbc007`).

- [ ] Enum-as-string-union (TS idiom):
  ```ts
  export const RuntimeKind = {
    Lambda: 'lambda', Fargate: 'fargate', Ec2: 'ec2',
    CloudRun: 'cloud_run', CloudFunctions: 'cloud_functions', Gce: 'gce',
    AzureFunctions: 'azure_functions', AzureVm: 'azure_vm',
    Vercel: 'vercel_fluid', K8sPod: 'k8s_pod', Unknown: 'unknown',
  } as const;
  export type RuntimeKind = typeof RuntimeKind[keyof typeof RuntimeKind];
  ```
  Same string values as Python (cross-SDK event portability — events serialize with these strings).
- [ ] `export function resolveRuntime(): RuntimeKind` — same cascade as Python's `resolve_runtime`. Reads `process.env`.
- [ ] In browser bundles, this returns `'unknown'` (process.env doesn't have AWS_LAMBDA_FUNCTION_NAME). Tests cover Node only.
- [ ] 12 tests port from Python. Use `vi.stubEnv()` per test or restore env in `afterEach`.
- [ ] Commit — `feat(compute-ts): runtime resolver — serverless > k8s > cloud_detect IaaS`.

---

### Task 4: `core/fargate-metadata.ts` — ECS task metadata helper

**Python sibling:** `fargate_metadata.py` (commit `08f2b22`).

- [ ] Types + state:
  ```ts
  export interface FargateTaskMetadata { vcpuCount: number; memoryBytesLimit: number; }
  let cached: FargateTaskMetadata | null = null;
  let resolved = false;
  let warned = false;
  export function _resetForTests() { cached = null; resolved = false; warned = false; }
  ```
- [ ] `export async function fetchFargateMetadata(): Promise<FargateTaskMetadata | null>` — reads `process.env.ECS_CONTAINER_METADATA_URI_V4` || `process.env.ECS_CONTAINER_METADATA_URI`; fetches `${url}/task` with a 250 ms timeout via `AbortController`; caches the result.
- [ ] **The load-bearing line:** `const memoryBytes = memMib * 1024 * 1024` (binary MiB → bytes per Decision #7).
- [ ] Failure modes: missing env → `null`; fetch error → `null` + WARN_ONCE; malformed Limits → `null`.
- [ ] 6 tests use `nock` or `vi.spyOn(global, 'fetch')`.
- [ ] Commit — `feat(compute-ts): Fargate ECS task metadata helper`.

---

### Task 5: Catalog integrity tests

**Files:** `typescript/test/compute-catalog-integrity.test.ts`.

- [ ] Load via `import catalog from '../src/data/compute_prices.json' assert { type: 'json' };` (or `fs.readFileSync` if the assert form isn't enabled).
- [ ] 13 cases port from Python `test_compute_catalog_integrity.py`: structure, Decimal-parseable via `new Decimal(...)`, freshness soft-warn (use `console.warn` not `expect.fail`), provider/runtime presence, arch-keying, ARM < x86, top SKUs present, every `*_usd` and `vcpu_count` parses.
- [ ] Commit — `test(compute-ts): catalog integrity tests`.

---

### Task 6: `pricing/compute-pricing.ts` — engine + 11 billing models + 5-tier ladder

**Python sibling:** `compute_pricing.py` (commit `e379f40` + the `3f5b327` Fargate rate refresh).

- [ ] Types:
  ```ts
  export interface ComputeCost {
    costUsd: Decimal;
    pricingSource: string;
    costConfidence: 'exact' | 'computed' | 'estimated' | 'unknown';
  }
  export class ComputePricingEngine {
    private catalog: Record<string, any>;
    readonly catalogVersion: string;
    constructor(catalogOverride?: object) { ... }
  }
  ```
- [ ] Constants:
  ```ts
  const GB_DECIMAL = new Decimal('1000000000');           // 10^9
  const GIB_BINARY = new Decimal(1024 * 1024 * 1024);     // 2^30
  const HOUR_S = new Decimal(3600);
  const MS_PER_S = new Decimal(1000);
  ```
- [ ] `HARDCODED` map: copy values from Python `_HARDCODED` block (post-refresh — Fargate is `'0.0000112444'` / `'0.0000012347'`).
- [ ] Constructor loads `data/compute_prices.json` (via `import` or `fs.readFileSync`); failure → empty catalog + WARN_ONCE.
- [ ] Public method:
  ```ts
  resolveComputeCost(
    details: Record<string, any>,
    cloudEnv: CloudEnv,
    overrides: Record<string, string>,
    windowS?: Decimal,
  ): ComputeCost
  ```
  Wrap the dispatch in `try/catch` for Tier 5 fail-silent — return `{ costUsd: new Decimal(0), pricingSource: 'compute_catalog:error:...', costConfidence: 'unknown' }`.
- [ ] Dispatch on `details.billing_model` — same table as Python's `_dispatch`.
- [ ] Per-billing-model methods. **Critical: Fargate uses `GIB_BINARY` as divisor**, NEVER `GB_DECIMAL`. The `test_fargate_uses_binary_gib_divisor` test pins this.
- [ ] Module-level warned-modes Set + `_resetWarningStateForTests()` (convention §11).
- [ ] 16 tests port from Python `test_compute_pricing.py`.
- [ ] Commit — `feat(compute-ts): pricing engine — per-billing-model math + degradation ladder`.

---

### Task 7: `core/compute-accountant.ts` — per-task accumulator

**Python sibling:** `compute_accountant.py` (commit `33cef0f`).

- [ ] Class:
  ```ts
  export class ComputeAccountant {
    private frozen = false;
    private startCpuUsec: number | null = null;
    readonly runtime: RuntimeKind;
    // ... opt fields (lambdaMemoryMb, fargateVcpu, fargateMemoryMib, architecture,
    //                 initializationType, region) via constructor options bag
    constructor(opts: { runtime: RuntimeKind; lambdaMemoryMb?: number; ... });
    snapshotStart(): void;
    snapshotEndAndBuild(durationMs: number): Record<string, any> | null;
    buildServerlessEvent(durationMs: number, memoryBytesPeak: number): Record<string, any> | null;
  }
  ```
  **No mutex needed** — TS is single-threaded; the freeze flag is sufficient.
- [ ] `_detectArch()` uses `process.arch` (`'arm64'` → `'arm64'`; else `'x86_64'`). In browser bundles default to `'x86_64'`.
- [ ] capture spec §6 case 6 fallback: `memory.peak` missing → fall through to `memory.current`.
- [ ] 8 tests port from Python.
- [ ] Commit — `feat(compute-ts): per-task accountant — cgroup start/end snapshots, single event`.

---

### Task 8: Wire `ComputeAccountant` into Task + extend tracker finalize

**Python sibling:** commit `91beccc`.

- [ ] Add to `Task`:
  ```ts
  _compute?: ComputeAccountant;  // in-memory only; never serialized
  ```
  Mirror the existing `_network` field's serialization-exclusion pattern (likely a `JSON.stringify` `replacer` or explicit field selection in `toDict`).
- [ ] In `core/tracker.ts::finalizeCosts`, after the existing egress finalize block, call `this.finalizeCompute(task)` wrapped in its own `try/catch` for Tier 5.
- [ ] `finalizeCompute(task)` does three things (mirror Python `_finalize_compute`):
  1. Long-running runtime → call `snapshotEndAndBuild` and `storage.insertEvent({event_type: 'compute_cost', cost_usd: new Decimal(0), details: { ..., cost_pending: true }})`.
  2. Walk events; for each `compute_cost` event with `details.cost_pending === true`, call `engine.resolveComputeCost(...)`, then `storage.updateEvent(...)` with new cost / source / confidence / `pricing_version: \`compute:${engine.catalogVersion}\``. Strip `cost_pending`.
  3. DELTA-based total adjustment: `task.computeCostUsd = task.computeCostUsd.plus(delta)`, `task.totalCostUsd = task.totalCostUsd.plus(delta)`. **Do NOT recompute total_cost_usd** from scratch — preserves retry_marker costs.
- [ ] Tracker constructor: add `computePricing: ComputePricingEngine`, `computeBillingOverrides: Record<string, string>`, `k8sNodeAware: boolean` fields.
- [ ] Integration test — port `test_compute_auto_emission_long_running.py`. Use `vi.spyOn` on the cgroup reader module to override reads per test.
- [ ] `pnpm test` — fix any existing tracker test that hardcoded a specific `totalCostUsd` (same delta-not-recompute fix as Python).
- [ ] Commit — `feat(compute-ts): auto-emit + back-fill compute_cost events at task finalize`.

---

### Task 9: Handler wraps + Options knobs

**Python sibling:** `compute_wrap.py` (commit `3babf79`).

- [ ] Extend `InitOptions` interface with:
  ```ts
  computeBillingOverrides?: Record<string, string>;
  k8sNodeAware?: boolean;
  ```
  Thread both through `init()` to `new CostTracker({ ... })`.
- [ ] `adapters/compute-wrap.ts`:
  ```ts
  export function wrapLambdaHandler<E, C, R>(
    handler: (event: E, context: C) => Promise<R> | R,
  ): (event: E, context: C) => Promise<R>
  ```
  Reads env vars, constructs `ComputeAccountant`, attaches to active task, times with `performance.now()`, persists event with `cost_pending: true`. Use `try { return await handler(...) } finally { ... }` so the event is ALWAYS emitted — handler exceptions are re-thrown after the event lands (capture spec §6 case 7).
  
  **No active task → pass through**: `if (!getCurrentTask()) return handler(event, context);` (capture spec §6 case 2).
- [ ] Stub `wrapCloudRunHandler`, `wrapCloudFunctionsHandler`, `wrapAzureFunctionsHandler`, `wrapVercelHandler` sharing an internal `timeAndCapture` helper.
- [ ] Export all five from `index.ts`.
- [ ] 6 tests port from Python `test_compute_wrap.py`.
- [ ] Commit — `feat(compute-ts): serverless handler wraps + Options knobs`.

---

### Task 10: Property invariants + Decision #9/#10 idle-gap + cross-runtime matrix

**Python sibling:** commit `c4c3bfb`.

- [ ] Three test files (vitest):
  - `compute-invariants.test.ts` — `it.each` table over all 11 billing models for the 6 invariants.
  - `compute-idle-gap.test.ts` — Decision #9 (EC2) + #10 (Fargate). Failure message references the decision number.
  - `compute-cross-runtime-matrix.test.ts` — one test per billing_model with canonical fixture + expected pricing_source substring.
- [ ] `pnpm test` must be all green.
- [ ] Commit — `test(compute-ts): property invariants + Decision #9/#10 idle-gap + cross-runtime matrix`.

---

### Task 11: Drift check vs Python canonical catalog

- [ ] Test using `fs.readFileSync` on both `typescript/src/data/compute_prices.json` and `python/src/dexcost/data/compute_prices.json`, assert byte-equal. Skip gracefully when running from a published npm package (use `fs.existsSync` on the Python path).
- [ ] Commit — `test(compute-ts): drift check vs Python canonical catalog`.

---

## Self-Review

Same equivalence checklist as the Go + Rust plans. Each TS module is a faithful translation of its Python sibling — same dispatch table, same `HARDCODED` constants (post-refresh), same fail-silent discipline, same idempotent freeze flag, same `RuntimeKind` string values.

**Cross-SDK invariants pinned by Task 10:**
- Fargate binary GiB divisor (~4.86% silent over-attribution bug prevented in TS too)
- ARM < x86 on Lambda + Fargate
- Decisions #9/#10 idle invisible
- All 11 billing models reachable via dispatch

**TypeScript-specific considerations:**
- **Browser-safety:** the cgroup reader, Fargate metadata, IMDS probes, handler wraps, and the compute-runtime resolver are all node-only. Each module no-ops in browser bundles via a `typeof process !== 'undefined' && process.versions?.node` guard. The pricing engine itself is pure — runs anywhere; only the measurement side is node-gated.
- **Runtime-agnostic:** Node, Bun, and Deno all expose `process.env` and `fs.promises`. Test under Node 18+ as the primary target; document the others in the README.
- **No locks:** TS is single-threaded — the accountant freeze flag is sufficient. No `Mutex` analog needed.
- **`Decimal.js` precision:** the existing egress pricing engine already uses it; same patterns apply (string-string ratio, never via `new Decimal(0.1)` which goes through float).
- **Module resolution:** for the catalog JSON import, the build must include `*.json` in the publish set (`package.json files` array — already in place for `egress_prices.json`).

**Known follow-ups:** GCP price re-verification (separate task in flight); K8s `/api/v1/nodes` opt-in probe across all SDKs; launch-prerequisite full catalog coverage.
