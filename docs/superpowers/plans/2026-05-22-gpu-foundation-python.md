# GPU Foundation (v1 capture + v2 cost) — Python SDK — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture GPU usage across the 8 providers and 4 billing models in the master coverage matrix (AWS EC2 GPU / GCP GCE GPU bundled + N1+accelerator / Azure VM GPU + NVadsA10 vGPU / Modal / RunPod / Lambda Labs / CoreWeave / Replicate) AND attribute dollars per task — populate a new `Task.gpu_cost_usd` field from auto-emitted `gpu_cost` events priced from the bundled `data/gpu_prices.json` catalog. AND emit one `gpu_utilization_signal` event per task per GPU as a side-channel signal (no cost) so the inevitable 380× idle-gap is actionable on first install (Decision #3). Implements both:

- `docs/superpowers/specs/2026-05-22-gpu-capture-design.md` (v1 — measurement, NVML cgroup-walk, two new event types)
- `docs/superpowers/specs/2026-05-22-gpu-cost-attribution-design.md` (v2 — cost math, catalog, pricing engine)

**One-plan rationale:** same as Phase 1 — v1 capture emits `gpu_cost` events with `cost_pending: true` and `cost_usd = 0`; v2 cost-attribution back-fills the dollars at task finalize. Either half is useless without the other.

