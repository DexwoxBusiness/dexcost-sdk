# Compute Foundation (v1 capture + v2 cost) — Rust SDK — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Port the Phase 1 compute capture + cost-attribution layer to the Rust SDK, mirroring the Python implementation (commits `a613924` … `bc3b45d` + the `3f5b327` price refresh).

**Reference implementations:**
- Python (source of truth): `python/src/dexcost/` — read the equivalent Python file BEFORE writing each Rust module.
- Go plan: `docs/superpowers/plans/2026-05-22-compute-foundation-go.md` — same 11-task structure; Rust diverges only on language idioms.
- Specs: `docs/superpowers/specs/2026-05-21-compute-{capture,cost-attribution}-design.md`
- Decisions: `docs/superpowers/decisions/2026-05-20-compute-foundation-decisions.md`

**Architecture:** five new files mirroring Python — `cloud_detect.rs` extended with `instance_type`, `core/cgroup_reader.rs`, `core/compute_runtime.rs`, `core/fargate_metadata.rs`, `pricing/compute_pricing.rs`, `core/compute_accountant.rs`. `core/tracker.rs::finalize_costs` gains a compute back-fill step.

**Tech stack:** Rust 2021, `rust_decimal` (already in `Cargo.toml`), `reqwest` (already in for IMDS), `serde_json`, `std::sync::{Mutex, OnceLock}`, `once_cell`. No new deps.

**Run tests with:** `cd rust && cargo test`

**Pre-requisites already landed on this branch:**
- v2 network capture: `cloud_detect.rs` (with parallel IMDS probes for AWS/GCP/Azure), `pricing/egress_pricing.rs`, `core/network_accountant.rs` + deferred-cost finalize pattern (see commit log on this branch).
- `EventType::ComputeCost` already in `core/models.rs:20+`.
- `Task::compute_cost_usd: Decimal` already on the Task struct.
- `rust/src/data/compute_prices.json` already synced from Python canonical by `scripts/sync_compute_catalog.sh`.

---

### Task 1: Extend `CloudEnv` with `instance_type` + IMDS probe extensions

**Files:**
- Modify: `rust/src/cloud_detect.rs`
- Test: tests in the same file (Rust convention) under `#[cfg(test)] mod tests`

**Python sibling:** `python/src/dexcost/cloud_detect.py` (commit `a613924`).

