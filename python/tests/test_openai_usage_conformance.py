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
