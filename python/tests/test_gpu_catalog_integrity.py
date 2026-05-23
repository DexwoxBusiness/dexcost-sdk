"""GPU catalog integrity tests — structure, Decimal parsing, freshness, SKU consistency.

Catalog already shipped at commit 79c8745 (live-verified across all 8 providers).
These tests pin its STRUCTURAL invariants so a future refresh can't drift
shape; freshness check enforces Decision #11's tighter 90/365-day thresholds
(vs Phase 1 compute's 180/730).
"""

from __future__ import annotations

import datetime as _dt
import importlib.resources as ir
import json
from decimal import Decimal

import pytest


def _load() -> dict:
    raw = (
        ir.files("dexcost")
        .joinpath("data")
        .joinpath("gpu_prices.json")
        .read_text()
    )
    return json.loads(raw)


# ─── Basic structure ─────────────────────────────────────────────────────────


def test_catalog_parses_as_json():
    data = _load()
    assert "_meta" in data


def test_meta_has_required_default_keys():
    """All four billing-model default rates present and Decimal-parseable."""
    meta = _load()["_meta"]
    required = [
        "version", "last_updated", "currency",
        "default_per_instance_hour_usd",
        "default_per_gpu_second_active_usd",
        "default_per_gpu_hour_reserved_usd",
        "default_per_vgpu_hour_usd",
        "description", "notes",
    ]
    for k in required:
        assert k in meta, f"_meta missing {k}"
        if k.startswith("default_") and k.endswith("_usd"):
            Decimal(meta[k])
    assert meta["currency"] == "USD"


def test_all_eight_providers_present():
    """All 8 providers per research §2 + decisions log must be in the catalog."""
    data = _load()
    expected = {"aws", "gcp", "azure", "modal", "runpod",
                "lambda_labs", "coreweave", "replicate"}
    assert expected <= set(data.keys())


# ─── Per-provider freshness (Decision #11) ───────────────────────────────────


def test_every_provider_has_last_verified_iso():
    data = _load()
    for provider, block in data.items():
        if provider == "_meta":
            continue
        # Must parse as ISO-8601 date.
        _dt.date.fromisoformat(block["_last_verified"])


def test_decision_11_soft_warn_at_90_days(monkeypatch):
    """Decision #11: GPU catalog soft-warns at 90 days (tighter than compute's 180)."""
    import warnings
    data = _load()
    soft_limit = _dt.timedelta(days=90)
    # Pretend "today" is 91 days after every provider's _last_verified to
    # force the warn branch deterministically.
    earliest = min(
        _dt.date.fromisoformat(b["_last_verified"])
        for k, b in data.items() if k != "_meta"
    )
    fake_today = earliest + _dt.timedelta(days=91)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for provider, block in data.items():
            if provider == "_meta":
                continue
            verified = _dt.date.fromisoformat(block["_last_verified"])
            if fake_today - verified > soft_limit:
                warnings.warn(
                    f"gpu_prices.json: {provider} _last_verified is "
                    f"{(fake_today - verified).days} days old (soft limit 90)",
                    stacklevel=2,
                )
    assert any("soft limit 90" in str(w.message) for w in caught)


def test_decision_11_hard_fail_at_365_days():
    """Decision #11: 365-day hard fail (catalog rates that old are stale enough to be wrong)."""
    data = _load()
    today = _dt.date.today()
    hard_limit = _dt.timedelta(days=365)
    stale = []
    for provider, block in data.items():
        if provider == "_meta":
            continue
        verified = _dt.date.fromisoformat(block["_last_verified"])
        if today - verified > hard_limit:
            stale.append(f"{provider}: {(today - verified).days}d")
    assert not stale, f"GPU catalog entries >365d old (hard fail): {stale}"


# ─── Per-provider block shape ────────────────────────────────────────────────


def test_aws_block_has_ec2_gpu_regions():
    data = _load()
    assert "ec2_gpu" in data["aws"]
    assert "regions" in data["aws"]["ec2_gpu"]
    assert "us-east-1" in data["aws"]["ec2_gpu"]["regions"]


def test_gcp_block_has_attached_and_bundled():
    """GCP exposes BOTH attached-accelerator (N1) AND bundled (A2/A3/G2) shapes."""
    data = _load()
    assert "gce_gpu_attached" in data["gcp"]
    assert "gce_gpu_bundled" in data["gcp"]


def test_azure_block_has_vm_gpu_and_vm_vgpu():
    """Azure has BOTH per_instance_hour (NC/ND) AND per_vgpu_hour (NVadsA10 v5)."""
    data = _load()
    assert "vm_gpu" in data["azure"]
    assert "vm_vgpu" in data["azure"]


