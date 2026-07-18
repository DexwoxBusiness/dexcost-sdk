"""Shared attribution-v2 corpus and durable-capture conversion coverage."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dexcost.attribution import (
    ATTRIBUTION_V2_CONTRACT_VERSION,
    to_attribution_event_v2,
    validate_attribution_event_v2,
)
from dexcost.models import Event

_CORPUS_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "attribution_v2" / "conformance.json"
)
_CORPUS: dict[str, Any] = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def _event(**overrides: Any) -> Event:
    values: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "occurred_at": datetime(2026, 7, 16, 10, 0, 0, 123000, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return Event(**values)


def test_contract_version_matches_shared_corpus() -> None:
    assert _CORPUS["contract_version"] == ATTRIBUTION_V2_CONTRACT_VERSION


@pytest.mark.parametrize("case", _CORPUS["valid"], ids=lambda case: case["name"])
def test_accepts_shared_valid_corpus(case: dict[str, Any]) -> None:
    result = validate_attribution_event_v2(case["event"])
    assert result.success, result.issues


@pytest.mark.parametrize("case", _CORPUS["invalid"], ids=lambda case: case["name"])
def test_rejects_shared_invalid_corpus(case: dict[str, Any]) -> None:
    result = validate_attribution_event_v2(case["event"])
    assert not result.success
    assert case["expected_error_path"] in {issue.path for issue in result.issues}


@pytest.mark.parametrize("occurred_at", ["2026-02-29T10:00:00Z", "2026-04-31T10:00:00Z"])
def test_rejects_impossible_calendar_dates(occurred_at: str) -> None:
    event = copy.deepcopy(_CORPUS["valid"][0]["event"])
    event["occurred_at"] = occurred_at
    result = validate_attribution_event_v2(event)
    assert not result.success
    assert "occurred_at" in {issue.path for issue in result.issues}


def test_keeps_anthropic_cache_buckets_disjoint_and_catalog_evidence() -> None:
    converted = to_attribution_event_v2(
        _event(
            event_type="llm_call",
            provider="anthropic",
            model="claude-sonnet-4-5",
            input_tokens=100,
            cached_tokens=1000,
            output_tokens=50,
            cost_usd=Decimal("0.00135"),
            cost_confidence="exact",
            pricing_source="service_catalog",
            pricing_version="llm:2026-07-16",
            details={"cache_creation_input_tokens": 25},
        )
    )
    assert converted is not None
    assert converted["usage"] == [
        {"metric": "input_tokens", "quantity": "100", "unit": "Tokens"},
        {"metric": "cache_read_input_tokens", "quantity": "1000", "unit": "Tokens"},
        {"metric": "cache_write_input_tokens", "quantity": "25", "unit": "Tokens"},
        {"metric": "output_tokens", "quantity": "50", "unit": "Tokens"},
    ]
    assert converted["cost_evidence"]["source"] == "sdk_catalog"
    assert converted["cost_evidence"]["confidence"] == "computed"


def test_subtracts_openai_cached_tokens_from_inclusive_input() -> None:
    converted = to_attribution_event_v2(
        _event(
            event_type="llm_call",
            provider="openai",
            input_tokens=1200,
            cached_tokens=1000,
            output_tokens=50,
        )
    )
    assert converted is not None
    assert converted["usage"][:2] == [
        {"metric": "input_tokens", "quantity": "200", "unit": "Tokens"},
        {"metric": "cache_read_input_tokens", "quantity": "1000", "unit": "Tokens"},
    ]


def test_retains_user_catalog_override_as_manual_evidence() -> None:
    converted = to_attribution_event_v2(
        _event(
            event_type="external_cost",
            cost_usd=Decimal("0.05"),
            cost_confidence="computed",
            pricing_source="user_override",
            service_name="search",
        )
    )
    assert converted is not None
    assert converted["cost_evidence"]["source"] == "manual"
    assert converted["cost_evidence"]["amount"] == "0.05"


def test_promotes_compute_quantities_and_closes_usage_period() -> None:
    converted = to_attribution_event_v2(
        _event(
            event_type="compute_cost",
            cost_confidence="computed",
            details={
                "billing_model": "lambda",
                "duration_ms": 2500,
                "memory_bytes_limit": 2 * 1024**3,
                "vcpu_seconds_used": 2.5,
                "invocation_count": 1,
                "region": "us-east-1",
            },
        )
    )
    assert converted is not None
    assert converted["component"] == "compute"
    assert {
        "metric": "memory_gib_seconds",
        "quantity": "5",
        "unit": "GiB-Seconds",
    } in converted["usage"]
    assert converted["usage_period"]["end_at"] == converted["occurred_at"]


@pytest.mark.parametrize(
    ("event_type", "pricing_source", "pricing_version", "details"),
    [
        (
            "compute_cost",
            "compute_catalog:aws:lambda:us-east-1:x86_64",
            "compute:1.0.0",
            {"billing_model": "lambda", "duration_ms": 1000, "invocation_count": 1},
        ),
        (
            "gpu_cost",
            "gpu_catalog:runpod:per_gpu_second_active:a100",
            "gpu:1.0.0",
            {"billing_model": "per_gpu_second_active", "gpu_seconds_used": 1, "duration_ms": 1000},
        ),
        (
            "network",
            "egress_catalog:aws:us-east-1",
            "egress:1.0.0",
            {"request_bytes": 1000},
        ),
    ],
)
def test_preserves_versioned_infrastructure_catalog_evidence(
    event_type: str, pricing_source: str, pricing_version: str, details: dict[str, Any]
) -> None:
    converted = to_attribution_event_v2(
        _event(
            event_type=event_type,
            pricing_source=pricing_source,
            pricing_version=pricing_version,
            cost_usd=Decimal("0.09"),
            cost_confidence="exact",
            details=details,
        )
    )
    assert converted is not None
    assert converted["cost_evidence"] == {
        "amount": "0.09",
        "currency": "USD",
        "source": "sdk_catalog",
        "confidence": "computed",
        "pricing_version": pricing_version,
    }


@pytest.mark.parametrize(
    ("event_type", "details", "metric"),
    [
        ("compute_cost", {"billing_model": "ec2", "vcpu_seconds_used": 2.5}, "vcpu_seconds"),
        (
            "gpu_cost",
            {"billing_model": "per_gpu_second_active", "gpu_seconds_used": 2.5},
            "gpu_seconds",
        ),
    ],
)
def test_keeps_active_time_usage_without_wall_duration(
    event_type: str, details: dict[str, Any], metric: str
) -> None:
    converted = to_attribution_event_v2(_event(event_type=event_type, details=details))
    assert converted is not None
    assert metric in {line["metric"] for line in converted["usage"]}
    assert converted["usage_period"] == {
        "start_at": converted["occurred_at"],
        "end_at": converted["occurred_at"],
    }


def test_keeps_network_directions_separate() -> None:
    converted = to_attribution_event_v2(
        _event(
            event_type="network",
            service_name="api.example.com",
            details={"request_bytes": 123, "response_bytes": 456},
        )
    )
    assert converted is not None
    assert converted["usage"] == [
        {"metric": "bytes_out", "quantity": "123", "unit": "Bytes"},
        {"metric": "bytes_in", "quantity": "456", "unit": "Bytes"},
    ]


def test_preserves_rate_registry_quantity_and_unit_semantics() -> None:
    converted = to_attribution_event_v2(
        _event(
            event_type="external_cost",
            service_name="ocr-api.com",
            cost_usd=Decimal("0.03"),
            cost_confidence="computed",
            pricing_source="rate_registry",
            pricing_version="rates:example",
            details={
                "attribution_usage_quantity": 3,
                "attribution_usage_per": "page",
            },
        )
    )
    assert converted is not None
    assert converted["usage"] == [{"metric": "page_count", "quantity": "3", "unit": "Pages"}]
    assert converted["cost_evidence"] == {
        "amount": "0.03",
        "currency": "USD",
        "source": "sdk_rate_registry",
        "confidence": "computed",
        "pricing_version": "rates:example",
    }


def test_preserves_browser_rate_per_minute_cost_evidence() -> None:
    converted = to_attribution_event_v2(
        _event(
            event_type="compute_cost",
            service_name="playwright_browser",
            cost_usd=Decimal("0.02"),
            cost_confidence="computed",
            pricing_source="rate_per_minute",
            details={"wall_clock_seconds": 120, "rate_per_minute": "0.01"},
        )
    )
    assert converted is not None
    assert converted["usage"] == [
        {"metric": "compute_seconds", "quantity": "120", "unit": "Seconds"}
    ]
    assert converted["cost_evidence"] == {
        "amount": "0.02",
        "currency": "USD",
        "source": "manual",
        "confidence": "computed",
    }


def test_preserves_retry_linkage_reason_usage_and_cost() -> None:
    retry_of = uuid.uuid4()
    converted = to_attribution_event_v2(
        _event(
            event_type="retry_marker",
            is_retry=True,
            retry_reason="rate_limit",
            retry_of=retry_of,
            cost_usd=Decimal("0.0042"),
        )
    )
    assert converted is not None
    assert converted["component"] == "external"
    assert converted["provider"] == {"name": "dexcost", "service": "retry"}
    assert converted["resource"] == {"type": "other", "id": "rate_limit"}
    assert converted["usage"] == [
        {"metric": "request_count", "quantity": "1", "unit": "Requests"}
    ]
    assert converted["retry_of"] == str(retry_of)
    assert converted["cost_evidence"] == {
        "amount": "0.0042",
        "currency": "USD",
        "source": "manual",
        "confidence": "exact",
    }


def test_drops_observability_only_events() -> None:
    assert to_attribution_event_v2(_event(event_type="gpu_utilization_signal")) is None


def test_drops_unknown_event_types_instead_of_misattributing_external_cost() -> None:
    assert to_attribution_event_v2(_event(event_type="future_internal_signal")) is None


def test_conversion_is_stable_and_never_transmits_details() -> None:
    internal = _event(
        event_type="external_cost",
        service_name="tavily",
        details={"secret": "must-not-leave-process"},
    )
    first = to_attribution_event_v2(internal)
    second = to_attribution_event_v2(internal)
    assert first == second
    assert first is not None
    assert first["observed_at"] == first["occurred_at"]
    assert "details" not in first