- [ ] Add `pub instance_type: Option<String>` to `CloudEnv` (default `None` via `..Default::default()` or explicit struct literals — audit all existing `CloudEnv { ... }` constructions and add `instance_type: None`).
- [ ] In `_probe_aws`: after the region GET, hit `/latest/meta-data/instance-type` with the same IMDSv2 token. Use a separate `Result::ok()` so a 404 there doesn't lose the resolved region.
- [ ] In `_probe_gcp`: after region resolves, GET `/computeMetadata/v1/instance/machine-type` with `Metadata-Flavor: Google`; strip prefix via `s.rsplit('/').next()`.
- [ ] In `_probe_azure`: parse `.compute.vmSize` from the existing JSON payload — zero extra HTTP.
- [ ] `start_background_detection` (the `tokio::spawn` block): when stitching env+IMDS, preserve `instance_type` from whichever source has it (mirrors Python's `_background`).
- [ ] Tests — port the 8 Python cases (struct-literal carrying field; AWS/GCP/Azure probes return instance_type; AWS/GCP failure-keeps-region; Azure missing-vmSize). Use `mockito` or a custom `Client` injection if the existing probe tests already do (check `cloud_detect.rs` tests for the pattern).
- [ ] Commit — `feat(compute-rust): extend CloudEnv with instance_type from IMDS (Decision #3)`.

---

### Task 2: `core/cgroup_reader.rs` — cgroup v2 file readers

**Python sibling:** `cgroup_reader.py` (commit `1609faf`).

- [ ] New module `rust/src/core/cgroup_reader.rs`, re-exported from `core/mod.rs`.
- [ ] Types:
  ```rust
  pub struct CpuStat { pub usage_usec: u64 }
  pub struct CpuMax { pub quota_us: Option<u64>, pub period_us: u64, pub vcpu_count: f64 }
  ```
- [ ] Functions return `Option<T>` (Rust idiom for the Python `None` sentinel):
  - `pub fn read_cpu_stat() -> Option<CpuStat>`
  - `pub fn read_cpu_max() -> Option<CpuMax>`
  - `pub fn read_memory_peak() -> Option<u64>`, `read_memory_max`, `read_memory_current`
- [ ] Package-level `static CGROUP_ROOT: Mutex<PathBuf>` or `OnceLock<PathBuf>` initialized to `/sys/fs/cgroup`. Tests override via a `set_cgroup_root_for_tests` helper.
- [ ] `read_cpu_max` falls back to `num_cpus::get() as f64` when the literal is `"max"`. (`num_cpus` is already in deps for the test runner — confirm or use `std::thread::available_parallelism()`.)
- [ ] Tests in `#[cfg(test)] mod tests` — 12 cases mirroring Python.
- [ ] Commit — `feat(compute-rust): cgroup v2 file readers`.

---

### Task 3: `core/compute_runtime.rs` — runtime cascade

**Python sibling:** `compute_runtime.py` (commit `afbc007`).

- [ ] Enum:
  ```rust
  #[derive(Clone, Copy, Debug, PartialEq, Eq)]
  pub enum RuntimeKind {
    Lambda, Fargate, Ec2, CloudRun, CloudFunctions, Gce,
    AzureFunctions, AzureVm, Vercel, K8sPod, Unknown,
  }
  ```
  Add `impl RuntimeKind { pub fn as_str(&self) -> &'static str }` returning the same string values as Python's enum (`"lambda"`, `"fargate"`, …) — these are persisted on event details across SDKs.
- [ ] `pub fn resolve_runtime() -> RuntimeKind` — cascade exactly as Python `compute_runtime.py`:
  1. `AWS_LAMBDA_FUNCTION_NAME` → `Lambda`
  2. `ECS_CONTAINER_METADATA_URI_V4` / `ECS_CONTAINER_METADATA_URI` → `Fargate`
  3. `K_SERVICE` + `FUNCTION_TARGET` → `CloudFunctions`; `K_SERVICE` alone → `CloudRun`
  4. `FUNCTIONS_WORKER_RUNTIME` → `AzureFunctions`
  5. `VERCEL` → `Vercel`
  6. `KUBERNETES_SERVICE_HOST` → `K8sPod`
  7. Fall through to `cloud_detect::get_cloud_env()` provider → `Ec2` / `Gce` / `AzureVm` / `Unknown`
- [ ] Tests — port the 12 Python cases. Use `std::env::set_var` inside `serial_test` guard (or sequential `#[cfg(test)]` to avoid env pollution).
- [ ] Commit — `feat(compute-rust): runtime resolver — serverless > k8s > cloud_detect IaaS`.

---

### Task 4: `core/fargate_metadata.rs` — ECS task metadata helper

**Python sibling:** `fargate_metadata.py` (commit `08f2b22`).

- [ ] Types + state:
  ```rust
  pub struct FargateTaskMetadata { pub vcpu_count: f64, pub memory_bytes_limit: u64 }
  static CACHE: OnceLock<Mutex<CacheState>> = OnceLock::new();
  struct CacheState { resolved: bool, cached: Option<FargateTaskMetadata>, warned: bool }
  ```
- [ ] `pub fn fetch_fargate_metadata() -> Option<FargateTaskMetadata>` — reads `ECS_CONTAINER_METADATA_URI_V4` or `ECS_CONTAINER_METADATA_URI`, hits `$URI/task`, parses JSON, returns cached result on subsequent calls.
- [ ] **The load-bearing line:** `memory_bytes = mem_mib * 1024 * 1024` (binary MiB → bytes per Decision #7).
- [ ] `pub fn reset_for_tests()` clears the cache.
- [ ] Tests use `mockito` to mock the endpoint. 6 cases port from Python.
- [ ] Commit — `feat(compute-rust): Fargate ECS task metadata helper`.

---

### Task 5: Catalog integrity tests

**Files:** Test in `rust/src/pricing/compute_pricing.rs` `#[cfg(test)] mod tests` (or a dedicated `compute_catalog_integrity_test.rs` under `tests/`).

**Python sibling:** `test_compute_catalog_integrity.py`.

- [ ] Read catalog via `include_str!("../data/compute_prices.json")` parsed with `serde_json::Value`.
- [ ] 13 cases: structure, Decimal-parseable, freshness soft-warn (use `eprintln!` not `panic!`), provider/runtime presence, arch-keying on Lambda+Fargate, ARM < x86, top SKUs present for EC2/GCE/Azure VM, every `*_usd` and `vcpu_count` parses as `Decimal::from_str`.
- [ ] Commit — `test(compute-rust): catalog integrity tests`.

---

### Task 6: `pricing/compute_pricing.rs` — engine + 11 billing models + 5-tier ladder

**Python sibling:** `compute_pricing.py` (commit `e379f40` + the `3f5b327` Fargate rate refresh). This is the heart of v2; translate line-for-line.

- [ ] Types:
  ```rust
  #[derive(Clone, Debug)]
  pub struct ComputeCost { pub cost_usd: Decimal, pub pricing_source: String, pub cost_confidence: String }

  pub struct ComputePricingEngine { catalog: serde_json::Value, catalog_version: String }
  ```
- [ ] Constants:
  ```rust
  static GB_DECIMAL: Lazy<Decimal> = Lazy::new(|| Decimal::from(1_000_000_000u64));
  static GIB_BINARY: Lazy<Decimal> = Lazy::new(|| Decimal::from(1024u64 * 1024 * 1024));
  static HOUR_S: Lazy<Decimal> = Lazy::new(|| Decimal::from(3600u64));
  static MS_PER_S: Lazy<Decimal> = Lazy::new(|| Decimal::from(1000u64));
  ```
- [ ] `HARDCODED` map (Tier-4 fallback) — copy the values from the Python `_HARDCODED` block (post-refresh — Fargate is `"0.0000112444"` / `"0.0000012347"` per commit `3f5b327`).
- [ ] Constructor: `pub fn new() -> Self` loads `include_str!("../data/compute_prices.json")` and parses with serde_json. Failures fall through to empty catalog + log_once via convention §11.
- [ ] Public entry:
  ```rust
  pub fn resolve_compute_cost(
    &self,
    details: &serde_json::Value,
    cloud_env: &CloudEnv,
    overrides: &HashMap<String, String>,
    window_s: Option<Decimal>,
  ) -> ComputeCost
  ```
  Wrap the dispatch in `std::panic::catch_unwind(...)` for Tier 5 fail-silent.
- [ ] Dispatch on `details["billing_model"]` to per-model unexported methods, exactly mirroring Python's `_dispatch`.
- [ ] Module-level `WARNED_MODES: Mutex<HashSet<String>>` + `reset_warning_state_for_tests()` per convention §11.
- [ ] 16 tests in `#[cfg(test)] mod tests`. **Critical:** the `test_fargate_uses_binary_gib_divisor` test must use the binary divisor (Decimal `1024^3`), NOT decimal (10^9) — the ~4.86% bug-prevention test.
- [ ] Commit — `feat(compute-rust): pricing engine — per-billing-model math + degradation ladder`.

---

### Task 7: `core/compute_accountant.rs` — per-task accumulator

**Python sibling:** `compute_accountant.py` (commit `33cef0f`).

- [ ] Struct:
  ```rust
  pub struct ComputeAccountant {
    inner: Mutex<Inner>,
    pub runtime: RuntimeKind,
    pub lambda_memory_mb: Option<u32>,
    pub fargate_vcpu: Option<f64>,
    pub fargate_memory_mib: Option<u64>,
    pub architecture: String,
    pub initialization_type: Option<String>,
    pub region: Option<String>,
  }
  struct Inner { frozen: bool, start_cpu_usec: Option<u64> }
  ```
  Use `std::sync::Mutex` (matches `NetworkAccountant` — the accountant is called from sync contexts).
- [ ] Constructor with builder pattern (Rust idiom for Python's kwargs):
  ```rust
  ComputeAccountant::new(runtime)
    .with_lambda_memory_mb(512)
    .with_region("us-east-1".into())
    .with_initialization_type("on-demand".into())
  ```
- [ ] `pub fn snapshot_start(&self)`, `pub fn snapshot_end_and_build(&self, duration_ms: i64) -> Option<serde_json::Value>`, `pub fn build_serverless_event(&self, duration_ms: i64, memory_bytes_peak: u64) -> Option<serde_json::Value>`.
- [ ] Idempotency: second call returns `None` after the freeze flag is set.
- [ ] capture spec §6 case 6 fallback: memory.peak missing → fall through to memory.current.
- [ ] `_detect_arch()` uses `std::env::consts::ARCH` (`"aarch64"` / `"arm64"` → `"arm64"`, else `"x86_64"`).
- [ ] 8 tests in `#[cfg(test)] mod tests`.
- [ ] Commit — `feat(compute-rust): per-task accountant — cgroup start/end snapshots, single event`.

---

### Task 8: Wire `ComputeAccountant` into Task + extend tracker finalize

**Python sibling:** commit `91beccc`.

- [ ] Add to `core/models.rs`:
  ```rust
  #[serde(skip)]
  pub compute: Option<Arc<ComputeAccountant>>,
  ```
  Use `Arc<>` (matching the `_network` field's `Arc<NetworkAccountant>`), defaults to `None`. Never serialised.
- [ ] In `core/tracker.rs`, after the existing egress finalize block, add `self.finalize_compute(&mut task)` wrapped in its own `std::panic::catch_unwind(...)` for Tier 5.
- [ ] `fn finalize_compute(&self, task: &mut Task)` does three things (mirror Python `_finalize_compute`):
  1. Long-running runtimes → call `snapshot_end_and_build` and `storage.insert_event(...)` with `cost_usd: Decimal::ZERO` and `cost_pending: true` in details.
  2. Walk events for the task; for each `compute_cost` event with `details["cost_pending"] == true`, call `engine.resolve_compute_cost(...)` and `storage.update_event(...)` with new cost / source / confidence / `pricing_version: format!("compute:{}", engine.catalog_version())`. Strip `cost_pending`.
  3. Track DELTA per event (`new - old`); `task.compute_cost_usd += delta`; `task.total_cost_usd += delta`. **Do NOT recompute total_cost_usd from scratch** — preserves retry_marker costs already summed by the main aggregation loop.
- [ ] Tracker construction: add `compute_pricing: ComputePricingEngine`, `compute_billing_overrides: HashMap<String, String>`, `k8s_node_aware: bool` fields. Default empty / false.
- [ ] Integration test — port `test_compute_auto_emission_long_running.py`. Use the same monkey-patch pattern: package-level test variables that override the cgroup reads.
- [ ] Run `cargo test --all` — fix any existing tracker test that expected a specific `total_cost_usd` (use the same delta-not-recompute discipline).
- [ ] Commit — `feat(compute-rust): auto-emit + back-fill compute_cost events at task finalize`.

---

### Task 9: Handler wraps + config knobs

**Python sibling:** `compute_wrap.py` (commit `3babf79`).

- [ ] Extend `Config` / `Options` struct (wherever Rust SDK's init knobs live) with:
  ```rust
  pub compute_billing_overrides: HashMap<String, String>,
  pub k8s_node_aware: bool,
  ```
- [ ] `adapters/compute_wrap.rs`:
  ```rust
  pub async fn wrap_lambda_handler<F, Fut, T, R>(handler: F, event: T, context: LambdaContext) -> R
    where F: FnOnce(T, LambdaContext) -> Fut, Fut: Future<Output = R>
  ```
  Reads env vars (`AWS_LAMBDA_FUNCTION_MEMORY_SIZE`, `AWS_LAMBDA_INITIALIZATION_TYPE`, `AWS_REGION`); constructs `ComputeAccountant::new(RuntimeKind::Lambda)`; attaches via `task.compute = Some(Arc::new(accountant))`; times the handler with `Instant::now()`; on exit (success or panic) reads `read_memory_peak()` and persists the event with `cost_pending: true`.
  
  Use `tokio::select!` or simple `let result = panic::AssertUnwindSafe(future).catch_unwind().await;` to catch handler panics so the event is ALWAYS persisted (capture spec §6 case 7).
- [ ] Stub wraps for the other four serverless runtimes sharing an internal `time_and_capture` helper.
- [ ] 6 tests (Lambda happy path, no-active-task pass-through, handler-error-still-emits, both knobs threaded, all five wraps exported from `lib.rs`).
- [ ] Commit — `feat(compute-rust): serverless handler wraps + config knobs`.

---

### Task 10: Property invariants + Decision #9/#10 idle-gap + cross-runtime matrix

**Python sibling:** commit `c4c3bfb`.

- [ ] Three test files (port Python line-for-line):
  - `pricing/compute_invariants_test.rs` (or `#[cfg(test)] mod compute_invariants`) — table-driven over all 11 billing models for the 6 invariants (cost ≥ 0; duration/memory linearity; ARM < x86 on Lambda+Fargate; confidence ∈ {computed, estimated}; pricing_source starts with `compute_catalog:`).
  - `core/compute_idle_gap_test.rs` — the load-bearing Decision #9 (EC2 idle invisible) + #10 (Fargate container idle invisible). **Failure message must reference the decision number.**
  - `pricing/compute_cross_runtime_matrix_test.rs` — one test per billing_model with hand-fixture asserting positive cost + expected pricing_source substring.
- [ ] `cargo test --all` must be all green before commit.
- [ ] Commit — `test(compute-rust): property invariants + Decision #9/#10 idle-gap + cross-runtime matrix`.

---

### Task 11: Drift check vs Python canonical catalog

- [ ] Test asserting `include_str!("../../../rust/src/data/compute_prices.json")` == `include_str!("../../../python/src/dexcost/data/compute_prices.json")` (byte-equal). Skip gracefully in published-crate environment where the Python file isn't reachable.
- [ ] Commit — `test(compute-rust): drift check vs Python canonical catalog`.

---

## Self-Review

Same equivalence checklist as the Go plan §11. Each Rust module must be a faithful translation of its Python sibling — same dispatch table, same `HARDCODED` constants (post-refresh), same fail-silent discipline, same idempotent freeze flag, same `RuntimeKind` string values (cross-SDK event portability).

**Cross-SDK invariants pinned by Task 10:**
- Fargate binary GiB divisor (~4.86% silent over-attribution bug prevented in Rust too)
- ARM < x86 on Lambda + Fargate
- Decisions #9/#10 idle invisible
- All 11 billing models reachable via dispatch

**Rust-specific gotchas:**
- `rust_decimal` panics on overflow; use `checked_mul` / `checked_div` only at the divisor boundary if catalog rates somehow exceed `i64::MAX` (they don't, but the tests should pin this).
- `std::env::set_var` is not thread-safe across tests; use `serial_test::serial` on runtime-resolver tests if `cargo test` runs in parallel mode.
- `tokio::sync::Mutex` vs `std::sync::Mutex`: use `std::sync::Mutex` for the accountant (it's called from sync contexts inside tracker finalize), matching `NetworkAccountant`. Reserve tokio mutexes for the async IMDS probes only.
- `Arc<ComputeAccountant>` on Task lets the handler wrap stash a clone in task context without the borrow checker fighting the finalize step.

**Known follow-ups:** TypeScript port (next plan); K8s `/api/v1/nodes` opt-in probe across all SDKs; launch-prerequisite full catalog coverage.
