"""Provider-neutral capability identity, propagation, and wire coverage."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

import dexcost
from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.capabilities import (
    canonical_tool_capability_name,
    capability_context,
    get_capability,
)
from dexcost.models.capability import CapabilityIdentity
from dexcost.models.event import Event
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _workflow(name: str = "support.resolve") -> CapabilityIdentity:
    return CapabilityIdentity(
        name=name,
        kind="workflow",
        namespace="dexcost.agent",
        version="2026-08-21",
        source="project",
        source_id=f"{name}/v1",
        invocation="automatic",
    )


def test_capability_identity_is_strict_and_round_trips() -> None:
    capability = _workflow()
    assert CapabilityIdentity.from_dict(capability.to_dict()) == capability
    assert capability.to_dict() == {
        "name": "support.resolve",
        "kind": "workflow",
        "namespace": "dexcost.agent",
        "version": "2026-08-21",
        "source": "project",
        "source_id": "support.resolve/v1",
        "invocation": "automatic",
    }

    with pytest.raises(ValueError, match="canonical lowercase"):
        CapabilityIdentity(name="Support Resolve", kind="workflow")
    with pytest.raises(ValueError, match="source_id requires source"):
        CapabilityIdentity(name="support.resolve", kind="workflow", source_id="v1")
    with pytest.raises(ValueError, match="unknown capability"):
        CapabilityIdentity.from_dict({"name": "support.resolve", "kind": "workflow", "x": 1})


@pytest.mark.asyncio
async def test_capability_context_nests_restores_and_isolates_async_tasks() -> None:
    outer = _workflow("outer.run")
    inner = _workflow("inner.run")
    assert get_capability() is None
    with capability_context(outer):
        assert get_capability() == outer
        with capability_context(inner):
            assert get_capability() == inner
        assert get_capability() == outer

        async def observe(capability: CapabilityIdentity) -> CapabilityIdentity | None:
            with capability_context(capability):
                await asyncio.sleep(0)
                return get_capability()

        observed = await asyncio.gather(observe(outer), observe(inner))
        assert observed[0] == outer
        assert observed[1] == inner
    assert get_capability() is None


def test_context_is_snapshotted_durably_and_emitted_without_details(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "capability.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    capability = _workflow()
    try:
        with tracker.task(task_type="support.resolve") as task, capability_context(capability):
            task.record_llm_call("openai", "gpt-5", 10, 5, "0.01")

        persisted = storage.query_events(task_id=str(task.task_id))[0]
        assert persisted.details["attribution_capability"] == capability.to_dict()
        wire = to_attribution_observation_v3(persisted)
        assert wire is not None
        assert wire["capability"] == capability.to_dict()
        assert "details" not in wire
    finally:
        storage.close()


def test_explicit_capability_overrides_context_for_manual_cost(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "capability-override.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    explicit = CapabilityIdentity(name="billing.charge", kind="skill", invocation="explicit")
    try:
        with tracker.task(task_type="billing.run") as task, capability_context(_workflow()):
            event = task.record_cost("payments", "0.01", capability=explicit)
        assert event.details["attribution_capability"] == explicit.to_dict()
    finally:
        storage.close()


def test_tools_default_to_direct_capability_but_inherit_richer_context(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "tool-capability.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    workflow = _workflow()
    try:
        with tracker.task(task_type="agent.run") as task:
            direct = task.record_tool_call("Web Search / V2")
            with capability_context(workflow):
                nested = task.record_tool_call("Web Search / V2")

        direct_identity = direct.details["attribution_capability"]
        assert direct_identity == {
            "name": canonical_tool_capability_name("Web Search / V2"),
            "kind": "tool",
            "invocation": "explicit",
        }
        assert nested.details["attribution_capability"] == workflow.to_dict()
    finally:
        storage.close()


def test_generator_decorator_snapshots_capability_at_invocation(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "generator-capability.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    workflow = _workflow()

    @tracker.track_tool("pages")
    def pages() -> Generator[int, None, None]:
        yield 1

    try:
        with tracker.task(task_type="pages.run"):
            with capability_context(workflow):
                stream = pages()
                assert next(stream) == 1
            assert list(stream) == []
        event = storage.query_events()[0]
        assert event.details["attribution_capability"] == workflow.to_dict()
    finally:
        storage.close()


def test_direct_storage_capture_snapshots_active_context(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "direct-capability.db")
    capability = _workflow()
    event = Event(task_id=uuid.uuid4(), event_type="external_cost")
    try:
        with capability_context(capability):
            storage.insert_event(event)
        assert storage.query_events()[0].details["attribution_capability"] == capability.to_dict()
    finally:
        storage.close()


def test_top_level_capability_api_is_public() -> None:
    capability = _workflow()
    assert dexcost.CapabilityIdentity is CapabilityIdentity
    with dexcost.capability_context(capability):
        assert dexcost.get_capability() == capability
