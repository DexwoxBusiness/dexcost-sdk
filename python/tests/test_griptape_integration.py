"""Griptape public API, native event, privacy, and lifecycle compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("griptape")

from griptape.artifacts import ErrorArtifact, TextArtifact
from griptape.drivers.prompt.dummy_prompt_driver import DummyPromptDriver
from griptape.events import (
    EventBus,
    FinishActionsSubtaskEvent,
    FinishPromptEvent,
    FinishStructureRunEvent,
)
from griptape.structures import Agent

from dexcost.integrations.griptape import track_griptape
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


class _EventPublishingStructure:
    def run(self, prompt: str) -> str:
        EventBus.publish_event(
            FinishPromptEvent(
                id="prompt-event-1",
                model="gpt-4o-mini",
                result="private model output",
                input_token_count=40,
                output_token_count=10,
            )
        )
        EventBus.publish_event(
            FinishActionsSubtaskEvent(
                id="action-event-1",
                task_id="task-opaque-1",
                task_parent_ids=[],
                task_child_ids=[],
                task_input=TextArtifact(prompt),
                task_output=TextArtifact("private tool output"),
                subtask_parent_task_id="parent-opaque-1",
                subtask_thought="private chain of thought",
                subtask_actions=[
                    {
                        "tag": "call-1",
                        "name": "browser.search",
                        "path": "browser.search",
                        "input": {"query": prompt},
                    }
                ],
            )
        )
        EventBus.publish_event(
            FinishStructureRunEvent(
                id="structure-event-1",
                structure_id="structure-opaque-1",
                output_task_input=TextArtifact(prompt),
                output_task_output=TextArtifact("private structure output"),
            )
        )
        return "private structure output"


def test_real_event_listener_captures_actions_without_private_content(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "griptape-events.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        tracked = track_griptape(_EventPublishingStructure(), tracker)
        assert tracked.run("private customer prompt") == "private structure output"
        tasks = storage.query_tasks(task_type="griptape.run")
        assert len(tasks) == 1
        assert tasks[0].status == "success"
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        event = events[0]
        assert event.provider == "griptape"
        assert event.service_name == "browser.search"
        assert event.details["attribution_capability"] == {
            "name": "browser.search",
            "kind": "tool",
            "namespace": "griptape",
            "source": "other",
            "source_id": "browser.search",
            "invocation": "nested",
        }
        serialized = str(event.to_dict())
        assert "private customer prompt" not in serialized
        assert "private tool output" not in serialized
        assert "private chain of thought" not in serialized
        assert "private structure output" not in serialized
    finally:
        storage.close()


def test_explicit_llm_fallback_uses_real_finish_prompt_event(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "griptape-llm.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        with pytest.warns(RuntimeWarning, match="fallback path"):
            tracked = track_griptape(
                _EventPublishingStructure(),
                tracker,
                capture_llm_events=True,
            )
        tracked.run("private customer prompt")
        events = storage.query_events()
        assert {event.event_type for event in events} == {"llm_call", "external_cost"}
        llm = next(event for event in events if event.event_type == "llm_call")
        assert llm.input_tokens == 40
        assert llm.output_tokens == 10
        assert "private model output" not in str(llm.to_dict())
    finally:
        storage.close()


def test_real_structure_failure_is_detected_when_griptape_does_not_raise(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "griptape-failure.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class FailedStructure:
        def run(self) -> FailedStructure:
            EventBus.publish_event(
                FinishStructureRunEvent(
                    id="failed-structure-event",
                    structure_id="failed-structure",
                    output_task_input=TextArtifact("private input"),
                    output_task_output=ErrorArtifact("private failure"),
                )
            )
            return self

    try:
        track_griptape(FailedStructure(), tracker).run()
        task = storage.query_tasks(task_type="griptape.run")[0]
        assert task.status == "failed"
        assert "private failure" not in str(task.to_dict())
    finally:
        storage.close()


def test_actual_griptape_agent_run_stream_is_wrapped_and_finalized(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "griptape-real-agent.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    # One attempt avoids a retry delay while still exercising the installed
    # Agent, Structure.run_stream, EventListener, and ErrorArtifact lifecycle.
    agent = Agent(prompt_driver=DummyPromptDriver(max_attempts=1))
    try:
        tracked = track_griptape(agent, tracker)
        assert isinstance(tracked, Agent)
        stream = tracked.run_stream("private real-agent prompt")
        list(stream)
        task = storage.query_tasks(task_type="griptape.run_stream")[0]
        assert task.status == "failed"
        assert "private real-agent prompt" not in str(task.to_dict())
    finally:
        storage.close()
