"""Tests for session-based auto-grouping."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from dexcost.context import (
    clear_context,
    get_current_task,
    set_context,
    set_current_task,
    task_context,
)
from dexcost.models.task import Task
from dexcost.session import SessionManager, get_session_manager, reset_session_manager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Reset session and context state around each test."""
    reset_session_manager()
    set_current_task(None)
    clear_context()
    yield
    reset_session_manager()
    set_current_task(None)
    clear_context()


# ---------------------------------------------------------------------------
# Basic session creation
# ---------------------------------------------------------------------------


class TestSessionCreation:
    """First call creates a session task."""

    def test_creates_session_when_no_task(self) -> None:
        mgr = SessionManager()
        task = mgr.get_or_create_session("llm_call")

        assert task is not None
        assert task.task_type == "agent_session"
        assert task.status == "pending"

    def test_session_is_set_as_current_task(self) -> None:
        mgr = SessionManager()
        task = mgr.get_or_create_session("llm_call")

        assert get_current_task() is task


class TestSessionReuse:
    """Second call in same context reuses session task."""

    def test_reuses_existing_session(self) -> None:
        mgr = SessionManager()
        task1 = mgr.get_or_create_session("llm_call")
        task2 = mgr.get_or_create_session("http_call")

        assert task1 is task2
        assert task1.task_id == task2.task_id


# ---------------------------------------------------------------------------
# Agent-based task type
# ---------------------------------------------------------------------------


class TestAgentTaskType:
    """set_context(agent='foo') makes session task_type = 'foo'."""

    def test_agent_sets_task_type(self) -> None:
        set_context(agent="research_agent")
        mgr = SessionManager()
        task = mgr.get_or_create_session("llm_call")

        assert task.task_type == "research_agent"

    def test_no_agent_defaults_to_agent_session(self) -> None:
        set_context(customer_id="cust-1")
        mgr = SessionManager()
        task = mgr.get_or_create_session("llm_call")

        assert task.task_type == "agent_session"

    def test_context_attribution_inherited(self) -> None:
        set_context(
            customer_id="cust-123",
            project_id="proj-456",
            agent="support_bot",
        )
        mgr = SessionManager()
        task = mgr.get_or_create_session("llm_call")

        assert task.customer_id == "cust-123"
        assert task.project_id == "proj-456"
        assert task.task_type == "support_bot"


# ---------------------------------------------------------------------------
# Explicit task precedence
# ---------------------------------------------------------------------------


class TestExplicitTaskPrecedence:
    """Explicit dexcost.task() takes precedence over session."""

    def test_explicit_task_returned(self) -> None:
        explicit_task = Task(task_type="explicit_task")
        mgr = SessionManager()

        with task_context(explicit_task):
            task = mgr.get_or_create_session("llm_call")
            assert task is explicit_task
            assert task.task_type == "explicit_task"

    def test_session_created_after_explicit_task_exits(self) -> None:
        explicit_task = Task(task_type="explicit_task")
        mgr = SessionManager()

        with task_context(explicit_task):
            task = mgr.get_or_create_session("llm_call")
            assert task is explicit_task

        # After explicit task exits, a new session should be created
        set_current_task(None)
        # Need a fresh manager to avoid thread-id reuse
        mgr2 = SessionManager()
        task2 = mgr2.get_or_create_session("llm_call")
        assert task2 is not explicit_task
        assert task2.task_type == "agent_session"


# ---------------------------------------------------------------------------
# Thread isolation
# ---------------------------------------------------------------------------


class TestThreadIsolation:
    """Different threads get different sessions."""

    def test_threads_get_separate_sessions(self) -> None:
        mgr = SessionManager()
        results: dict[str, Task] = {}
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            task = mgr.get_or_create_session("llm_call")
            results[name] = task
            barrier.wait(timeout=5)

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert results["a"].task_id != results["b"].task_id

    def test_same_thread_reuses_session(self) -> None:
        mgr = SessionManager()
        results: list[Task] = []

        def worker() -> None:
            t1 = mgr.get_or_create_session("llm_call")
            t2 = mgr.get_or_create_session("http_call")
            results.extend([t1, t2])

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)

        assert results[0].task_id == results[1].task_id


# ---------------------------------------------------------------------------
# Idle session finalization
# ---------------------------------------------------------------------------


class TestIdleFinalization:
    """finalize_idle_sessions finalizes sessions with no recent activity."""

    def test_finalize_idle_session(self) -> None:
        mgr = SessionManager()
        task = mgr.get_or_create_session("llm_call")

        # Finalize with 0 idle time -> should finalize immediately
        finalized = mgr.finalize_idle_sessions(idle_seconds=0.0)

        assert len(finalized) == 1
        assert finalized[0].task_id == task.task_id
        assert finalized[0].status == "success"
        assert finalized[0].ended_at is not None

    def test_active_session_not_finalized(self) -> None:
        mgr = SessionManager()
        mgr.get_or_create_session("llm_call")

        # With a very high idle threshold, nothing should be finalized
        finalized = mgr.finalize_idle_sessions(idle_seconds=9999.0)
        assert len(finalized) == 0
