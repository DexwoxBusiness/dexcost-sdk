"""Caller-controlled idempotency and durable conflict semantics."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import dexcost
from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.idempotency import get_idempotency_key, idempotency_key
from dexcost.models.event import Event
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def test_idempotency_key_validates_and_restores_nested_scopes() -> None:
    assert get_idempotency_key() is None
    with idempotency_key("order-42"):
        assert get_idempotency_key() == "order-42"
        with idempotency_key("order-42-step-2"):
            assert get_idempotency_key() == "order-42-step-2"
        assert get_idempotency_key() == "order-42"
    assert get_idempotency_key() is None

    for invalid in ("", "x" * 256, "contains space", "snowman-☃"):
        with pytest.raises(ValueError), idempotency_key(invalid):
            pass


def test_identical_tool_capture_collapses_to_first_durable_event(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "idempotent-tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        with tracker.task(task_type="order.run") as task:
            with idempotency_key("order-42-charge"):
                first = task.record_tool_call("payments", cost_usd="0.01")
            with idempotency_key("order-42-charge"):
                repeated = task.record_tool_call("payments", cost_usd="0.01")

        assert repeated.event_id == first.event_id
        assert repeated.occurred_at == first.occurred_at
        assert storage.query_events(task_id=str(task.task_id)) == [first]
    finally:
        storage.close()


def test_key_reuse_for_different_economic_facts_is_rejected(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "idempotency-conflict.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        with tracker.task(task_type="order.run") as task:
            task.record_tool_call(
                "payments",
                cost_usd="0.01",
                idempotency_key="order-42-charge",
            )
            with pytest.raises(ValueError, match="different economic facts"):
                task.record_tool_call(
                    "payments",
                    cost_usd="0.02",
                    idempotency_key="order-42-charge",
                )
    finally:
        storage.close()


def test_same_key_can_identify_distinct_component_operations(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "idempotency-components.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        with (
            tracker.task(task_type="workflow.run") as task,
            idempotency_key("workflow-9"),
        ):
            search = task.record_tool_call("search")
            charge = task.record_tool_call("payments")
        assert search.event_id != charge.event_id
        assert len(storage.query_events(task_id=str(task.task_id))) == 2
    finally:
        storage.close()


def test_one_ambient_scope_distinguishes_occurrences_and_replays_deterministically(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "idempotency-occurrences.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        with tracker.task(task_type="workflow.run") as task:
            with idempotency_key("workflow-10"):
                first = task.record_tool_call("search", cost_usd="0.01")
                second = task.record_tool_call("search", cost_usd="0.01")
            with idempotency_key("workflow-10"):
                first_replay = task.record_tool_call("search", cost_usd="0.01")
                second_replay = task.record_tool_call("search", cost_usd="0.01")

        assert first.event_id != second.event_id
        assert first_replay.event_id == first.event_id
        assert second_replay.event_id == second.event_id
        assert first.details["_dexcost_idempotency_occurrence"] == 0
        assert second.details["_dexcost_idempotency_occurrence"] == 1
        persisted = {
            event.event_id: event
            for event in storage.query_events(task_id=str(task.task_id))
        }
        assert persisted == {first.event_id: first, second.event_id: second}
    finally:
        storage.close()


def test_llm_manual_capture_uses_same_durable_idempotency_contract(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "idempotent-llm.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        with tracker.task(task_type="assistant.run") as task:
            first = task.record_llm_call(
                "openai",
                "gpt-5",
                10,
                5,
                "0.01",
                idempotency_key="request-123",
            )
            repeated = task.record_llm_call(
                "openai",
                "gpt-5",
                10,
                5,
                "0.01",
                idempotency_key="request-123",
            )
        assert repeated.event_id == first.event_id
        assert len(storage.query_events(task_id=str(task.task_id))) == 1
    finally:
        storage.close()


def test_raw_idempotency_key_never_persists_or_reaches_wire(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "idempotency-privacy.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    raw_key = "customer-secret-order-42"
    try:
        with tracker.task(task_type="privacy.run") as task:
            event = task.record_tool_call("search", idempotency_key=raw_key)
        persisted = storage.query_events(task_id=str(task.task_id))[0]
        assert raw_key not in str(persisted.to_dict())
        assert len(persisted.details["_dexcost_idempotency_sha256"]) == 64
        wire = to_attribution_observation_v3(event)
        assert wire is not None
        assert raw_key not in str(wire)
        assert "idempotency" not in str(wire)
    finally:
        storage.close()


def test_explicit_event_id_duplicate_is_idempotent_or_conflicting(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "event-id.db")
    event_id = uuid.uuid4()
    first = Event(event_id=event_id, task_id=uuid.uuid4(), event_type="external_cost")
    storage.insert_event(first)
    try:
        storage.insert_event(first)
        conflict = Event.from_dict(first.to_dict())
        conflict.service_name = "different"
        with pytest.raises(ValueError, match="already exists with different contents"):
            storage.insert_event(conflict)
    finally:
        storage.close()


def test_top_level_idempotency_api_is_wired(tmp_path: Path) -> None:
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "global-idempotency.db"),
        auto_instrument=[],
        track_http=False,
        track_network=False,
    )
    try:
        task_id = uuid.uuid4()
        with dexcost.attach_task(task_id), dexcost.idempotency_key("worker-op-7"):
            first = dexcost.report_tool_call("worker")
        with dexcost.attach_task(task_id), dexcost.idempotency_key("worker-op-7"):
            repeated = dexcost.report_tool_call("worker")
        assert first.event_id == repeated.event_id
        assert dexcost.get_idempotency_key() is None
    finally:
        dexcost.close()
