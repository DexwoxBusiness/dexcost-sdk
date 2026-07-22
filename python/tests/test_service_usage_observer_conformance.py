from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dexcost.adapters.http import (
    _persist_event,
    _provider_observation_event_id,
    clear_recorded_events,
    get_recorded_events,
)
from dexcost.models.event import Event
from dexcost.service_usage_observers import ServiceUsageObservers

ROOT = Path(__file__).resolve().parents[2]


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
        observed = observers.observe(
            case["url"], case["headers"], case["response"], case.get("request")
        )
        expected: list[dict[str, Any]] = case["expected"]
        assert len(observed) == len(expected), case["name"]
        for actual, wanted in zip(observed, expected, strict=True):
            assert actual.service_key == wanted["service_key"]
            assert actual.provider_name == wanted["provider_name"]
            assert actual.provider_service == wanted["provider_service"]
            assert actual.component == wanted["component"]
            assert actual.metric == wanted["metric"]
            assert str(actual.quantity) == wanted["quantity"]
            assert actual.resource_type == wanted.get("resource_type")
            assert actual.resource_id == wanted.get("resource_id")
            assert actual.provider_record_id == wanted.get("provider_record_id")


def test_packaged_observer_manifest_matches_canonical_manifest() -> None:
    canonical = json.loads((ROOT / "fixtures" / "service_usage_observers.json").read_text())
    packaged = json.loads(
        (ROOT / "python" / "src" / "dexcost" / "data" / "service_usage_observers.json").read_text()
    )
    assert packaged == canonical
