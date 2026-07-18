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
        observed = observers.observe(case["url"], case["headers"], case["response"])
        expected: dict[str, Any] | None = case["expected"]
        if expected is None:
            assert observed is None, case["name"]
            continue
        assert observed is not None, case["name"]
        assert observed.service_key == expected["service_key"]
        assert observed.provider_name == expected["provider_name"]
        assert observed.provider_service == expected["provider_service"]
        assert observed.component == expected["component"]
        assert observed.metric == expected["metric"]
        assert str(observed.quantity) == expected["quantity"]
        assert observed.resource_id == expected.get("resource_id")
        assert observed.provider_record_id == expected.get("provider_record_id")


def test_packaged_observer_manifest_matches_canonical_manifest() -> None:
    canonical = json.loads((ROOT / "fixtures" / "service_usage_observers.json").read_text())
    packaged = json.loads(
        (ROOT / "python" / "src" / "dexcost" / "data" / "service_usage_observers.json").read_text()
    )
    assert packaged == canonical
