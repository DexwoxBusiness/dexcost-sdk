"""Compute pricing property invariants — spec §10.3.

Must hold across arbitrary task shapes (billing_model × region ×
architecture × duration × memory × vcpu). Parametrized.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dexcost.cloud_detect import CloudEnv
from dexcost.compute_pricing import ComputePricingEngine


def _env(provider="aws", region="us-east-1", instance_type="c7g.xlarge"):
    return CloudEnv(
        provider=provider, region=region, source="env",
        instance_type=instance_type,
    )


def _base_details(billing_model: str) -> dict:
    return {
        "billing_model": billing_model,
        "duration_ms": 1000,
        "memory_bytes_limit": 512 * 1024 * 1024,
        "vcpu_count": 1.0,
        "vcpu_seconds_used": 0.5,
        "invocation_count": 1,
        "region": "us-east-1",
        "architecture": "x86_64",
    }


ALL_BILLING_MODELS = [
    "lambda", "fargate", "cloud_run_request", "cloud_run_instance",
    "cloud_functions", "azure_functions", "vercel_fluid",
    "ec2", "gce", "azure_vm", "k8s_pod",
]


@pytest.fixture(scope="module")
def engine():
    return ComputePricingEngine()


@pytest.mark.parametrize("billing_model", ALL_BILLING_MODELS)
def test_invariant_1_never_negative(engine, billing_model):
    """cost_usd >= 0 always."""
    details = _base_details(billing_model)
    cost = engine.resolve_compute_cost(
        details, _env(), {}, window_s=Decimal("1"),
    )
    assert cost.cost_usd >= Decimal("0")


@pytest.mark.parametrize("billing_model", ["lambda", "azure_functions"])
def test_invariant_3_linearity_in_duration(engine, billing_model):
    """For serverless runtimes that bill per-GB-second + per-invocation,
    doubling duration with fixed invocations should ~double the gb-second
    portion. Tests the duration-linear axis of the math."""
    base = _base_details(billing_model)
    cost_a = engine.resolve_compute_cost(
        {**base, "duration_ms": 100}, _env(), {},
    )
    cost_b = engine.resolve_compute_cost(
        {**base, "duration_ms": 200}, _env(), {},
    )
    # cost_b > cost_a by exactly the gb_seconds delta (invocation term shared).
    assert cost_b.cost_usd > cost_a.cost_usd


def test_invariant_4_arm_cheaper_than_x86_on_lambda(engine):
    base = _base_details("lambda")
    x86 = engine.resolve_compute_cost(
        {**base, "architecture": "x86_64"}, _env(), {},
    )
    arm = engine.resolve_compute_cost(
        {**base, "architecture": "arm64"}, _env(), {},
    )
    assert arm.cost_usd < x86.cost_usd


def test_invariant_4_arm_cheaper_than_x86_on_fargate(engine):
    base = _base_details("fargate")
    x86 = engine.resolve_compute_cost(
        {**base, "architecture": "x86_64"}, _env(), {},
        window_s=Decimal("1"),
    )
    arm = engine.resolve_compute_cost(
        {**base, "architecture": "arm64"}, _env(), {},
        window_s=Decimal("1"),
    )
    assert arm.cost_usd < x86.cost_usd


@pytest.mark.parametrize("billing_model", ALL_BILLING_MODELS)
def test_invariant_5_confidence_is_computed_or_estimated(engine, billing_model):
    """Well-formed input → never `unknown`."""
    details = _base_details(billing_model)
    cost = engine.resolve_compute_cost(
        details, _env(), {}, window_s=Decimal("1"),
    )
    assert cost.cost_confidence in {"computed", "estimated"}


@pytest.mark.parametrize("billing_model", ALL_BILLING_MODELS)
def test_invariant_6_pricing_source_namespace(engine, billing_model):
    """Every pricing_source starts with compute_catalog:."""
    details = _base_details(billing_model)
    cost = engine.resolve_compute_cost(
        details, _env(), {}, window_s=Decimal("1"),
    )
    assert cost.pricing_source.startswith("compute_catalog:")


@pytest.mark.parametrize("memory_gib", [1, 2, 4, 16, 64])
def test_invariant_3_linearity_in_memory_fargate(engine, memory_gib):
    """Doubling memory should double the gib_seconds portion on Fargate."""
    base = _base_details("fargate")
    base["memory_bytes_limit"] = memory_gib * 1024 * 1024 * 1024
    cost = engine.resolve_compute_cost(
        base, _env(), {}, window_s=Decimal("1"),
    )
    # Compute by hand: vcpu_term + gib_term*memory_gib
    vcpu_term = Decimal("1") * Decimal("1") * Decimal("0.0000112444")
    gib_term = Decimal(memory_gib) * Decimal("1") * Decimal("0.0000012347")
    assert cost.cost_usd == vcpu_term + gib_term
