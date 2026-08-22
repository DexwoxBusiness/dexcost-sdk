"""Opt-in compatibility gate for the currently supported CrewAI release."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest


def test_current_crewai_real_crew_and_event_bus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("crewai_core")

    # CrewAI initializes user storage and telemetry during import. Point its
    # storage at the test directory and explicitly disable network telemetry.
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    monkeypatch.setenv("CREWAI_STORAGE_DIR", "dexcost-compat")

    import crewai_core.paths as crewai_paths
    import crewai_core.token_manager as token_manager

    monkeypatch.setattr(
        token_manager.TokenManager,
        "_get_secure_storage_path",
        staticmethod(lambda: tmp_path),
    )
    monkeypatch.setattr(crewai_paths, "db_storage_path", lambda: str(tmp_path))
    monkeypatch.setattr(
        crewai_paths.appdirs,
        "user_data_dir",
        lambda *args, **kwargs: str(tmp_path),
    )

    import crewai
    from crewai import Agent, Crew, Task
    from crewai.events import (
        LLMCallCompletedEvent,
        LLMCallStartedEvent,
        crewai_event_bus,
    )
    from crewai.events.types.llm_events import LLMCallType
    from crewai.llms.base_llm import BaseLLM, llm_call_context

    from dexcost.integrations import track_crewai
    from dexcost.storage.sqlite import SQLiteStorage
    from dexcost.tracker import CostTracker

    version = tuple(int(part) for part in crewai.__version__.split(".")[:2])
    assert version >= (1, 0)

    class LocalLLM(BaseLLM):
        provider: str = "local-test"

        def call(
            self,
            messages: Any,
            tools: Any = None,
            callbacks: Any = None,
            available_functions: Any = None,
            from_task: Any = None,
            from_agent: Any = None,
            response_model: Any = None,
        ) -> str:
            del (
                messages,
                tools,
                callbacks,
                available_functions,
                from_task,
                from_agent,
                response_model,
            )
            with llm_call_context() as call_id:
                crewai_event_bus.emit(
                    self,
                    LLMCallStartedEvent(
                        model=self.model,
                        call_id=call_id,
                        messages=None,
                    ),
                )
                crewai_event_bus.emit(
                    self,
                    LLMCallCompletedEvent(
                        model=self.model,
                        call_id=call_id,
                        response="Final Answer: done",
                        call_type=LLMCallType.LLM_CALL,
                        usage={
                            "prompt_tokens": 12,
                            "completion_tokens": 3,
                            "cached_tokens": 2,
                        },
                        response_id="real-crewai-response",
                    ),
                )
            return "Final Answer: done"

    storage = SQLiteStorage(tmp_path / "dexcost.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    try:
        agent = Agent(
            role="Tester",
            goal="Return done",
            backstory="Local compatibility agent",
            llm=LocalLLM(model="gpt-4o-mini"),
            verbose=False,
        )
        task = Task(
            description="Return done without tools",
            expected_output="done",
            agent=agent,
        )
        crew = Crew(
            agents=[agent],
            tasks=[task],
            tracing=False,
            verbose=False,
        )
        with pytest.warns(RuntimeWarning, match="fallback path"):
            tracked = track_crewai(crew, tracker, capture_llm_events=True)
        assert isinstance(tracked, Crew)
        assert "done" in str(tracked.kickoff()).lower()

        tasks = storage.query_tasks(task_type="crewai.kickoff")
        assert len(tasks) == 1
        assert tasks[0].status == "success"
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        event = events[0]
        assert (event.input_tokens, event.output_tokens, event.cached_tokens) == (12, 3, 2)
        uuid.UUID(event.details["provider_record_id"])
        assert "Return done without tools" not in str(event.to_dict())
    finally:
        storage.close()
