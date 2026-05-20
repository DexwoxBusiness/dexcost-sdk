# Compute Foundation (v1 capture + v2 cost) — Go SDK — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Port the Phase 1 compute capture + cost-attribution layer from Python to Go. Mirror the Python TDD task breakdown step-for-step, translated to Go idioms.

**Reference implementation:** the Python SDK shipped on this branch (`claude/network-capture-cost-attribution-bEszJ`). Every Go module here has a direct Python sibling — read the Python file before writing the Go equivalent so design intent and the spec contracts transfer cleanly. The specs (`docs/superpowers/specs/2026-05-21-compute-capture-design.md` and `docs/superpowers/specs/2026-05-21-compute-cost-attribution-design.md`) and the decisions log (`docs/superpowers/decisions/2026-05-20-compute-foundation-decisions.md`) are the binding contracts; the Go port honours them through Python equivalence.

**Architecture:** five new files mirroring Python — `cloud/cloud_detect.go` extended with `InstanceType`, `core/cgroup_reader.go`, `core/compute_runtime.go`, `core/fargate_metadata.go`, `pricing/compute_pricing.go`, `core/compute_accountant.go`. Tracker's existing `finalizeCosts` flow gains a compute back-fill step parallel to its egress one.

**Tech stack:** Go 1.21+, `github.com/shopspring/decimal`, stdlib `net/http` / `os` / `sync` / `encoding/json`. No new dependencies — `decimal` is already in `go.mod`.

**Run tests with:** `cd go && go test ./...`

**Pre-requisites already landed on this branch:**
- v2 network capture shipped (`cloud.CloudEnv`, `pricing.EgressPricingEngine`, `core.NetworkAccountant` + deferred-cost finalize pattern).
- `EventTypeComputeCost` and `Task.ComputeCostUSD` already defined in `core/models.go`.
- `tracker.finalizeCosts` already sums `compute_cost` events into `Task.ComputeCostUSD` (line 519/541) — the existing aggregation works once events get a real `CostUSD`.
- `go/pricing/data/compute_prices.json` already synced from the Python canonical via `scripts/sync_compute_catalog.sh` (commit `bc3b45d`).

---

### Task 1: Extend `cloud.CloudEnv` with `InstanceType` + IMDS instance-type extraction

**Files:**
- Modify: `go/cloud/cloud_detect.go`
- Test: `go/cloud/cloud_detect_instance_type_test.go` (create)

**Python sibling:** `python/src/dexcost/cloud_detect.py` (commit `a613924`).

- [ ] **Step 1: Write the failing test** — `go/cloud/cloud_detect_instance_type_test.go`. Test cases (port from Python `test_cloud_detect_instance_type.py`):
  - `TestCloudEnvCarriesInstanceTypeField` — struct literal with `InstanceType: "c7g.xlarge"`
  - `TestAWSProbeReturnsInstanceType` — mock `http.Client` so `_probeAWS` returns `CloudEnv{..., InstanceType: "c7g.xlarge"}`
  - `TestAWSProbeInstanceTypeFailureDoesNotLoseRegion`
  - `TestGCPProbeReturnsMachineType` — strip the `projects/.../machineTypes/` prefix
  - `TestAzureProbeReturnsVMSize` — `vmSize` lives in the existing JSON payload
- [ ] **Step 2: Run test to verify it fails** — `go test ./cloud/... -run InstanceType`
- [ ] **Step 3: Extend the `CloudEnv` struct** — add `InstanceType string` after `Source` (Go style: zero-value `""` means unresolved).
- [ ] **Step 4: Extend each probe** to fetch instance-type:
  - `_probeAWS`: after the region GET, hit `/latest/meta-data/instance-type` with the same IMDSv2 token. Wrap the call separately so a failure doesn't lose the region.
  - `_probeGCP`: after region resolves, GET `/computeMetadata/v1/instance/machine-type` with `Metadata-Flavor: Google`; strip the prefix via `path.Base(s)`.
  - `_probeAzure`: read `.compute.vmSize` from the existing JSON payload — zero extra HTTP cost.
