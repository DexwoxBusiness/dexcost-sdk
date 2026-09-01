"""Shared xAI alias, lane, and exact-cost conformance."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from dexcost.instruments.openai import (
    _provider_model,
    _xai_pricing_lane,
    _xai_provider_cost,
)

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "xai_pricing_conformance.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text())


@pytest.mark.parametrize(
    ("reported", "expected"),
    _FIXTURE["model_cases"].items(),
    ids=_FIXTURE["model_cases"].keys(),
)
def test_xai_model_aliases(reported: str, expected: str) -> None:
    assert _provider_model(reported, "xai") == expected


@pytest.mark.parametrize("case", _FIXTURE["lane_cases"], ids=lambda case: case["id"])
def test_xai_pricing_lanes(case: dict[str, object]) -> None:
    assert (
        _xai_pricing_lane(
            case["usage"],
            int(case["total_input_tokens"]),
            case.get("service_tier"),
            "xai",
        )
        == case["expected"]
    )


@pytest.mark.parametrize("case", _FIXTURE["tick_cases"])
def test_xai_usd_ticks(case: dict[str, object]) -> None:
    amount = _xai_provider_cost({"cost_in_usd_ticks": case["ticks"]}, "xai")
    expected = case["expected_usd"]
    assert amount == (None if expected is None else Decimal(str(expected)))


def test_xai_helpers_are_provider_scoped() -> None:
    assert _provider_model("grok-4.3-latest", "openai") == "grok-4.3-latest"
    assert _xai_pricing_lane({}, 10, None, "openai") is None
    assert _xai_provider_cost({"cost_in_usd_ticks": 10_000_000_000}, "openai") is None
