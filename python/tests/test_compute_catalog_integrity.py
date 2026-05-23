"""Compute catalog integrity — structure, Decimal parsing, freshness, dispatch coverage."""

from __future__ import annotations

import datetime as _dt
import importlib.resources as ir
import json
import warnings
from decimal import Decimal


def _load() -> tuple[dict, str]:
    raw = (
        ir.files("dexcost")
        .joinpath("data")
        .joinpath("compute_prices.json")
        .read_text()
    )
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
        "default_azure_functions_execution_usd",
        "default_azure_functions_gb_second_usd",
        "default_vercel_cpu_hour_usd", "default_vercel_memory_gb_hour_usd",
        "default_ec2_vcpu_hour_usd", "default_k8s_pod_vcpu_hour_usd",
        "description", "notes",
    ]
    for k in required:
        assert k in meta, f"_meta missing {k}"
        if k.startswith("default_") and k.endswith("_usd"):
            Decimal(meta[k])  # raises if not a parseable Decimal
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
    aws_runtimes = set(data["aws"].keys()) - {"_last_verified"}
    assert {"lambda", "fargate", "ec2"} <= aws_runtimes
    gcp_runtimes = set(data["gcp"].keys()) - {"_last_verified"}
    assert {"cloud_run", "cloud_functions", "gce"} <= gcp_runtimes
    azure_runtimes = set(data["azure"].keys()) - {"_last_verified"}
    assert {"functions_consumption", "vm"} <= azure_runtimes
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
    for arch in ("x86_64", "arm64"):
        Decimal(default[arch]["vcpu_second_usd"])
        Decimal(default[arch]["gib_second_usd"])


def test_arm_cheaper_than_x86_on_lambda():
    """ARM is ~20% cheaper than x86 on Lambda per AWS pricing — guards
    against an arch-keying regression that silently bills ARM at x86 rates."""
    data, _ = _load()
    region = next(iter(data["aws"]["lambda"]["regions"].values()))
    arm = Decimal(region["arm64"]["gb_second_usd"])
    x86 = Decimal(region["x86_64"]["gb_second_usd"])
    assert arm < x86, "arm64 must be cheaper than x86_64 on Lambda"


def test_arm_cheaper_than_x86_on_fargate():
    data, _ = _load()
    region = next(iter(data["aws"]["fargate"]["regions"].values()))
    arm = Decimal(region["arm64"]["vcpu_second_usd"])
    x86 = Decimal(region["x86_64"]["vcpu_second_usd"])
    assert arm < x86, "arm64 must be cheaper than x86_64 on Fargate"


def test_top_instance_types_present_for_ec2_us_east_1():
    data, _ = _load()
    instance_types = (
        data["aws"]["ec2"]["regions"]["us-east-1"]["instance_types"]
    )
    for must_have in ("c7g.xlarge", "m7i.large", "t3.medium"):
        assert must_have in instance_types, f"missing EC2 SKU: {must_have}"
        Decimal(instance_types[must_have]["hourly_usd"])
        Decimal(instance_types[must_have]["vcpu_count"])


def test_top_instance_types_present_for_gce_us_central1():
    data, _ = _load()
    instance_types = (
        data["gcp"]["gce"]["regions"]["us-central1"]["instance_types"]
    )
    for must_have in ("n2-standard-2", "e2-standard-4"):
        assert must_have in instance_types, f"missing GCE SKU: {must_have}"


def test_top_instance_types_present_for_azure_vm_eastus():
    data, _ = _load()
    instance_types = (
        data["azure"]["vm"]["regions"]["eastus"]["instance_types"]
    )
    for must_have in ("Standard_D2s_v3", "Standard_B2ms"):
        assert must_have in instance_types, f"missing Azure VM SKU: {must_have}"


def test_every_rate_is_decimal_parseable():
    """Walk the entire catalog and assert every USD field is Decimal-clean."""
    data, _ = _load()

    def _walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{path}.{k}")
        elif isinstance(node, str):
            # Heuristic: paths ending in _usd or vcpu_count must parse as Decimal.
            if path.endswith("_usd") or path.endswith("vcpu_count"):
                try:
                    Decimal(node)
                except Exception as e:  # noqa: BLE001
                    raise AssertionError(
                        f"{path} not Decimal-parseable: {node!r}"
                    ) from e

    _walk(data, "")


def test_every_dispatch_billing_model_has_a_rate_path():
    """Each billing_model in the §5 dispatch table must reach a rate path —
    either a per-runtime regions/default block, or a _meta default."""
    data, _ = _load()
    meta = data["_meta"]
    # lambda
    assert "default_lambda_request_usd" in meta
    # fargate
    assert "default_fargate_vcpu_second_usd" in meta
    # cloud_run_request, cloud_run_instance, cloud_functions
    assert "default_cloud_run_request_usd" in meta
    # azure_functions
    assert "default_azure_functions_execution_usd" in meta
    # vercel_fluid
    assert "default_vercel_cpu_hour_usd" in meta
    # ec2, gce, azure_vm (per-vcpu-hour share path)
    assert "default_ec2_vcpu_hour_usd" in meta
    # k8s_pod
    assert "default_k8s_pod_vcpu_hour_usd" in meta
