"""Egress pricing resolver — every tier of the §7.1 ladder."""

import json
from decimal import Decimal

import pytest

from dexcost.egress_pricing import EgressPricingEngine, _reset_warning_state


@pytest.fixture
def engine():
    return EgressPricingEngine()


@pytest.fixture(autouse=True)
def _clean_warnings():
    _reset_warning_state()
    yield
    _reset_warning_state()


def test_tier1_region_match_is_computed(engine):
    r = engine.resolve_rate("aws", "us-east-1")
    assert r.rate_per_gb == Decimal("0.09")
    assert r.pricing_source == "egress_catalog:aws:us-east-1"
    assert r.cost_confidence == "computed"


def test_tier2_provider_known_region_missing_is_estimated(engine):
    r = engine.resolve_rate("aws", "moon-base-1")
    assert r.rate_per_gb == Decimal("0.09")
    assert r.pricing_source == "egress_catalog:aws:default"
    assert r.cost_confidence == "estimated"


def test_tier3_unknown_provider_falls_to_meta_default(engine):
    r = engine.resolve_rate(None, None)
    assert r.rate_per_gb == Decimal("0.09")
    assert r.pricing_source == "egress_catalog:default"
    assert r.cost_confidence == "estimated"


def test_internal_traffic_is_free_and_exact(engine):
    r = engine.rate_for_internal()
    assert r.rate_per_gb == Decimal("0")
    assert r.pricing_source == "egress_catalog:internal"
    assert r.cost_confidence == "exact"


def test_tier4_missing_catalog_falls_to_hardcoded(tmp_path):
    bogus = tmp_path / "no.json"
    eng = EgressPricingEngine(catalog_path=bogus)
    r = eng.resolve_rate("aws", "us-east-1")
    assert r.rate_per_gb == Decimal("0.09")
    assert r.cost_confidence == "estimated"


def test_tier4_malformed_catalog_falls_to_hardcoded(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    eng = EgressPricingEngine(catalog_path=bad)
    r = eng.resolve_rate("aws", "us-east-1")
    assert r.rate_per_gb == Decimal("0.09")
    assert r.cost_confidence == "estimated"


def test_tier4_meta_default_missing_falls_to_hardcoded(tmp_path):
    bad = tmp_path / "no_meta_default.json"
    bad.write_text(json.dumps({"_meta": {"version": "x", "currency": "USD"}}))
    eng = EgressPricingEngine(catalog_path=bad)
    r = eng.resolve_rate(None, None)
    assert r.rate_per_gb == Decimal("0.09")
    assert r.cost_confidence == "estimated"


def test_warn_once_per_failure_mode(tmp_path, caplog):
    import logging
    bogus = tmp_path / "missing.json"
    with caplog.at_level(logging.WARNING, logger="dexcost.egress_pricing"):
        EgressPricingEngine(catalog_path=bogus)
        EgressPricingEngine(catalog_path=bogus)
    msgs = [r for r in caplog.records if "catalog" in r.getMessage().lower()]
    assert len(msgs) == 1


def test_warn_distinct_modes_independently(tmp_path, caplog):
    import logging
    missing = tmp_path / "missing.json"
    malformed = tmp_path / "bad.json"
    malformed.write_text("{")
    with caplog.at_level(logging.WARNING, logger="dexcost.egress_pricing"):
        EgressPricingEngine(catalog_path=missing)
        EgressPricingEngine(catalog_path=malformed)
    msgs = [r.getMessage().lower() for r in caplog.records]
    assert any("not found" in m for m in msgs)
    assert any("malformed" in m for m in msgs)


def test_decimal_no_float_drift():
    assert Decimal("0.1093") * Decimal("1000000000") == Decimal("109300000.0000")
    assert Decimal("0.087") * Decimal("12345678") == Decimal("1074073.986")


def test_pricing_version_from_meta(engine):
    assert engine.catalog_version == "1.0.0"


def test_egress_rate_is_immutable(engine):
    r = engine.resolve_rate("aws", "us-east-1")
    with pytest.raises(Exception):  # frozen dataclass
        r.rate_per_gb = Decimal("99")  # type: ignore[misc]
