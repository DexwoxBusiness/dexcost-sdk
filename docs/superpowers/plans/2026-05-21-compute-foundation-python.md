# Compute Foundation (v1 capture + v2 cost) — Python SDK — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture compute cost across the 9 runtime families in the master capability table (Lambda / Fargate / EC2 / Cloud Run / Cloud Functions / GCE / Azure Functions / Azure VM / Vercel / K8s pod) AND attribute dollars per task — populate the existing `Task.compute_cost_usd` field from auto-emitted `compute_cost` events priced from a bundled `data/compute_prices.json` catalog. Implements both:

- `docs/superpowers/specs/2026-05-21-compute-capture-design.md` (v1 — measurement & event shape)
- `docs/superpowers/specs/2026-05-21-compute-cost-attribution-design.md` (v2 — cost math, catalog, pricing engine)

**One-plan rationale:** v1 capture emits `compute_cost` events with `cost_pending: true` and `cost_usd = 0`; v2 cost-attribution back-fills the dollars at task finalize. Either half is useless without the other, so they ship together. Mirrors the deferred-cost pattern from network v2 §6.4.

**Architecture:** Five new modules — a bundled `data/compute_prices.json` catalog, a `compute_pricing.py` resolver dispatching on `details.billing_model`, a `cgroup_reader.py` helper reading `/sys/fs/cgroup/{cpu.stat, cpu.max, memory.peak, memory.max, memory.current}`, a `compute_runtime.py` cascade resolving the active runtime, and a `compute_accountant.py` per-task accumulator. `cloud_detect.py` extends to carry `instance_type` from IMDS in the existing Phase 2 background thread (Decision #3 — one probe, two extractions). `_aggregate_costs` gains the compute-cost back-fill step after the existing network back-fill.

**Tech Stack:** Python 3.10+, stdlib `decimal` / `threading` / `urllib.request` / `pathlib`, `sqlite3`, `pytest`. No new runtime dependencies.

**Run tests with:** `cd python && uv run pytest <path> -v`

**Pre-requisites already landed on this branch:**
- v2 network capture (cloud_detect, egress_pricing, deferred-cost pattern, `update_event` sync_status fix) — the compute layer reuses ALL of these.
- `EventType.COMPUTE_COST = "compute_cost"` already in `models/enums.py` — no enum change needed.
- `Task.compute_cost_usd` already exists in `models/task.py` — no schema migration needed.
- `_aggregate_costs` already aggregates `compute_cost` events into `task.compute_cost_usd` (tracker.py:1098) — the existing summation works once events get a real `cost_usd`.

---

### Task 1: `CloudEnv.instance_type` extension + IMDS instance-type probe

**Files:**
- Modify: `python/src/dexcost/cloud_detect.py`
- Test: `python/tests/test_cloud_detect_instance_type.py` (create)

Decision #3 sharpening: the IMDS instance-type extraction shares the SAME background thread as the region probe. One probe, two values extracted. The result lives on `CloudEnv` alongside `provider` / `region` / `source`.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_cloud_detect_instance_type.py`:

```python
"""CloudEnv carries instance_type from IMDS (Decision #3)."""

from unittest.mock import patch

from dexcost.cloud_detect import CloudEnv, _probe_aws, _probe_azure, _probe_gcp


def test_cloud_env_carries_instance_type():
    env = CloudEnv(provider="aws", region="us-east-1",
                   source="imds", instance_type="c7g.xlarge")
    assert env.instance_type == "c7g.xlarge"


def test_cloud_env_instance_type_defaults_to_none():
    env = CloudEnv(provider=None, region=None, source="none")
    assert env.instance_type is None


def test_aws_probe_returns_instance_type(monkeypatch):
    # Mock the urlopen chain — token PUT then 2 GETs.
    calls = []

    class FakeResp:
        def __init__(self, body): self._body = body
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return self._body

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/api/token"):
            return FakeResp(b"TOKEN")
        if req.full_url.endswith("/placement/region"):
            return FakeResp(b"us-east-1")
        if req.full_url.endswith("/instance-type"):
            return FakeResp(b"c7g.xlarge")
        raise AssertionError(f"unexpected url {req.full_url}")

    with patch("dexcost.cloud_detect.urllib.request.urlopen", fake_urlopen):
        env = _probe_aws()
    assert env.region == "us-east-1"
    assert env.instance_type == "c7g.xlarge"
    assert any("/instance-type" in u for u in calls)


def test_gcp_probe_returns_machine_type(monkeypatch):
    class FakeResp:
        def __init__(self, body): self._body = body
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return self._body

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/instance/zone"):
            return FakeResp(b"projects/123/zones/us-central1-a")
        if req.full_url.endswith("/instance/machine-type"):
            return FakeResp(b"projects/123/machineTypes/n2-standard-2")
        raise AssertionError(f"unexpected url {req.full_url}")

    with patch("dexcost.cloud_detect.urllib.request.urlopen", fake_urlopen):
        env = _probe_gcp()
    assert env.region == "us-central1"
    assert env.instance_type == "n2-standard-2"


def test_azure_probe_returns_vm_size():
    import json as _json

    class FakeResp:
        def __init__(self, body): self._body = body
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return self._body

    def fake_urlopen(req, timeout=None):
        body = _json.dumps({"compute": {"location": "eastus",
                                        "vmSize": "Standard_D2s_v3"}}).encode()
        return FakeResp(body)

    with patch("dexcost.cloud_detect.urllib.request.urlopen", fake_urlopen):
        env = _probe_azure()
    assert env.region == "eastus"
    assert env.instance_type == "Standard_D2s_v3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cloud_detect_instance_type.py -v`
Expected: FAIL — `CloudEnv.instance_type` does not exist.

- [ ] **Step 3: Extend `CloudEnv`**

In `python/src/dexcost/cloud_detect.py`, add `instance_type: str | None = None` to the `CloudEnv` dataclass (after `source`). Frozen dataclass requires the field to be a kwarg, default None — existing callers continue to work.

- [ ] **Step 4: Extend each provider probe**

`_probe_aws`: after the region read, hit `/latest/meta-data/instance-type` with the same token. Return `CloudEnv(..., instance_type=instance_type or None)`. Wrap the second GET in its own try/except so a region success isn't lost if instance-type fails.

`_probe_gcp`: after the zone read, hit `/computeMetadata/v1/instance/machine-type` with `Metadata-Flavor: Google`. Strip `projects/.../machineTypes/` prefix from the response. Return `CloudEnv(..., instance_type=...)`.

`_probe_azure`: the existing `/metadata/instance` JSON already includes `.compute.vmSize` — just read it alongside `.compute.location`. No extra HTTP call.

- [ ] **Step 5: Preserve `instance_type` when stitching env+IMDS results**

In `_background` (orchestration in `start_background_detection`): when Phase 2 produced a partial result, preserve `instance_type` from whichever source had it.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cloud_detect_instance_type.py tests/test_cloud_detect.py -v`
Expected: PASS (existing cloud_detect tests still green since `instance_type` defaults to None).

- [ ] **Step 7: Commit**

```bash
git add python/src/dexcost/cloud_detect.py python/tests/test_cloud_detect_instance_type.py
git commit -m "feat(compute): extend CloudEnv with instance_type from IMDS (Decision #3)"
```

---

### Task 2: `CgroupReader` — `/sys/fs/cgroup/*` file parsing

**Files:**
- Create: `python/src/dexcost/cgroup_reader.py`
- Test: `python/tests/test_cgroup_reader.py` (create)

Pure helpers — no I/O outside `/sys/fs/cgroup/`, fail-silent on missing files (non-Linux, cgroup-v1, container without cgroup mount).

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_cgroup_reader.py`:

```python
"""cgroup v2 file parsing — cpu.stat / cpu.max / memory.peak / memory.max."""

from pathlib import Path
from unittest.mock import patch

from dexcost.cgroup_reader import (
    CpuStat, CpuMax, read_cpu_stat, read_cpu_max,
    read_memory_peak, read_memory_max, read_memory_current,
)


def _seed_cgroup_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    return tmp_path


def test_read_cpu_stat_parses_usage_usec(tmp_path):
    root = _seed_cgroup_dir(tmp_path, {
        "cpu.stat": "usage_usec 12345\nuser_usec 6000\nsystem_usec 6345\n"
                    "nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n",
    })
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        s = read_cpu_stat()
    assert isinstance(s, CpuStat)
    assert s.usage_usec == 12345


def test_read_cpu_max_with_quota(tmp_path):
    root = _seed_cgroup_dir(tmp_path, {"cpu.max": "100000 100000\n"})
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        m = read_cpu_max()
    assert m == CpuMax(quota_us=100000, period_us=100000, vcpu_count=1.0)


def test_read_cpu_max_quota_fraction(tmp_path):
    # 256 shares / 1024 = 0.25 vCPU (a small Fargate task).
    root = _seed_cgroup_dir(tmp_path, {"cpu.max": "25000 100000\n"})
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        m = read_cpu_max()
    assert m.vcpu_count == 0.25


def test_read_cpu_max_unlimited(tmp_path):
    root = _seed_cgroup_dir(tmp_path, {"cpu.max": "max 100000\n"})
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        m = read_cpu_max()
    # Falls back to nproc — assertion is "not None and > 0".
    assert m.quota_us is None
    assert m.vcpu_count > 0


def test_read_memory_peak(tmp_path):
    root = _seed_cgroup_dir(tmp_path, {"memory.peak": "2147483648\n"})
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        v = read_memory_peak()
    assert v == 2147483648


def test_read_memory_max_finite(tmp_path):
    root = _seed_cgroup_dir(tmp_path, {"memory.max": "1073741824\n"})
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        v = read_memory_max()
    assert v == 1073741824


def test_read_memory_max_unlimited(tmp_path):
    root = _seed_cgroup_dir(tmp_path, {"memory.max": "max\n"})
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        v = read_memory_max()
    assert v is None


def test_missing_files_return_none(tmp_path):
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", tmp_path):
        assert read_cpu_stat() is None
        assert read_cpu_max() is None
        assert read_memory_peak() is None
        assert read_memory_max() is None
        assert read_memory_current() is None


def test_malformed_cpu_stat_returns_none(tmp_path):
    root = _seed_cgroup_dir(tmp_path, {"cpu.stat": "garbage\n"})
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        assert read_cpu_stat() is None


def test_memory_peak_falls_back_to_current_when_missing(tmp_path):
    # Kernel < 5.19 — memory.peak absent, memory.current present.
    root = _seed_cgroup_dir(tmp_path, {"memory.current": "1024\n"})
    with patch("dexcost.cgroup_reader._CGROUP_ROOT", root):
        assert read_memory_peak() is None  # no fabrication; caller decides fallback
        assert read_memory_current() == 1024
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_cgroup_reader.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `cgroup_reader.py`**

Create `python/src/dexcost/cgroup_reader.py`:

```python
"""Cgroup v2 file readers.

Fail-silent contract (convention §9): every read returns None on missing /
malformed input. Non-Linux hosts, cgroup-v1 kernels, and containers without
a cgroup mount all silently return None — the caller decides the fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class CpuStat:
    usage_usec: int


@dataclass(frozen=True)
class CpuMax:
    quota_us: int | None   # None if "max" (no quota set)
    period_us: int
    vcpu_count: float      # quota/period, or os.cpu_count() if unlimited


def _read_int(name: str) -> int | None:
    try:
        raw = (_CGROUP_ROOT / name).read_text().strip()
    except OSError:
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def read_cpu_stat() -> CpuStat | None:
    """cpu.stat — `usage_usec <N>` (microseconds of CPU time consumed)."""
    try:
        raw = (_CGROUP_ROOT / "cpu.stat").read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        if line.startswith("usage_usec "):
            try:
                return CpuStat(usage_usec=int(line.split()[1]))
            except (ValueError, IndexError):
                return None
    return None


def read_cpu_max() -> CpuMax | None:
    """cpu.max — `<quota|max> <period>` (microseconds)."""
    try:
        raw = (_CGROUP_ROOT / "cpu.max").read_text().strip()
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 2:
        return None
    try:
        period_us = int(parts[1])
    except ValueError:
        return None
    if parts[0] == "max":
        return CpuMax(
            quota_us=None,
            period_us=period_us,
            vcpu_count=float(os.cpu_count() or 1),
        )
    try:
        quota_us = int(parts[0])
    except ValueError:
        return None
    if period_us <= 0:
        return None
    return CpuMax(
        quota_us=quota_us,
        period_us=period_us,
        vcpu_count=quota_us / period_us,
    )


def read_memory_peak() -> int | None:
    """memory.peak — bytes (kernel >= 5.19). None if file absent."""
    return _read_int("memory.peak")


def read_memory_max() -> int | None:
    """memory.max — bytes. Returns None if 'max' (unlimited)."""
    return _read_int("memory.max")


def read_memory_current() -> int | None:
    """memory.current — bytes at the moment of read."""
    return _read_int("memory.current")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_cgroup_reader.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/cgroup_reader.py python/tests/test_cgroup_reader.py
git commit -m "feat(compute): cgroup v2 file readers (cpu.stat, cpu.max, memory.{peak,max,current})"
```

---

### Task 3: `ComputeRuntimeResolver` — runtime detection cascade

**Files:**
- Create: `python/src/dexcost/compute_runtime.py`
- Test: `python/tests/test_compute_runtime.py` (create)

Resolves the active runtime at task start. Cascade priority per capture spec §5.5 (k8s wins over the underlying VM; serverless env vars win over IaaS DMI).

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_compute_runtime.py`:

```python
"""Compute runtime resolution — env-var cascade + cloud_detect fallback."""

from dexcost.compute_runtime import resolve_runtime, RuntimeKind


def test_lambda_env_wins(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert resolve_runtime() == RuntimeKind.LAMBDA


def test_fargate_env_wins(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    assert resolve_runtime() == RuntimeKind.FARGATE


def test_cloud_run_env_wins(monkeypatch):
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "ECS_CONTAINER_METADATA_URI_V4"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("K_SERVICE", "svc")
    assert resolve_runtime() == RuntimeKind.CLOUD_RUN


def test_azure_functions_env_wins(monkeypatch):
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "ECS_CONTAINER_METADATA_URI_V4", "K_SERVICE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("FUNCTIONS_WORKER_RUNTIME", "python")
    assert resolve_runtime() == RuntimeKind.AZURE_FUNCTIONS


def test_vercel_env_wins(monkeypatch):
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "ECS_CONTAINER_METADATA_URI_V4",
              "K_SERVICE", "FUNCTIONS_WORKER_RUNTIME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("VERCEL", "1")
    assert resolve_runtime() == RuntimeKind.VERCEL


def test_k8s_wins_over_ec2(monkeypatch):
    # KUBERNETES_SERVICE_HOST set + AWS DMI both true → k8s_pod wins per §5.5.
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "ECS_CONTAINER_METADATA_URI_V4",
              "K_SERVICE", "FUNCTIONS_WORKER_RUNTIME", "VERCEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert resolve_runtime() == RuntimeKind.K8S_POD


def test_falls_through_to_cloud_detect_ec2(monkeypatch):
    from dexcost import cloud_detect
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "ECS_CONTAINER_METADATA_URI_V4",
              "K_SERVICE", "FUNCTIONS_WORKER_RUNTIME", "VERCEL",
              "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv("aws", "us-east-1", "dmi", instance_type="c7g.xlarge"),
    )
    assert resolve_runtime() == RuntimeKind.EC2


def test_undetected_returns_unknown(monkeypatch):
    from dexcost import cloud_detect
    for v in ("AWS_LAMBDA_FUNCTION_NAME", "ECS_CONTAINER_METADATA_URI_V4",
              "K_SERVICE", "FUNCTIONS_WORKER_RUNTIME", "VERCEL",
              "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv(None, None, "none"),
    )
    assert resolve_runtime() == RuntimeKind.UNKNOWN
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_compute_runtime.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `compute_runtime.py`**

Create `python/src/dexcost/compute_runtime.py`:

```python
"""Active compute-runtime resolver.

Cascade priority (capture spec §5.5):
  1. Serverless env vars (Lambda, Fargate, Cloud Run, Azure Functions, Vercel)
  2. KUBERNETES_SERVICE_HOST → k8s_pod (wins over underlying VM)
  3. cloud_detect IaaS (EC2 / GCE / Azure VM)
  4. UNKNOWN
"""

from __future__ import annotations

import os
from enum import Enum

from dexcost import cloud_detect


class RuntimeKind(str, Enum):
    LAMBDA = "lambda"
    FARGATE = "fargate"
    EC2 = "ec2"
    CLOUD_RUN = "cloud_run"
    CLOUD_FUNCTIONS = "cloud_functions"
    GCE = "gce"
    AZURE_FUNCTIONS = "azure_functions"
    AZURE_VM = "azure_vm"
    VERCEL = "vercel_fluid"
    K8S_POD = "k8s_pod"
    UNKNOWN = "unknown"


def resolve_runtime() -> RuntimeKind:
    # 1. Serverless env vars (highest priority).
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return RuntimeKind.LAMBDA
    if os.environ.get("ECS_CONTAINER_METADATA_URI_V4"):
        return RuntimeKind.FARGATE
    if os.environ.get("K_SERVICE"):
        # Cloud Run AND Cloud Functions Gen2 both set K_SERVICE. Gen2 also sets
        # FUNCTION_TARGET; distinguish so dashboards can break out function-vs-service.
        if os.environ.get("FUNCTION_TARGET"):
            return RuntimeKind.CLOUD_FUNCTIONS
        return RuntimeKind.CLOUD_RUN
    if os.environ.get("FUNCTIONS_WORKER_RUNTIME"):
        return RuntimeKind.AZURE_FUNCTIONS
    if os.environ.get("VERCEL"):
        return RuntimeKind.VERCEL

    # 2. K8s wins over underlying VM (avoids double-counting; capture §5.5).
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return RuntimeKind.K8S_POD

    # 3. Fall through to cloud_detect for IaaS.
    env = cloud_detect.get_cloud_env()
    if env.provider == "aws":
        return RuntimeKind.EC2
    if env.provider == "gcp":
        return RuntimeKind.GCE
    if env.provider == "azure":
        return RuntimeKind.AZURE_VM

    return RuntimeKind.UNKNOWN
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_compute_runtime.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/compute_runtime.py python/tests/test_compute_runtime.py
git commit -m "feat(compute): runtime resolver — serverless env vars > k8s > cloud_detect IaaS"
```

---

### Task 4: `FargateTaskMetadata` — ECS metadata endpoint helper

**Files:**
- Create: `python/src/dexcost/fargate_metadata.py`
- Test: `python/tests/test_fargate_metadata.py` (create)

One HTTP call per process, cached. Exposes `vcpu_count` (float) and `memory_bytes_limit` (MiB → bytes per Decision #7). Fail-silent on unreachable.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_fargate_metadata.py`:

```python
"""Fargate ECS task metadata helper."""

import json
from unittest.mock import patch

import pytest

from dexcost.fargate_metadata import (
    FargateTaskMetadata, fetch_fargate_metadata, _reset_for_tests,
)


@pytest.fixture(autouse=True)
def _clear():
    _reset_for_tests()


def test_returns_vcpu_and_memory(monkeypatch):
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc"
    )

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self):
            return json.dumps({
                "TaskARN": "arn:...:task/abc",
                "Limits": {"CPU": 0.5, "Memory": 1024},  # MiB
            }).encode()

    with patch("dexcost.fargate_metadata.urllib.request.urlopen",
               lambda *a, **k: FakeResp()):
        m = fetch_fargate_metadata()
    assert isinstance(m, FargateTaskMetadata)
    assert m.vcpu_count == 0.5
    # 1024 MiB → bytes (Decision #7 — Fargate is binary GiB).
    assert m.memory_bytes_limit == 1024 * 1024 * 1024


def test_no_env_var_returns_none(monkeypatch):
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    assert fetch_fargate_metadata() is None


def test_unreachable_returns_none_and_logs_once(monkeypatch, caplog):
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc"
    )

    def boom(*a, **k):
        raise OSError("network unreachable")

    with patch("dexcost.fargate_metadata.urllib.request.urlopen", boom):
        assert fetch_fargate_metadata() is None
        assert fetch_fargate_metadata() is None  # second call still None, no extra log

    messages = [r.getMessage() for r in caplog.records
                if "fargate metadata" in r.getMessage().lower()]
    assert len(messages) == 1  # log-once per convention §11


def test_cached_after_first_success(monkeypatch):
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc"
    )
    calls = {"n": 0}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self):
            calls["n"] += 1
            return json.dumps({"Limits": {"CPU": 1, "Memory": 512}}).encode()

    with patch("dexcost.fargate_metadata.urllib.request.urlopen",
               lambda *a, **k: FakeResp()):
        a = fetch_fargate_metadata()
        b = fetch_fargate_metadata()
    assert a is b is not None
    assert calls["n"] == 1
```

- [ ] **Step 2: Implement**

Create `python/src/dexcost/fargate_metadata.py`:

```python
"""Fargate ECS task metadata reader.

Hits `${ECS_CONTAINER_METADATA_URI_V4}/task` once per process and caches
the parsed result. Exposes vcpu_count (float) and memory_bytes_limit (int,
converted from MiB per Decision #7).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

_log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 0.25  # seconds

_lock = threading.Lock()
_cached: "FargateTaskMetadata | None" = None
_resolved = False
_warned = False


def _reset_for_tests() -> None:
    global _cached, _resolved, _warned
    with _lock:
        _cached = None
        _resolved = False
        _warned = False


@dataclass(frozen=True)
class FargateTaskMetadata:
    vcpu_count: float
    memory_bytes_limit: int


def fetch_fargate_metadata() -> FargateTaskMetadata | None:
    global _cached, _resolved, _warned

    with _lock:
        if _resolved:
            return _cached

    base = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if not base:
        with _lock:
            _resolved = True
        return None

    url = base.rstrip("/") + "/task"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError,
            json.JSONDecodeError) as exc:
        with _lock:
            _resolved = True
            if not _warned:
                _warned = True
                _log.warning(
                    "fargate metadata unreachable (%s); compute cost will fall "
                    "through to default rates", exc,
                )
        return None

    limits = payload.get("Limits", {}) or {}
    try:
        vcpu = float(limits.get("CPU"))
        mem_mib = int(limits.get("Memory"))
    except (TypeError, ValueError):
        with _lock:
            _resolved = True
        return None

    # Decision #7 — Fargate memory is in MiB (binary), NOT MB. The ~4.86%
    # silent over-attribution bug if confused. Convert to bytes via binary GiB.
    memory_bytes = mem_mib * 1024 * 1024

    result = FargateTaskMetadata(
        vcpu_count=vcpu, memory_bytes_limit=memory_bytes,
    )
    with _lock:
        _cached = result
        _resolved = True
    return result
```

- [ ] **Step 3: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_fargate_metadata.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add python/src/dexcost/fargate_metadata.py python/tests/test_fargate_metadata.py
git commit -m "feat(compute): Fargate ECS task metadata helper (MiB->bytes per Decision #7)"
```

---

### Task 5: Bundled compute catalog `data/compute_prices.json`

**Files:**
- Create: `python/src/dexcost/data/compute_prices.json`
- Test: `python/tests/test_compute_catalog_integrity.py` (create)

This task is **the launch-prerequisite data-entry job** called out in spec §4.6. Lambda x86_64 + arm64 in all commercial AWS regions; Fargate x86_64 + arm64 in all commercial AWS regions; Cloud Run + Cloud Functions in all commercial GCP regions; Azure Functions Consumption in all commercial Azure regions; Vercel Fluid (single global); top ~50 EC2 / GCE / Azure VM instance types.

- [ ] **Step 1: Write the failing integrity test**

Create `python/tests/test_compute_catalog_integrity.py`:

```python
"""Compute catalog integrity — structure, Decimal parsing, freshness, dispatch coverage."""

import datetime as _dt
import importlib.resources as ir
import json
import warnings
from decimal import Decimal


def _load():
    raw = ir.files("dexcost").joinpath("data").joinpath("compute_prices.json").read_text()
    return json.loads(raw), raw


def test_catalog_parses_as_json():
    data, _ = _load()
    assert "_meta" in data


def test_meta_has_required_default_keys():
    data, _ = _load()
    meta = data["_meta"]
    required = [
        "version", "last_updated", "currency",
        "default_lambda_request_usd", "default_lambda_gb_second_usd",
        "default_fargate_vcpu_second_usd", "default_fargate_gib_second_usd",
        "default_cloud_run_request_usd", "default_cloud_run_vcpu_second_usd",
        "default_cloud_run_gib_second_usd",
        "default_azure_functions_execution_usd", "default_azure_functions_gb_second_usd",
        "default_vercel_cpu_hour_usd", "default_vercel_memory_gb_hour_usd",
        "default_ec2_vcpu_hour_usd", "default_k8s_pod_vcpu_hour_usd",
        "description", "notes",
    ]
    for k in required:
        assert k in meta, f"_meta missing {k}"
        Decimal(meta[k]) if k.startswith("default_") else None
    assert meta["currency"] == "USD"


def test_every_provider_has_last_verified():
    data, _ = _load()
    today = _dt.date.today()
    soft_limit = _dt.timedelta(days=180)
    for provider, block in data.items():
        if provider == "_meta":
            continue
        verified = _dt.date.fromisoformat(block["_last_verified"])
        if today - verified > soft_limit:
            warnings.warn(
                f"compute_prices.json: {provider} _last_verified is "
                f"{(today - verified).days} days old (soft limit 180)",
                stacklevel=2,
            )


def test_all_providers_and_runtimes_present():
    data, _ = _load()
    assert {"aws", "gcp", "azure", "vercel"} <= set(data.keys())
    assert {"lambda", "fargate", "ec2"} <= set(data["aws"].keys()) - {"_last_verified"}
    assert {"cloud_run", "cloud_functions", "gce"} <= set(data["gcp"].keys()) - {"_last_verified"}
    assert {"functions_consumption", "vm"} <= set(data["azure"].keys()) - {"_last_verified"}
    assert "fluid" in data["vercel"]


def test_lambda_has_both_architectures():
    data, _ = _load()
    default = data["aws"]["lambda"]["default"]
    assert set(default.keys()) == {"x86_64", "arm64"}
    for arch in ("x86_64", "arm64"):
        Decimal(default[arch]["request_usd"])
        Decimal(default[arch]["gb_second_usd"])


def test_fargate_has_both_architectures():
    data, _ = _load()
    default = data["aws"]["fargate"]["default"]
    assert set(default.keys()) == {"x86_64", "arm64"}


def test_arm_cheaper_than_x86_on_lambda():
    data, _ = _load()
    region = next(iter(data["aws"]["lambda"]["regions"].values()))
    arm = Decimal(region["arm64"]["gb_second_usd"])
    x86 = Decimal(region["x86_64"]["gb_second_usd"])
    assert arm < x86, "arm64 must be cheaper than x86_64 per AWS pricing"


def test_top_instance_types_present_for_ec2_us_east_1():
    data, _ = _load()
    instance_types = data["aws"]["ec2"]["regions"]["us-east-1"]["instance_types"]
    for must_have in ("c7g.xlarge", "m7i.large", "t3.medium"):
        assert must_have in instance_types, f"missing EC2 SKU: {must_have}"
        Decimal(instance_types[must_have]["hourly_usd"])
        Decimal(instance_types[must_have]["vcpu_count"])


def test_every_dispatch_billing_model_has_a_rate_path():
    # Each `billing_model` enum from the spec dispatch table must have a
    # rate path: either a per-runtime regions/default block, or a _meta default.
    data, _ = _load()
    meta = data["_meta"]
    assert "default_lambda_request_usd" in meta          # lambda
    assert "default_fargate_vcpu_second_usd" in meta     # fargate
    assert "default_cloud_run_request_usd" in meta       # cloud_run_request, cloud_run_instance, cloud_functions
    assert "default_azure_functions_execution_usd" in meta  # azure_functions
    assert "default_vercel_cpu_hour_usd" in meta         # vercel_fluid
    assert "default_ec2_vcpu_hour_usd" in meta           # ec2, gce, azure_vm (per-vcpu-hour share)
    assert "default_k8s_pod_vcpu_hour_usd" in meta       # k8s_pod
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_compute_catalog_integrity.py -v`
Expected: FAIL — file does not exist.

- [ ] **Step 3: Author `compute_prices.json`**

Create `python/src/dexcost/data/compute_prices.json` using the exact shape in spec §4.1. Skeleton (must be filled out region-by-region; do NOT ship with the skeleton's "..." regions):

```json
{
  "_meta": {
    "version": "1.0.0",
    "last_updated": "2026-05-21",
    "currency": "USD",
    "default_lambda_request_usd": "0.0000002",
    "default_lambda_gb_second_usd": "0.0000166667",
    "default_fargate_vcpu_second_usd": "0.0000111111",
    "default_fargate_gib_second_usd": "0.0000012222",
    "default_cloud_run_request_usd": "0.0000004",
    "default_cloud_run_vcpu_second_usd": "0.000024",
    "default_cloud_run_gib_second_usd": "0.0000025",
    "default_azure_functions_execution_usd": "0.0000002",
    "default_azure_functions_gb_second_usd": "0.000016",
    "default_vercel_cpu_hour_usd": "0.128",
    "default_vercel_memory_gb_hour_usd": "0.0106",
    "default_ec2_vcpu_hour_usd": "0.0464",
    "default_k8s_pod_vcpu_hour_usd": "0.0464",
    "description": "Dexcost compute catalog — per-billing-model rates by cloud provider/region. Community-maintained; submit PRs to add or refresh rates.",
    "notes": "Rates are standard on-demand pricing, FIRST tier only. Sustained-use discounts (GCE), Savings Plans (AWS), and Reserved Instances are not modelled. Lambda/Azure Functions/Vercel rates are DECIMAL GB (10^9 bytes); Fargate/Cloud Run rates are BINARY GiB (2^30 bytes) — see spec §6.2."
  },
  "aws":    { "_last_verified": "2026-05-21", "lambda": { "...": "..." }, "fargate": { "...": "..." }, "ec2": { "...": "..." } },
  "gcp":    { "_last_verified": "2026-05-21", "cloud_run": { "...": "..." }, "cloud_functions": { "...": "..." }, "gce": { "...": "..." } },
  "azure":  { "_last_verified": "2026-05-21", "functions_consumption": { "...": "..." }, "vm": { "...": "..." } },
  "vercel": { "_last_verified": "2026-05-21", "fluid": { "...": "..." } }
}
```

> **Data entry instructions:** Open each provider's public pricing pages and transcribe every commercial region. Decimal-string-encoded throughout. Human-review pass per provider block before saving.
> - AWS Lambda: https://aws.amazon.com/lambda/pricing/
> - AWS Fargate: https://aws.amazon.com/fargate/pricing/
> - AWS EC2 (top 50 SKUs by usage): https://aws.amazon.com/ec2/pricing/on-demand/
> - GCP Cloud Run: https://cloud.google.com/run/pricing
> - GCP Cloud Functions: https://cloud.google.com/functions/pricing
> - GCP Compute Engine (top 50 SKUs): https://cloud.google.com/compute/all-pricing
> - Azure Functions: https://azure.microsoft.com/en-us/pricing/details/functions/
> - Azure VM (top 50 SKUs): https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/
> - Vercel Fluid: https://vercel.com/docs/pricing

The exact rates change quarterly; this task captures the catalog snapshot at SDK ship time. Ongoing refresh is the catalog-update workflow, not a code change.

- [ ] **Step 4: Verify bundling**

```bash
cd python && uv run python -c "import importlib.resources as ir; print(ir.files('dexcost').joinpath('data').joinpath('compute_prices.json').is_file())"
```

Expected: `True`. The `data/` directory is already shipped (see `egress_prices.json`). No `pyproject.toml` change.

- [ ] **Step 5: Run integrity test**

Run: `cd python && uv run pytest tests/test_compute_catalog_integrity.py -v`
Expected: PASS (every catalog assertion green; the freshness check is `warnings.warn`).

- [ ] **Step 6: Commit**

```bash
git add python/src/dexcost/data/compute_prices.json python/tests/test_compute_catalog_integrity.py
git commit -m "feat(compute): bundle compute price catalog (AWS/GCP/Azure/Vercel)"
```

---

### Task 6: `compute_pricing.py` — dispatch + per-billing-model math + degradation ladder

**Files:**
- Create: `python/src/dexcost/compute_pricing.py`
- Test: `python/tests/test_compute_pricing.py` (create)

The heart of the v2 layer. Dispatches on `details.billing_model` to apply the math from spec §6, with the per-runtime memory-unit conversion table from §6.2 pinned at the catalog-lookup boundary.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_compute_pricing.py`:

```python
"""Compute pricing — per-billing-model math, degradation ladder, no-float-drift."""

import json
from decimal import Decimal

import pytest

from dexcost.cloud_detect import CloudEnv
from dexcost.compute_pricing import ComputePricingEngine, _reset_warning_state


@pytest.fixture
def engine():
    return ComputePricingEngine()


def _env(provider="aws", region="us-east-1", instance_type=None):
    return CloudEnv(provider=provider, region=region, source="env",
                    instance_type=instance_type)


# -------- Lambda --------------------------------------------------------------

def test_lambda_x86_canonical_case(engine):
    # 1024 MiB (= 1024*1024*1024 bytes), 100 ms, x86_64, us-east-1.
    details = {
        "billing_model": "lambda",
        "duration_ms": 100,
        "memory_bytes_limit": 1024 * 1024 * 1024,
        "vcpu_count": 1.0,
        "vcpu_seconds_used": 0,
        "invocation_count": 1,
        "region": "us-east-1",
        "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(details, _env(), {})
    # Lambda divisor is DECIMAL GB (10^9 bytes) per Decision #7.
    # gb_seconds = 1024^3 / 10^9 * (100/1000) ≈ 0.10737418240
    # cost = 1 * 0.0000002 + gb_seconds * 0.0000166667
    gb_seconds = Decimal(1024 * 1024 * 1024) / Decimal("1000000000") * Decimal("0.1")
    expected = Decimal("0.0000002") + gb_seconds * Decimal("0.0000166667")
    assert cost.cost_usd == expected
    assert cost.cost_confidence == "computed"


def test_lambda_arm_is_cheaper(engine):
    base_details = {
        "billing_model": "lambda", "duration_ms": 100,
        "memory_bytes_limit": 1024 * 1024 * 1024, "vcpu_count": 1.0,
        "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "us-east-1",
    }
    x86 = engine.resolve_compute_cost({**base_details, "architecture": "x86_64"}, _env(), {})
    arm = engine.resolve_compute_cost({**base_details, "architecture": "arm64"}, _env(), {})
    assert arm.cost_usd < x86.cost_usd


# -------- Fargate -------------------------------------------------------------

def test_fargate_uses_binary_gib_divisor(engine):
    # The bug-prevention test. 0.5 vCPU * 1 GiB * 60s in us-east-1.
    details = {
        "billing_model": "fargate",
        "duration_ms": 60_000,
        "memory_bytes_limit": 1024 * 1024 * 1024,  # 1 GiB
        "vcpu_count": 0.5,
        "vcpu_seconds_used": 30,
        "invocation_count": 0,
        "region": "us-east-1",
        "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(details, _env(), {})
    # cost = (0.5 * 60) * 0.0000111111 + (1.0 * 60) * 0.0000012222
    # The "1.0 GiB" multiplier MUST come from 1024^3 / 1024^3, NOT 1024^3 / 10^9.
    # If the implementation confuses the divisor, the GiB term becomes
    # 1.073741824 instead of 1.0 (~7.4% over-attribution).
    vcpu_term = Decimal("30") * Decimal("0.0000111111")
    gib_term = Decimal("60") * Decimal("0.0000012222")  # exactly 1.0 GiB * 60s
    assert cost.cost_usd == vcpu_term + gib_term


# -------- Cloud Run (default = request-based) ---------------------------------

def test_cloud_run_default_is_estimated(engine):
    details = {
        "billing_model": "cloud_run_request",
        "duration_ms": 250,
        "memory_bytes_limit": 256 * 1024 * 1024,  # 256 MiB
        "vcpu_count": 0.5,
        "vcpu_seconds_used": 0,
        "invocation_count": 1,
        "region": "us-central1",
        "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(provider="gcp", region="us-central1"), {},
    )
    assert cost.cost_confidence == "estimated"
    assert cost.pricing_source == "compute_catalog:cloud_run:request_based_default"


# -------- Cloud Run override (instance-based) ---------------------------------

def test_cloud_run_instance_override_is_computed(engine):
    details = {
        "billing_model": "cloud_run_request",  # capture emits request_; override flips
        "duration_ms": 0, "memory_bytes_limit": 256 * 1024 * 1024,
        "vcpu_count": 0.5, "vcpu_seconds_used": 0, "invocation_count": 0,
        "region": "us-central1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(provider="gcp", region="us-central1"),
        {"cloud_run": "instance"},
        window_s=Decimal("60"),
    )
    assert cost.cost_confidence == "computed"
    assert cost.pricing_source.endswith("instance_override")


# -------- Vercel --------------------------------------------------------------

def test_vercel_active_cpu_approximates_wall_duration(engine):
    details = {
        "billing_model": "vercel_fluid",
        "duration_ms": 500,
        "memory_bytes_limit": 256 * 1024 * 1024,
        "vcpu_count": 1.0, "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": None, "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(details, _env(provider=None, region=None), {})
    assert cost.cost_usd > 0
    assert cost.cost_confidence == "computed"


# -------- EC2 instance share --------------------------------------------------

def test_ec2_share_factor_math(engine):
    # 1 vCPU-second used over 60s window on a 4-vCPU c7g.xlarge.
    # share_factor = (1) / (4 * 60) = 0.004166...
    # task_instance_hours = share_factor * (60 / 3600)
    # cost = task_instance_hours * 0.1450
    details = {
        "billing_model": "ec2",
        "duration_ms": 60_000,
        "memory_bytes_limit": 0, "vcpu_count": 4.0,
        "vcpu_seconds_used": 1.0, "invocation_count": 0,
        "region": "us-east-1",
        "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(provider="aws", region="us-east-1",
                      instance_type="c7g.xlarge"),
        {}, window_s=Decimal("60"),
    )
    expected_share = Decimal("1") / (Decimal("4") * Decimal("60"))
    expected_hours = expected_share * (Decimal("60") / Decimal("3600"))
    expected = expected_hours * Decimal("0.1450")
    assert cost.cost_usd == expected


# -------- K8s pod default (no node-aware) ------------------------------------

def test_k8s_pod_limits_math(engine):
    details = {
        "billing_model": "k8s_pod",
        "duration_ms": 60_000,
        "memory_bytes_limit": 512 * 1024 * 1024,
        "vcpu_count": 0.5, "vcpu_seconds_used": 0.3,
        "invocation_count": 0, "region": None,
        "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(provider=None, region=None), {},
        window_s=Decimal("60"),
    )
    expected = Decimal("0.5") * (Decimal("60") / Decimal("3600")) * Decimal("0.0464")
    assert cost.cost_usd == expected
    assert cost.cost_confidence == "computed"


# -------- Degradation ladder --------------------------------------------------

def test_tier3_unknown_provider_falls_to_meta_default(engine):
    # Provider not detected → universal default rate.
    details = {
        "billing_model": "lambda", "duration_ms": 100,
        "memory_bytes_limit": 128 * 1024 * 1024,
        "vcpu_count": 1.0, "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": None, "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(details, _env(provider=None, region=None), {})
    assert cost.cost_confidence == "estimated"
    assert cost.pricing_source == "compute_catalog:default:lambda"


def test_tier4_missing_catalog_uses_hardcoded(tmp_path):
    bogus = tmp_path / "no.json"
    eng = ComputePricingEngine(catalog_path=bogus)
    details = {
        "billing_model": "lambda", "duration_ms": 100,
        "memory_bytes_limit": 128 * 1024 * 1024,
        "vcpu_count": 1.0, "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "us-east-1", "architecture": "x86_64",
    }
    cost = eng.resolve_compute_cost(details, _env(), {})
    assert cost.cost_usd > 0
    assert cost.pricing_source.startswith("compute_catalog:hardcoded")
    assert cost.cost_confidence == "estimated"


def test_tier5_computation_failure_returns_zero():
    eng = ComputePricingEngine()
    bad = {"billing_model": "lambda", "duration_ms": "not-a-number"}
    cost = eng.resolve_compute_cost(bad, _env(), {})
    assert cost.cost_usd == Decimal("0")


# -------- No-float-drift ------------------------------------------------------

def test_decimal_no_float_drift_per_conversion():
    # Fargate / Cloud Run: binary GiB.
    assert Decimal(2 * 1024 * 1024 * 1024) / Decimal(1024 * 1024 * 1024) == Decimal("2")
    # Lambda / Azure Functions / Vercel: decimal GB.
    assert Decimal(2 * 1000 * 1000 * 1000) / Decimal("1000000000") == Decimal("2")
    # Per-billing-model multiplication step against hand-computed expected.
    assert Decimal("0.0000166667") * Decimal("1024") == Decimal("0.0170666608")


# -------- Warning state -------------------------------------------------------

def test_warn_once_per_failure_mode(tmp_path, caplog):
    _reset_warning_state()
    bogus = tmp_path / "missing.json"
    ComputePricingEngine(catalog_path=bogus)
    ComputePricingEngine(catalog_path=bogus)
    msgs = [r.getMessage() for r in caplog.records
            if "compute catalog" in r.getMessage().lower()]
    assert len(msgs) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_compute_pricing.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `compute_pricing.py`**

Create `python/src/dexcost/compute_pricing.py`. Structure mirrors `egress_pricing.py`:

- `ComputePricingEngine(catalog_path=None)` — loads bundled catalog, exposes `catalog_version`.
- `resolve_compute_cost(details, cloud_env, overrides, window_s=None) → ComputeCost(cost_usd, pricing_source, cost_confidence)`.
- Internal dispatch by `details["billing_model"]` to per-billing-model functions.
- Five-tier degradation: per-region exact → per-runtime default → `_meta.default_*` → hardcoded constants → `cost_usd=0` (Tier 5 try/except in the public method).
- Module-level `_warned_modes` set + `_reset_warning_state()` test helper, mirroring `egress_pricing` exactly.

Key per-billing-model implementations (all use `Decimal` arithmetic per Decision #7 — NEVER float):

```python
# Hardcoded constants — Tier 4 last resort. Must match _meta defaults.
_HARDCODED = {
    "lambda":             {"request_usd": Decimal("0.0000002"),
                           "gb_second_usd": Decimal("0.0000166667")},
    "fargate":            {"vcpu_second_usd": Decimal("0.0000111111"),
                           "gib_second_usd": Decimal("0.0000012222")},
    "cloud_run_request":  {"request_usd": Decimal("0.0000004"),
                           "vcpu_second_usd": Decimal("0.000024"),
                           "gib_second_usd": Decimal("0.0000025")},
    "cloud_run_instance": {"vcpu_second_usd": Decimal("0.000024"),
                           "gib_second_usd": Decimal("0.0000025")},
    "cloud_functions":    {"request_usd": Decimal("0.0000004"),
                           "vcpu_second_usd": Decimal("0.000024"),
                           "gib_second_usd": Decimal("0.0000025")},
    "azure_functions":    {"execution_usd": Decimal("0.0000002"),
                           "gb_second_usd": Decimal("0.000016")},
    "vercel_fluid":       {"active_cpu_hour_usd": Decimal("0.128"),
                           "memory_gb_hour_usd": Decimal("0.0106"),
                           "invocation_usd": Decimal("0.000000600")},
    "ec2":                {"vcpu_hour_usd": Decimal("0.0464")},
    "gce":                {"vcpu_hour_usd": Decimal("0.0475")},
    "azure_vm":           {"vcpu_hour_usd": Decimal("0.046")},
    "k8s_pod":            {"vcpu_hour_usd": Decimal("0.0464")},
}

_GB_DECIMAL = Decimal("1000000000")
_GIB_BINARY = Decimal(1024 * 1024 * 1024)
_HOUR_S = Decimal("3600")


def _lambda_cost(details, rate):
    duration_s = Decimal(details["duration_ms"]) / Decimal("1000")
    memory_gb = Decimal(details["memory_bytes_limit"]) / _GB_DECIMAL
    gb_seconds = memory_gb * duration_s
    invocations = Decimal(details["invocation_count"])
    return invocations * rate["request_usd"] + gb_seconds * rate["gb_second_usd"]


def _fargate_cost(details, rate, window_s):
    memory_gib = Decimal(details["memory_bytes_limit"]) / _GIB_BINARY  # binary!
    vcpu_count = Decimal(str(details["vcpu_count"]))
    return (vcpu_count * window_s) * rate["vcpu_second_usd"] \
         + (memory_gib * window_s) * rate["gib_second_usd"]


def _cloud_run_request_cost(details, rate):
    duration_s = Decimal(details["duration_ms"]) / Decimal("1000")
    memory_gib = Decimal(details["memory_bytes_limit"]) / _GIB_BINARY
    vcpu_count = Decimal(str(details["vcpu_count"]))
    invocations = Decimal(details["invocation_count"])
    return invocations * rate["request_usd"] \
         + (vcpu_count * duration_s) * rate["vcpu_second_usd"] \
         + (memory_gib * duration_s) * rate["gib_second_usd"]


def _cloud_run_instance_cost(details, rate, window_s):
    memory_gib = Decimal(details["memory_bytes_limit"]) / _GIB_BINARY
    vcpu_count = Decimal(str(details["vcpu_count"]))
    return (vcpu_count * window_s) * rate["vcpu_second_usd"] \
         + (memory_gib * window_s) * rate["gib_second_usd"]


def _azure_functions_cost(details, rate):
    duration_s = Decimal(details["duration_ms"]) / Decimal("1000")
    memory_gb = Decimal(details["memory_bytes_limit"]) / _GB_DECIMAL
    invocations = Decimal(details["invocation_count"])
    return invocations * rate["execution_usd"] \
         + (memory_gb * duration_s) * rate["gb_second_usd"]


def _vercel_cost(details, rate):
    duration_s = Decimal(details["duration_ms"]) / Decimal("1000")
    memory_gb = Decimal(details["memory_bytes_limit"]) / _GB_DECIMAL
    invocations = Decimal(details["invocation_count"])
    active_cpu_hours = duration_s / _HOUR_S
    memory_gb_hours = memory_gb * (duration_s / _HOUR_S)
    return invocations * rate["invocation_usd"] \
         + active_cpu_hours * rate["active_cpu_hour_usd"] \
         + memory_gb_hours * rate["memory_gb_hour_usd"]


def _instance_share_cost(details, rate, instance_hourly, window_s):
    vcpu_count = Decimal(str(details["vcpu_count"]))
    vcpu_seconds = Decimal(str(details["vcpu_seconds_used"]))
    if vcpu_count <= 0 or window_s <= 0:
        return Decimal("0")
    share_factor = vcpu_seconds / (vcpu_count * window_s)
    task_instance_hours = share_factor * (window_s / _HOUR_S)
    return task_instance_hours * instance_hourly


def _k8s_pod_limits_cost(details, rate, window_s):
    vcpu_count = Decimal(str(details["vcpu_count"]))
    return vcpu_count * (window_s / _HOUR_S) * rate["vcpu_hour_usd"]
```

The public `resolve_compute_cost` wraps the dispatch in a try/except that returns `ComputeCost(cost_usd=Decimal("0"), pricing_source="compute_catalog:error:<billing_model>", cost_confidence="unknown")` on any failure — Tier 5.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_compute_pricing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/compute_pricing.py python/tests/test_compute_pricing.py
git commit -m "feat(compute): pricing engine — per-billing-model math + degradation ladder"
```

---

### Task 7: `ComputeAccountant` — per-task in-process accumulator

**Files:**
- Create: `python/src/dexcost/compute_accountant.py`
- Test: `python/tests/test_compute_accountant.py` (create)

Per-task accumulator analogous to `NetworkAccountant`. Holds start cgroup snapshot + runtime context, then at task finalize emits one `compute_cost` event with `cost_pending: true`.

- [ ] **Step 1: Write the failing test**

Create `python/tests/test_compute_accountant.py`:

```python
"""ComputeAccountant — start/end cgroup snapshots, single event per task, fail-silent."""

from unittest.mock import patch

import pytest

from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind


def test_long_running_runtime_emits_one_event_with_diff(monkeypatch):
    from dexcost.cgroup_reader import CpuStat, CpuMax
    with patch("dexcost.compute_accountant.read_cpu_stat",
               side_effect=[CpuStat(usage_usec=1_000_000),
                            CpuStat(usage_usec=4_000_000)]), \
         patch("dexcost.compute_accountant.read_cpu_max",
               return_value=CpuMax(quota_us=100000, period_us=100000, vcpu_count=1.0)), \
         patch("dexcost.compute_accountant.read_memory_peak", return_value=512 * 1024 * 1024), \
         patch("dexcost.compute_accountant.read_memory_max", return_value=1024 * 1024 * 1024):
        a = ComputeAccountant(runtime=RuntimeKind.EC2)
        a.snapshot_start()
        event_details = a.snapshot_end_and_build(duration_ms=60_000)
    assert event_details["billing_model"] == "ec2"
    assert event_details["vcpu_seconds_used"] == pytest.approx(3.0)
    assert event_details["memory_bytes_peak"] == 512 * 1024 * 1024
    assert event_details["memory_bytes_limit"] == 1024 * 1024 * 1024
    assert event_details["vcpu_count"] == 1.0
    assert event_details["cost_pending"] is True


def test_serverless_runtime_emits_invocation_event():
    a = ComputeAccountant(runtime=RuntimeKind.LAMBDA, lambda_memory_mb=512,
                          architecture="x86_64")
    details = a.build_serverless_event(duration_ms=200, memory_bytes_peak=400 * 1024 * 1024)
    assert details["billing_model"] == "lambda"
    assert details["duration_ms"] == 200
    assert details["invocation_count"] == 1
    assert details["memory_bytes_limit"] == 512 * 1000 * 1000  # decimal MB → bytes (Lambda)
    assert details["architecture"] == "x86_64"
    assert details["cost_pending"] is True


def test_second_event_no_ops_per_task():
    """capture §5.3 — at most one event per task per runtime."""
    a = ComputeAccountant(runtime=RuntimeKind.LAMBDA, lambda_memory_mb=128,
                          architecture="x86_64")
    a.build_serverless_event(duration_ms=10, memory_bytes_peak=0)
    second = a.build_serverless_event(duration_ms=20, memory_bytes_peak=0)
    assert second is None  # idempotent — second call returns None


def test_non_linux_fallback_emits_with_estimated_vcpu_used():
    with patch("dexcost.compute_accountant.read_cpu_stat", return_value=None), \
         patch("dexcost.compute_accountant.read_cpu_max", return_value=None), \
         patch("dexcost.compute_accountant.read_memory_peak", return_value=None), \
         patch("dexcost.compute_accountant.read_memory_max", return_value=None):
        a = ComputeAccountant(runtime=RuntimeKind.EC2)
        a.snapshot_start()
        details = a.snapshot_end_and_build(duration_ms=60_000)
    # cgroup missing → vcpu_seconds_used = 0, vcpu_count falls back to nproc.
    assert details["vcpu_seconds_used"] == 0
    assert details["vcpu_count"] > 0
```

- [ ] **Step 2: Implement `compute_accountant.py`**

Create `python/src/dexcost/compute_accountant.py`:

```python
"""Per-task compute accountant.

Holds start cgroup snapshot + runtime context for one dexcost task. At task
finalize, emits exactly one `compute_cost` event with `cost_pending: true` —
the pricing engine back-fills cost_usd via the deferred-cost pattern.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from dexcost.cgroup_reader import (
    read_cpu_max, read_cpu_stat, read_memory_current, read_memory_max,
    read_memory_peak,
)
from dexcost.compute_runtime import RuntimeKind


class ComputeAccountant:
    """One per dexcost task. Single-writer, lock-guarded for safety."""

    def __init__(self,
                 runtime: RuntimeKind,
                 lambda_memory_mb: int | None = None,
                 fargate_vcpu: float | None = None,
                 fargate_memory_mib: int | None = None,
                 architecture: str | None = None,
                 initialization_type: str | None = None,
                 region: str | None = None) -> None:
        self._lock = threading.Lock()
        self._frozen = False
        self.runtime = runtime
        self.lambda_memory_mb = lambda_memory_mb
        self.fargate_vcpu = fargate_vcpu
        self.fargate_memory_mib = fargate_memory_mib
        self.architecture = architecture or _detect_arch()
        self.initialization_type = initialization_type
        self.region = region
        self._start_cpu_usec: int | None = None

    # ----- Long-running runtimes (Fargate / EC2 / GCE / Azure VM / K8s) -------

    def snapshot_start(self) -> None:
        s = read_cpu_stat()
        self._start_cpu_usec = s.usage_usec if s else None

    def snapshot_end_and_build(self, duration_ms: int) -> dict[str, Any] | None:
        with self._lock:
            if self._frozen:
                return None
            self._frozen = True

        end = read_cpu_stat()
        cpu_max = read_cpu_max()
        mem_peak = read_memory_peak() or read_memory_current() or 0
        mem_limit = read_memory_max() or 0

        vcpu_seconds_used = 0.0
        if end and self._start_cpu_usec is not None:
            vcpu_seconds_used = (end.usage_usec - self._start_cpu_usec) / 1_000_000

        vcpu_count = cpu_max.vcpu_count if cpu_max else float(os.cpu_count() or 1)

        return {
            "billing_model": _billing_model_for(self.runtime),
            "duration_ms": duration_ms,
            "memory_bytes_peak": int(mem_peak),
            "memory_bytes_limit": int(mem_limit),
            "vcpu_count": vcpu_count,
            "vcpu_seconds_used": vcpu_seconds_used,
            "invocation_count": 0,
            "region": self.region,
            "architecture": self.architecture,
            "initialization_type": None,
            "cost_pending": True,
        }

    # ----- Serverless runtimes ------------------------------------------------

    def build_serverless_event(self, duration_ms: int,
                               memory_bytes_peak: int) -> dict[str, Any] | None:
        with self._lock:
            if self._frozen:
                return None
            self._frozen = True

        if self.runtime == RuntimeKind.LAMBDA:
            # Lambda env var is in MB (decimal); convert to bytes via 10^6.
            mem_limit = (self.lambda_memory_mb or 128) * 1_000_000
        elif self.runtime == RuntimeKind.FARGATE:
            mem_limit = (self.fargate_memory_mib or 0) * 1024 * 1024
        else:
            # Cloud Run / Azure Functions / Vercel — cgroup memory.max.
            mem_limit = read_memory_max() or memory_bytes_peak

        cpu_max = read_cpu_max()
        vcpu_count = (
            self.fargate_vcpu
            if self.runtime == RuntimeKind.FARGATE and self.fargate_vcpu is not None
            else (cpu_max.vcpu_count if cpu_max else float(os.cpu_count() or 1))
        )

        return {
            "billing_model": _billing_model_for(self.runtime),
            "duration_ms": duration_ms,
            "memory_bytes_peak": memory_bytes_peak,
            "memory_bytes_limit": mem_limit,
            "vcpu_count": vcpu_count,
            "vcpu_seconds_used": 0,
            "invocation_count": 1,
            "region": self.region,
            "architecture": self.architecture,
            "initialization_type": self.initialization_type,
            "cost_pending": True,
        }


def _billing_model_for(runtime: RuntimeKind) -> str:
    mapping = {
        RuntimeKind.LAMBDA: "lambda",
        RuntimeKind.FARGATE: "fargate",
        RuntimeKind.EC2: "ec2",
        RuntimeKind.GCE: "gce",
        RuntimeKind.AZURE_VM: "azure_vm",
        RuntimeKind.CLOUD_RUN: "cloud_run_request",
        RuntimeKind.CLOUD_FUNCTIONS: "cloud_functions",
        RuntimeKind.AZURE_FUNCTIONS: "azure_functions",
        RuntimeKind.VERCEL: "vercel_fluid",
        RuntimeKind.K8S_POD: "k8s_pod",
    }
    return mapping.get(runtime, "unknown")


def _detect_arch() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if "aarch64" in machine or "arm64" in machine:
        return "arm64"
    return "x86_64"
```

- [ ] **Step 3: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_compute_accountant.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add python/src/dexcost/compute_accountant.py python/tests/test_compute_accountant.py
git commit -m "feat(compute): per-task accountant — cgroup start/end snapshots, single event"
```

---

### Task 8: Attach `_compute` to `Task`; wire into `_aggregate_costs`

**Files:**
- Modify: `python/src/dexcost/models/task.py`
- Modify: `python/src/dexcost/tracker.py`
- Test: `python/tests/test_compute_auto_emission_long_running.py` (create)

For long-running runtimes (EC2 / GCE / Azure VM / Fargate / K8s pod / Cloud Run instance), `_aggregate_costs` snapshots the cgroup at task end, builds the `compute_cost` event with `cost_pending: true`, persists it, then back-fills `cost_usd` from `ComputePricingEngine`.

For serverless runtimes (Lambda / Cloud Run request / Cloud Functions / Azure Functions / Vercel), the event is built by the handler wrap (Task 9) and persisted there with `cost_pending: true`; `_aggregate_costs` only does the back-fill.

- [ ] **Step 1: Add `_compute` attribute to Task**

In `python/src/dexcost/models/task.py`, mirror the `_network` attribute pattern. Add to `__post_init__` (or wherever `_network` is initialized):

```python
        from dexcost.compute_accountant import ComputeAccountant
        from dexcost.compute_runtime import resolve_runtime
        self._compute: ComputeAccountant | None = None  # lazy — set by tracker
```

Do NOT instantiate `ComputeAccountant` eagerly in Task — the tracker creates it at task start when it knows the runtime + config (Task 9 wires this).

- [ ] **Step 2: Extend `_aggregate_costs` with the compute back-fill**

In `python/src/dexcost/tracker.py:1076`, after the existing network finalize block, add:

```python
        # Compute capture — emit + price compute_cost event(s).
        from dexcost.compute_pricing import ComputePricingEngine
        from dexcost.compute_runtime import RuntimeKind

        if task._compute is not None and task._compute.runtime != RuntimeKind.UNKNOWN:
            duration_ms = 0
            if task.ended_at and task.started_at:
                duration_ms = int(
                    (task.ended_at - task.started_at).total_seconds() * 1000
                )

            long_running = task._compute.runtime in {
                RuntimeKind.FARGATE, RuntimeKind.EC2, RuntimeKind.GCE,
                RuntimeKind.AZURE_VM, RuntimeKind.K8S_POD,
            }
            if long_running:
                # Build + persist the event with cost_pending=true.
                details = task._compute.snapshot_end_and_build(duration_ms)
                if details is not None:
                    from dexcost.models.event import Event
                    import uuid as _uuid
                    from datetime import datetime as _dt, timezone as _tz
                    ev = Event(
                        event_id=_uuid.uuid4(),
                        task_id=task.task_id,
                        event_type="compute_cost",
                        timestamp=_dt.now(_tz.utc),
                        cost_usd=Decimal("0"),
                        details=details,
                    )
                    self._storage.insert_event(ev)

        # Back-fill cost_pending compute events.
        from dexcost.cloud_detect import get_cloud_env
        cloud_env = get_cloud_env()
        engine = ComputePricingEngine()
        overrides = getattr(self, "_compute_billing_overrides", {}) or {}

        events = self._storage.query_events(task_id=str(task.task_id))
        window_s = Decimal("0")
        if task.ended_at and task.started_at:
            window_s = Decimal(
                str((task.ended_at - task.started_at).total_seconds())
            )
        for ev in events:
            if ev.event_type != "compute_cost":
                continue
            details = ev.details or {}
            if not details.get("cost_pending"):
                continue
            try:
                priced = engine.resolve_compute_cost(
                    details, cloud_env, overrides, window_s=window_s,
                )
            except Exception:  # noqa: BLE001 — Tier 5 fail-silent
                continue
            ev.cost_usd = priced.cost_usd
            ev.pricing_source = priced.pricing_source
            ev.cost_confidence = priced.cost_confidence
            ev.pricing_version = f"compute:{engine.catalog_version}"
            new_details = dict(details)
            new_details.pop("cost_pending", None)
            ev.details = new_details
            self._storage.update_event(ev)

        # Re-sum compute_cost_usd from the back-filled events.
        task.compute_cost_usd = Decimal("0")
        for ev in self._storage.query_events(task_id=str(task.task_id)):
            if ev.event_type == "compute_cost":
                task.compute_cost_usd += ev.cost_usd
        task.total_cost_usd = (
            task.llm_cost_usd + task.external_cost_usd
            + task.compute_cost_usd + task.network_cost_usd
        )
```

> The duplicated `query_events` call is intentional: the first runs before we've inserted the long-running event; we need a fresh read post-insert to back-fill it. Optimize later only if perf shows it matters.

- [ ] **Step 3: Write the integration test**

Create `python/tests/test_compute_auto_emission_long_running.py`:

```python
"""End-to-end: long-running runtime auto-emits + back-fills compute_cost."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from dexcost import cloud_detect
from dexcost.cgroup_reader import CpuStat, CpuMax
from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def test_ec2_task_emits_and_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv("aws", "us-east-1", "imds",
                              instance_type="c7g.xlarge"),
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    tracker = CostTracker(storage=storage)
    started = datetime.now(timezone.utc) - timedelta(seconds=60)
    t = Task(task_id=uuid.uuid4(), task_type="x", started_at=started)
    t.ended_at = started + timedelta(seconds=60)
    storage.insert_task(t)

    a = ComputeAccountant(runtime=RuntimeKind.EC2, region="us-east-1",
                          architecture="x86_64")
    with patch("dexcost.compute_accountant.read_cpu_stat",
               return_value=CpuStat(usage_usec=0)):
        a.snapshot_start()
    t._compute = a

    with patch("dexcost.compute_accountant.read_cpu_stat",
               return_value=CpuStat(usage_usec=1_000_000)), \
         patch("dexcost.compute_accountant.read_cpu_max",
               return_value=CpuMax(quota_us=400000, period_us=100000, vcpu_count=4.0)), \
         patch("dexcost.compute_accountant.read_memory_peak",
               return_value=512 * 1024 * 1024), \
         patch("dexcost.compute_accountant.read_memory_max",
               return_value=8 * 1024 * 1024 * 1024):
        tracker._aggregate_costs(t)

    events = storage.query_events(task_id=str(t.task_id))
    compute_events = [e for e in events if e.event_type == "compute_cost"]
    assert len(compute_events) == 1
    ev = compute_events[0]
    assert ev.cost_usd > Decimal("0")
    assert ev.pricing_source.startswith("compute_catalog:")
    assert ev.cost_confidence == "computed"
    assert "cost_pending" not in (ev.details or {})
    assert t.compute_cost_usd == ev.cost_usd
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_compute_auto_emission_long_running.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/dexcost/models/task.py python/src/dexcost/tracker.py python/tests/test_compute_auto_emission_long_running.py
git commit -m "feat(compute): auto-emit + back-fill compute_cost events at task finalize"
```

---

### Task 9: Handler-wrap decorators for serverless runtimes + `init()` knobs

**Files:**
- Create: `python/src/dexcost/compute_wrap.py`
- Modify: `python/src/dexcost/tracker.py` (init signature + `_compute_billing_overrides` field)
- Modify: `python/src/dexcost/__init__.py` (expose `wrap_lambda_handler` etc.)
- Test: `python/tests/test_compute_wrap.py` (create)

Decorators that wrap a serverless handler:
- Read env vars for runtime context (`AWS_LAMBDA_FUNCTION_MEMORY_SIZE`, `AWS_LAMBDA_INITIALIZATION_TYPE`, etc.)
- Start a dexcost task (if not already in one)
- Measure `duration_ms` via `time.monotonic_ns()`
- Read `memory.peak` from cgroup at exit
- Call `task._compute.build_serverless_event(...)` → persist event → `_aggregate_costs` back-fills cost

Also adds two `init()` knobs per spec §5.2:
- `compute_billing_overrides: dict[str, str] | None = None`
- `k8s_node_aware: bool = False`

- [ ] **Step 1: Implement `compute_wrap.py`**

Mirror the existing `auto_task.py` pattern. The wrap decorator factory takes a `RuntimeKind` and an arg-extractor for runtime-specific env vars. For Lambda:

```python
def wrap_lambda_handler(fn):
    """Wrap a Lambda handler to emit a compute_cost event per invocation."""
    import functools, time
    @functools.wraps(fn)
    def _wrapped(event, context):
        from dexcost.compute_accountant import ComputeAccountant
        from dexcost.compute_runtime import RuntimeKind
        from dexcost.context import get_current_task
        from dexcost.cgroup_reader import read_memory_peak
        import os

        task = get_current_task()
        if task is None:
            # No active dexcost task — pass through. Capture spec §6 case 2.
            return fn(event, context)

        mem_mb = int(os.environ.get("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "128"))
        init_type = os.environ.get("AWS_LAMBDA_INITIALIZATION_TYPE", "on-demand")
        region = os.environ.get("AWS_REGION")
        accountant = ComputeAccountant(
            runtime=RuntimeKind.LAMBDA,
            lambda_memory_mb=mem_mb,
            initialization_type=init_type,
            region=region,
        )
        task._compute = accountant

        t0 = time.monotonic_ns()
        try:
            return fn(event, context)
        finally:
            duration_ms = (time.monotonic_ns() - t0) // 1_000_000
            peak = read_memory_peak() or 0
            details = accountant.build_serverless_event(
                duration_ms=int(duration_ms),
                memory_bytes_peak=peak,
            )
            if details is not None:
                _persist_compute_event(task, details)
    return _wrapped
```

`_persist_compute_event` is a private helper that builds the `Event` object and calls `tracker._storage.insert_event`. The back-fill happens automatically on next `_aggregate_costs`.

Repeat the pattern for `wrap_cloud_run_handler` (HTTP middleware), `wrap_azure_functions_handler`, `wrap_vercel_handler`, `wrap_cloud_functions_handler`. Each pulls its runtime-specific env vars per the table in research §1.

- [ ] **Step 2: Add `init()` knobs**

In `tracker.py` `CostTracker.__init__`, add two kwargs:

```python
        compute_billing_overrides: dict[str, str] | None = None,
        k8s_node_aware: bool = False,
```

Store as `self._compute_billing_overrides` and `self._k8s_node_aware`. The `_aggregate_costs` compute block (Task 8) already reads `self._compute_billing_overrides`.

For `k8s_node_aware`, implement in a new helper `_resolve_k8s_node_share()` called from the compute back-fill block when `runtime == K8S_POD` AND `self._k8s_node_aware is True`. Reads `KUBERNETES_SERVICE_HOST`, calls `/api/v1/nodes/<spec.nodeName>`, parses `.status.capacity.cpu` and `.metadata.labels["node.kubernetes.io/instance-type"]`. Fail-silent + log-once via convention §11 on 403 / timeout / malformed.

- [ ] **Step 3: Tests**

Create `python/tests/test_compute_wrap.py`:

```python
"""Handler wraps emit compute_cost events per invocation."""

import os
from unittest.mock import patch

from dexcost.compute_wrap import wrap_lambda_handler


def test_lambda_wrap_emits_event(monkeypatch, tmp_path):
    from dexcost.tracker import CostTracker
    from dexcost.storage.sqlite import SQLiteStorage
    from dexcost.context import set_current_task
    from dexcost.models.task import Task
    import uuid
    from datetime import datetime, timezone

    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "1024")
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "on-demand")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    tracker = CostTracker(storage=storage)
    t = Task(task_id=uuid.uuid4(), task_type="lambda",
             started_at=datetime.now(timezone.utc))
    storage.insert_task(t)
    token = set_current_task(t)

    @wrap_lambda_handler
    def handler(event, context):
        return {"statusCode": 200}

    with patch("dexcost.compute_wrap.read_memory_peak",
               return_value=256 * 1024 * 1024):
        try:
            handler({}, type("Ctx", (), {})())
        finally:
            from dexcost.context import _current_task
            _current_task.reset(token)

    events = storage.query_events(task_id=str(t.task_id))
    compute = [e for e in events if e.event_type == "compute_cost"]
    assert len(compute) == 1
    assert compute[0].details["billing_model"] == "lambda"
    assert compute[0].details["invocation_count"] == 1
    assert compute[0].details["architecture"] in {"x86_64", "arm64"}
    assert compute[0].details["initialization_type"] == "on-demand"


def test_no_active_task_passes_through(monkeypatch):
    @wrap_lambda_handler
    def handler(event, context):
        return "ok"
    # No task in context → wrap is a no-op pass-through.
    assert handler({}, None) == "ok"
```

- [ ] **Step 4: Expose in `__init__.py`**

Add to `python/src/dexcost/__init__.py`:

```python
from dexcost.compute_wrap import (
    wrap_lambda_handler, wrap_cloud_run_handler, wrap_cloud_functions_handler,
    wrap_azure_functions_handler, wrap_vercel_handler,
)
```

- [ ] **Step 5: Run tests**

Run: `cd python && uv run pytest tests/test_compute_wrap.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add python/src/dexcost/compute_wrap.py python/src/dexcost/tracker.py python/src/dexcost/__init__.py python/tests/test_compute_wrap.py
git commit -m "feat(compute): serverless handler wraps + init knobs (compute_billing_overrides, k8s_node_aware)"
```

---

### Task 10: Property invariants + Decision #9/#10 idle-gap pinning tests

**Files:**
- Test: `python/tests/test_compute_invariants.py` (create)
- Test: `python/tests/test_compute_idle_gap.py` (create)
- Test: `python/tests/test_compute_cross_runtime_matrix.py` (create)

No production code — pins the spec §10.3 property invariants and the explicit Decisions #9 + #10 "idle is invisible" contract, so future refactors can't silently regress the design.

- [ ] **Step 1: Property invariants**

Create `python/tests/test_compute_invariants.py`. Parametrized over (billing_model, region, architecture, duration_ms, memory_bytes, vcpu_count). Asserts:

1. `cost_usd >= Decimal("0")` always.
2. `task.compute_cost_usd == sum(e.cost_usd for e in compute_events)`.
3. Linearity: `2× duration → ~2× cost_usd` modulo per-request constants.
4. ARM < x86 on Lambda/Fargate same SKU.
5. `cost_confidence ∈ {"computed", "estimated"}` on well-formed input (never `"unknown"`).
6. `pricing_source.startswith("compute_catalog:")` always.

- [ ] **Step 2: Decision #9 + #10 explicit idle-gap test**

Create `python/tests/test_compute_idle_gap.py`:

```python
"""Decisions #9 + #10 — idle compute is invisible to dexcost. The gap IS the design."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from dexcost import cloud_detect
from dexcost.cgroup_reader import CpuStat, CpuMax
from dexcost.compute_accountant import ComputeAccountant
from dexcost.compute_runtime import RuntimeKind
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def test_ec2_idle_between_tasks_is_invisible(tmp_path, monkeypatch):
    """Two 60s tasks with 600s idle between them on a 4 vCPU @ $0.1450/hr c7g.xlarge.

    The cloud bill for the full 720s window would be 720/3600 * 0.1450 = $0.029.
    dexcost MUST report STRICTLY LESS than that — the 600s idle gap is excluded.
    """
    monkeypatch.setattr(
        cloud_detect, "_result",
        cloud_detect.CloudEnv("aws", "us-east-1", "imds", instance_type="c7g.xlarge"),
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    tracker = CostTracker(storage=storage)

    def _run_task(start_offset_s, duration_s, cpu_used_seconds):
        started = datetime.now(timezone.utc) + timedelta(seconds=start_offset_s)
        t = Task(task_id=uuid.uuid4(), task_type="x", started_at=started)
        t.ended_at = started + timedelta(seconds=duration_s)
        storage.insert_task(t)
        a = ComputeAccountant(runtime=RuntimeKind.EC2, region="us-east-1",
                              architecture="x86_64")
        with patch("dexcost.compute_accountant.read_cpu_stat",
                   return_value=CpuStat(usage_usec=0)):
            a.snapshot_start()
        t._compute = a
        with patch("dexcost.compute_accountant.read_cpu_stat",
                   return_value=CpuStat(usage_usec=int(cpu_used_seconds * 1_000_000))), \
             patch("dexcost.compute_accountant.read_cpu_max",
                   return_value=CpuMax(quota_us=400000, period_us=100000, vcpu_count=4.0)), \
             patch("dexcost.compute_accountant.read_memory_peak",
                   return_value=512 * 1024 * 1024), \
             patch("dexcost.compute_accountant.read_memory_max",
                   return_value=8 * 1024 * 1024 * 1024):
            tracker._aggregate_costs(t)
        return t.compute_cost_usd

    cost_a = _run_task(0, 60, cpu_used_seconds=10)
    cost_b = _run_task(660, 60, cpu_used_seconds=10)
    total = cost_a + cost_b

    full_window_cloud_share = (Decimal("720") / Decimal("3600")) * Decimal("0.1450")
    assert total < full_window_cloud_share, (
        f"dexcost total {total} must be < cloud share {full_window_cloud_share} "
        f"on long-running runtimes — the 600s idle gap is by design (Decision #9)"
    )
    # Sanity: total is still > 0 (we DO bill the 120s of dexcost-covered time).
    assert total > Decimal("0")


def test_fargate_container_idle_tail_is_invisible(tmp_path, monkeypatch):
    """Decision #10 — Fargate container idle tail between last dexcost task and
    container shutdown is invisible to dexcost. Same shape as #9 at container scope."""
    # Similar test structure with RuntimeKind.FARGATE; assert dexcost compute
    # total < (full container lifetime × Fargate rate). Omitted for brevity —
    # implementer should write this following the EC2 shape above.
```

- [ ] **Step 3: Cross-runtime regression matrix**

Create `python/tests/test_compute_cross_runtime_matrix.py`. One test per `billing_model` value (lambda, fargate, ec2, cloud_run_request, cloud_run_instance, cloud_functions, azure_functions, vercel_fluid, k8s_pod) — each emits a known fixture and asserts a hand-computed cost. Catches a dispatch-table regression where a `billing_model` silently routes to the wrong arithmetic.

- [ ] **Step 4: Run all three suites + full regression**

Run: `cd python && uv run pytest tests/test_compute_invariants.py tests/test_compute_idle_gap.py tests/test_compute_cross_runtime_matrix.py -v`
Then: `cd python && uv run pytest -q` — full suite, every test green.

- [ ] **Step 5: Commit**

```bash
git add python/tests/test_compute_invariants.py python/tests/test_compute_idle_gap.py python/tests/test_compute_cross_runtime_matrix.py
git commit -m "test(compute): property invariants + Decision #9/#10 idle-gap contract + cross-runtime matrix"
```

---

### Task 11: Catalog sync script (for future Go/Rust/TS ports)

**Files:**
- Create: `scripts/sync_compute_catalog.sh`
- Modify: `.github/workflows/<existing-catalog-check>.yml` (add a `--check` step)

Per convention §6, Python is the canonical source; the other three SDKs bundle a synced copy. This script does the copy, with a `--check` mode for CI.

- [ ] **Step 1: Author the script**

Create `scripts/sync_compute_catalog.sh` mirroring the existing `scripts/sync_egress_catalog.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

SRC="python/src/dexcost/data/compute_prices.json"
DESTS=(
  "go/data/compute_prices.json"
  "rust/data/compute_prices.json"
  "typescript/data/compute_prices.json"
)

if [[ "${1:-}" == "--check" ]]; then
  drift=0
  for dst in "${DESTS[@]}"; do
    if ! diff -q "$SRC" "$dst" >/dev/null 2>&1; then
      echo "DRIFT: $dst differs from $SRC"
      drift=1
    fi
  done
  exit $drift
fi

for dst in "${DESTS[@]}"; do
  mkdir -p "$(dirname "$dst")"
  cp "$SRC" "$dst"
  echo "Synced: $dst"
done
```

- [ ] **Step 2: Wire CI**

Find the existing CI step that runs `scripts/sync_egress_catalog.sh --check` and add a parallel `bash scripts/sync_compute_catalog.sh --check` step.

- [ ] **Step 3: Run locally**

```bash
chmod +x scripts/sync_compute_catalog.sh
scripts/sync_compute_catalog.sh
scripts/sync_compute_catalog.sh --check  # should exit 0 after the copy
```

- [ ] **Step 4: Commit**

```bash
git add scripts/sync_compute_catalog.sh .github/
git commit -m "build(compute): catalog sync script + CI drift check"
```

---

## Self-Review

**Spec coverage** — every section of both compute specs maps to a task:

| Spec section | Task(s) |
|---|---|
| Capture §1 Summary (coverage matrix) | 3 (runtime resolver dispatches on all 9 RuntimeKinds), 6 (pricing engine handles all 9 billing models) |
| Capture §3 Decisions | All decisions threaded through tasks below |
| Capture §4 Data Model | 7 (`compute_cost` event details shape), 8 (no new Task fields — `compute_cost_usd` already there) |
| Capture §5 Components | 2 (CgroupReader), 3 (Runtime resolver), 4 (Fargate metadata), 7 (ComputeAccountant), 9 (HandlerWrap) |
| Capture §6 Error handling | 2 (fail-silent on missing cgroup), 4 (Fargate metadata log-once), 6 (Tier 5 try/except), 9 (no-active-task no-op) |
| Capture §7 Testing | 2, 3, 4, 6, 7, 8, 9, 10 |
| Capture §9 Control Layer contract | enforced by event schema (Task 7) — no new code |
| Cost v2 §1 Purpose | All tasks |
| Cost v2 §2 Decisions | 1 (#3 IMDS instance type), 5 (catalog), 6 (#7 conversion table, #1 cloud_run override, #2 vercel approximation), 8 (#8 window, #9/#10 idle invisible enforced), 9 (#1 overrides knob, #4 k8s_node_aware knob, #5 PC tracked) |
| Cost v2 §3 Architecture | 1, 5, 6 (modules), 7 (accountant), 8 (finalize wiring) |
| Cost v2 §4 Catalog | 5 |
| Cost v2 §5 Pricing engine | 6 |
| Cost v2 §6 Per-billing-model math | 6 (with the §6.2 conversion table pinned in tests) |
| Cost v2 §7 Degradation ladder | 6 (Tiers 1–4 in resolve, Tier 5 in try/except wrapping public method) |
| Cost v2 §8 Schema (no migration) | n/a — verified by absence of migration tasks |
| Cost v2 §9 Cost attribution flow | 8 |
| Cost v2 §10 Testing | 6, 8, 10 |
| Cost v2 §11 Future / §12 Non-goals | n/a |

**Placeholder scan** — no `TBD`/`TODO`. Task 5 step 3 contains the only literal data-entry handoff: every commercial AWS/GCP/Azure region for Lambda/Fargate/Cloud Run/Cloud Functions/Azure Functions, plus top ~50 EC2/GCE/Azure VM SKUs, must be transcribed by hand from each provider's public pricing pages. The skeleton is provided; the integrity tests in step 1 fail loudly if a provider block / runtime block is missing.

**Type consistency** — `ComputeCost` is a frozen dataclass with `(cost_usd: Decimal, pricing_source: str, cost_confidence: str)`; `CloudEnv` extends with `instance_type: str | None`; `CpuStat` / `CpuMax` are frozen dataclasses; `ComputeAccountant.build_serverless_event` / `snapshot_end_and_build` return `dict[str, Any] | None`; `_persist_compute_event` constructs `Event` instances with `cost_usd=Decimal("0")` + `cost_pending: true` marker. All catalog rates are Decimal-string-encoded; conversion divisors are Decimal literals (NEVER `1e9` / `1024**3` as floats — pinned by `test_decimal_no_float_drift_per_conversion`).

**Pre-requisite chain (already merged on this branch):**
1. v2 network capture — `cloud_detect`, `egress_pricing`, deferred-cost pattern, `update_event` `sync_status='pending'` fix. The compute layer reuses ALL of these.
2. `EventType.COMPUTE_COST = "compute_cost"` (baseline) — no enum change needed.
3. `Task.compute_cost_usd` + `_aggregate_costs` summation (baseline) — the existing aggregation works once events get a real `cost_usd`.

**Known follow-ups (out of scope, not gaps):**
- **Go / Rust / TypeScript ports** — each its own spec → plan → implementation cycle. The shared `compute_prices.json` from Task 5 + the `sync_compute_catalog.sh` from Task 11 are the cross-SDK contract.
- **Lambda provisioned-concurrency idle billing** — `AWS_LAMBDA_INITIALIZATION_TYPE` captured for all values (Decision #5). v1.1 adds the PC idle period math additively — no schema migration needed.
- **Lambda init-phase tracking** — v1 handler wrap starts at handler entry, after init has already happened. v1.1 adds a Lambda Extensions API hook for init-phase capture.
- **Cgroup-v1 readers** — RHEL 7 / CentOS 7 fall through to `estimated` confidence in v1. v1.1 adds the v1 file layout if customer demand surfaces.
- **Azure Functions Premium + Dedicated plans** — v1 covers Consumption only.
- **Multi-CPU Vercel Fluid sandboxes** — v1 approximates as 1.0 active CPU per wall hour.
- **GPU runtimes (Modal / RunPod / etc.)** — detected by `cloud_detect`, billed by subsystem C (Phase 2).
- **`compute_by_runtime` per-task aggregate** — analog of `network_by_host`. Deferred until a dashboard need surfaces.
- **K8s `/api/v1/nodes` node-share path** — opt-in via `k8s_node_aware: true` (Task 9 step 2). Default zero-config uses pod-limits × duration (Decision #4 path c).
- **Cost Intelligence / reconciliation surface** — Control Layer scope. Where Decision #9/#10 idle gaps get explained as line items; where the dexcost-vs-cloud-invoice variance gets surfaced.
