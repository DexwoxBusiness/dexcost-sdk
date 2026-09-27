from __future__ import annotations

import json
from pathlib import Path

import pytest

from dexcost.instruments.openai import (
    _mistral_pricing_lane,
    _provider_model,
)

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = json.loads((_ROOT / "fixtures" / "mistral_pricing_conformance.json").read_text())


@pytest.mark.parametrize(
    ("reported", "expected"),
    _FIXTURE["model_cases"].items(),
)
def test_mistral_model_canonicalization(reported: str, expected: str) -> None:
    assert _provider_model(reported, "mistral") == expected


@pytest.mark.parametrize("case", _FIXTURE["lane_cases"], ids=lambda case: case["id"])
def test_mistral_pricing_lanes(case: dict[str, object]) -> None:
    usage = {"service_tier": case["service_tier"]}
    assert _mistral_pricing_lane(usage, "mistral", "chat_completions") == case["expected"]


@pytest.mark.parametrize("case", _FIXTURE["surface_cases"], ids=lambda case: case["id"])
def test_mistral_pricing_surfaces(case: dict[str, object]) -> None:
    usage = {"service_tier": "standard"}
    assert _mistral_pricing_lane(usage, "mistral", str(case["surface"])) == case["expected"]


def test_mistral_helper_is_provider_scoped() -> None:
    assert (
        _mistral_pricing_lane(
            {"service_tier": "standard"},
            "openai",
            "chat_completions",
        )
        is None
    )


def test_mistral_sdk_maps_retain_no_direct_provider_money() -> None:
    python_map = json.loads(
        (_ROOT / "python" / "src" / "dexcost" / "data" / "model_cost_map.json").read_text()
    )
    typescript_map = json.loads(
        (_ROOT / "typescript" / "src" / "pricing" / "cost_map.json").read_text()
    )
    assert python_map == typescript_map
    for model in _FIXTURE["model_cases"].values():
        assert model not in python_map
