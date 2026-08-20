"""Shared attribution-v3 corpus and durable Python conversion conformance."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.attribution.v3_types import ATTRIBUTION_V3_CONTRACT_VERSION
from dexcost.attribution.v3_validate import validate_attribution_observation_v3
from dexcost.models.event import Event
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_PATH = _REPO_ROOT / "fixtures" / "attribution_v3" / "conformance.json"
_SCHEMA_PATH = _REPO_ROOT / "fixtures" / "attribution_v3" / "schemas.json"
_CORPUS = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
_VALID_BY_ID = {case["id"]: case["event"] for case in _CORPUS["valid_observations"]}
_LOCAL_GPU = json.loads(
    (_REPO_ROOT / "fixtures" / "attribution_v3" / "local_gpu_usage.json").read_text(
        encoding="utf-8"
    )
)


def _parent_and_key(
    target: dict[str, Any], path: str
) -> tuple[dict[str, Any] | list[Any], str | int]:
    parts = path.split(".")
    parent: dict[str, Any] | list[Any] = target
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key: str | int = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    return parent, key


def _materialize(test_case: dict[str, Any]) -> dict[str, Any]:
    if "event" in test_case:
        return copy.deepcopy(test_case["event"])
    event = copy.deepcopy(_VALID_BY_ID[test_case["mutate_from"]])
    for path, value in test_case.get("set", {}).items():
        parent, key = _parent_and_key(event, path)
        parent[key] = copy.deepcopy(value)
    for path in test_case.get("delete", []):
        parent, key = _parent_and_key(event, path)
        del parent[key]
    if "append_usage" in test_case:
        event["usage"].append(copy.deepcopy(test_case["append_usage"]))
    if "append_dimension" in test_case:
        event["usage"][0]["dimensions"].append(copy.deepcopy(test_case["append_dimension"]))
    return event


def _event(**overrides: Any) -> Event:
    values: dict[str, Any] = {
        "event_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "task_id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
        "occurred_at": datetime(2026, 8, 11, 10, 0, 0, 123000, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return Event(**values)


def test_contract_version_and_packaged_schema_are_pinned() -> None:
    assert _CORPUS["observation_contract_version"] == ATTRIBUTION_V3_CONTRACT_VERSION
    packaged = files("dexcost.attribution").joinpath("attribution-v3-schema.json")
    assert packaged.read_bytes() == _SCHEMA_PATH.read_bytes()


def test_shared_valid_observations() -> None:
    for test_case in _CORPUS["valid_observations"]:
        result = validate_attribution_observation_v3(test_case["event"])
        assert result.success, (test_case["id"], result.issues)
        assert result.issues == ()


def test_shared_invalid_observations_fail_at_promised_path() -> None:
    for test_case in _CORPUS["invalid_observations"]:
        result = validate_attribution_observation_v3(_materialize(test_case))
        assert not result.success, test_case["id"]
        assert test_case["expected_error_path"] in {issue.path for issue in result.issues}, (
            test_case["id"],
            result.issues,
        )


def test_llm_conversion_has_stable_operation_and_usage_identities() -> None:
    event = _event(
        event_type="llm_call",
        provider="anthropic",
        model="claude-sonnet-4-5",
        input_tokens=100,
        cached_tokens=1_000,
        output_tokens=50,
        cost_usd=Decimal("0.00135"),
        cost_confidence="exact",
        pricing_source="service_catalog",
        pricing_version="llm:2026-08-11",
        details={"cache_creation_input_tokens": 25},
    )
    first = to_attribution_observation_v3(event)
    second = to_attribution_observation_v3(event)

    assert first == second
    assert first is not None
    assert first["schema_version"] == "3"
    assert first["usage_snapshot"] == "full"
    assert first["operation"] == {
        "id": str(event.event_id),
        "name": "llm.call",
        "status": "succeeded",
        "attempt": {"id": str(event.event_id), "number": 1},
    }
    assert first["cost_evidence"] == {
        "amount": "0.00135",
        "currency": "USD",
        "source": "sdk_catalog",
        "confidence": "computed",
        "pricing_version": "llm:2026-08-11",
    }
    assert len(first["usage"]) == 4
    assert all(line["dimensions"] == [] for line in first["usage"])
    assert len({line["line_id"] for line in first["usage"]}) == 4
    assert validate_attribution_observation_v3(first).success


def test_successful_compute_without_usage_is_not_invented() -> None:
    assert to_attribution_observation_v3(_event(event_type="compute_cost")) is None


def test_retry_linkage_is_nested_under_operation_attempt() -> None:
    retry_of = uuid.uuid4()
    event = _event(
        event_id=uuid.uuid4(),
        event_type="retry_marker",
        is_retry=True,
        retry_reason="rate_limit",
        retry_of=retry_of,
        cost_usd=Decimal("0.02"),
        cost_confidence="exact",
        details={
            "attribution_operation_id": str(retry_of),
            "attribution_attempt_number": 2,
        },
    )
    converted = to_attribution_observation_v3(event)
    assert converted is not None
    assert converted["operation"]["id"] == str(retry_of)
    assert converted["operation"]["status"] == "failed"
    assert converted["operation"]["attempt"] == {
        "id": str(event.event_id),
        "number": 2,
        "retry_of": str(retry_of),
    }
    assert converted["resource"] == {"type": "other", "id": "rate_limit"}
    assert "retry_of" not in converted


def test_unknown_explicit_meter_remains_visible_and_unpriced() -> None:
    converted = to_attribution_observation_v3(
        _event(
            event_type="external_cost",
            service_name="future-provider",
            details={
                "attribution_component": "telephony",
                "attribution_usage_metric": "provider_new_meter",
                "attribution_usage_unit": "Widgets",
                "attribution_usage_quantity": "7.5",
                "attribution_dimensions": [
                    {
                        "key": "priority",
                        "value": {"type": "string", "value": "fast"},
                    }
                ],
            },
        )
    )
    assert converted is not None
    assert converted["component"] == "telephony"
    assert converted["usage"][0]["metric"] == "provider_new_meter"
    assert converted["usage"][0]["unit"] == "Widgets"
    assert converted["usage"][0]["quantity"] == "7.5"
    assert converted["usage"][0]["dimensions"] == [
        {"key": "priority", "value": {"type": "string", "value": "fast"}}
    ]
    assert "cost_evidence" not in converted


def test_gpu_utilization_is_non_monetary_extensible_usage() -> None:
    converted = to_attribution_observation_v3(
        _event(
            event_type="gpu_utilization_signal",
            details={
                "gpu_index": 0,
                "gpu_sku": "h100",
                "sm_util_pct": 42.5,
                "vram_used_peak_bytes": 1024,
                "task_duration_ms": 60_000,
            },
        )
    )
    assert converted is not None
    assert converted["component"] == "gpu"
    assert converted["operation"]["status"] == "unknown"
    assert converted["usage_period"] == {
        "start_at": "2026-08-11T09:59:00.123000Z",
        "end_at": "2026-08-11T10:00:00.123000Z",
    }
    assert [line["metric"] for line in converted["usage"]] == [
        "gpu.sm_utilization_percent",
        "gpu.vram_peak_bytes",
    ]
    assert "cost_evidence" not in converted


def test_local_gpu_usage_matches_shared_usage_only_contract() -> None:
    converted = to_attribution_observation_v3(
        _event(event_type="gpu_cost", cost_usd=Decimal("0"), details=_LOCAL_GPU["details"])
    )
    expected = _LOCAL_GPU["expected"]
    assert converted is not None
    assert converted["component"] == expected["component"]
    assert converted["provider"] == {
        "name": expected["provider_name"],
        "service": expected["provider_service"],
    }
    assert converted["resource"] == {
        "type": expected["resource_type"],
        "id": expected["resource_id"],
    }
    assert converted["usage"] == [
        {
            "line_id": converted["usage"][0]["line_id"],
            "metric": expected["usage_metric"],
            "unit": expected["usage_unit"],
            "quantity": expected["usage_quantity"],
            "dimensions": [],
        }
    ]
    assert "cost_evidence" not in converted
    assert validate_attribution_observation_v3(converted).success


def test_retry_chain_persists_one_operation_root_and_increasing_attempts(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(db_path=tmp_path / "retry-chain.db")
    tracker = CostTracker(
        storage=storage,
        auto_instrument=[],
        enable_retry_heuristics=True,
        retry_heuristic_threshold=0.5,
    )
    task = tracker.start_task(task_type="retry-chain")
    first = task.record_llm_call("openai", "gpt-4o", 100, 50, "0.05", error_type="rate_limit")
    second = task.record_llm_call("openai", "gpt-4o", 100, 50, "0.05", error_type="rate_limit")
    third = task.record_llm_call("openai", "gpt-4o", 100, 50, "0.05", error_type="rate_limit")

    assert second.retry_of == first.event_id
    assert third.retry_of == second.event_id
    persisted = {
        event.event_id: event for event in storage.query_events(task_id=str(task.task_id))
    }[third.event_id]
    assert persisted.details["attribution_operation_id"] == str(first.event_id)
    assert persisted.details["attribution_attempt_number"] == 3
    converted = to_attribution_observation_v3(persisted)
    assert converted is not None
    assert converted["operation"] == {
        "id": str(first.event_id),
        "name": "llm.call",
        "status": "failed",
        "attempt": {
            "id": str(third.event_id),
            "number": 3,
            "retry_of": str(second.event_id),
        },
        # The failure marker that drives status="failed" also carries the
        # error identity onto the wire.
        "error": {"type": "rate_limit"},
    }
