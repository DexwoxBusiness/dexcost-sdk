"""Compute pricing — per-billing-model math, degradation ladder, no-float-drift."""

from __future__ import annotations

from decimal import Decimal

import pytest

from dexcost.cloud_detect import CloudEnv


@pytest.fixture(autouse=True)
def _reset():
    from dexcost.compute_pricing import _reset_warning_state
    _reset_warning_state()


def _env(provider="aws", region="us-east-1", instance_type=None):
    return CloudEnv(
        provider=provider, region=region, source="env",
        instance_type=instance_type,
    )


@pytest.fixture
def engine():
    from dexcost.compute_pricing import ComputePricingEngine
    return ComputePricingEngine()


# ─── Lambda ──────────────────────────────────────────────────────────────────

def test_lambda_x86_canonical_case(engine):
    """1024 MiB × 100 ms × 1 invocation in us-east-1 (x86_64).

    Lambda uses DECIMAL GB (10^9 bytes) per Decision #7:
      gb_seconds = 1024^3 / 10^9 * (100/1000) ≈ 0.107374
      cost = 1 * 0.0000002 + gb_seconds * 0.0000166667
    """
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
    gb_seconds = (
        Decimal(1024 * 1024 * 1024) / Decimal("1000000000") * Decimal("0.1")
    )
    expected = Decimal("0.0000002") + gb_seconds * Decimal("0.0000166667")
    assert cost.cost_usd == expected
    assert cost.cost_confidence == "computed"
    assert cost.pricing_source == "compute_catalog:aws:lambda:us-east-1:x86_64"


def test_lambda_arm_is_cheaper(engine):
    base = {
        "billing_model": "lambda", "duration_ms": 100,
        "memory_bytes_limit": 1024 * 1024 * 1024, "vcpu_count": 1.0,
        "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "us-east-1",
    }
    x86 = engine.resolve_compute_cost({**base, "architecture": "x86_64"}, _env(), {})
    arm = engine.resolve_compute_cost({**base, "architecture": "arm64"}, _env(), {})
    assert arm.cost_usd < x86.cost_usd


# ─── Fargate (the binary GiB bug-prevention test) ────────────────────────────

def test_fargate_uses_binary_gib_divisor(engine):
    """0.5 vCPU × 1 GiB × 60 s in us-east-1.

    Fargate uses BINARY GiB per Decision #7 — divisor 1024^3. If the
    implementation confuses it for decimal GB (10^9), the GiB term becomes
    1.073741824 instead of 1.0 — silent ~7.4% over-attribution.
    """
    details = {
        "billing_model": "fargate",
        "duration_ms": 60_000,
        "memory_bytes_limit": 1024 * 1024 * 1024,  # exactly 1 GiB
        "vcpu_count": 0.5,
        "vcpu_seconds_used": 30,
        "invocation_count": 0,
        "region": "us-east-1",
        "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(), {}, window_s=Decimal("60"),
    )
    vcpu_term = Decimal("0.5") * Decimal("60") * Decimal("0.0000111111")
    gib_term = Decimal("1") * Decimal("60") * Decimal("0.0000012222")
    assert cost.cost_usd == vcpu_term + gib_term


# ─── Cloud Run ───────────────────────────────────────────────────────────────

