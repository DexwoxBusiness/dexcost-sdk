"""Cross-billing-model matrix — one canonical case per discriminator.

Catches dispatch-table regressions: if a future refactor accidentally routes
per_vgpu_hour through the per_instance_hour math (or any other mis-wire), at
least one of these tests fails with a specific billing_model in the failure
message.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dexcost.cloud_detect import CloudEnv
from dexcost.gpu_pricing import GpuPricingEngine


@pytest.fixture(scope="module")
def engine():
    return GpuPricingEngine()


# ─── per_gpu_second_active ──────────────────────────────────────────────────

def test_dispatch_per_gpu_second_active_modal(engine):
    details = {
        "billing_model": "per_gpu_second_active",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 1,
        "region": None, "duration_ms": 1000, "gpu_seconds_used": 1.0,
        "instance_type": None, "vgpu_profile": None, "mig_profile": None,
    }
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("modal", None, "env"), window_s=Decimal("1"),
    )
    assert cost.cost_usd > 0
    assert "modal" in cost.pricing_source
    assert "per_gpu_second_active" in cost.pricing_source


# ─── per_instance_hour ──────────────────────────────────────────────────────

def test_dispatch_per_instance_hour_aws(engine):
    details = {
        "billing_model": "per_instance_hour",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 8,
        "region": "us-east-1", "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": "p5.48xlarge", "vgpu_profile": None, "mig_profile": None,
    }
    cloud = CloudEnv("aws", "us-east-1", "imds", instance_type="p5.48xlarge")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_usd > 0
    assert "aws" in cost.pricing_source
    assert "ec2_gpu" in cost.pricing_source


def test_dispatch_per_instance_hour_gcp_bundled(engine):
    details = {
        "billing_model": "per_instance_hour",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 8,
        "region": "us-central1", "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": "a3-highgpu-8g", "vgpu_profile": None, "mig_profile": None,
    }
    cloud = CloudEnv("gcp", "us-central1", "imds", instance_type="a3-highgpu-8g")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_usd > 0
    assert "gcp" in cost.pricing_source
    assert "gce_gpu_bundled" in cost.pricing_source


def test_dispatch_per_instance_hour_azure_vm_gpu(engine):
    details = {
        "billing_model": "per_instance_hour",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 8,
        "region": "eastus", "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": "Standard_ND96isr_H100_v5",
        "vgpu_profile": None, "mig_profile": None,
    }
    cloud = CloudEnv("azure", "eastus", "imds",
                      instance_type="Standard_ND96isr_H100_v5")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_usd > 0
    assert "azure" in cost.pricing_source
    assert "vm_gpu" in cost.pricing_source


# ─── per_gpu_hour_reserved ──────────────────────────────────────────────────

def test_dispatch_per_gpu_hour_reserved_lambda_labs(engine):
    details = {
        "billing_model": "per_gpu_hour_reserved",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 8,
        "region": None, "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": None, "vgpu_profile": None, "mig_profile": None,
    }
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("lambda_labs", None, "dmi"), window_s=Decimal("60"),
    )
    assert cost.cost_usd > 0
    assert "lambda_labs" in cost.pricing_source
    assert "per_gpu_hour_reserved" in cost.pricing_source


def test_dispatch_per_gpu_hour_reserved_coreweave(engine):
    details = {
        "billing_model": "per_gpu_hour_reserved",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 8,
        "region": None, "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": None, "vgpu_profile": None, "mig_profile": None,
    }
    cost = engine.resolve_gpu_cost(
        details, CloudEnv("coreweave", None, "dmi"), window_s=Decimal("60"),
    )
    assert cost.cost_usd > 0
    assert "coreweave" in cost.pricing_source


# ─── per_vgpu_hour ──────────────────────────────────────────────────────────

def test_dispatch_per_vgpu_hour_azure_nv6(engine):
    details = {
        "billing_model": "per_vgpu_hour",
        "gpu_vendor": "nvidia", "gpu_sku": "a10-vgpu-1of6", "gpu_count": 1,
        "region": "eastus", "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": "Standard_NV6ads_A10_v5",
        "vgpu_profile": "1/6 A10", "mig_profile": None,
    }
    cloud = CloudEnv("azure", "eastus", "imds",
                      instance_type="Standard_NV6ads_A10_v5")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_usd > 0
    assert "azure" in cost.pricing_source
    assert "vm_vgpu" in cost.pricing_source


# ─── per_gpu_hour_reserved + GCP N1 attached accelerator (Decision #9) ─────

def test_dispatch_gcp_n1_attached_accelerator(engine):
    """GCP N1 + nvidia-h100-80gb accelerator detected via NVML-only fallback."""
    details = {
        "billing_model": "per_gpu_hour_reserved",
        "gpu_vendor": "nvidia", "gpu_sku": "h100-80gb-sxm5", "gpu_count": 1,
        "region": "us-central1", "duration_ms": 60_000, "gpu_seconds_used": 1.0,
        "instance_type": "n1-standard-8", "vgpu_profile": None, "mig_profile": None,
    }
    cloud = CloudEnv("gcp", "us-central1", "imds",
                      instance_type="n1-standard-8")
    cost = engine.resolve_gpu_cost(details, cloud, window_s=Decimal("60"))
    assert cost.cost_usd > 0
    # Decision #9 — should resolve through gce_gpu_attached.accelerator_types
    assert "gcp" in cost.pricing_source
    assert "gce_gpu_attached" in cost.pricing_source
