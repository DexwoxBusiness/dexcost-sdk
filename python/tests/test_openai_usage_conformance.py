"""Shared OpenAI Chat/Responses token-bucket conformance."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from dexcost.instruments.openai_usage import OpenAIUsageError, normalize_openai_usage

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "openai_usage_conformance.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text())


@pytest.mark.parametrize("case", _FIXTURE["valid_cases"], ids=lambda case: case["id"])
def test_valid_openai_usage(case: dict[str, object]) -> None:
    assert asdict(normalize_openai_usage(case["usage"])) == case["expected"]


@pytest.mark.parametrize("case", _FIXTURE["invalid_cases"], ids=lambda case: case["id"])
def test_invalid_openai_usage(case: dict[str, object]) -> None:
    with pytest.raises(OpenAIUsageError, match=f"^{case['expected_error']}$"):
        normalize_openai_usage(case["usage"])


def test_pydantic_v2_provider_extras_remain_billable() -> None:
    class ProviderModel:
        def __init__(self, fields: dict[str, object], extras: dict[str, object]) -> None:
            self.__dict__.update(fields)
            self.__pydantic_extra__ = extras

    usage = ProviderModel(
        {
            "input_tokens": 40,
            "output_tokens": 12,
            "input_tokens_details": ProviderModel(
                {"cached_tokens": 10},
                {"cache_creation_input_tokens": 5},
            ),
            "output_tokens_details": ProviderModel({"reasoning_tokens": 2}, {}),
        },
        {},
    )

    assert asdict(normalize_openai_usage(usage)) == {
        "total_input_tokens": 40,
        "input_tokens": 25,
        "cache_read_input_tokens": 10,
        "cache_write_input_tokens": 5,
        "total_output_tokens": 12,
        "output_tokens": 10,
        "reasoning_output_tokens": 2,
    }


def test_deepseek_top_level_cache_hit_tokens_are_disjoint() -> None:
    assert asdict(normalize_openai_usage({
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "prompt_cache_hit_tokens": 4,
        "prompt_cache_miss_tokens": 16,
    })) == {
        "total_input_tokens": 20,
        "input_tokens": 16,
        "cache_read_input_tokens": 4,
        "cache_write_input_tokens": 0,
        "total_output_tokens": 10,
        "output_tokens": 10,
        "reasoning_output_tokens": 0,
    }
