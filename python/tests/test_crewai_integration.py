"""CrewAI adapter behavior independent of provider-specific instrumentation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import dexcost
from dexcost.integrations import crewai as crewai_integration
from dexcost.integrations.crewai import CREWAI_EXECUTION_METHODS, track_crewai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _without_installed_crewai(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lifecycle edge cases run in the main test environment. A separate
    # compatibility test uses the real current CrewAI package.
    monkeypatch.setattr(crewai_integration, "_ensure_event_handlers", lambda: None)
    monkeypatch.setattr(crewai_integration, "_flush_event_handlers", lambda: None)


def _tool_event(**overrides: object) -> SimpleNamespace:
    started = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "event_id": "tool-event-1",
        "tool_name": "web.search",
        "tool_args": {"query": "private customer prompt"},
        "output": "private tool output",
        "agent_id": "agent-opaque-1",
        "task_id": "task-opaque-1",
        "from_cache": False,
        "run_attempts": 1,
        "started_at": started,
        "finished_at": started + timedelta(milliseconds=125),
        "failure": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _llm_event(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "event_id": "llm-event-1",
        "call_id": "provider-call-1",
        "response_id": "provider-response-1",
        "model": "gpt-4o-mini",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 25,
        },
        "messages": "private prompt",
        "response": "private response",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_current_execution_surface_is_not_kickoff_only() -> None:
    assert {
        "kickoff",
        "kickoff_async",
        "akickoff",
        "kickoff_for_each",
        "kickoff_for_each_async",
        "akickoff_for_each",
        "stream_events",
        "astream",
        "resume",
        "resume_async",
        "replay",
        "train",
        "test",
        "execute_task",
        "aexecute_task",
    } <= CREWAI_EXECUTION_METHODS


def test_native_tool_event_is_private_and_capability_attributed(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "crewai-tool.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Crew:
        def kickoff(self) -> str:
            event = _tool_event()
            crewai_integration._handle_tool_finished(None, event)
            crewai_integration._handle_tool_finished(None, event)
            return "private crew output"

    try:
        assert track_crewai(Crew(), tracker).kickoff() == "private crew output"
        tasks = storage.query_tasks(task_type="crewai.kickoff")
        assert len(tasks) == 1
        assert tasks[0].status == "success"
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        event = events[0]
        assert event.provider == "crewai"
        assert event.service_name == "web.search"
        assert event.latency_ms == 125
        assert event.details["attribution_capability"] == {
            "name": "web.search",
            "kind": "tool",
            "namespace": "crewai",
            "source": "other",
            "source_id": "web.search",
            "invocation": "nested",
        }
        serialized = str(event.to_dict())
        assert "private customer prompt" not in serialized
        assert "private tool output" not in serialized
    finally:
        storage.close()


def test_native_tool_failure_records_type_without_error_content(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "crewai-tool-error.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Crew:
        def kickoff(self) -> None:
            crewai_integration._handle_tool_error(
                None,
                _tool_event(
                    event_id="tool-error-1",
                    error=LookupError("private tool failure"),
                ),
            )

    try:
        track_crewai(Crew(), tracker).kickoff()
        event = storage.query_events()[0]
        assert event.details["attribution_operation_status"] == "failed"
        assert event.details["attribution_error_type"] == "LookupError"
        assert "private tool failure" not in str(event.to_dict())
    finally:
        storage.close()


def test_provider_instrumentation_is_authoritative_by_default(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "crewai-no-double-count.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Crew:
        def kickoff(self) -> None:
            crewai_integration._handle_llm_completed(None, _llm_event())

    try:
        track_crewai(Crew(), tracker).kickoff()
        assert storage.query_events() == []
    finally:
        storage.close()


def test_explicit_llm_fallback_records_usage_without_content(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "crewai-llm-fallback.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Crew:
        def kickoff(self) -> None:
            crewai_integration._handle_llm_completed(None, _llm_event())

    try:
        with pytest.warns(RuntimeWarning, match="fallback path"):
            tracked = track_crewai(Crew(), tracker, capture_llm_events=True)
        tracked.kickoff()
        event = storage.query_events()[0]
        assert event.event_type == "llm_call"
        assert event.input_tokens == 100
        assert event.output_tokens == 20
        assert event.cached_tokens == 25
        assert event.details["provider_record_id"] == "provider-call-1"
        serialized = str(event.to_dict())
        assert "private prompt" not in serialized
        assert "private response" not in serialized
    finally:
        storage.close()


def test_current_crewai_usage_aliases_are_normalized() -> None:
    assert crewai_integration._usage_tokens(
        {
            "prompt_token_count": 50,
            "candidates_token_count": 7,
            "cached_prompt_tokens": 12,
        }
    ) == (50, 7, 12)
    assert crewai_integration._usage_tokens(
        {
            "input_tokens": 30,
            "output_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 9},
        }
    ) == (30, 5, 9)


def test_framework_failure_event_marks_swallowed_failure(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "crewai-native-failure.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Crew:
        def kickoff(self) -> str:
            crewai_integration._handle_execution_failed(
                None,
                SimpleNamespace(error="private workflow failure"),
            )
            return "framework swallowed the failure"

    try:
        track_crewai(Crew(), tracker).kickoff()
        task = storage.query_tasks(task_type="crewai.kickoff")[0]
        assert task.status == "failed"
        assert "private workflow failure" not in str(task.to_dict())
    finally:
        storage.close()


def test_top_level_api_uses_globally_configured_tracker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SQLiteStorage(tmp_path / "crewai-global.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Crew:
        def kickoff(self) -> str:
            return "done"

    monkeypatch.setattr(dexcost, "_global_tracker", tracker)
    try:
        assert dexcost.track_crewai(Crew()).kickoff() == "done"
        assert storage.query_tasks(task_type="crewai.kickoff")[0].status == "success"
    finally:
        storage.close()