def test_cloud_run_default_is_estimated(engine):
    details = {
        "billing_model": "cloud_run_request",
        "duration_ms": 250,
        "memory_bytes_limit": 256 * 1024 * 1024,
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


def test_cloud_run_instance_override_is_computed(engine):
    details = {
        "billing_model": "cloud_run_request",
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


# ─── Azure Functions ─────────────────────────────────────────────────────────

def test_azure_functions_canonical(engine):
    details = {
        "billing_model": "azure_functions", "duration_ms": 200,
        "memory_bytes_limit": 512 * 1000 * 1000,
        "vcpu_count": 1.0, "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "eastus", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(provider="azure", region="eastus"), {},
    )
    # Decimal GB divisor per Decision #7.
    gb_seconds = (
        Decimal(512 * 1000 * 1000) / Decimal("1000000000") * Decimal("0.2")
    )
    expected = Decimal("0.0000002") + gb_seconds * Decimal("0.000016")
    assert cost.cost_usd == expected


# ─── Vercel ──────────────────────────────────────────────────────────────────

def test_vercel_active_cpu_approximates_wall_duration(engine):
    details = {
        "billing_model": "vercel_fluid",
        "duration_ms": 500,
        "memory_bytes_limit": 256 * 1000 * 1000,
        "vcpu_count": 1.0, "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": None, "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(provider=None, region=None), {},
    )
    assert cost.cost_usd > 0
    assert cost.cost_confidence == "computed"


# ─── EC2 instance share ──────────────────────────────────────────────────────

def test_ec2_share_factor_math(engine):
    """1 vCPU-second used over 60s window on a 4-vCPU c7g.xlarge.

    share_factor       = 1 / (4 * 60) = 0.004166...
    task_instance_hrs  = share_factor * (60 / 3600)
    cost               = task_instance_hrs * 0.1450
    """
    details = {
        "billing_model": "ec2",
        "duration_ms": 60_000,
        "memory_bytes_limit": 0, "vcpu_count": 4.0,
        "vcpu_seconds_used": 1.0, "invocation_count": 0,
        "region": "us-east-1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details,
        _env(provider="aws", region="us-east-1", instance_type="c7g.xlarge"),
        {}, window_s=Decimal("60"),
    )
    expected_share = Decimal("1") / (Decimal("4") * Decimal("60"))
    expected_hours = expected_share * (Decimal("60") / Decimal("3600"))
    expected = expected_hours * Decimal("0.1450")
    assert cost.cost_usd == expected


# ─── K8s pod default (no node-aware) ─────────────────────────────────────────

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
    expected = (
        Decimal("0.5") * (Decimal("60") / Decimal("3600")) * Decimal("0.0464")
    )
    assert cost.cost_usd == expected
    assert cost.cost_confidence == "computed"


# ─── Degradation ladder ──────────────────────────────────────────────────────

def test_tier2_unknown_region_falls_to_runtime_default(engine):
    """Provider+runtime in catalog but region absent → per-runtime default
    block, ``estimated`` confidence (Tier 2 of the §7.1 ladder)."""
    details = {
        "billing_model": "lambda", "duration_ms": 100,
        "memory_bytes_limit": 128 * 1000 * 1000,
        "vcpu_count": 1.0, "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": None, "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(
        details, _env(provider=None, region=None), {},
    )
    assert cost.cost_confidence == "estimated"
    assert cost.pricing_source == "compute_catalog:aws:lambda:default:x86_64"


def test_tier4_missing_catalog_uses_hardcoded(tmp_path):
    from dexcost.compute_pricing import ComputePricingEngine
    bogus = tmp_path / "no.json"
    eng = ComputePricingEngine(catalog_path=bogus)
    details = {
        "billing_model": "lambda", "duration_ms": 100,
        "memory_bytes_limit": 128 * 1000 * 1000,
        "vcpu_count": 1.0, "vcpu_seconds_used": 0, "invocation_count": 1,
        "region": "us-east-1", "architecture": "x86_64",
    }
    cost = eng.resolve_compute_cost(details, _env(), {})
    assert cost.cost_usd > 0
    assert cost.pricing_source.startswith("compute_catalog:hardcoded")
    assert cost.cost_confidence == "estimated"


def test_tier5_computation_failure_returns_zero(engine):
    bad = {"billing_model": "lambda", "duration_ms": "not-a-number"}
    cost = engine.resolve_compute_cost(bad, _env(), {})
    assert cost.cost_usd == Decimal("0")


def test_unknown_billing_model_returns_zero(engine):
    bad = {
        "billing_model": "totally_made_up", "duration_ms": 100,
        "memory_bytes_limit": 0, "vcpu_count": 1.0,
        "vcpu_seconds_used": 0, "invocation_count": 0,
        "region": "us-east-1", "architecture": "x86_64",
    }
    cost = engine.resolve_compute_cost(bad, _env(), {})
    assert cost.cost_usd == Decimal("0")


# ─── No-float-drift ──────────────────────────────────────────────────────────

def test_decimal_no_float_drift_per_conversion():
    """Pin Decision #7: divisors stay Decimal, NEVER coerce through float."""
    # Fargate / Cloud Run — binary GiB.
    assert (
        Decimal(2 * 1024 * 1024 * 1024) / Decimal(1024 * 1024 * 1024)
        == Decimal("2")
    )
    # Lambda / Azure Functions / Vercel — decimal GB.
    assert (
        Decimal(2 * 1000 * 1000 * 1000) / Decimal("1000000000")
        == Decimal("2")
    )
    # Multiplication step against hand-computed expected:
    # 166667 * 1024 = 170,667,008 → 0.0000166667 * 1024 = 0.0170667008
    assert Decimal("0.0000166667") * Decimal("1024") == Decimal("0.0170667008")


# ─── Warning state ───────────────────────────────────────────────────────────

def test_warn_once_per_failure_mode(tmp_path, caplog):
    import logging

    from dexcost.compute_pricing import (
        ComputePricingEngine, _reset_warning_state,
    )

    _reset_warning_state()
    bogus = tmp_path / "missing.json"
    with caplog.at_level(logging.WARNING):
        ComputePricingEngine(catalog_path=bogus)
        ComputePricingEngine(catalog_path=bogus)
    msgs = [
        r.getMessage() for r in caplog.records
        if "compute catalog" in r.getMessage().lower()
    ]
    assert len(msgs) == 1


def test_catalog_version_exposed(engine):
    assert engine.catalog_version.startswith("1.")