**Architecture:** seven new files mirroring Phase 1 compute — `data/gpu_prices.json` (already shipped commit `79c8745`), `gpu_pricing.py` (dispatch + 4 billing-model math + 5-tier ladder), `nvml_reader.py` (NVML library wrapper, fail-silent on missing driver), `cgroup_walker.py` (Decision #1 scope classifier), `gpu_runtime.py` (runtime cascade), `gpu_accountant.py` (per-task accumulator), `gpu_wrap.py` (serverless handler decorators for Modal/RunPod/Replicate). `_aggregate_costs` gains a third back-fill block after compute.

**Tech stack:** Python 3.10+, `pynvml` (aka `nvidia-ml-py` — NVIDIA-official, actively maintained 2025-2026), stdlib `decimal` / `threading` / `pathlib`, `pytest`, `unittest.mock`. **`nvidia-ml-py` is added as an OPTIONAL dependency** (not required for SDK install — GPU capture silently no-ops if pynvml isn't installed or NVML driver isn't loaded). Pin: `nvidia-ml-py>=12.535,<14.0`.

**Run tests with:** `cd python && uv run pytest <path> -v`

**Pre-requisites already landed on this branch:**
- v2 network capture + v2 compute capture all green (Phase 1 — `cloud_detect`, `egress_pricing`, `compute_pricing`, deferred-cost pattern, `update_event` sync_status fix).
- `EventType` enum has `compute_cost`, `external_cost`, `llm_call`, `network`, `retry_marker`; **needs `gpu_cost` + `gpu_utilization_signal` added** (Task 0).
- `Task.compute_cost_usd` field exists; **needs `Task.gpu_cost_usd` added** (Task 0).
- `cloud_detect.CloudEnv.instance_type` already wired (Phase 1) — the IMDS instance-type → GPU SKU resolution for `per_instance_hour` billing depends on this.
- `python/src/dexcost/data/gpu_prices.json` already shipped at `79c8745`; no Task 5 catalog-authoring work needed (just integrity tests).
- `compute_runtime.py` exists; **gpu_runtime.py is a SIBLING module**, not an extension — they coexist.

---

### Task 0: Add `EventType` values + `Task.gpu_cost_usd` field + schema migration

**Files:**
- Modify: `python/src/dexcost/models/enums.py`
- Modify: `python/src/dexcost/models/task.py`
- Modify: `python/src/dexcost/storage/migrations.py` (if applicable; bump schema version + ALTER)
- Modify: `python/schemas/dexcost-event.v1.json` (event_type enum gains two values)
- Test: `python/tests/test_task_gpu_cost_field.py` (create)

**Phase 1 equivalent:** Task 1 of the compute plan ("`Task.network_cost_usd` field"). Same shape.

- [ ] **Step 1: Failing test** — `tests/test_task_gpu_cost_field.py`:

```python
"""Task model carries gpu_cost_usd, parallel to the other *_cost_usd fields."""
from decimal import Decimal
from dexcost.models.task import Task


def test_gpu_cost_usd_defaults_to_zero():
    t = Task(task_type="x")
    assert t.gpu_cost_usd == Decimal("0")
    assert isinstance(t.gpu_cost_usd, Decimal)


def test_gpu_cost_usd_round_trip_through_dict():
    t = Task(task_type="x")
    t.gpu_cost_usd = Decimal("3.99")
    d = t.to_dict()
    assert d["gpu_cost_usd"] == "3.99"
    t2 = Task.from_dict(d)
    assert t2.gpu_cost_usd == Decimal("3.99")


def test_from_dict_defaults_gpu_cost_usd_for_old_payloads():
    d = Task(task_type="x").to_dict()
    d.pop("gpu_cost_usd")
    t = Task.from_dict(d)
    assert t.gpu_cost_usd == Decimal("0")


def test_event_type_enum_includes_gpu_values():
    from dexcost.models.enums import EventType
    assert EventType.GPU_COST.value == "gpu_cost"
    assert EventType.GPU_UTILIZATION_SIGNAL.value == "gpu_utilization_signal"
```

- [ ] **Step 2: Run, verify fail** (`gpu_cost_usd` doesn't exist; enum values missing).

- [ ] **Step 3: Add to enum** (`models/enums.py`):
  ```python
  class EventType(str, Enum):
      ...
      GPU_COST = "gpu_cost"
      GPU_UTILIZATION_SIGNAL = "gpu_utilization_signal"
  ```

- [ ] **Step 4: Add field** to `Task` dataclass — `gpu_cost_usd: Decimal = Decimal("0")` after `network_cost_usd`. Update `to_dict()` / `from_dict()` (with `.get("gpu_cost_usd", "0")` default for old payloads, mirroring `network_cost_usd`).

- [ ] **Step 5: SQLite migration** (if `migrations.py` has `TARGET_SCHEMA_VERSION` — bump it, add `ALTER TABLE tasks ADD COLUMN gpu_cost_usd TEXT NOT NULL DEFAULT '0'`). Mirror Phase 1's network_cost_usd migration pattern exactly.

- [ ] **Step 6: Update event schema** — `dexcost-event.v1.json` `event_type` enum gains the two new values.

- [ ] **Step 7: Verify pass + commit** — `feat(gpu): add Task.gpu_cost_usd + EventType.{GPU_COST,GPU_UTILIZATION_SIGNAL} + schema migration`.

---

### Task 1: `NvmlReader` — NVML library wrapper

**Files:**
- Create: `python/src/dexcost/nvml_reader.py`
- Test: `python/tests/test_nvml_reader.py`

**Phase 1 equivalent:** Task 2 (CgroupReader). Same shape — fail-silent helpers, all calls return `None` on missing/inaccessible/malformed.

**Python binding:** `pynvml` from `nvidia-ml-py` package. **Add to `pyproject.toml` as `[project.optional-dependencies] gpu = ["nvidia-ml-py>=12.535,<14.0"]`** so users opt in via `pip install dexcost[gpu]`. The reader's top-level `import` of `pynvml` MUST be wrapped in `try/except ImportError` so SDK install without `gpu` extra still works and GPU capture silently no-ops.

- [ ] **Step 1: Failing test** — covers:
  - `nvml_available()` returns `False` when `pynvml` not importable (mock `ImportError`)
  - `init_nvml()` returns `False` on `NVML_ERROR_DRIVER_NOT_LOADED` (mock)
  - `init_nvml()` returns `True` on success
  - `get_device_count()` returns count after init; `None` if not init
  - `get_product_name(handle)` returns the NFC-normalized + whitespace-collapsed lowercase string (Decision #4 sharpening)
  - `get_product_name` handles bytes input (older pynvml versions return bytes)
  - `get_compute_running_processes(handle)` returns list of `ProcessInfo(pid, used_gpu_memory)` tuples
  - `get_compute_running_processes` returns `None` on `NVML_ERROR_NO_PERMISSION` (the load-bearing case from the verification matrix)
  - `get_process_utilization(handle, last_seen_timestamps: dict[int, int])` returns `dict[pid, UtilSample]` AND updates the timestamps dict in place (Decision #8 — persistent state)
  - `get_memory_info(handle)` returns `(used_bytes, total_bytes)`
  - `get_mig_mode(handle)` returns `True`/`False` (Decision #2 detection)

- [ ] **Step 2: Implement** `nvml_reader.py`. Top-level:
  ```python
  try:
      import pynvml
      _NVML_AVAILABLE = True
  except ImportError:
      _NVML_AVAILABLE = False
  ```
  Wrap every NVML call in `try/except pynvml.NVMLError as exc:` translating to `None` returns + log-once tokens (`gpu_no_driver_in_container`, `gpu_nvml_permission_denied:<pid>`, etc).

  Critical: `get_product_name()` MUST apply `unicodedata.normalize("NFC", name).lower().split()` then `" ".join(...)` to handle the non-breaking-space / zero-width-character cases from the Decision #4 sharpening.

- [ ] **Step 3: Verify pass + commit** — `feat(gpu): NVML library wrapper with fail-silent contract + NFC-normalized productName`.

---

### Task 2: `CgroupWalker` — Decision #1 scope classifier

**Files:**
- Create: `python/src/dexcost/cgroup_walker.py`
- Test: `python/tests/test_cgroup_walker.py`

**Phase 1 equivalent:** Task 2 (CgroupReader) — same fail-silent file-IO shape.

**Decision #1 sharpening is the spec** — this module must classify `/proc/self/cgroup` content into the 7 prefixes from the spec's classification table.

- [ ] **Step 1: Failing test** — table-driven over the prefix table:

```python
@pytest.mark.parametrize("proc_self_cgroup,expected_scope,expected_pid_set_behavior", [
    # cgroup v2 — single line, prefix matters
    ("0::/docker/abc123\n",                                "container", "walk_cgroup_procs"),
    ("0::/kubepods.slice/kubepods-burstable.slice/...\n",  "container", "walk_cgroup_procs"),
    ("0::/system.slice/docker-abc.scope\n",                "container", "walk_cgroup_procs"),
    ("0::/system.slice/containerd-abc.scope\n",            "container", "walk_cgroup_procs"),
    ("0::/system.slice/crio-abc.scope\n",                  "container", "walk_cgroup_procs"),
    ("0::/user.slice/user-1000.slice/session-2.scope\n",   "bare_metal_user_slice", "self_pid_only"),
    ("0::/\n",                                              "root_cgroup", "self_pid_only"),
    ("0::/some/unknown/path\n",                            "unknown",    "self_pid_only"),
    # cgroup v1 (deferred to v1.1) — multi-line, multiple controllers
    ("12:devices:/docker/abc\n11:cpuset:/docker/abc\n...", "cgroup_v1",  "self_pid_only"),  # v1.1 will walk; v1 self-only
])
def test_classify_scope(monkeypatch, tmp_path, proc_self_cgroup, expected_scope, expected_pid_set_behavior):
    proc = tmp_path / "cgroup"
    proc.write_text(proc_self_cgroup)
    monkeypatch.setattr(cgroup_walker, "_PROC_SELF_CGROUP", str(proc))
    scope = cgroup_walker.classify_scope()
    assert scope.kind == expected_scope
    pids = cgroup_walker.enumerate_pids(scope)
    if expected_pid_set_behavior == "self_pid_only":
        assert pids == [os.getpid()]
    # else walk_cgroup_procs case asserted in a separate test with /sys/fs/cgroup fixture
```

Plus a separate test for `enumerate_pids` on container scope — uses `tmp_path` to fake `/sys/fs/cgroup/docker/abc123/cgroup.procs` containing `"1234\n5678\n9012\n"`, asserts return is `[1234, 5678, 9012]`.

Plus a test for the `cgroup.procs` denied case (file unreadable / EACCES) → returns `None` + logs once `gpu_cgroup_walk_forbidden`.

- [ ] **Step 2: Implement** `cgroup_walker.py`:
  ```python
  @dataclass(frozen=True)
  class CgroupScope:
      kind: str  # one of "container", "bare_metal_user_slice", "root_cgroup", "unknown", "cgroup_v1"
      path: str | None  # the unified cgroup path for container scope; None otherwise

  _CONTAINER_PREFIXES = (
      "kubepods.slice/",   # K8s 1.25+
      "kubepods/",          # legacy K8s
      "docker/",
      "system.slice/docker-",
      "containerd/",
      "system.slice/containerd-",
      "system.slice/crio-",
      "crio/",
  )
  ```
  Per spec §6 case 5 — silent return of `self_pid_only` for non-container scopes; log-once tokens per Decision #1 sharpening.

- [ ] **Step 3: Verify pass + commit** — `feat(gpu): cgroup-scope classifier (Decision #1 verification-gate implementation)`.

---

### Task 3: `gpu_runtime.py` — Runtime cascade with NVML detection

**Files:**
- Create: `python/src/dexcost/gpu_runtime.py`
- Test: `python/tests/test_gpu_runtime.py`

**Phase 1 equivalent:** Task 3 (`compute_runtime.py`) — same shape, different inputs. `compute_runtime` remains untouched; this is a sibling module.

- [ ] **Step 1: Failing test** — parametrized matrix of (env vars + `CloudEnv` + NVML availability) → `GpuRuntimeKind`:

```python
@pytest.mark.parametrize("env,cloud_env,nvml_available,expected", [
    # serverless GPU clouds — env detection wins
    ({"MODAL_TASK_ID": "x"},   CloudEnv("modal", None, "env"),   True,  GpuRuntimeKind.MODAL),
    ({"RUNPOD_POD_ID": "x"},   CloudEnv("runpod", None, "env"),  True,  GpuRuntimeKind.RUNPOD),
    ({"REPLICATE_MODEL": "x"}, CloudEnv("replicate", None, "env"), True, GpuRuntimeKind.REPLICATE),

    # IaaS GPU via cloud_detect + NVML present
    ({}, CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge"), True, GpuRuntimeKind.AWS_EC2_GPU),
    ({}, CloudEnv("gcp", "us-central1", "imds", instance_type="a3-highgpu-8g"), True, GpuRuntimeKind.GCP_GCE_BUNDLED),
    ({}, CloudEnv("gcp", "us-central1", "imds", instance_type="n1-standard-8"), True, GpuRuntimeKind.GCP_GCE_N1_ATTACHED),  # NVML-only fallback (Decision #9)
    ({}, CloudEnv("azure", "eastus", "imds", instance_type="Standard_ND96isr_H100_v5"), True, GpuRuntimeKind.AZURE_VM_GPU),
    ({}, CloudEnv("azure", "eastus", "imds", instance_type="Standard_NV6ads_A10_v5"),  True, GpuRuntimeKind.AZURE_VM_VGPU),

    # No GPU → no events emitted (NVML unavailable)
    ({"MODAL_TASK_ID": "x"}, CloudEnv("modal", None, "env"), False, GpuRuntimeKind.NONE),
    ({}, CloudEnv("aws", "us-east-1", "imds", instance_type="c7g.xlarge"), True, GpuRuntimeKind.NONE),  # no GPU on c7g

    # Reserved GPU clouds via cloud_detect — Lambda Labs, CoreWeave
    ({}, CloudEnv("lambda_labs", None, "dmi"), True, GpuRuntimeKind.LAMBDA_LABS),
    ({}, CloudEnv("coreweave", None, "dmi"), True, GpuRuntimeKind.COREWEAVE),
])
def test_resolve_gpu_runtime(env, cloud_env, nvml_available, expected, monkeypatch):
    for k, v in env.items(): monkeypatch.setenv(k, v)
    monkeypatch.setattr("dexcost.gpu_runtime.cloud_detect.get_cloud_env", lambda: cloud_env)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.nvml_available", lambda: nvml_available)
    monkeypatch.setattr("dexcost.gpu_runtime.nvml_reader.get_device_count", lambda: (1 if nvml_available else 0))
    assert resolve_gpu_runtime() == expected
```

Plus test the GPU-instance-type vs CPU-instance-type discrimination — `_is_gpu_instance_type("p5.48xlarge")` → `True`, `_is_gpu_instance_type("c7g.xlarge")` → `False`. The matcher is regex-based against AWS GPU families: `^(g4|g4dn|g5|g5g|g6|g6e|p3|p4d|p4de|p5|p5e|p5en)\.`. Similar matchers for GCP A2/A3/G2 and Azure NC/ND/NV.

- [ ] **Step 2: Implement** `gpu_runtime.py`. Enum:
  ```python
  class GpuRuntimeKind(str, Enum):
      MODAL = "modal"
      RUNPOD = "runpod"
      REPLICATE = "replicate"
      LAMBDA_LABS = "lambda_labs"
      COREWEAVE = "coreweave"
      AWS_EC2_GPU = "aws_ec2_gpu"
      GCP_GCE_BUNDLED = "gcp_gce_bundled"
      GCP_GCE_N1_ATTACHED = "gcp_gce_n1_attached"
      AZURE_VM_GPU = "azure_vm_gpu"
      AZURE_VM_VGPU = "azure_vm_vgpu"
      NONE = "none"
  ```
  Cascade: serverless env vars → IaaS via cloud_detect + GPU-family regex matcher → NONE if NVML unavailable.

- [ ] **Step 3: Verify pass + commit** — `feat(gpu): runtime cascade — serverless env > IaaS GPU family > NVML presence`.

---

### Task 4: Catalog integrity tests (catalog already shipped at `79c8745`)

**Files:**
- Test: `python/tests/test_gpu_catalog_integrity.py` (create)

**Phase 1 equivalent:** the catalog integrity tests in `test_compute_catalog_integrity.py`. Same shape.

- [ ] **Step 1: Write the integrity test** — assertions:
  - Catalog parses as JSON
  - `_meta` has required `default_per_instance_hour_usd`, `default_per_gpu_second_active_usd`, `default_per_gpu_hour_reserved_usd`, `default_per_vgpu_hour_usd`, `version`, `last_updated`, `currency`, `description`, `notes`
  - Every `_meta.default_*_usd` is Decimal-parseable
  - Every provider has `_last_verified` parseable as ISO-8601 date
  - **Soft-warn at 90 days, hard-fail at 365** (Decision #11 — tighter than Phase 1's 180/730)
  - All 8 providers present: aws, gcp, azure, modal, runpod, lambda_labs, coreweave, replicate
  - AWS has `ec2_gpu.regions` with at least `us-east-1` populated
  - GCP has `gce_gpu_bundled` AND `gce_gpu_attached`
  - Azure has both `vm_gpu` AND `vm_vgpu` blocks
  - Walk the tree — every `hourly_usd` / `gpu_second_usd` / `gpu_hour_usd` / `vgpu_hour_usd` is Decimal-parseable
  - Every SKU entry has `gpu_sku` (canonical key) and `aliases` (list — may be empty)
  - **Cross-provider SKU consistency:** all entries with `gpu_sku: "h100-80gb-sxm5"` must be valid Decimal rates (the canonical key links across providers — verifies the agent's pricing-refresh `gpu_sku` discipline held)
  - Every `billing_model` referenced in any provider block matches one of the 4 dispatch values

- [ ] **Step 2: Verify pass + commit** — `test(gpu): catalog integrity tests`.

---

### Task 5: `gpu_pricing.py` — engine + 4 billing models + 5-tier ladder

**Files:**
- Create: `python/src/dexcost/gpu_pricing.py`
- Test: `python/tests/test_gpu_pricing.py`

**Phase 1 equivalent:** Task 6 of compute plan (`compute_pricing.py`) — same architectural shape, smaller fanout (4 billing models vs 11), no per-runtime memory-unit conversion table (Decision #7 — no VRAM divisor).

This is the heart of v2; the longest task.

- [ ] **Step 1: Failing test** — one canonical case per billing model + the degradation ladder:

```python
import pytest
from decimal import Decimal
from dexcost.gpu_pricing import GpuPricingEngine, _reset_warning_state
from dexcost.cloud_detect import CloudEnv


@pytest.fixture(autouse=True)
def _reset(): _reset_warning_state()


@pytest.fixture
def engine(): return GpuPricingEngine()


# ─── per_gpu_second_active (highest-precision regime) ──────────────────────

def test_modal_h100_per_second(engine):
    """Modal H100 × 1.234 seconds.
       gpu_seconds_used = 1.234; rate = $0.001097/s; expected ≈ $0.00135"""
    details = {
        "billing_model": "per_gpu_second_active",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 1,
        "region": None, "duration_ms": 1234, "gpu_seconds_used": 1.234,
        "instance_type": None, "vgpu_profile": None, "mig_profile": None,
    }
    cost = engine.resolve_gpu_cost(details, CloudEnv("modal", None, "env"), window_s=Decimal("1.234"))
    assert cost.cost_usd == Decimal("1.234") * Decimal("0.001097")
    assert cost.cost_confidence == "computed"
    assert "modal" in cost.pricing_source
    assert cost.pricing_source.endswith("h100")  # or whatever the catalog key is


# ─── per_gpu_hour_reserved (share-factor against per-GPU rate) ─────────────

def test_lambda_h100_sxm_share(engine):
    """Lambda Labs 8x H100 SXM5; 1 GPU-second used over 60s window with 8 GPUs.
       share_factor = 1/(8*60); task_gpu_hours = share * 60/3600 * 8.
       cost = task_gpu_hours * 3.99"""
    details = {
        "billing_model": "per_gpu_hour_reserved",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 8,
        "region": None, "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": None, "vgpu_profile": None, "mig_profile": None,
    }
    cost = engine.resolve_gpu_cost(details, CloudEnv("lambda_labs", None, "dmi"), window_s=Decimal("60"))
    expected_share = Decimal("1.0") / (Decimal("8") * Decimal("60"))
    expected_gpu_hours = expected_share * (Decimal("60") / Decimal("3600")) * Decimal("8")
    expected = expected_gpu_hours * Decimal("3.99")
    assert cost.cost_usd == expected


# ─── per_instance_hour (AWS p5/p4d, GCP A3, Azure ND) ─────────────────────

def test_aws_p5_share(engine):
    """AWS p5.48xlarge $98.32/hr; 1 GPU-second used over 60s window with 8 GPUs."""
    details = {
        "billing_model": "per_instance_hour",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 8,
        "region": "us-east-1", "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": "p5.48xlarge",
        "vgpu_profile": None, "mig_profile": None,
    }
    cloud = CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    expected_share = Decimal("1") / (Decimal("8") * Decimal("60"))
    expected_hours = expected_share * (Decimal("60") / Decimal("3600"))
    expected = expected_hours * Decimal("98.32")
    assert cost.cost_usd == expected
    assert cost.cost_confidence == "computed"


# ─── per_vgpu_hour (Azure NVadsA10 v5 fractional) ──────────────────────────

def test_azure_nv6_vgpu_share(engine):
    """Standard_NV6ads_A10_v5 (1/6 A10); 1 GPU-second used over 60s window."""
    details = {
        "billing_model": "per_vgpu_hour",
        "gpu_vendor": "nvidia", "gpu_sku": "a10-vgpu-1of6", "gpu_count": 1,
        "region": "eastus", "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": "Standard_NV6ads_A10_v5",
        "vgpu_profile": "1/6 A10", "mig_profile": None,
    }
    cloud = CloudEnv("azure", "eastus", "imds", instance_type="Standard_NV6ads_A10_v5")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    # share = 1/60; vgpu_hours = (1/60) * (60/3600) = 1/3600
    # cost = 1/3600 * $0.454 (or whatever catalog has — assert math shape)
    assert cost.cost_usd > 0
    assert cost.cost_confidence == "computed"


# ─── degradation ladder ────────────────────────────────────────────────────

def test_tier3_unknown_sku_device_class_fallback(engine):
    """Unknown productName matched to hopper class → estimated confidence."""
    details = {
        "billing_model": "per_instance_hour",
        "gpu_vendor": "nvidia", "gpu_sku": None,  # alias resolution failed
        "gpu_count": 1, "region": "us-east-1", "duration_ms": 60_000,
        "gpu_seconds_used": 1.0, "instance_type": "p_FUTURE.something",
        "vgpu_profile": None, "mig_profile": None,
        "_nvml_product_name_lower": "nvidia b300 200gb hbm4",  # hypothetical 2026-Q4 SKU
    }
    cloud = CloudEnv("aws", "us-east-1", "imds")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_confidence == "estimated"
    assert "device_class_fallback" in cost.pricing_source


def test_tier4_missing_catalog_uses_hardcoded(tmp_path):
    bogus = tmp_path / "no.json"
    eng = GpuPricingEngine(catalog_path=bogus)
    details = {"billing_model": "per_gpu_second_active", "gpu_seconds_used": 1.0,
               "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 1,
               "region": None, "duration_ms": 1000, "instance_type": None,
               "vgpu_profile": None, "mig_profile": None}
    cost = eng.resolve_gpu_cost(details, CloudEnv("modal", None, "env"), window_s=Decimal("1"))
    assert cost.cost_usd > 0
    assert "hardcoded" in cost.pricing_source


def test_tier5_computation_failure_returns_zero(engine):
    bad = {"billing_model": "per_gpu_second_active", "gpu_seconds_used": "garbage"}
    cost = engine.resolve_gpu_cost(bad, CloudEnv(None, None, "none"), window_s=Decimal("1"))
    assert cost.cost_usd == Decimal("0")
    assert cost.cost_confidence == "unknown"


def test_unknown_billing_model_returns_zero(engine):
    bad = {"billing_model": "made_up", "gpu_seconds_used": 1.0,
           "gpu_count": 1, "duration_ms": 1000, "gpu_vendor": "nvidia",
           "gpu_sku": "x", "region": None, "instance_type": None,
           "vgpu_profile": None, "mig_profile": None}
    cost = engine.resolve_gpu_cost(bad, CloudEnv(None, None, "none"))
    assert cost.cost_usd == Decimal("0")


def test_warn_once_per_failure_mode(tmp_path, caplog):
    import logging
    _reset_warning_state()
    bogus = tmp_path / "missing.json"
    with caplog.at_level(logging.WARNING):
        GpuPricingEngine(catalog_path=bogus)
        GpuPricingEngine(catalog_path=bogus)
    msgs = [r.getMessage() for r in caplog.records if "gpu catalog" in r.getMessage().lower()]
    assert len(msgs) == 1


# ─── decision-#1 fallback labelling ────────────────────────────────────────

def test_self_pid_only_fallback_label():
    """When details carry `_cgroup_scope_fallback`, pricing_source ends with `:self_pid_only`."""
    engine = GpuPricingEngine()
    details = {
        "billing_model": "per_gpu_second_active",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 1,
        "region": None, "duration_ms": 1000, "gpu_seconds_used": 1.0,
        "instance_type": None, "vgpu_profile": None, "mig_profile": None,
        "_cgroup_scope_fallback": "self_pid_only",  # accountant sets this when fallback fires
    }
    cost = engine.resolve_gpu_cost(details, CloudEnv("modal", None, "env"), window_s=Decimal("1"))
    assert cost.pricing_source.endswith(":self_pid_only")
    assert cost.cost_confidence == "estimated"
```

- [ ] **Step 2: Implement** `gpu_pricing.py`. Structure:

```python
@dataclass(frozen=True)
class GpuCost:
    cost_usd: Decimal
    pricing_source: str
    cost_confidence: str


_HARDCODED = {  # Tier-4 — must mirror _meta defaults from gpu_prices.json
    "per_instance_hour":      {"hourly_usd":     Decimal("55.04")},
    "per_gpu_second_active":  {"gpu_second_usd": Decimal("0.000694")},
    "per_gpu_hour_reserved":  {"gpu_hour_usd":   Decimal("3.99")},
    "per_vgpu_hour":          {"vgpu_hour_usd":  Decimal("0.454")},
}

_HOUR_S = Decimal("3600")
_MS_PER_S = Decimal("1000")

# device_class default rates (Decision #4 — cold-start fallback)
_DEVICE_CLASS_DEFAULTS = {
    # per_instance_hour fallback when sku unknown
    "hopper":        {"hourly_usd": Decimal("98.32"),  "gpu_second_usd": Decimal("0.001097"), "gpu_hour_usd": Decimal("3.99")},
    "ampere":        {"hourly_usd": Decimal("32.77"),  "gpu_second_usd": Decimal("0.000833"), "gpu_hour_usd": Decimal("2.20")},
    "ada_lovelace":  {"hourly_usd": Decimal("12.00"),  "gpu_second_usd": Decimal("0.000400"), "gpu_hour_usd": Decimal("1.50")},
    "blackwell":     {"hourly_usd": Decimal("180.00"), "gpu_second_usd": Decimal("0.002500"), "gpu_hour_usd": Decimal("6.50")},  # estimate; Decision #4
}


class GpuPricingEngine:
    def __init__(self, catalog_path: str | Path | None = None) -> None:
        ...  # mirror compute_pricing.py's catalog-load logic exactly

    def resolve_gpu_cost(self, details, cloud_env, window_s=None) -> GpuCost:
        billing_model = (details or {}).get("billing_model") or "unknown"
        try:
            cost = self._dispatch(billing_model, details, cloud_env, window_s)
            # Decision #1 measurement-fallback suffix
            scope_fb = (details or {}).get("_cgroup_scope_fallback")
            if scope_fb:
                cost = GpuCost(cost.cost_usd, cost.pricing_source + f":{scope_fb}", "estimated")
            return cost
        except Exception as exc:
            _warn_once(f"gpu_pricing_failure:{billing_model}", f"...")
            return GpuCost(Decimal("0"), f"gpu_catalog:error:{billing_model}", "unknown")

    def _dispatch(self, billing_model, details, cloud_env, window_s):
        if billing_model == "per_gpu_second_active":  return self._per_gpu_second(details, cloud_env)
        if billing_model == "per_instance_hour":      return self._per_instance_hour(details, cloud_env, window_s)
        if billing_model == "per_gpu_hour_reserved":  return self._per_gpu_hour(details, cloud_env, window_s)
        if billing_model == "per_vgpu_hour":          return self._per_vgpu_hour(details, cloud_env, window_s)
        return GpuCost(Decimal("0"), f"gpu_catalog:unsupported:{billing_model}", "unknown")

    # ── per-billing-model math (see spec §6 for exact formulas) ──
```

The per-billing-model math is documented in cost spec §6. Translate one-for-one.

- [ ] **Step 3: Verify pass + commit** — `feat(gpu): pricing engine — 4 billing models + 5-tier ladder + Decision #4 device-class fallback`.

---

### Task 6: `GpuAccountant` — per-task accumulator

**Files:**
- Create: `python/src/dexcost/gpu_accountant.py`
- Test: `python/tests/test_gpu_accountant.py`

**Phase 1 equivalent:** Task 7 (`compute_accountant.py`) — same shape (idempotent freeze flag, snapshot start/end pair, `cost_pending: true` emission).

**Difference from compute:** the accountant tracks per-PID NVML utilization samples via Decision #8's persistent timestamps, walks cgroup PIDs per Decision #1's table, and emits BOTH `gpu_cost` AND one or more `gpu_utilization_signal` events at finalize.

- [ ] **Step 1: Failing test** — covers:
  - `snapshot_start()` reads cgroup scope + initial NVML samples + persists timestamps
  - `snapshot_end_and_build(duration_ms)` returns a tuple `(gpu_cost_event_details, [signal_event_details])`
  - Second call returns `(None, None)` (idempotent freeze)
  - Modal runtime case: returns details with `billing_model: "per_gpu_second_active"`
  - Lambda Labs runtime case: `per_gpu_hour_reserved`
  - AWS EC2 p5: `per_instance_hour` + `instance_type` populated
  - Azure NVadsA10 v5: `per_vgpu_hour` + `vgpu_profile` populated (per Decision #10's verification-pending path: when productName doesn't distinguish, `vgpu_profile` reads from IMDS `vmSize` → spec hint or `None`)
  - MIG-mode detection: when NVML reports MIG UUIDs, `details.mig_profile` populated, log-once fires (Decision #2)
  - Non-root container case: NVML permission error on `get_compute_running_processes` → degrade to self-PID-only; `details._cgroup_scope_fallback = "self_pid_only"`
  - Bare-metal-no-container case: cgroup scope is `bare_metal_user_slice` → `details._cgroup_scope_fallback = "no_container_scope"`
  - Multi-container K8s case: NVML reports PIDs not in cgroup → `details._cgroup_scope_fallback = "multi_container_pod_partial"`
  - Window-averaged `sm_util_pct` (Decision #3 sharpening): mock task with 80% util for 4 sec + 0% for 1 sec → signal event has `sm_util_pct ≈ 64`, NOT `0`
  - Sub-100ms task: signal event has `sm_util_pct = None`

- [ ] **Step 2: Implement** `gpu_accountant.py`. Class:

```python
class GpuAccountant:
    def __init__(self, runtime: GpuRuntimeKind, cloud_env: CloudEnv) -> None:
        self._lock = threading.Lock()
        self._frozen = False
        self.runtime = runtime
        self.cloud_env = cloud_env
        self._scope: CgroupScope | None = None
        self._initial_pids: set[int] = set()
        self._initial_timestamps: dict[int, int] = {}  # pid → lastSeenTimeStamp
        self._device_handles: list = []
        # Per-device, per-PID running totals
        self._sm_us_total: dict[int, dict[int, int]] = {}  # device_idx → pid → microsecs
        self._vram_used_peak: dict[int, int] = {}  # device_idx → bytes

    def snapshot_start(self) -> None:
        if not nvml_reader.init_nvml(): return
        count = nvml_reader.get_device_count() or 0
        self._device_handles = [nvml_reader.get_device_handle(i) for i in range(count)]
        self._scope = cgroup_walker.classify_scope()
        self._initial_pids = set(cgroup_walker.enumerate_pids(self._scope) or [os.getpid()])
        # baseline NVML samples per device per PID
        for device_idx, handle in enumerate(self._device_handles):
            self._initial_timestamps[device_idx] = {}
            samples = nvml_reader.get_process_utilization(handle, {})  # initial
            for pid, sample in (samples or {}).items():
                self._initial_timestamps[device_idx][pid] = sample.last_seen_timestamp

    def snapshot_end_and_build(self, duration_ms: int) -> tuple[dict | None, list[dict] | None]:
        with self._lock:
            if self._frozen: return (None, None)
            self._frozen = True
        # ... compute gpu_seconds_used, window-averaged sm_util_pct, vram_used_peak ...
        # ... build single gpu_cost_event_details + one signal_event per device ...
        return (gpu_cost_event_details, signal_event_details_list)
```

Critical implementation notes:
- The window-averaged `sm_util_pct` is `total_sm_us / task_duration_us * 100` per device, NOT a point sample.
- The Decision #1 fallback ladder is checked by the accountant, not the pricing engine: scope kind determines `_cgroup_scope_fallback` value; the pricing engine just suffixes the pricing_source.
- MIG detection at start: if any device has MIG mode, log-once `gpu_mig_detected_full_billing_applied`, populate `details.mig_profile` from the MIG UUID (Decision #2).
- TS port equivalent: `nvidia-smi --query-gpu=... --query-compute-apps=...` CSV parsing replaces NVML calls (not relevant in this Python plan; called out for cross-SDK consistency).

- [ ] **Step 3: Verify pass + commit** — `feat(gpu): per-task accountant — cgroup walk + NVML snapshot pair + dual-event emission`.

---

### Task 7: `gpu_wrap.py` — Serverless handler wraps

**Files:**
- Create: `python/src/dexcost/gpu_wrap.py`
- Test: `python/tests/test_gpu_wrap.py`

**Phase 1 equivalent:** Task 9 (`compute_wrap.py`) — same shape: per-runtime decorator that times the handler, instantiates accountant, persists events with `cost_pending: true`.

- [ ] **Step 1: Failing test** — covers `wrap_modal_handler`, `wrap_runpod_handler`, `wrap_replicate_handler`. Same shape as Phase 1's `test_compute_wrap.py`:
  - Lambda-wrap-style: decorated function emits one `gpu_cost` event + N `gpu_utilization_signal` events
  - No active task → pass-through (no events)
  - Handler exception → events still emit, exception re-raised
  - `init()` knobs: NO new knobs (per cost spec §5.2 — GPU billing models are unambiguous per provider; no `compute_billing_overrides`-equivalent needed)

- [ ] **Step 2: Implement** `gpu_wrap.py`. Mirror `compute_wrap.py`'s `_time_and_capture` shared helper but adapted to emit TWO event types (the `gpu_cost` + one or more `gpu_utilization_signal`).

- [ ] **Step 3: Expose in `__init__.py`** — `from dexcost.gpu_wrap import wrap_modal_handler, wrap_runpod_handler, wrap_replicate_handler`.

- [ ] **Step 4: Verify pass + commit** — `feat(gpu): serverless handler wraps (Modal / RunPod / Replicate)`.

---

### Task 8: Wire `GpuAccountant` into Task + extend `_aggregate_costs`

**Files:**
- Modify: `python/src/dexcost/models/task.py` (add `_gpu` field)
- Modify: `python/src/dexcost/tracker.py` (extend `_aggregate_costs` + add `_finalize_gpu`)
- Test: `python/tests/test_gpu_auto_emission_long_running.py` + `test_gpu_dual_event_emission.py`

**Phase 1 equivalent:** Task 8 (compute auto-emission). Same shape: third back-fill block after the existing egress + compute blocks; same delta-not-recompute discipline.

- [ ] **Step 1: Add `_gpu: GpuAccountant | None = None` field** to `Task` (in-memory only, mirrors `_network` / `_compute`).

- [ ] **Step 2: Extend `_aggregate_costs`** after the existing `_finalize_compute` call:
  ```python
  try:
      self._finalize_gpu(task)
  except Exception:
      _log.warning("gpu cost computation failed for task %s", task.task_id, exc_info=True)
  ```

- [ ] **Step 3: Implement `_finalize_gpu(self, task)`** mirroring `_finalize_compute`:
  1. Long-running runtimes (AWS_EC2_GPU, GCP_GCE_BUNDLED, GCP_GCE_N1_ATTACHED, AZURE_VM_GPU, AZURE_VM_VGPU, LAMBDA_LABS, COREWEAVE) → call `task._gpu.snapshot_end_and_build(duration_ms)` → persist 1 `gpu_cost` event + N `gpu_utilization_signal` events.
  2. Serverless runtimes (MODAL, RUNPOD, REPLICATE) have already emitted via the handler wrap; this step is a no-op for them.
  3. Walk events for the task; for each `gpu_cost` event with `details.cost_pending == true`, call `GpuPricingEngine.resolve_gpu_cost(...)` and update event (set `cost_usd`, `pricing_source`, `cost_confidence`, `pricing_version`, strip `cost_pending`).
  4. `gpu_utilization_signal` events are NEVER priced. Skip them in the back-fill walk.
  5. Adjust `task.gpu_cost_usd` and `task.total_cost_usd` by DELTA per back-filled event (delta-not-recompute discipline — preserves retry_marker costs from main aggregation loop).

- [ ] **Step 4: Add `_gpu_pricing` field to tracker** — `self._gpu_pricing = GpuPricingEngine()` in `CostTracker.__init__`, alongside `_compute_pricing` and `_egress_pricing`.

- [ ] **Step 5: Integration test** — port `test_compute_auto_emission_long_running.py` to GPU:
  - Mock long-running EC2 p5 task with cgroup walk returning 8 PIDs, NVML mock returning 30 GPU-seconds across devices → assert 1 `gpu_cost` event + N `gpu_utilization_signal` events emitted, cost back-filled, task aggregates correct
  - Dual-event integrity: assert that `gpu_utilization_signal` events have `cost_usd == 0` AND are NOT included in `task.gpu_cost_usd` (this is the load-bearing test for the Decision #3 convention carve-out)

- [ ] **Step 6: Verify full suite green** — `uv run pytest -q` — fix any existing tracker test that expected specific `total_cost_usd` (delta-not-recompute pattern).

- [ ] **Step 7: Commit** — `feat(gpu): auto-emit gpu_cost + gpu_utilization_signal events; back-fill at task finalize`.

---

### Task 9: Property invariants + Decision #6 idle-gap + cross-billing-model matrix

**Files:**
- Test: `python/tests/test_gpu_invariants.py`
- Test: `python/tests/test_gpu_idle_gap.py`
- Test: `python/tests/test_gpu_cross_billing_model_matrix.py`
- Test: `python/tests/test_gpu_utilization_signal_observability_only.py`

**Phase 1 equivalent:** Task 10 (property invariants + Decision #9/#10 idle-gap + cross-runtime matrix). Same shape, scoped to GPU.

- [ ] **Step 1: Property invariants** (cost spec §10.3 — 7 invariants):
  1. `task.gpu_cost_usd >= Decimal("0")` always
  2. `task.gpu_cost_usd == sum(e.cost_usd for e in task.gpu_cost_events)`
  3. Linearity: same SKU/runtime, `A.gpu_seconds_used == 2 × B.gpu_seconds_used`, same window → `A.cost_usd ≈ 2 × B.cost_usd`
  4. H100 rate > A100 rate on same provider (newer/faster more expensive)
  5. Per-GPU-second × 3600 ≈ per-GPU-hour rate (within 5-15%; Modal markup is real and the gap is the point)
  6. `cost_confidence ∈ {"computed", "estimated"}` (never `"unknown"` on well-formed input)
  7. `pricing_source.startswith("gpu_catalog:")`

- [ ] **Step 2: Decision #6 idle-gap test** — two tasks on the same Lambda Labs H100 with 50-minute idle between them. Assert `sum(task.gpu_cost_usd) < (full_window_hours × $3.99 × 1)`. The inequality IS the design (Decision #6 — idle invisible). Failure message references the decision number explicitly.

- [ ] **Step 3: Cross-billing-model matrix** — one test per `billing_model` value emitting a canonical fixture and asserting positive cost + expected `pricing_source` substring. 4 tests (Modal, EC2 p5, Lambda Labs H100, Azure NV6 vGPU).

- [ ] **Step 4: `gpu_utilization_signal` observability-only test** — the load-bearing convention-carve-out test:
  - Generate a task that emits 1 `gpu_cost` ($X) AND 1 `gpu_utilization_signal` (no cost)
  - Assert `task.gpu_cost_usd == X` (signal NOT included)
  - Assert `task.total_cost_usd == ... + X` (signal NOT included)
  - Assert the signal event has NO `cost_usd`, `pricing_source`, `cost_confidence`, `pricing_version` fields
  - This test pins the convention §1 carve-out into executable form

- [ ] **Step 5: Verify pass + commit** — `test(gpu): property invariants + Decision #6 idle-gap + cross-billing-model matrix + signal-event observability-only contract`.

---

### Task 10: Catalog sync script + conventions §1 update

**Files:**
- Already shipped at `79c8745`: `scripts/sync_gpu_catalog.sh`
- Modify: `docs/superpowers/conventions.md` (add §1 carve-out for observability signal events per Decision #3)
- Test: `python/tests/test_gpu_catalog_sync_consistency.py` (cross-SDK drift check)

- [ ] **Step 1: Update `conventions.md` §1** — add the carve-out paragraph from the decisions log:

> A subsystem MAY introduce a secondary 'signal' event type alongside its primary cost event, provided the signal event has no `cost_usd` field and is documented as observability-only. Phase 2 GPU's `gpu_utilization_signal` is the reference example.

Plus updates to §3 (`pricing_source` patterns: add GPU's `:self_pid_only`, `:no_container_scope`, `:multi_container_pod_partial`, `:device_class_fallback`, `:full_a10_assumption`) and §8 (measurement primitives — add NVML cgroup-walk row).

- [ ] **Step 2: Cross-SDK drift-check test** — `test_gpu_catalog_sync_consistency.py`. Asserts the Python canonical `gpu_prices.json` is byte-equal to the bundled copies in `go/pricing/data/`, `rust/src/data/`, `typescript/src/data/` (skip the test gracefully if the other SDK dirs aren't reachable, e.g. when running from a published wheel — `pytest.skip("non-monorepo install")`).

- [ ] **Step 3: Verify pass + commit** — `docs(conventions): §1 observability-signal carve-out + §3 GPU pricing_source patterns + §8 NVML cgroup-walk primitive`.

---

## Self-Review

**Spec coverage** — every section of both GPU specs maps to a task:

| Spec section | Task(s) |
|---|---|
| Capture §1 Summary (coverage matrix) | 3 (runtime resolver dispatches all 10 GpuRuntimeKinds), 5 (pricing engine handles all 4 billing models) |
| Capture §3 Decisions | All decisions threaded through tasks below |
| Capture §4 Data Model | 0 (EventType + Task.gpu_cost_usd + schema migration), 6 (event details shape) |
| Capture §5 Components | 1 (NvmlReader), 2 (CgroupWalker), 3 (GpuRuntimeResolver), 6 (GpuAccountant), 7 (HandlerWrap) |
| Capture §6 Error handling (15 cases) | 1, 2, 5 (Tier 5 try/except), 6 (Decision #1 fallback labels), 7 (no-active-task no-op) |
| Capture §7 Testing | 1, 2, 3, 5, 6, 7, 8, 9 |
| Capture §9 Control Layer contract | enforced by event schema (Task 0) + event details (Task 6) — no new code |
| Cost v2 §1 Purpose | All tasks |
| Cost v2 §2 Decisions | 1 (Decision #5 vendor field), 4 (catalog), 5 (Decision #4 alias matching + device-class fallback, Decision #7 no VRAM divisor, Decision #11 refresh cadence), 6 (Decision #1 cgroup walk, Decision #2 MIG, Decision #3 dual-event emission, Decision #8 timestamp state, Decision #10 vGPU best-effort), 8 (Decision #6 idle-gap delta math), 9 (Decision #6 explicit idle-gap test) |
| Cost v2 §3 Architecture | 0, 5, 6 |
| Cost v2 §4 Catalog | 4 (integrity tests; catalog already shipped) |
| Cost v2 §5 Pricing engine | 5 |
| Cost v2 §6 Per-billing-model math | 5 (with the spec §6 formulas pinned in tests) |
| Cost v2 §7 Degradation ladder | 5 (Tiers 1–4 in resolve, Tier 5 try/except wrap) |
| Cost v2 §8 Schema (migration) | 0 |
| Cost v2 §9 Cost attribution flow | 8 |
| Cost v2 §10 Testing | 5, 8, 9 |
| Cost v2 §11/§12 Future / Non-goals | n/a |
| Conventions §1 carve-out, §3 patterns, §8 primitive | 10 |

**Placeholder scan** — no `TBD`/`TODO`. The catalog rates ARE the placeholders the spec explicitly accepts (verified live at `79c8745`; Lambda Labs NVML alias strings and Azure NVadsA10 v5 vGPU profile distinction marked `(pending spike capture)` in the spec — when captures land, the catalog's `aliases` arrays update; no code changes).

**Type consistency** — `GpuCost` is a frozen dataclass `(cost_usd: Decimal, pricing_source: str, cost_confidence: str)`; `GpuRuntimeKind` is a string-valued enum (matches Python compute pattern + Phase 1 cross-SDK convention); `CgroupScope` is a frozen dataclass `(kind: str, path: str | None)`; the accountant's `snapshot_end_and_build` returns `tuple[dict | None, list[dict] | None]` so both event types are constructed in one place.

**Pre-requisite chain (already merged on this branch):**
1. Phase 1 network capture (v1 + v2)
2. Phase 1 compute capture (v1 + v2)
3. `Task.network_cost_usd` + `compute_cost_usd` field precedent + delta-not-recompute discipline in `_aggregate_costs`
4. `EventType.COMPUTE_COST` precedent for the new `GPU_COST` / `GPU_UTILIZATION_SIGNAL` enum values
5. `gpu_prices.json` bundled at `79c8745` (Python canonical + Go/Rust/TS synced via `scripts/sync_gpu_catalog.sh`)
6. Verification matrix scaffolding at `1980628` — the Decision #1 cgroup-scope classification table in this plan's Task 2 references the prefixes the verification matrix will empirically confirm

**Known follow-ups (out of scope of this plan, not gaps):**
- **Go / Rust / TypeScript ports** — each its own plan → implementation cycle. The Phase 1 4-SDK parallel-worktree pattern (commits `bc3b45d` … `0292bd7`) is the template.
- **K8s `/api/v1/nodes` opt-in probe** — same as Phase 1's deferred follow-up.
- **DCGM exporter scrape enrichment** — v1.1 if customer demand surfaces.
- **Inference-server `/metrics` enrichment** (vLLM, Triton, TGI) — v1.1.
- **AMD ROCm + Intel oneAPI** — v1.1, schema forward-compatible via `details.gpu_vendor`.
- **Cgroup-v1 walker** — v1.1 if RHEL 7 / CentOS 7 customer demand surfaces.
- **MIG per-slice billing** — v1.1 if any cloud ever ships per-slice pricing.
- **GPU-aware reconciliation surface** (Control Layer) — uses `gpu_utilization_signal` events.
- **Cross-container K8s pod attribution via Downward API** — v1.1.
- **Verification matrix P0 captures** — `(pending spike capture)` markers in the specs reference live tests that need to land in `docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/`. When they do, the spec sections gated on them get revised (Decision #1 classification table augmented with empirical prefixes; Decision #10 vGPU resolved as case A/B/C).
