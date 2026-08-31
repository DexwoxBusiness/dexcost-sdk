"""Tests for session-based auto-grouping."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
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
from dexcost.session import SessionManager, reset_session_manager
from dexcost.storage.sqlite import SQLiteStorage

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


class TestAgentIdentity:
    """Agent identity remains separate from the session task type."""

    def test_agent_does_not_replace_task_type(self) -> None:
        set_context(agent="research_agent", agent_version="v1")
        mgr = SessionManager()
        task = mgr.get_or_create_session("llm_call")

        assert task.task_type == "agent_session"
        assert task.agent_id == "research_agent"
        assert task.agent_version == "v1"
        assert task.root_task_id == task.task_id

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
            agent_version="demo-v2",
            workflow_id="support_resolution",
            workflow_session_id="ticket-123",
        )
        mgr = SessionManager()
        task = mgr.get_or_create_session("llm_call")

        assert task.customer_id == "cust-123"
        assert task.project_id == "proj-456"
        assert task.task_type == "agent_session"
        assert task.agent_id == "support_bot"
        assert task.agent_version == "demo-v2"
        assert task.workflow_id == "support_resolution"
        assert task.workflow_session_id == "ticket-123"
        assert task.root_task_id == task.task_id


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


class TestAsyncIsolation:
    """Concurrent requests on one event-loop thread get separate sessions."""

    @pytest.mark.asyncio
    async def test_concurrent_customers_get_separate_sessions(self) -> None:
        mgr = SessionManager()
        ready = asyncio.Event()
        started = 0

        async def request(customer_id: str) -> tuple[Task, Task]:
            nonlocal started
            set_context(customer_id=customer_id)
            first = mgr.get_or_create_session("llm_call")
            started += 1
            if started == 2:
                ready.set()
            await ready.wait()
            second = mgr.get_or_create_session("http_call")
            return first, second

        (first_a, second_a), (first_b, second_b) = await asyncio.gather(
            request("customer-a"),
            request("customer-b"),
        )

        assert first_a is second_a
        assert first_b is second_b
        assert first_a.task_id != first_b.task_id
        assert first_a.customer_id == "customer-a"
        assert first_b.customer_id == "customer-b"

    @pytest.mark.asyncio
    async def test_child_does_not_inherit_parent_auto_session(self) -> None:
        mgr = SessionManager()
        set_context(customer_id="parent")
        parent = mgr.get_or_create_session("llm_call")

        async def child_request() -> Task:
            set_context(customer_id="child")
            return mgr.get_or_create_session("llm_call")

        child = await asyncio.create_task(child_request())

        assert child.task_id != parent.task_id
        assert child.customer_id == "child"


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

    def test_finalization_persists_and_requeues_synced_session(self, tmp_path: Path) -> None:
        storage = SQLiteStorage(db_path=tmp_path / "session.db")
        mgr = SessionManager()
        task = mgr.get_or_create_session("http_call", storage=storage)
        storage.mark_tasks_synced([str(task.task_id)])

        finalized = mgr.finalize_idle_sessions(
            idle_seconds=0.0,
            storage=storage,
        )

        assert [item.task_id for item in finalized] == [task.task_id]
        persisted = storage.get_task(str(task.task_id))
        assert persisted is not None
        assert persisted.status == "success"
        assert persisted.ended_at is not None
        assert [item.task_id for item in storage.query_pending_tasks_for_sync()] == [task.task_id]

    def test_finalized_context_session_is_not_reused(self) -> None:
        mgr = SessionManager()
        original = mgr.get_or_create_session("http_call")
        mgr.finalize_idle_sessions(idle_seconds=0.0)

        replacement = mgr.get_or_create_session("http_call")

        assert replacement.task_id != original.task_id
        assert replacement.ended_at is None
