"""Tests for sdks/python/src/dexcost/auto_task.py"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dexcost.auto_task import (
    create_auto_task,
    finalize_auto_task,
    needs_auto_task,
)
from dexcost.context import set_context, clear_context, set_current_task, task_context
from dexcost.models.event import Event
from dexcost.models.task import Task

@pytest.fixture(autouse=True)
def _clean_context():
    """Ensure context is clean for every test."""
    clear_context()
    yield
    clear_context()


# ---------------------------------------------------------------------------
# 1. create_auto_task with context → has customer_id, project_id
# ---------------------------------------------------------------------------


def test_create_auto_task_with_context() -> None:
    set_context(
        customer_id="cust-abc",
        project_id="proj-xyz",
        metadata={"env": "prod"},
    )
    task = create_auto_task("resolve_ticket")

    assert task.customer_id == "cust-abc"
    assert task.project_id == "proj-xyz"
    assert task.metadata == {"env": "prod"}
    assert task.task_type == "resolve_ticket"
    assert task.status == "pending"


# ---------------------------------------------------------------------------
# 2. create_auto_task without context → customer_id is None
# ---------------------------------------------------------------------------


def test_create_auto_task_without_context() -> None:
    # Ensure no context is set (default ContextVar gives None)
    task = create_auto_task("generate_report")

    assert task.customer_id is None
    assert task.project_id is None
    assert task.metadata == {}
    assert task.task_type == "generate_report"


# ---------------------------------------------------------------------------
# 3. finalize_auto_task sets status, ended_at, aggregates LLM cost
# ---------------------------------------------------------------------------


def test_finalize_auto_task_llm_cost() -> None:
    task = Task(task_type="llm_task")
    event = Event(
        event_type="llm_call",
        cost_usd=Decimal("0.0042"),
        input_tokens=100,
        output_tokens=50,
        cached_tokens=10,
    )

    finalize_auto_task(task, event, status="success")

    assert task.status == "success"
    assert task.ended_at is not None
    assert task.llm_cost_usd == Decimal("0.0042")
    assert task.total_cost_usd == Decimal("0.0042")
    assert task.total_input_tokens == 100
    assert task.total_output_tokens == 50
    assert task.total_cached_tokens == 10


def test_finalize_auto_task_external_cost() -> None:
    task = Task(task_type="external_task")
    event = Event(event_type="external_cost", cost_usd=Decimal("0.01"))

    finalize_auto_task(task, event)

    assert task.external_cost_usd == Decimal("0.01")
    assert task.total_cost_usd == Decimal("0.01")
    assert task.status == "success"


def test_finalize_auto_task_failed_status() -> None:
    task = Task(task_type="failing_task")
    event = Event(event_type="llm_call", cost_usd=Decimal("0.002"))

    finalize_auto_task(task, event, status="failed")

    assert task.status == "failed"
    assert task.failure_count == 1


def test_finalize_auto_task_retry_event() -> None:
    task = Task(task_type="retry_task")
    event = Event(
        event_type="llm_call",
        cost_usd=Decimal("0.005"),
        is_retry=True,
    )

    finalize_auto_task(task, event)

    # is_retry causes retry_count and retry_cost_usd to be incremented
    assert task.retry_count >= 1
    assert task.retry_cost_usd >= Decimal("0.005")


# ---------------------------------------------------------------------------
# 4. needs_auto_task returns True when no task is active
# ---------------------------------------------------------------------------


def test_needs_auto_task_when_no_task() -> None:
    # By default there is no active task
    assert needs_auto_task() is True


# ---------------------------------------------------------------------------
# 5. needs_auto_task returns False when a task is active
# ---------------------------------------------------------------------------


def test_needs_auto_task_when_task_active() -> None:
    task = Task(task_type="active_task")
    with task_context(task):
        assert needs_auto_task() is False

    # After the context exits the task is gone
    assert needs_auto_task() is True
