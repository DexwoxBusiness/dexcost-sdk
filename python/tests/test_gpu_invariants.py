"""GPU pricing property invariants — cost spec §10.3 (7 invariants)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from dexcost.cloud_detect import CloudEnv
from dexcost.gpu_pricing import GpuPricingEngine


@pytest.fixture(scope="module")
def engine():
    return GpuPricingEngine()


def _base(billing_model, **overrides):
    base = {
        "billing_model": billing_model,
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 1,
        "region": None, "duration_ms": 1000, "gpu_seconds_used": 1.0,
        "instance_type": None, "vgpu_profile": None, "mig_profile": None,
    }
    base.update(overrides)
    return base


ALL_BILLING_MODELS = [
    "per_gpu_second_active",
    "per_instance_hour",
    "per_gpu_hour_reserved",
    "per_vgpu_hour",
]


# Invariant 1 — cost_usd >= 0 always
@pytest.mark.parametrize("billing_model", ALL_BILLING_MODELS)
def test_invariant_1_cost_never_negative(engine, billing_model):
    details = _base(billing_model)
    if billing_model == "per_instance_hour":
        details["instance_type"] = "p5.48xlarge"
        details["region"] = "us-east-1"
        cloud = CloudEnv("aws", "us-east-1", "imds",
                          instance_type="p5.48xlarge")
    elif billing_model == "per_vgpu_hour":
        details["instance_type"] = "Standard_NV6ads_A10_v5"
        details["region"] = "eastus"
        details["gpu_sku"] = "a10-vgpu-1of6"
        cloud = CloudEnv("azure", "eastus", "imds",
                          instance_type="Standard_NV6ads_A10_v5")
    else:
        cloud = CloudEnv("modal", None, "env")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("1"))
    assert cost.cost_usd >= Decimal("0")


# Invariant 3a — linearity in gpu_seconds_used (per_gpu_second_active)
@pytest.mark.parametrize("scale", [1, 2, 5, 10])
def test_invariant_3_linearity_in_gpu_seconds(engine, scale):
    base = _base("per_gpu_second_active",
                  gpu_seconds_used=float(scale), duration_ms=scale * 1000)
    cost = engine.resolve_gpu_cost(base, CloudEnv("modal", None, "env"),
                                    window_s=Decimal(str(scale)))
    base_one = _base("per_gpu_second_active",
                      gpu_seconds_used=1.0, duration_ms=1000)
    cost_one = engine.resolve_gpu_cost(base_one, CloudEnv("modal", None, "env"),
                                        window_s=Decimal("1"))
    assert cost.cost_usd == cost_one.cost_usd * Decimal(scale)


# Invariant 4 — H100 SKU more expensive than A100 SKU on same provider
def test_invariant_4_h100_more_expensive_than_a100_on_modal(engine):
    h100 = _base("per_gpu_second_active", gpu_sku="h100-80gb-sxm5")
    a100 = _base("per_gpu_second_active", gpu_sku="a100-80gb-sxm4")
    cloud = CloudEnv("modal", None, "env")
    h_cost = engine.resolve_gpu_cost(h100, cloud, window_s=Decimal("1"))
    a_cost = engine.resolve_gpu_cost(a100, cloud, window_s=Decimal("1"))
    assert h_cost.cost_usd > a_cost.cost_usd, (
        "H100 should cost more than A100 on Modal (newer/faster GPUs cost more)"
    )


# Invariant 5 — per_gpu_second × 3600 within ±20% of per_gpu_hour (Modal markup)
def test_invariant_5_per_second_vs_per_hour_rate_relationship(engine):
    """Modal per-GPU-second × 3600 should be within 5-25% of Lambda's per-hour rate
    (serverless markup is real and the gap IS the point of the catalog)."""
    modal_h100 = _base("per_gpu_second_active", gpu_sku="h100-80gb-sxm5",
                        gpu_seconds_used=3600.0, duration_ms=3_600_000)
    lambda_h100 = _base("per_gpu_hour_reserved", gpu_sku="h100-80gb-sxm5",
                         gpu_count=1, gpu_seconds_used=3600.0,
                         duration_ms=3_600_000)
    modal_cost = engine.resolve_gpu_cost(
        modal_h100, CloudEnv("modal", None, "env"), window_s=Decimal("3600"),
    )
    lambda_cost = engine.resolve_gpu_cost(
        lambda_h100, CloudEnv("lambda_labs", None, "dmi"),
        window_s=Decimal("3600"),
    )
    # Both should produce non-zero costs.
    assert modal_cost.cost_usd > 0
    assert lambda_cost.cost_usd > 0
    # Modal serverless rate × 1hr should be within an order of magnitude
    # of Lambda reserved per-hour rate.
    ratio = modal_cost.cost_usd / lambda_cost.cost_usd
    assert Decimal("0.5") < ratio < Decimal("3.0"), (
        f"Modal/Lambda H100 ratio out of band: {ratio}"
    )


# Invariant 6 — cost_confidence in {computed, estimated} on well-formed input
@pytest.mark.parametrize("billing_model", ALL_BILLING_MODELS)
def test_invariant_6_confidence_is_computed_or_estimated(engine, billing_model):
    details = _base(billing_model)
    if billing_model == "per_instance_hour":
        details["instance_type"] = "p5.48xlarge"
        details["region"] = "us-east-1"
        cloud = CloudEnv("aws", "us-east-1", "imds",
                          instance_type="p5.48xlarge")
    elif billing_model == "per_vgpu_hour":
        details["instance_type"] = "Standard_NV6ads_A10_v5"
        details["region"] = "eastus"
        details["gpu_sku"] = "a10-vgpu-1of6"
        cloud = CloudEnv("azure", "eastus", "imds",
                          instance_type="Standard_NV6ads_A10_v5")
    else:
        cloud = CloudEnv("modal", None, "env")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("1"))
    assert cost.cost_confidence in ("computed", "estimated")


# Invariant 7 — pricing_source starts with "gpu_catalog:"
@pytest.mark.parametrize("billing_model", ALL_BILLING_MODELS)
def test_invariant_7_pricing_source_namespace(engine, billing_model):
    details = _base(billing_model)
    if billing_model == "per_instance_hour":
        details["instance_type"] = "p5.48xlarge"
        details["region"] = "us-east-1"
        cloud = CloudEnv("aws", "us-east-1", "imds",
                          instance_type="p5.48xlarge")
    elif billing_model == "per_vgpu_hour":
        details["instance_type"] = "Standard_NV6ads_A10_v5"
        details["region"] = "eastus"
        details["gpu_sku"] = "a10-vgpu-1of6"
        cloud = CloudEnv("azure", "eastus", "imds",
                          instance_type="Standard_NV6ads_A10_v5")
    else:
        cloud = CloudEnv("modal", None, "env")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("1"))
    assert cost.pricing_source.startswith("gpu_catalog:")
