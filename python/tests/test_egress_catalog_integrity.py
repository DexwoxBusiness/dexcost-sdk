"""Catalog integrity — structure, Decimal parsing, freshness."""

import datetime as _dt
import importlib.resources as ir
import json
import warnings
from decimal import Decimal


def _load():
    raw = ir.files("dexcost").joinpath("data").joinpath("egress_prices.json").read_text()
    return json.loads(raw), raw


def test_catalog_parses_as_json():
    data, _ = _load()
    assert "_meta" in data


def test_meta_has_required_keys():
    data, _ = _load()
    meta = data["_meta"]
    for k in ("version", "last_updated", "currency",
              "default_rate_usd_per_gb", "description", "notes"):
        assert k in meta, f"_meta missing {k}"
    assert meta["currency"] == "USD"
    Decimal(meta["default_rate_usd_per_gb"])


def test_every_rate_is_decimal_parseable():
    data, _ = _load()
    for provider, block in data.items():
        if provider == "_meta":
            continue
        Decimal(block["default_usd_per_gb"])
        for region, rate in block["regions"].items():
            try:
                Decimal(rate)
            except Exception as e:  # noqa: BLE001
                raise AssertionError(f"{provider}.{region} not Decimal: {rate}") from e


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
                f"egress_prices.json: {provider} _last_verified is "
                f"{(today - verified).days} days old (soft limit 180)",
                stacklevel=2,
            )


def test_aws_gcp_azure_present():
    data, _ = _load()
    for p in ("aws", "gcp", "azure"):
        assert p in data, f"missing provider block: {p}"


def test_known_anchor_regions_have_rates():
    data, _ = _load()
    assert data["aws"]["regions"].get("us-east-1") is not None
    assert data["gcp"]["regions"].get("us-central1") is not None
    assert data["azure"]["regions"].get("eastus") is not None
