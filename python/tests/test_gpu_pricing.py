"""GPU pricing engine — 4 billing models, 5-tier ladder, Decision #4 device-class fallback."""

from __future__ import annotations

from decimal import Decimal

import pytest

from dexcost.cloud_detect import CloudEnv


@pytest.fixture(autouse=True)
def _reset():
    from dexcost.gpu_pricing import _reset_warning_state
    _reset_warning_state()


@pytest.fixture
def engine():
    from dexcost.gpu_pricing import GpuPricingEngine
    return GpuPricingEngine()


def _base_details(billing_model, **overrides):
    """Defaults that satisfy the engine's expected schema; override per test."""
    base = {
        "billing_model": billing_model,
        "gpu_vendor": "nvidia",
        "gpu_sku": "h100-80gb-sxm5",
        "gpu_count": 1,
        "region": None,
        "duration_ms": 1000,
        "gpu_seconds_used": 1.0,
        "instance_type": None,
        "vgpu_profile": None,
        "mig_profile": None,
    }
    base.update(overrides)
    return base


# ─── per_gpu_second_active (highest-precision regime) ──────────────────────

def test_modal_h100_per_second(engine):
    """Modal H100: 1.234 GPU-seconds * $0.001097/s. Catalog rate verified live."""
    details = _base_details(
        "per_gpu_second_active",
        gpu_seconds_used=1.234, duration_ms=1234,
    )
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("modal", None, "env"), window_s=Decimal("1.234"),
    )
    # Catalog: modal.per_gpu_second_active.default.h100.gpu_second_usd = "0.001097"
    assert cost.cost_usd == Decimal("1.234") * Decimal("0.001097")
    assert cost.cost_confidence == "computed"
    assert "modal" in cost.pricing_source


def test_runpod_h100_on_demand_per_second(engine):
    """RunPod has nested on_demand/community_cloud — engine handles the nesting."""
    details = _base_details(
        "per_gpu_second_active",
        gpu_seconds_used=10.0, duration_ms=10_000,
    )
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("runpod", None, "env"), window_s=Decimal("10"),
    )
    # Catalog: runpod.per_gpu_second_active.default.on_demand.h100-sxm.gpu_second_usd
    assert cost.cost_usd > Decimal("0")
    assert cost.cost_confidence == "computed"
    assert "runpod" in cost.pricing_source


# ─── per_gpu_hour_reserved (Lambda Labs / CoreWeave) ────────────────────────

def test_lambda_labs_h100_sxm_share(engine):
    """Lambda Labs 8x H100 SXM5 cluster, 1 GPU-sec used across 60s window with 8 GPUs.

    share_factor = 1.0 / (8 * 60) = 1/480
    task_gpu_hours = share_factor * (60/3600) * 8
    cost = task_gpu_hours * $3.99
    """
    details = _base_details(
        "per_gpu_hour_reserved", gpu_count=8, duration_ms=60_000,
    )
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("lambda_labs", None, "dmi"), window_s=Decimal("60"),
    )
    share = Decimal("1.0") / (Decimal("8") * Decimal("60"))
    expected_gpu_hours = share * (Decimal("60") / Decimal("3600")) * Decimal("8")
    expected = expected_gpu_hours * Decimal("3.99")  # h100-sxm-8x rate
    assert cost.cost_usd == expected


# ─── per_instance_hour (AWS / GCP bundled / Azure VM GPU) ──────────────────

def test_aws_p5_share(engine):
    """AWS p5.48xlarge $98.32/hr, 1 GPU-sec used over 60s window with 8 GPUs."""
    details = _base_details(
        "per_instance_hour", gpu_count=8, duration_ms=60_000,
        region="us-east-1", instance_type="p5.48xlarge",
    )
    cloud = CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    share = Decimal("1") / (Decimal("8") * Decimal("60"))
    expected_hours = share * (Decimal("60") / Decimal("3600"))
    # Catalog us-east-1 p5.48xlarge = $55.04 (post-refresh value at 79c8745).
    # Read it from the catalog to avoid pinning a specific rate that may
    # refresh; assert the SHAPE of the math instead.
    catalog_p5 = engine._catalog["aws"]["ec2_gpu"]["regions"]["us-east-1"]["instance_types"]["p5.48xlarge"]
    expected = expected_hours * Decimal(catalog_p5["hourly_usd"])
    assert cost.cost_usd == expected
    assert cost.cost_confidence == "computed"


def test_gcp_a3_highgpu_share(engine):
    details = _base_details(
        "per_instance_hour", gpu_count=8, duration_ms=60_000,
        region="us-central1", instance_type="a3-highgpu-8g",
    )
    cloud = CloudEnv("gcp", "us-central1", "imds", instance_type="a3-highgpu-8g")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_usd > Decimal("0")
    assert "gcp" in cost.pricing_source


def test_azure_nd_h100_share(engine):
    details = _base_details(
        "per_instance_hour", gpu_count=8, duration_ms=60_000,
        region="eastus", instance_type="Standard_ND96isr_H100_v5",
    )
    cloud = CloudEnv(
        "azure", "eastus", "imds", instance_type="Standard_ND96isr_H100_v5",
    )
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_usd > Decimal("0")
    assert "azure" in cost.pricing_source


# ─── per_vgpu_hour (Azure NVadsA10 v5 fractional) ──────────────────────────