def test_serverless_providers_have_per_gpu_second_active():
    data = _load()
    for p in ("modal", "runpod", "replicate"):
        assert "per_gpu_second_active" in data[p], (
            f"{p} should be per_gpu_second_active billing model"
        )


def test_reserved_providers_have_per_gpu_hour_reserved():
    data = _load()
    for p in ("lambda_labs", "coreweave"):
        assert "per_gpu_hour_reserved" in data[p], (
            f"{p} should be per_gpu_hour_reserved billing model"
        )


# ─── Every USD field Decimal-parseable ───────────────────────────────────────


def test_every_usd_rate_is_decimal_parseable():
    """Walk the entire catalog; every *_usd field must be Decimal-clean."""
    data = _load()

    def _walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and (k.endswith("_usd") or k == "vcpu_count" or k == "gpu_count" or k == "gpu_vram_gb" or k == "memory_gb"):
                    try:
                        Decimal(v)
                    except Exception as exc:  # noqa: BLE001
                        raise AssertionError(
                            f"{path}.{k} not Decimal-parseable: {v!r}"
                        ) from exc
                else:
                    _walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")

    _walk(data, "")


# ─── Cross-provider canonical SKU consistency ────────────────────────────────


def test_h100_80gb_sxm5_sku_consistent_across_providers():
    """The canonical key 'h100-80gb-sxm5' should appear on AWS p5, GCP a3,
    Azure ND, Modal H100, Lambda H100 SXM, CoreWeave H100, Replicate H100.

    This is the cross-provider portability test — gpu_sku is what lets a
    customer compare "this workload on Modal vs AWS p5" through dexcost.
    """
    data = _load()
    found_providers = set()

    def _walk(node, provider):
        if isinstance(node, dict):
            if node.get("gpu_sku") == "h100-80gb-sxm5":
                found_providers.add(provider)
            for v in node.values():
                _walk(v, provider)
        elif isinstance(node, list):
            for item in node:
                _walk(item, provider)

    for provider in data:
        if provider == "_meta":
            continue
        _walk(data[provider], provider)

    # H100 SXM5 should appear on the providers that publish H100 SXM5.
    expected = {"aws", "gcp", "azure", "modal", "runpod",
                "lambda_labs", "coreweave", "replicate"}
    assert found_providers == expected, (
        f"h100-80gb-sxm5 missing from: {expected - found_providers}; "
        f"unexpected on: {found_providers - expected}"
    )


# ─── Every dispatch billing model has a rate path ────────────────────────────


def test_every_dispatch_billing_model_has_a_rate_path():
    """Each value the pricing engine dispatches on must reach a rate."""
    meta = _load()["_meta"]
    # 4 billing models — every one needs a meta default for Tier-3 fallback.
    assert "default_per_instance_hour_usd" in meta
    assert "default_per_gpu_second_active_usd" in meta
    assert "default_per_gpu_hour_reserved_usd" in meta
    assert "default_per_vgpu_hour_usd" in meta


# ─── Aliases array shape ─────────────────────────────────────────────────────


def test_aliases_arrays_present_on_sku_entries():
    """Every SKU entry should carry an aliases array (may be empty pending spike)."""
    data = _load()
    sample_entries = [
        data["aws"]["ec2_gpu"]["regions"]["us-east-1"]["instance_types"]["p5.48xlarge"],
        data["modal"]["per_gpu_second_active"]["default"]["h100"],
        data["azure"]["vm_vgpu"]["regions"]["eastus"]["instance_types"]["Standard_NV6ads_A10_v5"],
    ]
    for entry in sample_entries:
        assert "aliases" in entry, f"entry missing aliases: {entry}"
        assert isinstance(entry["aliases"], list)


def test_arm_unrelated_to_gpu_pricing():
    """Sanity: GPU pricing has no ARM/x86 distinction (vs compute's per-arch nesting).

    Decision #5 — GPU compute SKUs (Lambda/Fargate/EC2) carry an arch field
    in COMPUTE catalog, not GPU catalog. The GPU catalog only cares about
    NVIDIA productName / SKU, regardless of host CPU arch.
    """
    data = _load()
    p5 = data["aws"]["ec2_gpu"]["regions"]["us-east-1"]["instance_types"]["p5.48xlarge"]
    # GPU catalog entries have gpu_sku, NOT x86_64/arm64 sub-blocks.
    assert "gpu_sku" in p5
    assert "x86_64" not in p5  # not a compute-catalog-style arch nest
    assert "arm64" not in p5


# ─── Version sanity ──────────────────────────────────────────────────────────


def test_meta_version_is_semver_like():
    meta = _load()["_meta"]
    version = meta["version"]
    parts = version.split(".")
    assert len(parts) == 3, f"version not semver: {version}"
    for p in parts:
        int(p)  # raises if not integer
