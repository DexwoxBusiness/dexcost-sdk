from __future__ import annotations

import json
from pathlib import Path

import pytest

from dexcost.instruments.openai import _groq_pricing_lane

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = json.loads((_ROOT / "fixtures" / "groq_pricing_conformance.json").read_text())


@pytest.mark.parametrize("case", _FIXTURE["lane_cases"], ids=lambda case: case["id"])
def test_groq_pricing_lanes(case: dict[str, object]) -> None:
    response = {
        "service_tier": case["service_tier"],
        "choices": [{"message": {"executed_tools": case["executed_tools"]}}],
    }
    assert _groq_pricing_lane(response, None, "groq") == case["expected"]


def test_groq_helper_is_provider_scoped() -> None:
    assert _groq_pricing_lane({}, None, "openai") is None


def test_groq_sdk_maps_retain_metadata_but_no_money() -> None:
    python_map = json.loads(
        (_ROOT / "python" / "src" / "dexcost" / "data" / "model_cost_map.json").read_text()
    )
    typescript_map = json.loads(
        (_ROOT / "typescript" / "src" / "pricing" / "cost_map.json").read_text()
    )
    assert python_map == typescript_map
    groq_entries = {key: value for key, value in python_map.items() if key.startswith("groq/")}
    assert groq_entries
    for model, metadata in groq_entries.items():
        money_fields = [key for key in metadata if "cost" in key or "price" in key]
        assert money_fields == [], f"{model} retains SDK money fields: {money_fields}"
