"""Tests for LangChain callback handler integration (US-032)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from dexcost.context import _current_task, get_current_task, set_current_task
from dexcost.integrations import DexcostCallbackHandler
from dexcost.integrations.langchain import DexcostCallbackHandler as DirectImport
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture()
def handler(tracker: CostTracker) -> DexcostCallbackHandler:
    """Create a DexcostCallbackHandler from the tracker."""
    return DexcostCallbackHandler(tracker)


@pytest.fixture()
def _task_context(storage: SQLiteStorage) -> Generator[Task, None, None]:
    """Set up and tear down a task context for tests that need one."""
    task = Task(task_type="test_task", customer_id="cust-1")
    storage.insert_task(task)
    token = set_current_task(task)
    yield task
    _current_task.reset(token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> MagicMock:
    """Build a mock LangChain LLMResult with token_usage."""
    response = MagicMock()
    response.llm_output = {
        "token_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    }
    return response


def _make_llm_response_no_usage() -> MagicMock:
    """Build a mock LangChain LLMResult without token_usage."""
    response = MagicMock()
    response.llm_output = {}
    return response


def _make_serialized(model: str = "gpt-4") -> dict[str, Any]:
    """Build a minimal serialized dict like LangChain provides."""
    return {"kwargs": {"model": model}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDexcostCallbackHandler:
    """US-032: LangChain callback handler."""

    def test_handler_captures_llm_call(
        self,
        handler: DexcostCallbackHandler,
        storage: SQLiteStorage,
        _task_context: Task,
    ) -> None:
        """on_llm_end with usage data creates an Event in storage."""
        run_id = uuid.uuid4()
        handler.on_llm_start(_make_serialized("gpt-4"), ["Hello"], run_id=run_id)
        handler.on_llm_end(_make_llm_response(), run_id=run_id)

        events = storage.query_events(task_id=str(_task_context.task_id))
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "llm_call"
        assert event.provider == "langchain"
        assert event.model == "gpt-4"
        assert event.task_id == _task_context.task_id

    def test_handler_extracts_tokens(
        self,
        handler: DexcostCallbackHandler,
        storage: SQLiteStorage,
        _task_context: Task,
    ) -> None:
        """Verify input_tokens and output_tokens extracted from mock response."""
        run_id = uuid.uuid4()
        handler.on_llm_start(_make_serialized(), ["Hi"], run_id=run_id)
        handler.on_llm_end(
            _make_llm_response(prompt_tokens=200, completion_tokens=80),
            run_id=run_id,
        )

        events = storage.query_events(task_id=str(_task_context.task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 200
        assert events[0].output_tokens == 80

    def test_handler_computes_cost(
        self,
        handler: DexcostCallbackHandler,
        storage: SQLiteStorage,
        _task_context: Task,
    ) -> None:
        """Verify cost_usd is computed from PricingEngine when tokens are available."""
        run_id = uuid.uuid4()
        handler.on_llm_start(_make_serialized("gpt-4"), ["Test"], run_id=run_id)
        handler.on_llm_end(
            _make_llm_response(prompt_tokens=100, completion_tokens=50),
            run_id=run_id,
        )

        events = storage.query_events(task_id=str(_task_context.task_id))
        assert len(events) == 1
        event = events[0]
        # Cost should be computed (not zero since tokens are present)
        assert event.cost_confidence == "computed"
        # pricing_source depends on whether gpt-4 is in the bundled map
        # but cost_usd should be a Decimal
        assert isinstance(event.cost_usd, Decimal)

    def test_handler_no_task_context(
        self,
        handler: DexcostCallbackHandler,
        storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Outside task context: no crash, warning logged, no event recorded."""
        # Ensure no task context is active
        assert get_current_task() is None

        run_id = uuid.uuid4()
        with caplog.at_level(logging.WARNING):
            handler.on_llm_start(_make_serialized(), ["Hello"], run_id=run_id)
            handler.on_llm_end(_make_llm_response(), run_id=run_id)

        assert "outside a task context" in caplog.text
        # No events should be recorded in storage
        all_events = storage.query_events()
        assert len(all_events) == 0

    def test_handler_missing_usage(
        self,
        handler: DexcostCallbackHandler,
        storage: SQLiteStorage,
        _task_context: Task,
    ) -> None:
        """Response without token_usage sets cost_confidence='unknown'."""
        run_id = uuid.uuid4()
        handler.on_llm_start(_make_serialized(), ["Hello"], run_id=run_id)
        handler.on_llm_end(_make_llm_response_no_usage(), run_id=run_id)

        events = storage.query_events(task_id=str(_task_context.task_id))
        assert len(events) == 1
        event = events[0]
        assert event.cost_confidence == "unknown"
        assert event.cost_usd == Decimal("0")
        assert event.input_tokens is None
        assert event.output_tokens is None

    def test_handler_error_event(
        self,
        handler: DexcostCallbackHandler,
        storage: SQLiteStorage,
        _task_context: Task,
    ) -> None:
        """on_llm_error creates event with error details."""
        run_id = uuid.uuid4()
        handler.on_llm_start(_make_serialized("gpt-4"), ["Test"], run_id=run_id)
        handler.on_llm_error(
            RuntimeError("API rate limit exceeded"),
            run_id=run_id,
        )

        events = storage.query_events(task_id=str(_task_context.task_id))
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "llm_call"
        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "unknown"
        assert event.details["error_type"] == "RuntimeError"
        assert "rate limit" in event.details["error"]

    def test_handler_model_extraction(
        self,
        handler: DexcostCallbackHandler,
        storage: SQLiteStorage,
        _task_context: Task,
    ) -> None:
        """Extracts model from serialized['kwargs']['model']."""
        run_id = uuid.uuid4()
        serialized: dict[str, Any] = {"kwargs": {"model": "claude-3-opus-20240229"}}
        handler.on_llm_start(serialized, ["Prompt"], run_id=run_id)
        handler.on_llm_end(_make_llm_response(), run_id=run_id)

        events = storage.query_events(task_id=str(_task_context.task_id))
        assert len(events) == 1
        assert events[0].model == "claude-3-opus-20240229"

    def test_handler_exported(self) -> None:
        """DexcostCallbackHandler is importable from dexcost.integrations."""
        from dexcost.integrations import DexcostCallbackHandler as Imported

        assert Imported is DirectImport