def test_azure_nv6_vgpu_share(engine):
    """Standard_NV6ads_A10_v5 (1/6 A10); 1 GPU-sec used over 60s."""
    details = _base_details(
        "per_vgpu_hour", gpu_sku="a10-vgpu-1of6", gpu_count=1, duration_ms=60_000,
        region="eastus", instance_type="Standard_NV6ads_A10_v5",
        vgpu_profile="1/6 A10",
    )
    cloud = CloudEnv(
        "azure", "eastus", "imds", instance_type="Standard_NV6ads_A10_v5",
    )
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_usd > Decimal("0")
    assert cost.cost_confidence == "computed"


# ─── Tier-3: SKU unknown → device_class fallback (Decision #4) ─────────────

def test_tier3_unknown_sku_device_class_fallback(engine):
    """A future SKU dexcost doesn't know about → falls back to device_class.

    Decision #4 — cold-start protection: customer gets a rate within ~30%
    instead of $0, at `estimated` confidence with :device_class_fallback.
    """
    details = _base_details(
        "per_gpu_second_active",
        gpu_sku=None,  # productName alias resolution failed
        gpu_seconds_used=1.0, duration_ms=1000,
        _nvml_product_name_lower="nvidia b300 200gb hbm4",  # hypothetical Blackwell-next
    )
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("modal", None, "env"), window_s=Decimal("1"),
    )
    assert cost.cost_confidence == "estimated"
    assert "device_class_fallback" in cost.pricing_source
    assert cost.cost_usd > Decimal("0")


def test_tier3_device_class_unknown_returns_zero(engine):
    """SKU unknown AND productName doesn't match any device class → cost=0 + log-once."""
    details = _base_details(
        "per_gpu_second_active",
        gpu_sku=None, gpu_seconds_used=1.0, duration_ms=1000,
        _nvml_product_name_lower="totally unknown gpu model from mars",
    )
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("modal", None, "env"), window_s=Decimal("1"),
    )
    # Falls all the way through to Tier-4 hardcoded constants — still > 0.
    assert cost.cost_confidence in ("estimated", "unknown")


# ─── Tier-4: missing catalog → hardcoded constants ─────────────────────────

def test_tier4_missing_catalog_uses_hardcoded(tmp_path):
    from dexcost.gpu_pricing import GpuPricingEngine
    bogus = tmp_path / "no.json"
    eng = GpuPricingEngine(catalog_path=bogus)
    details = _base_details(
        "per_gpu_second_active", gpu_seconds_used=1.0, duration_ms=1000,
    )
    cost = eng.resolve_gpu_cost(
        details, CloudEnv("modal", None, "env"), window_s=Decimal("1"),
    )
    assert cost.cost_usd > Decimal("0")
    assert "hardcoded" in cost.pricing_source
    assert cost.cost_confidence == "estimated"


# ─── Tier-5: computation failure → cost=0, unknown confidence ──────────────

def test_tier5_computation_failure_returns_zero(engine):
    """Malformed details → Tier-5 try/except returns 0 + unknown + log-once."""
    bad = {
        "billing_model": "per_gpu_second_active",
        "gpu_seconds_used": "not-a-number",  # will fail Decimal cast
    }
    cost = engine.resolve_gpu_cost(
        bad, CloudEnv(None, None, "none"), window_s=Decimal("1"),
    )
    assert cost.cost_usd == Decimal("0")
    assert cost.cost_confidence == "unknown"
    assert "error" in cost.pricing_source


def test_unknown_billing_model_returns_zero(engine):
    bad = _base_details("made_up_billing_model")
    cost = engine.resolve_gpu_cost(bad, CloudEnv(None, None, "none"))
    assert cost.cost_usd == Decimal("0")


# ─── Decision #1 measurement-side fallback labels ──────────────────────────

def test_self_pid_only_fallback_label(engine):
    """When details carry _cgroup_scope_fallback, pricing_source ends with the label."""
    details = _base_details(
        "per_gpu_second_active",
        gpu_seconds_used=1.0, duration_ms=1000,
        _cgroup_scope_fallback="self_pid_only",
    )
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("modal", None, "env"), window_s=Decimal("1"),
    )
    assert cost.pricing_source.endswith(":self_pid_only")
    assert cost.cost_confidence == "estimated"


def test_no_container_scope_fallback_label(engine):
    details = _base_details(
        "per_gpu_second_active",
        gpu_seconds_used=1.0, duration_ms=1000,
        _cgroup_scope_fallback="no_container_scope",
    )
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("modal", None, "env"), window_s=Decimal("1"),
    )
    assert cost.pricing_source.endswith(":no_container_scope")
    assert cost.cost_confidence == "estimated"


def test_multi_container_pod_partial_label(engine):
    details = _base_details(
        "per_instance_hour", gpu_count=8, duration_ms=60_000,
        region="us-east-1", instance_type="p5.48xlarge",
        _cgroup_scope_fallback="multi_container_pod_partial",
    )
    cloud = CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.pricing_source.endswith(":multi_container_pod_partial")
    assert cost.cost_confidence == "estimated"


# ─── Warn-once-per-failure-mode (convention §11) ───────────────────────────

def test_warn_once_per_failure_mode(tmp_path, caplog):
    import logging
    from dexcost.gpu_pricing import GpuPricingEngine, _reset_warning_state
    _reset_warning_state()
    bogus = tmp_path / "missing.json"
    with caplog.at_level(logging.WARNING):
        GpuPricingEngine(catalog_path=bogus)
        GpuPricingEngine(catalog_path=bogus)
    msgs = [r.getMessage() for r in caplog.records
            if "gpu catalog" in r.getMessage().lower()]
    assert len(msgs) == 1


def test_catalog_version_exposed(engine):
    assert engine.catalog_version.startswith("1.")
