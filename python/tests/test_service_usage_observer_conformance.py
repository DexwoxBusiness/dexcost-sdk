from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dexcost.adapters.http import (
    _persist_event,
    _provider_observation_event_id,
    clear_recorded_events,
    get_recorded_events,
)
from dexcost.models.event import Event
from dexcost.service_usage_observers import ServiceUsageObservers

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UNITS = {
    "input_tokens": "Tokens", "input_image_tokens": "Tokens",
    "output_image_tokens": "Tokens", "output_tokens": "Tokens",
    "audio_seconds": "Seconds", "characters": "Characters", "image_count": "Images",
    "request_count": "Requests", "credit_count": "Credits",
}


def test_provider_observation_id_is_stable_across_sdk_languages() -> None:
    observation = SimpleNamespace(
        provider_name="assemblyai",
        service_key="assemblyai_transcription",
        provider_record_id="aa-123",
    )
    event_id = _provider_observation_event_id(observation)
    assert str(event_id) == "2dc521b3-742a-5f61-9942-c4a59e6935f6"


def test_repeated_provider_observation_identity_is_recorded_once() -> None:
    clear_recorded_events()
    event_id = uuid.UUID("2dc521b3-742a-5f61-9942-c4a59e6935f6")
    _persist_event(Event(event_id=event_id, event_type="external_cost"))
    _persist_event(Event(event_id=event_id, event_type="external_cost"))
    assert len(get_recorded_events()) == 1
    clear_recorded_events()


def test_shared_service_usage_observer_conformance() -> None:
    fixture = json.loads(
        (ROOT / "fixtures" / "service_usage_observation_conformance.json").read_text()
    )
    observers = ServiceUsageObservers()
    for case in fixture["cases"]:
        status_code = int(case.get("status_code", 200))
        observed = (
            observers.observe(
                case["url"],
                case["headers"],
                case["response"],
                case.get("request"),
                case.get("request_headers", []),
                case.get("method"),
            )
            if 200 <= status_code < 300
            else []
        )
        expected: list[dict[str, Any]] = case["expected"]
        assert len(observed) == len(expected), case["name"]
        for actual, wanted in zip(observed, expected, strict=True):
            assert actual.service_key == wanted["service_key"]
            assert actual.provider_name == wanted["provider_name"]
            assert actual.provider_service == wanted["provider_service"]
            assert actual.component == wanted["component"]
            assert actual.metric == wanted["metric"]
            assert actual.unit == (
                wanted.get("unit") or DEFAULT_UNITS[wanted["metric"]]
            )
            assert str(actual.quantity) == wanted["quantity"]
            assert actual.resource_type == wanted.get("resource_type")
            assert actual.resource_id == wanted.get("resource_id")
            assert actual.provider_record_id == wanted.get("provider_record_id")
            assert actual.provider_region == wanted.get("provider_region")
            assert (
                str(actual.provider_cost_usd)
                if actual.provider_cost_usd is not None
                else None
            ) == wanted.get("provider_cost_usd")
            assert (
                str(actual.provider_cost_amount)
                if actual.provider_cost_amount is not None
                else None
            ) == wanted.get("provider_cost_amount")
            assert actual.provider_cost_currency == wanted.get("provider_cost_currency")
            assert list(actual.dimensions) == wanted.get("dimensions", [])


def test_packaged_observer_manifest_matches_canonical_manifest() -> None:
    canonical = json.loads((ROOT / "fixtures" / "service_usage_observers.json").read_text())
    packaged = json.loads(
        (ROOT / "python" / "src" / "dexcost" / "data" / "service_usage_observers.json").read_text()
    )
    assert packaged == canonical


def _numeric_response_observer(predicate: dict[str, Any]) -> ServiceUsageObservers:
    return ServiceUsageObservers(
        data={
            "_meta": {"version": "test", "observer_count": 1},
            "observers": [
                {
                    "service_key": "numeric_response_test",
                    "provider_name": "test_provider",
                    "provider_service": "test_service",
                    "component": "external",
                    "domains": ["numeric.example"],
                    "endpoints": ["/v1/check"],
                    "endpoint_match": "exact",
                    "fixed_quantity": "1",
                    "usage_metric": "request_count",
                    "response_all": [predicate],
                    "source_url": "https://numeric.example/docs",
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("predicate", "response"),
    [
        ({"path": "status", "operator": "equals", "value": 1}, {"status": 1.0}),
        ({"path": "status", "operator": "one_of", "values": [1]}, {"status": 1.0}),
        (
            {"path": "statuses[]", "operator": "collection_all_equals", "value": 1},
            {"statuses": [1.0, 1]},
        ),
    ],
)
def test_response_predicates_use_json_number_semantics(
    predicate: dict[str, Any], response: dict[str, Any]
) -> None:
    observer = _numeric_response_observer(predicate)
    assert len(observer.observe("https://numeric.example/v1/check", {}, response)) == 1

    boolean_path = "statuses" if predicate["operator"] == "collection_all_equals" else "status"
    boolean_response = {boolean_path: [True] if boolean_path == "statuses" else True}
    assert observer.observe("https://numeric.example/v1/check", {}, boolean_response) == []


def test_response_one_of_rejects_duplicate_json_numbers() -> None:
    with pytest.raises(ValueError, match="invalid response predicate"):
        _numeric_response_observer(
            {"path": "status", "operator": "one_of", "values": [1, 1.0]}
        )


def test_response_predicates_fail_open_for_unsafe_json_integers() -> None:
    maximum_safe_integer = 9_007_199_254_740_991
    observer = _numeric_response_observer(
        {"path": "status", "operator": "equals", "value": maximum_safe_integer}
    )
    assert len(
        observer.observe(
            "https://numeric.example/v1/check", {}, {"status": maximum_safe_integer}
        )
    ) == 1
    assert observer.observe(
        "https://numeric.example/v1/check", {}, {"status": maximum_safe_integer + 1}
    ) == []

    with pytest.raises(ValueError, match="invalid response predicate"):
        _numeric_response_observer(
            {
                "path": "status",
                "operator": "equals",
                "value": maximum_safe_integer + 1,
            }
        )


def test_azure_observer_owns_invalid_variants_without_trusting_spoofed_suffixes() -> None:
    observers = ServiceUsageObservers()
    custom_category = (
        "https://api.cognitive.microsofttranslator.com/translate?"
        "api-version=3.0&to=es&category=customer-model"
    )
    spoofed = (
        "https://resource.cognitiveservices.azure.com.evil.example/"
        "translator/text/v3.0/translate?api-version=3.0&to=es"
    )

    assert not observers.matches(custom_category)
    assert observers.owns_endpoint_boundary(custom_category)
    assert not observers.matches(spoofed)
    assert not observers.owns_endpoint_boundary(spoofed)


def test_paired_batch_observers_capture_request_and_response_bodies() -> None:
    observers = ServiceUsageObservers()
    url = "https://vision.googleapis.com/v1/images:annotate"
    assert observers.matches(url)
    assert observers.needs_request_body(url)
    assert observers.needs_response_body(url)
    assert observers.owns_endpoint_boundary(f"{url}/preview")
    assert not observers.matches(f"{url}/preview")