- [ ] **Step 5: Update `startBackgroundDetection`** to preserve `InstanceType` when stitching env+IMDS results (mirror Python's `_background` function).
- [ ] **Step 6: Verify** — `go test ./cloud/...` (new test passes; existing tests still green).
- [ ] **Step 7: Commit** — `feat(compute-go): extend CloudEnv with InstanceType from IMDS (Decision #3)`.

---

### Task 2: `core/cgroup_reader.go` — cgroup v2 file readers

**Files:**
- Create: `go/core/cgroup_reader.go`
- Test: `go/core/cgroup_reader_test.go`

**Python sibling:** `python/src/dexcost/cgroup_reader.py` (commit `1609faf`).

- [ ] **Step 1: Write the failing test.** Mirror the Python test cases:
  - `TestReadCPUStatParsesUsageUsec`
  - `TestReadCPUMaxWithQuota`
  - `TestReadCPUMaxQuotaFraction` (256/1024 = 0.25 vCPU)
  - `TestReadCPUMaxUnlimited` (literal `"max"`)
  - `TestReadMemoryPeak`, `TestReadMemoryMaxFinite`, `TestReadMemoryMaxUnlimited`, `TestReadMemoryCurrent`
  - `TestMissingFilesReturnNone` (return zero-value `CPUStat{}` + `false`, or use pointer types)
  - `TestMalformedCPUStatReturnsNone`
  - `TestMemoryPeakAbsentWhenKernelTooOld` — reader does NOT fabricate

  Use `t.TempDir()` for fixture files; expose the cgroup root path via a package-level variable so tests can swap it (`cgroupRoot = tmpDir` per test).
- [ ] **Step 2: Run test to verify it fails** — file doesn't exist.
- [ ] **Step 3: Implement `cgroup_reader.go`**.
  - Types: `CPUStat struct { UsageUsec int64 }` and `CPUMax struct { QuotaUS, PeriodUS int64; VCPUCount float64 }`.
  - Functions return `(value, ok bool)` pairs (Go idiom for the Python `None` sentinel): `ReadCPUStat() (CPUStat, bool)`, `ReadCPUMax() (CPUMax, bool)`, `ReadMemoryPeak() (int64, bool)`, `ReadMemoryMax() (int64, bool)`, `ReadMemoryCurrent() (int64, bool)`.
  - Package-level `cgroupRoot = "/sys/fs/cgroup"` overridable for tests.
  - `ReadCPUMax` falls back to `runtime.NumCPU()` when the literal is `"max"`.
  - Fail-silent on missing files / malformed input (return `_, false`).
- [ ] **Step 4: Verify + commit** — `feat(compute-go): cgroup v2 file readers`.

---

### Task 3: `core/compute_runtime.go` — runtime cascade

**Files:**
- Create: `go/core/compute_runtime.go`
- Test: `go/core/compute_runtime_test.go`

**Python sibling:** `python/src/dexcost/compute_runtime.py` (commit `afbc007`).

- [ ] **Step 1: Test cases** (port the 12 Python tests):
  - Lambda env > Fargate env > Cloud Run (FUNCTION_TARGET distinguishes Cloud Functions Gen2) > Azure Functions > Vercel
  - `KUBERNETES_SERVICE_HOST` wins over IaaS DMI signals
  - Falls through to `cloud.GetCloudEnv()` for EC2/GCE/Azure VM
  - `UNKNOWN` when nothing matches
  - Serverless wins over IaaS (Lambda+AWS DMI → Lambda)

  Use `t.Setenv` for env-var setup; reset `cloud.SetResultForTests(env)` for the IaaS-fallback cases.
- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Implement.**
  - `type RuntimeKind string` with constants: `RuntimeLambda`, `RuntimeFargate`, `RuntimeEC2`, `RuntimeCloudRun`, `RuntimeCloudFunctions`, `RuntimeGCE`, `RuntimeAzureFunctions`, `RuntimeAzureVM`, `RuntimeVercel`, `RuntimeK8sPod`, `RuntimeUnknown`. String values match Python's `RuntimeKind` enum (`"lambda"`, `"fargate"`, …) so events stay portable across SDKs.
  - `func ResolveRuntime() RuntimeKind { ... }` — cascade in the same priority order as Python (capture spec §5.5).
- [ ] **Step 4: Commit** — `feat(compute-go): runtime resolver — serverless > k8s > cloud_detect IaaS`.

---

### Task 4: `core/fargate_metadata.go` — ECS task metadata helper

**Files:**
- Create: `go/core/fargate_metadata.go`
- Test: `go/core/fargate_metadata_test.go`

**Python sibling:** `python/src/dexcost/fargate_metadata.py` (commit `08f2b22`).

- [ ] **Step 1: Test cases.** Use `httptest.NewServer` to mock the ECS endpoint; set `ECS_CONTAINER_METADATA_URI_V4` to the test server URL.
  - `TestReturnsVCPUAndMemory` — payload `{"Limits": {"CPU": 0.5, "Memory": 1024}}` → `VCPUCount=0.5`, `MemoryBytesLimit = 1024 * 1024 * 1024` (**MiB → bytes via binary GiB per Decision #7**).
  - `TestNoEnvVarReturnsNil`
  - `TestUnreachableReturnsNilAndLogsOnce` — server returns 500; second call still nil, only one log.
  - `TestCachedAfterFirstSuccess` — count test-server hits; second `FetchFargateMetadata()` must NOT hit the server.
  - `TestMalformedLimitsReturnsNil`
  - `TestV3URIAlsoWorks` (`ECS_CONTAINER_METADATA_URI` without `_V4`).
- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Implement.**
  - `type FargateTaskMetadata struct { VCPUCount float64; MemoryBytesLimit int64 }`
  - `var ( cacheMu sync.Mutex; cached *FargateTaskMetadata; resolved bool; warned bool )`
  - `func FetchFargateMetadata() *FargateTaskMetadata { ... }` — returns `nil` when not on Fargate / unreachable / malformed.
  - `func ResetForTests()` clears the cache.
  - Conversion: `memoryBytes := memMiB * 1024 * 1024` (the load-bearing line — the binary divisor prevents the silent ~4.86% over-attribution bug).
- [ ] **Step 4: Commit** — `feat(compute-go): Fargate ECS task metadata helper (MiB->bytes per Decision #7)`.

---

### Task 5: Compute catalog (already in place)

**Files:**
- Already synced: `go/pricing/data/compute_prices.json` (commit `bc3b45d`)
- Test: `go/pricing/compute_catalog_integrity_test.go` (create)

**Python sibling:** `python/tests/test_compute_catalog_integrity.py` (commit `dc4f419`).

The JSON is already in place from the catalog sync script. This task only adds the Go-side integrity tests so a future drift can't go undetected on the Go side.

- [ ] **Step 1: Tests.** Read the catalog via `embed.FS` (or `os.ReadFile` for tests). Mirror Python's 13 tests:
  - `TestCatalogParsesAsJSON`
  - `TestMetaHasRequiredDefaultKeys` — every `default_*_usd` parses as `decimal.Decimal`
  - `TestEveryProviderHasLastVerified` — soft-warn at 180 days via `t.Logf` (NOT `t.Errorf`)
  - `TestAllProvidersAndRuntimesPresent` — AWS{lambda, fargate, ec2}, GCP{cloud_run, cloud_functions, gce}, Azure{functions_consumption, vm}, vercel{fluid}
  - `TestLambdaHasBothArchitectures` + `TestFargateHasBothArchitectures`
  - `TestARMCheaperThanX86OnLambda` + same on Fargate
  - `TestTopInstanceTypesPresentForEC2USEast1` (`c7g.xlarge`, `m7i.large`, `t3.medium`)
  - `TestTopInstanceTypesPresentForGCEUSCentral1` (`n2-standard-2`, `e2-standard-4`)
  - `TestTopInstanceTypesPresentForAzureVMEastus` (`Standard_D2s_v3`, `Standard_B2ms`)
  - `TestEveryRateIsDecimalParseable` — walk the tree, every `*_usd` / `vcpu_count` parses cleanly
- [ ] **Step 2: Commit** — `test(compute-go): catalog integrity tests`.

---

### Task 6: `pricing/compute_pricing.go` — engine + 11 billing models + 5-tier ladder

**Files:**
- Create: `go/pricing/compute_pricing.go`
- Test: `go/pricing/compute_pricing_test.go`

**Python sibling:** `python/src/dexcost/compute_pricing.py` (commit `e379f40`). This is the heart of the v2 layer; the Python file is the source of truth — translate it line-for-line.

- [ ] **Step 1: Tests** (port all 16 Python tests):
  - Lambda x86 canonical case + ARM cheaper-than-x86
  - **Fargate binary GiB divisor pin** (the load-bearing test — `Decimal(1024*1024*1024) / Decimal(1024*1024*1024) == Decimal(1)`, NOT `/ Decimal(10^9) == 1.073741824`)
  - Cloud Run default `estimated` + `compute_catalog:cloud_run:request_based_default`
  - Cloud Run instance override `computed` + `instance_override` suffix
  - Azure Functions canonical
  - Vercel active-CPU approximation
  - EC2 share-factor math
  - K8s pod limits × duration math
  - Tier 2 unknown-region → per-runtime default
  - Tier 4 missing-catalog → hardcoded
  - Tier 5 computation failure → `cost_usd = 0`
  - Unknown billing model → `cost_usd = 0`
  - **`TestDecimalNoFloatDriftPerConversion`** — both Fargate binary and Lambda decimal divisors stay Decimal
  - `TestWarnOncePerFailureMode`
  - `TestCatalogVersionExposed`
- [ ] **Step 2: Implement.**
  - `type ComputeCost struct { CostUSD decimal.Decimal; PricingSource string; CostConfidence string }`
  - `type ComputePricingEngine struct { catalog map[string]any; catalogVersion string; ... }`
  - `NewComputePricingEngine() *ComputePricingEngine` — loads from `embed.FS` (`//go:embed data/compute_prices.json`).
  - Constants at top of file:
    ```go
    var (
      gbDecimal = decimal.NewFromInt(1_000_000_000)
      gibBinary = decimal.NewFromInt(1024 * 1024 * 1024)
      hourS     = decimal.NewFromInt(3600)
      msPerS    = decimal.NewFromInt(1000)
    )
    ```
  - `_HARDCODED` map mirroring Python `_HARDCODED` — values copied from `_meta` defaults so they stay in sync.
  - Public method:
    ```go
    func (e *ComputePricingEngine) ResolveComputeCost(
      details map[string]any,
      cloudEnv cloud.CloudEnv,
      overrides map[string]string,
      windowS decimal.Decimal,
    ) ComputeCost { ... }
    ```
    Wrap the entire body in a `defer func() { if r := recover(); r != nil { ... } }()` for Tier 5 fail-silent.
  - Dispatch on `details["billing_model"]` to per-model unexported methods (`lambdaCost`, `fargateCost`, `cloudRunRequestCost`, …).
  - Each per-model method: resolve rate via the 5-tier ladder, apply the §6 math from the spec.
  - Warn-once: package-level `warnedModes map[string]struct{}` + `warnMu sync.Mutex` + `ResetWarningStateForTests()` helper (convention §11).
- [ ] **Step 3: Verify + commit** — `feat(compute-go): pricing engine — per-billing-model math + degradation ladder`.

---

### Task 7: `core/compute_accountant.go` — per-task accumulator

**Files:**
- Create: `go/core/compute_accountant.go`
- Test: `go/core/compute_accountant_test.go`

**Python sibling:** `python/src/dexcost/compute_accountant.py` (commit `33cef0f`).

- [ ] **Step 1: Tests** (port 8 Python cases):
  - `TestLongRunningRuntimeEmitsOneEventWithDiff` — usage_usec diff 1M→4M → 3 vCPU-seconds
  - `TestServerlessLambdaEmitsInvocationEvent` — lambda memory MB → decimal-bytes
  - `TestSecondCallPerTaskNoOps` — capture §5.3 idempotency
  - `TestFargatePassesExplicitVCPUAndMemory` — MiB → binary bytes
  - `TestNonLinuxFallbackEmitsWithZeroVCPUSeconds` — cgroup nil → `VCPUSecondsUsed=0`, `VCPUCount` from `runtime.NumCPU()`
  - `TestMemoryPeakFallsBackToCurrentWhenMissing` — capture §6 case 6
  - `TestArchitectureAutoDetectedFromRuntimeGOARCH` — `runtime.GOARCH == "arm64"` → `"arm64"`, else `"x86_64"`
  - `TestLongRunningSnapshotFreezeAfterFinalize`
- [ ] **Step 2: Implement.**
  - `type ComputeAccountant struct { mu sync.Mutex; frozen bool; Runtime RuntimeKind; LambdaMemoryMB int; FargateVCPU float64; FargateMemoryMiB int; Architecture string; InitializationType string; Region string; startCPUUsec int64 }`
  - Constructor: `func NewComputeAccountant(runtime RuntimeKind, opts ...ComputeAccountantOption) *ComputeAccountant` with functional options (`WithLambdaMemoryMB(int)`, etc.) for the optional fields.
  - `SnapshotStart()` reads `ReadCPUStat()` and stores `startCPUUsec`.
  - `SnapshotEndAndBuild(durationMS int64) map[string]any` — diff cpu.stat, read memory.peak (fall through to memory.current), read cpu.max, build the details dict with `"cost_pending": true`. Returns `nil` if `frozen`.
  - `BuildServerlessEvent(durationMS, memoryPeak int64) map[string]any` — per-runtime memory unit (Lambda decimal MB → bytes, Fargate MiB → binary bytes, others from cgroup `memory.max`).
  - `_billingModelFor(runtime)` mirrors Python.
  - `_detectArch()` uses `runtime.GOARCH` (Go stdlib).
- [ ] **Step 3: Commit** — `feat(compute-go): per-task accountant — cgroup start/end snapshots, single event`.

---

### Task 8: Wire `ComputeAccountant` into Task + extend tracker finalize

**Files:**
- Modify: `go/core/models.go` (add `Compute *ComputeAccountant` field; unexported via lowercase name `compute` to mirror Python's `_compute`)
- Modify: `go/core/tracker.go`
- Test: `go/core/compute_auto_emission_long_running_test.go`

**Python sibling:** Task 8 (commit `91beccc`).

- [ ] **Step 1: Add `compute *ComputeAccountant`** field to `Task` (unexported, in-memory only, never serialised — mirrors `_network`). Default zero-value `nil`.
- [ ] **Step 2: Tracker finalize** — `tracker.finalizeCosts` already has an egress block; add a `finalizeCompute(task *Task)` method called immediately after, wrapped in its own `defer recover()` for Tier 5 fail-silent.
- [ ] **Step 3: `finalizeCompute` does three things** (mirror Python `_finalize_compute`):
  1. If the task's `compute` accountant is a long-running runtime (Fargate/EC2/GCE/Azure VM/K8s pod), call `SnapshotEndAndBuild` and persist a `compute_cost` event with `CostUSD = decimal.Zero` and `cost_pending: true`.
  2. Walk events for the task; for each `compute_cost` event with `details["cost_pending"] == true`, call `engine.ResolveComputeCost(...)` and update the event (set `CostUSD`, `PricingSource`, `CostConfidence`, `PricingVersion = "compute:<catalog_version>"`, strip `cost_pending`).
  3. Adjust `task.ComputeCostUSD` and `task.TotalCostUSD` by the DELTA per back-filled event (NOT a full recompute — preserves retry_marker costs already summed by the main loop).
- [ ] **Step 4: Tracker construction** — add `computePricingEngine *pricing.ComputePricingEngine`, `computeBillingOverrides map[string]string`, `k8sNodeAware bool` fields. Default empty / false.
- [ ] **Step 5: Integration test** — port Python's `test_ec2_task_emits_and_prices` and `test_unknown_runtime_emits_no_event` to Go using the same monkey-patch pattern (override `ReadCPUStat` etc. via package-level test variables, or pass an interface).
- [ ] **Step 6: Run full test suite** — `cd go && go test ./...`. Fix any regression in existing tracker tests that hit `TotalCostUSD` expectations (mirror Python's "adjust by delta, not recompute" pattern to preserve retry costs).
- [ ] **Step 7: Commit** — `feat(compute-go): auto-emit + back-fill compute_cost events at task finalize`.

---

### Task 9: Handler-wrap (`adapters/lambda.go`) + Options knobs

**Files:**
- Modify: `go/adapters/lambda.go` (already exists for `track_browser`-style integration — extend with compute capture)
- Modify: `go/options.go` (add `ComputeBillingOverrides map[string]string`, `K8sNodeAware bool` fields)
- Test: `go/adapters/lambda_compute_test.go`

**Python sibling:** `python/src/dexcost/compute_wrap.py` (commit `3babf79`).

- [ ] **Step 1: Extend `Options`** with the two knobs; wire them through `dexcost.Init(...)` to the global tracker. Mirror Python's `init()` signature.
- [ ] **Step 2: Implement `WrapLambdaHandler`** — Go-idiomatic equivalent of the Python decorator. Signature: `func WrapLambdaHandler[T any, R any](fn func(ctx context.Context, event T) (R, error)) func(context.Context, T) (R, error)`. Reads env vars (`AWS_LAMBDA_FUNCTION_MEMORY_SIZE`, `AWS_LAMBDA_INITIALIZATION_TYPE`, `AWS_REGION`); constructs `ComputeAccountant` with `RuntimeLambda`; attaches to `task.compute`; times the handler via `time.Now()` deltas; on exit reads `ReadMemoryPeak()` and persists the event with `cost_pending: true`.
- [ ] **Step 3: Stub wraps** for the other four serverless runtimes (`WrapCloudRunHandler`, `WrapCloudFunctionsHandler`, `WrapAzureFunctionsHandler`, `WrapVercelHandler`) — each is a thin shim around a shared internal `timeAndCapture` helper, same shape as Python's `_time_and_capture`.
- [ ] **Step 4: Tests** — port Python's 6 wrap tests:
  - Lambda wrap emits event with `cost_pending: true`
  - No active task → pass-through (no orphan event)
  - Handler error → event STILL emitted, error re-raised (capture spec §6 case 7)
  - `Options.ComputeBillingOverrides` threaded through `Init`
  - `Options.K8sNodeAware` threaded through
  - All five wraps exported from the top-level package
- [ ] **Step 5: Commit** — `feat(compute-go): serverless handler wraps + Options knobs`.

---

### Task 10: Property invariants + Decision #9/#10 idle-gap + cross-runtime matrix

**Files:**
- Test: `go/pricing/compute_invariants_test.go`
- Test: `go/core/compute_idle_gap_test.go`
- Test: `go/pricing/compute_cross_runtime_matrix_test.go`

**Python sibling:** Task 10 (commit `c4c3bfb`).

No production code — only tests. Port the three Python files line-for-line:

- [ ] **Step 1: Property invariants** — table-driven test (Go idiom) over all 11 billing models for:
  - Invariant 1: `CostUSD >= 0`
  - Invariant 3: linearity in duration (serverless) + memory (Fargate)
  - Invariant 4: ARM < x86 on Lambda + Fargate
  - Invariant 5: `CostConfidence ∈ {"computed", "estimated"}`
  - Invariant 6: `PricingSource` starts with `"compute_catalog:"`
- [ ] **Step 2: Decision #9 + #10 idle-gap tests** — the load-bearing customer-facing contract:
  - EC2: two 60s tasks with 600s idle between them → assert dexcost total < `(720/3600) * 0.1450`
  - Fargate: 3 tasks back-to-back then 50min idle tail → assert dexcost total < `4.0 * 3030 * <fargate_rate>`
  - Failure messages must point to the decision (`"...by design (Decision #9). If this test starts failing because total grew, check whether a refactor added synthetic idle pseudo-tasks."`)
- [ ] **Step 3: Cross-runtime regression matrix** — one sub-test per billing model with a canonical fixture asserting positive cost + expected `PricingSource` substring.
- [ ] **Step 4: Run full suite** — `cd go && go test ./...` must be all green.
- [ ] **Step 5: Commit** — `test(compute-go): property invariants + Decision #9/#10 idle-gap + cross-runtime matrix`.

---

### Task 11: Confirm sync script + cross-SDK contract test

**Files:**
- Test: `go/pricing/compute_catalog_sync_test.go`

**Python sibling:** `scripts/sync_compute_catalog.sh` (commit `bc3b45d`) — already runs against Go.

- [ ] **Step 1: Drift-check test** — assert the bundled Go catalog `go/pricing/data/compute_prices.json` is byte-identical to `python/src/dexcost/data/compute_prices.json`. Run from the test by reading both files via relative paths from the repo root (or skip if the Python file isn't reachable in CI mono-repo layout). Failure message: `"run: bash scripts/sync_compute_catalog.sh"`.
- [ ] **Step 2: Commit** — `test(compute-go): drift check vs Python canonical catalog`.

---

## Self-Review

**Spec coverage** — every section of both compute specs is covered by a task (same mapping as the Python plan §11 "Self-Review" table; one-to-one task correspondence makes the mapping inherit).

**Python equivalence checklist** — for each task, the Go file should be a structurally faithful translation of its Python sibling:

| Go module | Python sibling | Equivalence test |
|---|---|---|
| `cloud.CloudEnv` (extended) | `cloud_detect.py` | Same probe ordering; same fail-silent on instance-type fetch |
| `core/cgroup_reader.go` | `cgroup_reader.py` | Same return-on-missing/malformed; same `runtime.NumCPU()` fallback |
| `core/compute_runtime.go` | `compute_runtime.py` | Same cascade priority; same `RuntimeKind` string values (cross-SDK event portability) |
| `core/fargate_metadata.go` | `fargate_metadata.py` | MiB → binary bytes (the Decision #7 conversion) |
| `pricing/compute_pricing.go` | `compute_pricing.py` | Same dispatch table; same `_HARDCODED` constants; same 5-tier ladder; same warn-once discipline |
| `core/compute_accountant.go` | `compute_accountant.py` | Same idempotent freeze flag; same long-running vs serverless paths |
| Tracker `finalizeCompute` | `_finalize_compute` | Same delta-based total adjustment (not full recompute) |
| `adapters/lambda.go` wraps | `compute_wrap.py` | Same handler-exception-still-emits behavior |

**Cross-SDK invariants pinned by Task 10 tests:**
- Fargate binary GiB divisor (~4.86% silent over-attribution bug prevented in Go too)
- ARM < x86 on Lambda + Fargate
- Decisions #9/#10 idle invisible
- All 11 billing models reachable

**Known follow-ups (out of scope, not gaps):**
- Rust port (`docs/superpowers/plans/2026-05-22-compute-foundation-rust.md`)
- TypeScript port (`docs/superpowers/plans/2026-05-22-compute-foundation-typescript.md`)
- Both follow this Go plan's structure, with Rust → `tokio::sync::Mutex` for the cache + `Arc<RwLock>` for shared state, TS → no locks needed (single-threaded event loop) + a node-only cgroup reader (won't run in browser; safe to no-op).
- K8s `/api/v1/nodes` opt-in probe — flag wired in Task 9, the probe HTTP call deferred to a later focused task across all SDKs.
- Launch-prerequisite full catalog coverage (every commercial region, top ~50 SKUs per IaaS provider) — separate data-entry task tracked at the repo level.
