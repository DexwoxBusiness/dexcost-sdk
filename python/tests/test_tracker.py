"""Tests for CostTracker task-tracking decorator (US-007), context manager (US-008),
and manual start/end (US-009)."""

from __future__ import annotations

import asyncio
import gc
import uuid
import warnings
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import pytest

from dexcost.context import get_current_task
from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker, TrackedTask

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    """Create a fresh SQLite storage for each test."""
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    """Create a CostTracker backed by the tmp-based storage."""
    return CostTracker(storage=storage, auto_instrument=[])


def _insert_llm_event(
    storage: SQLiteStorage,
    task: Task,
    *,
    cost: str = "0.05",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cached_tokens: int = 0,
    is_retry: bool = False,
    retry_reason: str | None = None,
) -> Event:
    """Helper to insert an LLM event for a given task."""
    event = Event(
        task_id=task.task_id,
        event_type="llm_call",
        cost_usd=Decimal(cost),
        cost_confidence="exact",
        pricing_source="provider_response",
        provider="openai",
        model="gpt-4",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        is_retry=is_retry,
        retry_reason=retry_reason,
    )
    storage.insert_event(event)
    return event


# ---------------------------------------------------------------------------
# Sync decorator tests
# ---------------------------------------------------------------------------


class TestTrackTaskSync:
    """with tracker.task() on synchronous code."""

    def test_success_records_task(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="resolve_ticket"):
            pass

        tasks = storage.query_tasks(task_type="resolve_ticket")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.status == "success"
        assert task.ended_at is not None
        assert task.task_type == "resolve_ticket"

    def test_failure_records_task(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with pytest.raises(ValueError, match="boom"):
            with tracker.task(task_type="failing_task"):
                raise ValueError("boom")

        tasks = storage.query_tasks(task_type="failing_task")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.status == "failed"
        assert task.ended_at is not None
        assert task.failure_count == 1

    def test_returns_value_via_variable(self, tracker: CostTracker) -> None:
        result = None
        with tracker.task(task_type="identity"):
            result = 21 * 2

        assert result == 42

    def test_kwargs_passed_to_task(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(
            task_type="with_kwargs",
            customer_id="acme",
            project_id="proj-1",
            metadata={"tier": "premium"},
        ):
            pass

        tasks = storage.query_tasks(task_type="with_kwargs")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.customer_id == "acme"
        assert task.project_id == "proj-1"
        assert task.metadata == {"tier": "premium"}

    def test_sets_context_during_execution(self, tracker: CostTracker) -> None:
        observed: list[Task | None] = []

        with tracker.task(task_type="context_check"):
            observed.append(get_current_task())

        assert observed[0] is not None
        assert observed[0].task_type == "context_check"
        # Context should be cleaned up after
        assert get_current_task() is None


# ---------------------------------------------------------------------------
# Async decorator tests
# ---------------------------------------------------------------------------


class TestTrackTaskAsync:
    """async with tracker.task() interface."""

    def test_async_success(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        async def run() -> None:
            async with tracker.task(task_type="async_resolve"):
                pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_resolve")
        assert len(tasks) == 1
        assert tasks[0].status == "success"
        assert tasks[0].ended_at is not None

    def test_async_failure(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        async def run() -> None:
            async with tracker.task(task_type="async_fail"):
                raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_fail")
        assert len(tasks) == 1
        assert tasks[0].status == "failed"
        assert tasks[0].failure_count == 1

    def test_async_returns_value_via_variable(self, tracker: CostTracker) -> None:
        result = None

        async def run() -> None:
            nonlocal result
            async with tracker.task(task_type="async_val"):
                result = 9 + 1

        asyncio.run(run())
        assert result == 10

    def test_async_sets_context(self, tracker: CostTracker) -> None:
        observed: list[Task | None] = []

        async def run() -> None:
            async with tracker.task(task_type="async_ctx"):
                observed.append(get_current_task())

        asyncio.run(run())
        assert observed[0] is not None
        assert observed[0].task_type == "async_ctx"

    def test_async_kwargs(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        async def run() -> None:
            async with tracker.task(
                task_type="async_kw",
                customer_id="beta",
                project_id="proj-2",
                metadata={"env": "staging"},
            ):
                pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_kw")
        task = tasks[0]
        assert task.customer_id == "beta"
        assert task.project_id == "proj-2"
        assert task.metadata == {"env": "staging"}


# ---------------------------------------------------------------------------
# Cost aggregation tests
# ---------------------------------------------------------------------------


class TestCostAggregation:
    """Aggregated cost fields are computed from events."""

    def test_llm_costs_aggregated(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="llm_agg") as tracked:
            task = tracked.task
            _insert_llm_event(storage, task, cost="0.05", input_tokens=100, output_tokens=50)
            _insert_llm_event(storage, task, cost="0.03", input_tokens=80, output_tokens=30)

        tasks = storage.query_tasks(task_type="llm_agg")
        task = tasks[0]
        assert task.llm_cost_usd == Decimal("0.08")
        assert task.total_cost_usd == Decimal("0.08")
        assert task.total_input_tokens == 180
        assert task.total_output_tokens == 80

    def test_mixed_event_types(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="mixed") as tracked:
            task = tracked.task
            _insert_llm_event(storage, task, cost="0.10", input_tokens=200, output_tokens=100)
            # External cost event
            ext_event = Event(
                task_id=task.task_id,
                event_type="external_cost",
                cost_usd=Decimal("0.25"),
                service_name="google_search",
            )
            storage.insert_event(ext_event)
            # Compute cost event
            comp_event = Event(
                task_id=task.task_id,
                event_type="compute_cost",
                cost_usd=Decimal("0.02"),
                service_name="gpu_inference",
            )
            storage.insert_event(comp_event)

        tasks = storage.query_tasks(task_type="mixed")
        task = tasks[0]
        assert task.llm_cost_usd == Decimal("0.10")
        assert task.external_cost_usd == Decimal("0.25")
        assert task.compute_cost_usd == Decimal("0.02")
        assert task.total_cost_usd == Decimal("0.37")

    def test_retry_tracking(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="retries") as tracked:
            task = tracked.task
            _insert_llm_event(storage, task, cost="0.05")
            _insert_llm_event(storage, task, cost="0.05", is_retry=True, retry_reason="rate_limit")

        tasks = storage.query_tasks(task_type="retries")
        task = tasks[0]
        assert task.retry_count == 1
        assert task.retry_cost_usd == Decimal("0.05")
        assert task.total_cost_usd == Decimal("0.10")

    def test_no_events_zeroes(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="no_events"):
            pass

        tasks = storage.query_tasks(task_type="no_events")
        task = tasks[0]
        assert task.total_cost_usd == Decimal("0")
        assert task.llm_cost_usd == Decimal("0")

    def test_failure_still_aggregates(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with pytest.raises(RuntimeError), tracker.task(task_type="fail_agg") as tracked:
            task = tracked.task
            _insert_llm_event(storage, task, cost="0.07")
            raise RuntimeError("mid-task failure")

        tasks = storage.query_tasks(task_type="fail_agg")
        task = tasks[0]
        assert task.status == "failed"
        assert task.llm_cost_usd == Decimal("0.07")
        assert task.total_cost_usd == Decimal("0.07")


# ---------------------------------------------------------------------------
# Nested tracking tests
# ---------------------------------------------------------------------------


class TestNestedTracking:
    """Nested context managers link via parent_task_id."""

    def test_nested_sync(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="parent_task"):
            with tracker.task(task_type="child_task"):
                pass

        parent_tasks = storage.query_tasks(task_type="parent_task")
        child_tasks = storage.query_tasks(task_type="child_task")
        assert len(parent_tasks) == 1
        assert len(child_tasks) == 1
        assert child_tasks[0].parent_task_id == parent_tasks[0].task_id

    def test_nested_async(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        async def run() -> None:
            async with tracker.task(task_type="async_parent"):
                async with tracker.task(task_type="async_child"):
                    pass

        asyncio.run(run())

        parent_tasks = storage.query_tasks(task_type="async_parent")
        child_tasks = storage.query_tasks(task_type="async_child")
        assert len(parent_tasks) == 1
        assert len(child_tasks) == 1
        assert child_tasks[0].parent_task_id == parent_tasks[0].task_id


# ---------------------------------------------------------------------------
# Integration test (simulated OpenAI call)
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end: context manager records events, task aggregates them."""

    def test_simulated_openai_call(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Simulates an OpenAI call by inserting an LLM event inside the
        task context manager, then verifies the task and event are persisted
        with correct aggregated costs."""

        with tracker.task(
            task_type="resolve_ticket",
            customer_id="acme-corp",
            project_id="support-bot",
            metadata={"ticket_id": "T-1234"},
        ) as tracked:
            current_task = tracked.task

            # Simulate an OpenAI API call event
            event = Event(
                task_id=current_task.task_id,
                event_type="llm_call",
                cost_usd=Decimal("0.0032"),
                cost_confidence="exact",
                pricing_source="provider_response",
                provider="openai",
                model="gpt-4",
                input_tokens=150,
                output_tokens=75,
                cached_tokens=10,
                latency_ms=420,
            )
            storage.insert_event(event)

        # Task persisted correctly
        tasks = storage.query_tasks(task_type="resolve_ticket")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.status == "success"
        assert task.customer_id == "acme-corp"
        assert task.project_id == "support-bot"
        assert task.metadata == {"ticket_id": "T-1234"}
        assert task.ended_at is not None
        assert task.started_at is not None

        # Costs aggregated from events
        assert task.llm_cost_usd == Decimal("0.0032")
        assert task.total_cost_usd == Decimal("0.0032")
        assert task.total_input_tokens == 150
        assert task.total_output_tokens == 75
        assert task.total_cached_tokens == 10

        # Event persisted
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].provider == "openai"
        assert events[0].model == "gpt-4"


# ---------------------------------------------------------------------------
# Public API export tests
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """CostTracker is accessible from the top-level package."""

    def test_cost_tracker_exported(self) -> None:
        import dexcost

        assert dexcost.CostTracker is CostTracker

    def test_tracked_task_exported(self) -> None:
        import dexcost

        assert dexcost.TrackedTask is TrackedTask


# ---------------------------------------------------------------------------
# Context manager sync tests (US-008)
# ---------------------------------------------------------------------------


class TestTaskContextManagerSync:
    """with tracker.task(...) as task: interface."""

    def test_sync_success(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="cm_resolve"):
            pass

        tasks = storage.query_tasks(task_type="cm_resolve")
        assert len(tasks) == 1
        t = tasks[0]
        assert t.status == "success"
        assert t.ended_at is not None

    def test_sync_failure(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with pytest.raises(ValueError, match="cm_boom"), tracker.task(task_type="cm_fail"):
            raise ValueError("cm_boom")

        tasks = storage.query_tasks(task_type="cm_fail")
        assert len(tasks) == 1
        t = tasks[0]
        assert t.status == "failed"
        assert t.failure_count == 1
        assert t.ended_at is not None

    def test_sync_record_cost(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="cm_cost") as task:
            event = task.record_cost(service="google_maps", cost_usd="0.005")

        assert event.event_type == "external_cost"
        assert event.cost_usd == Decimal("0.005")
        assert event.service_name == "google_maps"
        assert event.pricing_source == "manual"

        tasks = storage.query_tasks(task_type="cm_cost")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.005")
        assert t.total_cost_usd == Decimal("0.005")

    def test_sync_record_cost_with_details(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        with tracker.task(task_type="cm_cost_details") as task:
            task.record_cost(
                service="ocr_api",
                cost_usd=Decimal("0.015"),
                details={"pages": 3},
            )

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details == {"pages": 3}

    def test_sync_record_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        tracker.register_rate("google_maps_geocode", per="request", cost_usd="0.005")

        with tracker.task(task_type="cm_usage") as task:
            event = task.record_usage(service="google_maps_geocode", units=3)

        assert event.cost_usd == Decimal("0.015")
        assert event.service_name == "google_maps_geocode"

        tasks = storage.query_tasks(task_type="cm_usage")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.015")
        assert t.total_cost_usd == Decimal("0.015")

    def test_sync_record_usage_no_rate(self, tracker: CostTracker) -> None:
        with (
            tracker.task(task_type="cm_no_rate") as task,
            pytest.raises(ValueError, match=r"No rate registered.*register_rate"),
        ):
            task.record_usage(service="unknown_service", units=1)

    def test_sync_mark_retry(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="cm_retry") as task:
            event = task.mark_retry(reason="rate_limit")

        assert event.event_type == "retry_marker"
        assert event.is_retry is True
        assert event.retry_reason == "rate_limit"
        assert event.cost_usd == Decimal("0")

        tasks = storage.query_tasks(task_type="cm_retry")
        t = tasks[0]
        assert t.retry_count == 1

    def test_sync_mark_retry_with_cost(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="cm_retry_cost") as task:
            task.mark_retry(reason="timeout", cost_usd="0.02")

        tasks = storage.query_tasks(task_type="cm_retry_cost")
        t = tasks[0]
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.02")
        assert t.total_cost_usd == Decimal("0.02")

    def test_sync_task_id_accessible(self, tracker: CostTracker) -> None:
        with tracker.task(task_type="cm_id") as task:
            assert isinstance(task.task_id, uuid.UUID)

    def test_sync_sets_context(self, tracker: CostTracker) -> None:
        observed: list[Task | None] = []

        with tracker.task(task_type="cm_ctx"):
            observed.append(get_current_task())

        assert observed[0] is not None
        assert observed[0].task_type == "cm_ctx"
        # Context cleaned up after exit
        assert get_current_task() is None

    def test_sync_kwargs(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(
            task_type="cm_kw",
            customer_id="acme",
            project_id="proj-1",
            metadata={"env": "test"},
        ):
            pass

        tasks = storage.query_tasks(task_type="cm_kw")
        t = tasks[0]
        assert t.customer_id == "acme"
        assert t.project_id == "proj-1"
        assert t.metadata == {"env": "test"}

    def test_sync_nested(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="cm_parent"):  # noqa: SIM117
            with tracker.task(task_type="cm_child"):
                pass

        parent_tasks = storage.query_tasks(task_type="cm_parent")
        child_tasks = storage.query_tasks(task_type="cm_child")
        assert len(parent_tasks) == 1
        assert len(child_tasks) == 1
        assert child_tasks[0].parent_task_id == parent_tasks[0].task_id


# ---------------------------------------------------------------------------
# Context manager async tests (US-008)
# ---------------------------------------------------------------------------


class TestTaskContextManagerAsync:
    """async with tracker.task(...) as task: interface."""

    def test_async_success(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        async def run() -> None:
            async with tracker.task(task_type="acm_resolve"):
                pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="acm_resolve")
        assert len(tasks) == 1
        assert tasks[0].status == "success"
        assert tasks[0].ended_at is not None

    def test_async_failure(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        async def run() -> None:
            async with tracker.task(task_type="acm_fail"):
                raise RuntimeError("async_cm_boom")

        with pytest.raises(RuntimeError, match="async_cm_boom"):
            asyncio.run(run())

        tasks = storage.query_tasks(task_type="acm_fail")
        assert len(tasks) == 1
        assert tasks[0].status == "failed"
        assert tasks[0].failure_count == 1

    def test_async_record_cost(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        async def run() -> None:
            async with tracker.task(task_type="acm_cost") as task:
                task.record_cost(service="s3_upload", cost_usd="0.001")

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="acm_cost")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.001")
        assert t.total_cost_usd == Decimal("0.001")

    def test_async_sets_context(self, tracker: CostTracker) -> None:
        observed: list[Task | None] = []

        async def run() -> None:
            async with tracker.task(task_type="acm_ctx"):
                observed.append(get_current_task())

        asyncio.run(run())
        assert observed[0] is not None
        assert observed[0].task_type == "acm_ctx"

    def test_async_record_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        tracker.register_rate("textract", per="page", cost_usd="0.015")

        async def run() -> None:
            async with tracker.task(task_type="acm_usage") as task:
                task.record_usage(service="textract", units=5)

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="acm_usage")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.075")
        assert t.total_cost_usd == Decimal("0.075")

    def test_async_nested(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        async def run() -> None:
            async with tracker.task(task_type="acm_parent"):  # noqa: SIM117
                async with tracker.task(task_type="acm_child"):
                    pass

        asyncio.run(run())

        parent_tasks = storage.query_tasks(task_type="acm_parent")
        child_tasks = storage.query_tasks(task_type="acm_child")
        assert len(parent_tasks) == 1
        assert len(child_tasks) == 1
        assert child_tasks[0].parent_task_id == parent_tasks[0].task_id


# ---------------------------------------------------------------------------
# Cost aggregation with context manager (US-008)
# ---------------------------------------------------------------------------


class TestCostAggregationContextManager:
    """Aggregated costs computed on context exit."""

    def test_aggregates_on_exit(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with tracker.task(task_type="cm_agg") as task:
            # LLM event
            _insert_llm_event(storage, task.task, cost="0.10", input_tokens=200, output_tokens=100)
            # External cost via record_cost
            task.record_cost(service="geocode", cost_usd="0.005")
            # Retry marker
            task.mark_retry(reason="rate_limit", cost_usd="0.01")

        tasks = storage.query_tasks(task_type="cm_agg")
        t = tasks[0]
        assert t.llm_cost_usd == Decimal("0.10")
        assert t.external_cost_usd == Decimal("0.005")
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.01")
        assert t.total_cost_usd == Decimal("0.115")
        assert t.total_input_tokens == 200
        assert t.total_output_tokens == 100

    def test_failure_still_aggregates(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with pytest.raises(RuntimeError), tracker.task(task_type="cm_fail_agg") as task:
            task.record_cost(service="ocr", cost_usd="0.03")
            raise RuntimeError("mid-task failure")

        tasks = storage.query_tasks(task_type="cm_fail_agg")
        t = tasks[0]
        assert t.status == "failed"
        assert t.external_cost_usd == Decimal("0.03")
        assert t.total_cost_usd == Decimal("0.03")


# ---------------------------------------------------------------------------
# Manual start/end tests (US-009)
# ---------------------------------------------------------------------------


class TestManualStartEnd:
    """tracker.start_task() / task.end() interface."""

    def test_start_task_returns_tracked_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="manual_resolve")
        assert isinstance(task, TrackedTask)
        assert isinstance(task.task_id, uuid.UUID)
        task.end()

    def test_start_task_sets_context(self, tracker: CostTracker) -> None:
        task = tracker.start_task(task_type="manual_ctx")
        assert get_current_task() is not None
        assert get_current_task() is task.task
        task.end()

    def test_start_task_with_kwargs(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(
            task_type="manual_kw",
            customer_id="acme",
            project_id="proj-1",
            metadata={"env": "test"},
        )
        task.end()

        tasks = storage.query_tasks(task_type="manual_kw")
        assert len(tasks) == 1
        t = tasks[0]
        assert t.customer_id == "acme"
        assert t.project_id == "proj-1"
        assert t.metadata == {"env": "test"}

    def test_end_success(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="manual_success")
        task.end(status="success")

        tasks = storage.query_tasks(task_type="manual_success")
        assert len(tasks) == 1
        t = tasks[0]
        assert t.status == "success"
        assert t.ended_at is not None

    def test_end_failed(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="manual_fail")
        task.end(status="failed")

        tasks = storage.query_tasks(task_type="manual_fail")
        assert len(tasks) == 1
        t = tasks[0]
        assert t.status == "failed"
        assert t.failure_count == 1
        assert t.ended_at is not None

    def test_end_resets_context(self, tracker: CostTracker) -> None:
        assert get_current_task() is None
        task = tracker.start_task(task_type="manual_ctx_reset")
        assert get_current_task() is not None
        task.end()
        assert get_current_task() is None

    def test_end_called_twice_raises(self, tracker: CostTracker) -> None:
        task = tracker.start_task(task_type="manual_double_end")
        task.end()
        with pytest.raises(RuntimeError, match="already been ended"):
            task.end()

    def test_record_cost_works(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="manual_cost")
        event = task.record_cost(service="google_maps", cost_usd="0.005")
        task.end()

        assert event.event_type == "external_cost"
        assert event.cost_usd == Decimal("0.005")

        tasks = storage.query_tasks(task_type="manual_cost")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.005")
        assert t.total_cost_usd == Decimal("0.005")

    def test_record_usage_works(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        tracker.register_rate("geocode_api", per="request", cost_usd="0.005")
        task = tracker.start_task(task_type="manual_usage")
        event = task.record_usage(service="geocode_api", units=3)
        task.end()

        assert event.cost_usd == Decimal("0.015")
        tasks = storage.query_tasks(task_type="manual_usage")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.015")

    def test_mark_retry_works(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="manual_retry")
        event = task.mark_retry(reason="rate_limit", cost_usd="0.01")
        task.end()

        assert event.is_retry is True
        assert event.retry_reason == "rate_limit"

        tasks = storage.query_tasks(task_type="manual_retry")
        t = tasks[0]
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.01")

    def test_task_id_passable(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """task_id can be used to insert events externally (cross-process)."""
        task = tracker.start_task(task_type="manual_passable")
        tid = task.task_id

        # Simulate another function/process inserting an event by task_id
        ext_event = Event(
            task_id=tid,
            event_type="external_cost",
            cost_usd=Decimal("0.50"),
            service_name="stripe_api",
        )
        storage.insert_event(ext_event)

        task.end()

        tasks = storage.query_tasks(task_type="manual_passable")
        t = tasks[0]
        assert t.external_cost_usd == Decimal("0.50")
        assert t.total_cost_usd == Decimal("0.50")

    def test_nested_manual_tasks(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        parent = tracker.start_task(task_type="manual_parent")
        child = tracker.start_task(task_type="manual_child")
        child.end()
        parent.end()

        parent_tasks = storage.query_tasks(task_type="manual_parent")
        child_tasks = storage.query_tasks(task_type="manual_child")
        assert len(parent_tasks) == 1
        assert len(child_tasks) == 1
        assert child_tasks[0].parent_task_id == parent_tasks[0].task_id

    def test_end_aggregates_all_costs(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="manual_agg")
        task.record_llm_call("openai", "gpt-4", 200, 100, "0.10")
        task.record_cost(service="geocode", cost_usd="0.005")
        task.mark_retry(reason="timeout", cost_usd="0.01")
        task.end()

        tasks = storage.query_tasks(task_type="manual_agg")
        t = tasks[0]
        assert t.llm_cost_usd == Decimal("0.10")
        assert t.external_cost_usd == Decimal("0.005")
        assert t.retry_count == 1
        assert t.retry_cost_usd == Decimal("0.01")
        assert t.total_cost_usd == Decimal("0.115")
        assert t.total_input_tokens == 200
        assert t.total_output_tokens == 100

    def test_end_default_status_is_success(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="manual_default")
        task.end()

        tasks = storage.query_tasks(task_type="manual_default")
        assert tasks[0].status == "success"


# ---------------------------------------------------------------------------
# record_llm_call tests (US-009)
# ---------------------------------------------------------------------------


class TestRecordLlmCall:
    """TrackedTask.record_llm_call() method."""

    def test_record_llm_call_basic(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        task = tracker.start_task(task_type="llm_basic")
        event = task.record_llm_call(
            provider="openai",
            model="gpt-4",
            input_tokens=150,
            output_tokens=75,
            cost_usd="0.003",
        )
        task.end()

        assert event.event_type == "llm_call"
        assert event.provider == "openai"
        assert event.model == "gpt-4"
        assert event.input_tokens == 150
        assert event.output_tokens == 75
        assert event.cost_usd == Decimal("0.003")
        assert event.pricing_source == "manual"
        assert event.cost_confidence == "exact"

    def test_record_llm_call_aggregated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="llm_agg_manual")
        task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")
        task.record_llm_call("anthropic", "claude-3", 200, 100, "0.08")
        task.end()

        tasks = storage.query_tasks(task_type="llm_agg_manual")
        t = tasks[0]
        assert t.llm_cost_usd == Decimal("0.13")
        assert t.total_cost_usd == Decimal("0.13")
        assert t.total_input_tokens == 300
        assert t.total_output_tokens == 150

    def test_record_llm_call_with_optional_fields(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        task = tracker.start_task(task_type="llm_opts")
        event = task.record_llm_call(
            provider="openai",
            model="gpt-4",
            input_tokens=100,
            output_tokens=50,
            cost_usd="0.005",
            cost_confidence="computed",
            cached_tokens=20,
            latency_ms=350,
            details={"request_id": "req-123"},
        )
        task.end()

        assert event.cached_tokens == 20
        assert event.latency_ms == 350
        assert event.cost_confidence == "computed"
        assert event.details == {"request_id": "req-123"}

        # Verify cached tokens aggregated
        tasks = storage.query_tasks(task_type="llm_opts")
        assert tasks[0].total_cached_tokens == 20

    def test_record_llm_call_in_context_manager(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """record_llm_call also works inside context manager tasks."""
        with tracker.task(task_type="cm_llm") as task:
            task.record_llm_call("openai", "gpt-4", 100, 50, "0.05")

        tasks = storage.query_tasks(task_type="cm_llm")
        t = tasks[0]
        assert t.llm_cost_usd == Decimal("0.05")
        assert t.total_input_tokens == 100


# ---------------------------------------------------------------------------
# GC warning tests (US-009)
# ---------------------------------------------------------------------------


class TestGCWarning:
    """Warning logged if task is garbage-collected without .end()."""

    def test_gc_warning_on_unended_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            task = tracker.start_task(task_type="gc_warn")
            # Drop reference — force GC
            del task
            gc.collect()

        resource_warnings = [x for x in w if issubclass(x.category, ResourceWarning)]
        assert len(resource_warnings) >= 1
        assert "garbage-collected without .end()" in str(resource_warnings[0].message)

    def test_no_warning_after_end(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            task = tracker.start_task(task_type="gc_no_warn")
            task.end()
            del task
            gc.collect()

        gc_warnings = [
            x
            for x in w
            if issubclass(x.category, ResourceWarning)
            and "garbage-collected without .end()" in str(x.message)
        ]
        assert len(gc_warnings) == 0

    def test_no_warning_for_context_manager(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with tracker.task(task_type="gc_cm") as task:
                pass
            del task
            gc.collect()

        gc_warnings = [
            x
            for x in w
            if issubclass(x.category, ResourceWarning)
            and "garbage-collected without .end()" in str(x.message)
        ]
        assert len(gc_warnings) == 0


# ---------------------------------------------------------------------------
# Experiment tracking (experiment_id + variant)
# ---------------------------------------------------------------------------


class TestExperimentTracking:
    def test_experiment_fields_in_context_manager_explicit(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """experiment_id and variant are recorded on the task via context manager."""
        with tracker.task(
            task_type="classify",
            experiment_id="exp-models-v1",
            variant="gpt4o-mini",
        ):
            pass
        tasks = storage.query_tasks(task_type="classify")
        assert len(tasks) == 1
        assert tasks[0].experiment_id == "exp-models-v1"
        assert tasks[0].variant == "gpt4o-mini"

    def test_experiment_fields_in_context_manager(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """experiment_id and variant work via context manager."""
        with tracker.task(
            task_type="summarise",
            experiment_id="pricing-trial",
            variant="claude-haiku",
        ) as t:
            pass
        tasks = storage.query_tasks(task_type="summarise")
        assert len(tasks) == 1
        assert tasks[0].experiment_id == "pricing-trial"
        assert tasks[0].variant == "claude-haiku"

    def test_experiment_fields_in_start_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """experiment_id and variant work via manual start_task."""
        handle = tracker.start_task(
            task_type="extract",
            experiment_id="exp-latency",
            variant="fast-model",
        )
        handle.end(status="success")
        tasks = storage.query_tasks(task_type="extract")
        assert len(tasks) == 1
        assert tasks[0].experiment_id == "exp-latency"
        assert tasks[0].variant == "fast-model"

    def test_experiment_fields_default_to_none(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Without experiment params, fields default to None."""
        with tracker.task(task_type="default_test"):
            pass
        tasks = storage.query_tasks(task_type="default_test")
        assert tasks[0].experiment_id is None
        assert tasks[0].variant is None

    def test_experiment_fields_round_trip(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Experiment fields survive insert → query round-trip."""
        with tracker.task(
            task_type="roundtrip",
            experiment_id="exp-rt",
            variant="v-alpha",
        ) as t:
            pass
        got = storage.get_task(str(t.task_id))
        assert got is not None
        assert got.experiment_id == "exp-rt"
        assert got.variant == "v-alpha"

    def test_experiment_fields_in_to_dict(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """experiment_id and variant appear in to_dict output."""
        with tracker.task(
            task_type="dict_test",
            experiment_id="exp-dict",
            variant="v-beta",
        ) as t:
            pass
        task = storage.get_task(str(t.task_id))
        assert task is not None
        d = task.to_dict()
        assert d["experiment_id"] == "exp-dict"
        assert d["variant"] == "v-beta"
