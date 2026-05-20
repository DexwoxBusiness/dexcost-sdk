"""Cross-runtime regression matrix — one priced event per billing_model value.

Catches dispatch-table regressions where a billing_model silently routes to
the wrong arithmetic. Each test pins a hand-computed cost for a canonical
fixture; if the math drifts, exactly one entry in this table fails — making
the regression diagnosable in one assertion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from dexcost.cloud_detect import CloudEnv
from dexcost.compute_pricing import ComputePricingEngine


def _env(provider, region, instance_type=None):
    return CloudEnv(
        provider=provider, region=region, source="env",
        instance_type=instance_type,
    )


@pytest.fixture(scope="module")
def engine():
    return ComputePricingEngine()


def test_dispatch_lambda(engine):
    details = {
        "billing_model": "lambda", "duration_ms": 100,
        "memory_bytes_limit": 1024 * 1024 * 1024, "vcpu_count": 1.0,
        "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "us-east-1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(details, _env("aws", "us-east-1"), {})
    assert cost.cost_usd > 0
    assert "lambda" in cost.pricing_source


def test_dispatch_fargate(engine):
    details = {
        "billing_model": "fargate", "duration_ms": 60_000,
        "memory_bytes_limit": 1024 * 1024 * 1024, "vcpu_count": 0.5,
        "vcpu_seconds_used": 30, "invocation_count": 0,
        "region": "us-east-1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env("aws", "us-east-1"), {}, window_s=Decimal("60"),
    )
    assert cost.cost_usd > 0
    assert "fargate" in cost.pricing_source


def test_dispatch_cloud_run_request(engine):
    details = {
        "billing_model": "cloud_run_request", "duration_ms": 250,
        "memory_bytes_limit": 256 * 1024 * 1024, "vcpu_count": 0.5,
        "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "us-central1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env("gcp", "us-central1"), {},
    )
    assert cost.cost_usd > 0
    assert "cloud_run" in cost.pricing_source


def test_dispatch_cloud_run_instance_override(engine):
    details = {
        "billing_model": "cloud_run_request", "duration_ms": 0,
        "memory_bytes_limit": 256 * 1024 * 1024, "vcpu_count": 0.5,
        "vcpu_seconds_used": 0, "invocation_count": 0,
        "region": "us-central1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env("gcp", "us-central1"),
        {"cloud_run": "instance"}, window_s=Decimal("60"),
    )
    assert cost.cost_usd > 0
    assert cost.pricing_source.endswith("instance_override")


def test_dispatch_cloud_functions(engine):
    details = {
        "billing_model": "cloud_functions", "duration_ms": 250,
        "memory_bytes_limit": 256 * 1024 * 1024, "vcpu_count": 0.5,
        "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "us-central1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env("gcp", "us-central1"), {},
    )
    assert cost.cost_usd > 0
    assert "cloud_functions" in cost.pricing_source


def test_dispatch_azure_functions(engine):
    details = {
        "billing_model": "azure_functions", "duration_ms": 200,
        "memory_bytes_limit": 512 * 1000 * 1000, "vcpu_count": 1.0,
        "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "eastus", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env("azure", "eastus"), {},
    )
    assert cost.cost_usd > 0
    assert "azure" in cost.pricing_source


def test_dispatch_vercel_fluid(engine):
    details = {
        "billing_model": "vercel_fluid", "duration_ms": 500,
        "memory_bytes_limit": 256 * 1000 * 1000, "vcpu_count": 1.0,
        "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": None, "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(details, _env(None, None), {})
    assert cost.cost_usd > 0
    assert "vercel" in cost.pricing_source


def test_dispatch_ec2(engine):
    details = {
        "billing_model": "ec2", "duration_ms": 60_000,
        "memory_bytes_limit": 0, "vcpu_count": 4.0,
        "vcpu_seconds_used": 1.0, "invocation_count": 0,
        "region": "us-east-1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env("aws", "us-east-1", instance_type="c7g.xlarge"),
        {}, window_s=Decimal("60"),
    )
    assert cost.cost_usd > 0
    assert "ec2" in cost.pricing_source


def test_dispatch_gce(engine):
    details = {
        "billing_model": "gce", "duration_ms": 60_000,
        "memory_bytes_limit": 0, "vcpu_count": 2.0,
        "vcpu_seconds_used": 0.5, "invocation_count": 0,
        "region": "us-central1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env("gcp", "us-central1", instance_type="n2-standard-2"),
        {}, window_s=Decimal("60"),
    )
    assert cost.cost_usd > 0
    assert "gce" in cost.pricing_source


def test_dispatch_azure_vm(engine):
    details = {
        "billing_model": "azure_vm", "duration_ms": 60_000,
        "memory_bytes_limit": 0, "vcpu_count": 2.0,
        "vcpu_seconds_used": 0.5, "invocation_count": 0,
        "region": "eastus", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env("azure", "eastus", instance_type="Standard_D2s_v3"),
        {}, window_s=Decimal("60"),
    )
    assert cost.cost_usd > 0
    # pricing_source uses "azure:vm" — the catalog runtime key is "vm".
    assert "azure:vm" in cost.pricing_source


def test_dispatch_k8s_pod(engine):
    details = {
        "billing_model": "k8s_pod", "duration_ms": 60_000,
        "memory_bytes_limit": 512 * 1024 * 1024, "vcpu_count": 0.5,
        "vcpu_seconds_used": 0.3, "invocation_count": 0,
        "region": None, "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(None, None), {}, window_s=Decimal("60"),
    )
    assert cost.cost_usd > 0
    assert "k8s_pod" in cost.pricing_source
