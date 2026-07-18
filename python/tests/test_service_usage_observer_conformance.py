from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dexcost.service_usage_observers import ServiceUsageObservers

ROOT = Path(__file__).resolve().parents[2]


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
