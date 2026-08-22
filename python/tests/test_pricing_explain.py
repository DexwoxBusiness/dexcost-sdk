"""Local pricing explanations preserve exact catalog provenance."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from dexcost.models.event import Event
from dexcost.models.pricing_explanation import PricingProvenance
from dexcost.pricing_explain import (
    explain_event_pricing,
    register_pricing_provenance,
)
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _provenance(release: int = 42) -> PricingProvenance:
    return PricingProvenance(
        catalog_source="active",
        stale=False,
        release_id=f"catalog-release-test-{release}",
        release_sequence=release,
        artifact_kind="llm_prices",
        artifact_sha256="a" * 64,
        artifact_schema_version="1",
        safety_policy_version="catalog-safety-v1",
    )


def test_storage_snapshots_registered_release_provenance(tmp_path: Path) -> None:
    version = "catalog-release:42:aaaaaaaaaaaa"
    register_pricing_provenance(version, _provenance())
    storage = SQLiteStorage(tmp_path / "explain.db")
    event = Event(
        task_id=uuid.uuid4(),
        event_type="llm_call",
        provider="openai",
        model="gpt-5",
        input_tokens=100,
        output_tokens=25,
        cached_tokens=10,
        cost_usd=Decimal("0.0125"),
        cost_confidence="computed",
        pricing_source="litellm",
        pricing_version=version,
        details={"prompt": "must never enter explanation"},
    )
    try:
        storage.insert_event(event)
        persisted = storage.query_events(event_id=str(event.event_id))[0]
        explanation = explain_event_pricing(persisted)

        assert explanation.status == "provisional"
        assert explanation.authority == "sdk_evidence"
        assert explanation.amount_usd == Decimal("0.0125")
        assert explanation.provenance == _provenance()
        assert dict(explanation.inputs) == {
            "cached_tokens": "10",
            "input_tokens": "100",
            "model": "gpt-5",
            "output_tokens": "25",
        }
        assert "must never enter explanation" not in str(explanation.to_dict())
    finally:
        storage.close()


def test_stored_provenance_is_not_reinterpreted_after_registry_change(tmp_path: Path) -> None:
    version = "catalog-release:43:bbbbbbbbbbbb"
    first = _provenance(43)
    register_pricing_provenance(version, first)
    storage = SQLiteStorage(tmp_path / "immutable-explain.db")
    event = Event(
        task_id=uuid.uuid4(),
        event_type="external_cost",
        cost_usd=Decimal("1"),
        pricing_source="service_catalog",
        pricing_version=version,
    )
    try:
        storage.insert_event(event)
        register_pricing_provenance(version, _provenance(99))
        persisted = storage.query_events(event_id=str(event.event_id))[0]
        assert explain_event_pricing(persisted).provenance == first
    finally:
        storage.close()


def test_unpriced_and_provider_reported_statuses_are_explicit() -> None:
    unpriced = Event(
        event_type="external_cost",
        cost_confidence="unknown",
        pricing_source="unknown",
    )
    exact = Event(
        event_type="llm_call",
        cost_usd=Decimal("0.01"),
        cost_confidence="exact",
        pricing_source="provider_reported",
    )
    assert explain_event_pricing(unpriced).status == "unpriced"
    assert explain_event_pricing(exact).status == "provider_reported"


def test_tracker_explains_by_event_id_and_enforces_task_ownership(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "tracker-explain.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        with tracker.task(task_type="one") as first:
            event = first.record_cost("maps", "0.01")
            assert first.explain_pricing(event.event_id).event_id == str(event.event_id)
        assert tracker.explain_pricing(str(event.event_id)).event_id == str(event.event_id)
        with (
            tracker.task(task_type="two") as second,
            pytest.raises(ValueError, match="does not belong"),
        ):
            second.explain_pricing(event)
        with pytest.raises(KeyError, match="was not found"):
            tracker.explain_pricing(uuid.uuid4())
    finally:
        storage.close()
