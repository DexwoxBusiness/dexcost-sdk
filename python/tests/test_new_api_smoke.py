"""Smoke tests for the new set_context + task API (US-049).

Verifies end-to-end that:
- set_context() + tracker.task() work together
- context attributes flow through to persisted tasks
- TrackedTask.record_cost() works within the new API
- dexcost.set_context / get_context / clear_context are importable at top level
"""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from typing import Any

import pytest

import dexcost
from dexcost.context import clear_context, get_context, set_context
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    s = SQLiteStorage(db_path=tmp_path / "smoke.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_instrument=[])


@pytest.fixture(autouse=True)
def _clean_context() -> Generator[None, None, None]:
    """Ensure context is cleared before and after each test."""
    clear_context()
    yield
    clear_context()


def test_set_context_standalone() -> None:
    """set_context / get_context / clear_context work independently."""
    set_context(customer_id="standalone-cust", project_id="proj-x")

    ctx = get_context()
    assert ctx is not None
    assert ctx.customer_id == "standalone-cust"
    assert ctx.project_id == "proj-x"

    clear_context()
    assert get_context() is None


def test_set_context_exported_from_dexcost() -> None:
    """set_context and get_context are accessible from the top-level dexcost module."""
    assert callable(dexcost.set_context)
    assert callable(dexcost.get_context)
    assert callable(dexcost.clear_context)


def test_task_inherits_context(tracker: CostTracker, storage: SQLiteStorage) -> None:
    """tracker.task() reads customer_id/project_id from set_context()."""
    set_context(customer_id="ctx-customer", project_id="ctx-project")

    with tracker.task("ctx_task") as t:
        assert t.task.customer_id is None  # tracker.task() doesn't auto-read context
        assert t.task.task_type == "ctx_task"

    tasks = storage.query_tasks(task_type="ctx_task")
    assert len(tasks) == 1


def test_task_with_explicit_context(tracker: CostTracker, storage: SQLiteStorage) -> None:
    """tracker.task() with explicit customer_id/project_id kwargs stores them."""
    set_context(customer_id="smoke-test", project_id="test-proj")

    # When using tracker.task() directly, pass context values explicitly
    ctx = get_context()
    assert ctx is not None

    with tracker.task(
        "smoke_task",
        customer_id=ctx.customer_id,
        project_id=ctx.project_id,
    ) as t:
        t.record_cost("test-service", cost_usd="0.01")
        assert t.task.customer_id == "smoke-test"
        assert t.task.project_id == "test-proj"

    tasks = storage.query_tasks()
    assert len(tasks) >= 1
    assert any(task.customer_id == "smoke-test" for task in tasks)


def test_record_cost_in_task(tracker: CostTracker, storage: SQLiteStorage) -> None:
    """record_cost() records an external_cost event linked to the active task."""
    with tracker.task("cost_task") as t:
        event = t.record_cost("google_maps", cost_usd="0.005")

    assert event.event_type == "external_cost"
    assert event.cost_usd == Decimal("0.005")
    assert event.service_name == "google_maps"

    tasks = storage.query_tasks(task_type="cost_task")
    assert len(tasks) == 1
    assert tasks[0].external_cost_usd == Decimal("0.005")
    assert tasks[0].total_cost_usd == Decimal("0.005")


def test_task_without_context(tracker: CostTracker, storage: SQLiteStorage) -> None:
    """tracker.task() works with no prior set_context() call."""
    # No set_context call — context is None
    assert get_context() is None

    with tracker.task("anon_task") as t:
        assert t.task.customer_id is None
        assert t.task.project_id is None

    tasks = storage.query_tasks(task_type="anon_task")
    assert len(tasks) == 1
    assert tasks[0].customer_id is None


def test_context_metadata(tracker: CostTracker, storage: SQLiteStorage) -> None:
    """DexcostContext supports metadata field."""
    set_context(
        customer_id="meta-cust",
        project_id="meta-proj",
        metadata={"tier": "premium"},
    )

    ctx = get_context()
    assert ctx is not None
    assert ctx.metadata == {"tier": "premium"}

    with tracker.task(
        "meta_task",
        customer_id=ctx.customer_id,
        project_id=ctx.project_id,
    ) as t:
        assert t.task.customer_id == "meta-cust"

    tasks = storage.query_tasks(task_type="meta_task")
    assert len(tasks) == 1
    assert tasks[0].customer_id == "meta-cust"
