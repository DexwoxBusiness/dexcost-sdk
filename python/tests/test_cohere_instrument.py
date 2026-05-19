"""Tests for Cohere auto-instrumentation (US-012).

All tests use mocked Cohere SDK objects — the real ``cohere`` package is
**not** required.  We simulate the module structure that
:func:`instrument_cohere` patches so the wrapt monkey-patching works
against our fakes.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fake Cohere module hierarchy
# ---------------------------------------------------------------------------


def _make_response(
    model: str = "command-r-plus",
    input_tokens: int = 100,
    output_tokens: int = 50,
    usage_present: bool = True,
) -> MagicMock:
    """Build a mock Cohere chat response."""
    resp = MagicMock()
    resp.model = model
    if usage_present:
        meta = MagicMock()
        billed_units = MagicMock()
        billed_units.input_tokens = input_tokens
        billed_units.output_tokens = output_tokens
        meta.billed_units = billed_units
        resp.meta = meta
    else:
        resp.meta = None
    return resp


def _make_stream_events(
    input_tokens: int = 100,
    output_tokens: int = 50,
    usage_present: bool = True,
) -> list[MagicMock]:
    """Build a list of mock Cohere chat-stream events.

    The list ends with a ``stream-end`` event carrying the full response
    (with ``meta.billed_units``) when *usage_present* is True.
    """
    text_event = MagicMock()
    text_event.event_type = "text-generation"
    events: list[MagicMock] = [text_event]

    end_event = MagicMock()
    end_event.event_type = "stream-end"
    if usage_present:
        response = MagicMock()
        meta = MagicMock()
        billed_units = MagicMock()
        billed_units.input_tokens = input_tokens
        billed_units.output_tokens = output_tokens
        meta.billed_units = billed_units
        response.meta = meta
        end_event.response = response
    else:
        end_event.response = None
    events.append(end_event)
    return events


def _install_fake_cohere() -> tuple[type, type]:
    """Install a fake ``cohere`` package into ``sys.modules``.

    Returns the sync ``Client`` class and async ``AsyncClient`` class
    so tests can set ``.chat`` behaviour.
    """
    cohere = types.ModuleType("cohere")

    class Client:
        @staticmethod
        def chat(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

        @staticmethod
        def chat_stream(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

    class AsyncClient:
        @staticmethod
        async def chat(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

        @staticmethod
        def chat_stream(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

    cohere.Client = Client  # type: ignore[attr-defined]
    cohere.AsyncClient = AsyncClient  # type: ignore[attr-defined]

    sys.modules["cohere"] = cohere

    return Client, AsyncClient  # type: ignore[return-value]


def _uninstall_fake_cohere() -> None:
    """Remove our fake cohere modules from ``sys.modules``.

    Sets each key to ``None`` so that any subsequent ``import cohere``
    raises ``ImportError`` immediately, correctly simulating a missing package
    even when the real cohere wheel is present in site-packages.
    """
    for key in list(sys.modules):
        if key == "cohere" or key.startswith("cohere."):
            sys.modules[key] = None  # type: ignore[assignment]


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
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _fake_cohere() -> Generator[None, None, None]:
    """Install/uninstall fake cohere for every test and ensure uninstrument."""
    _install_fake_cohere()
    yield
    # Always uninstrument after each test to reset module-level state
    from dexcost.instruments.cohere import uninstrument_cohere

    uninstrument_cohere()
    _uninstall_fake_cohere()


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


class TestSyncChat:
    """Sync cohere.Client.chat() tests."""

    def test_sync_records_event(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Mocked Cohere sync chat call inside tracked task records event."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response(
            model="command-r-plus",
            input_tokens=150,
            output_tokens=75,
        )
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        with tracker.task(task_type="sync_usage") as task:
            result = Client.chat(model="command-r-plus", message="Hello")

        assert result is response

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "cohere"
        assert ev.model == "command-r-plus"
        assert ev.input_tokens == 150
        assert ev.output_tokens == 75
        assert ev.cost_confidence == "exact"
        assert ev.cost_usd >= Decimal("0")

    def test_tokens_from_billed_units(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Token usage is extracted from response.meta.billed_units."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response(
            model="command-r-plus",
            input_tokens=300,
            output_tokens=120,
        )
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        with tracker.task(task_type="sync_billed") as task:
            Client.chat(model="command-r-plus", message="Hello world")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.input_tokens == 300
        assert ev.output_tokens == 120

    def test_missing_usage_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When response.meta is None, cost_confidence should be 'estimated'."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response(usage_present=False)
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        with tracker.task(task_type="sync_no_usage") as task:
            Client.chat(model="command-r-plus", message="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.cost_usd == Decimal("0")
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0

    def test_latency_recorded(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """latency_ms is populated on the event."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response()
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        with tracker.task(task_type="sync_latency") as task:
            Client.chat(model="command-r-plus", message="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].latency_ms is not None
        assert events[0].latency_ms >= 0

    def test_model_from_kwargs(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Model name is taken from the request kwargs."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response(model="command-r")
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        with tracker.task(task_type="sync_model") as task:
            Client.chat(model="command-r", message="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].model == "command-r"


# ---------------------------------------------------------------------------
# Streaming tests (Fix 2)
# ---------------------------------------------------------------------------


class TestStreamingChat:
    """Sync cohere.Client.chat_stream() — streamed cost capture."""

    def test_streaming_records_event_with_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """A streamed Cohere call records an llm_call event with token usage."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        events_stream = _make_stream_events(input_tokens=140, output_tokens=70)
        Client.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: iter(events_stream)
        )

        instrument_cohere(tracker)

        with tracker.task(task_type="stream_usage") as task:
            stream = Client.chat_stream(model="command-r-plus", message="Hello")
            collected = list(stream)

        assert len(collected) == 2

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "cohere"
        assert ev.model == "command-r-plus"
        assert ev.input_tokens == 140
        assert ev.output_tokens == 70
        assert ev.cost_confidence == "exact"

    def test_streaming_without_usage_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """A streamed call without billed_units records an estimated event."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        events_stream = _make_stream_events(usage_present=False)
        Client.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: iter(events_stream)
        )

        instrument_cohere(tracker)

        with tracker.task(task_type="stream_no_usage") as task:
            list(Client.chat_stream(model="command-r-plus", message="Hi"))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0


# ---------------------------------------------------------------------------
# Passthrough (no active task) tests
# ---------------------------------------------------------------------------


class TestPassthrough:
    """When no explicit task context is active, calls create an auto-task."""

    def test_no_task_context_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response()
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        result = Client.chat(model="command-r-plus", message="Hello")

        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        all_events = storage.query_events()
        assert len(all_events) >= 1


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------


class TestAsyncChat:
    """Async cohere.AsyncClient.chat() tests."""

    def test_async_records_event(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        from cohere import AsyncClient

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response(
            model="command-r-plus",
            input_tokens=200,
            output_tokens=80,
        )

        async def fake_chat(**kwargs: Any) -> Any:
            return response

        AsyncClient.chat = staticmethod(fake_chat)  # type: ignore[assignment]

        instrument_cohere(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_usage"):
                result = await AsyncClient.chat(model="command-r-plus", message="Hello")
                assert result is response

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_usage")
        assert len(tasks) == 1
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "cohere"
        assert ev.model == "command-r-plus"
        assert ev.input_tokens == 200
        assert ev.output_tokens == 80
        assert ev.cost_confidence == "exact"

    def test_async_missing_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        from cohere import AsyncClient

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response(usage_present=False)

        async def fake_chat(**kwargs: Any) -> Any:
            return response

        AsyncClient.chat = staticmethod(fake_chat)  # type: ignore[assignment]

        instrument_cohere(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_no_usage"):
                await AsyncClient.chat(model="command-r-plus", message="Hello")

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_no_usage")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert events[0].cost_confidence == "estimated"

    def test_async_no_task_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import AsyncClient

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response()

        async def fake_chat(**kwargs: Any) -> Any:
            return response

        AsyncClient.chat = staticmethod(fake_chat)  # type: ignore[assignment]

        instrument_cohere(tracker)

        async def run() -> Any:
            return await AsyncClient.chat(model="command-r-plus", message="Hello")

        result = asyncio.run(run())
        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        assert len(storage.query_events()) >= 1


# ---------------------------------------------------------------------------
# Instrument / uninstrument lifecycle tests
# ---------------------------------------------------------------------------


class TestInstrumentLifecycle:
    """instrument_cohere / uninstrument_cohere lifecycle."""

    def test_double_instrument_raises(self, tracker: CostTracker) -> None:
        from dexcost.instruments.cohere import instrument_cohere

        instrument_cohere(tracker)
        with pytest.raises(RuntimeError, match="already active"):
            instrument_cohere(tracker)

    def test_uninstrument_restores_original(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere, uninstrument_cohere

        original_chat = Client.chat

        response = _make_response()
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        # Verify it's patched (the chat method should be wrapped)
        assert Client.chat is not original_chat  # type: ignore[comparison-overlap]

        uninstrument_cohere()

        # After uninstrument, should be able to instrument again
        instrument_cohere(tracker)

    def test_uninstrument_when_not_patched_is_noop(self) -> None:
        from dexcost.instruments.cohere import uninstrument_cohere

        # Should not raise
        uninstrument_cohere()

    def test_missing_cohere_raises_import_error(self, tracker: CostTracker) -> None:
        """instrument_cohere raises ImportError if cohere is not installed."""
        from dexcost.instruments.cohere import instrument_cohere

        _uninstall_fake_cohere()

        blocked = {k: None for k in list(sys.modules) if k == "cohere" or k.startswith("cohere.")}
        blocked.setdefault("cohere", None)

        with patch.dict(sys.modules, blocked):
            with pytest.raises(ImportError, match="cohere"):
                instrument_cohere(tracker)

        # Re-install for cleanup
        _install_fake_cohere()


# ---------------------------------------------------------------------------
# Cost calculation integration tests
# ---------------------------------------------------------------------------


class TestCostCalculation:
    """Verify the pricing engine is used to calculate costs."""

    def test_cost_calculated_via_pricing_engine(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """With usage present, cost should be computed by the pricing engine."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response(
            model="command-r-plus",
            input_tokens=1000,
            output_tokens=500,
        )
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        with tracker.task(task_type="cost_calc") as task:
            Client.chat(model="command-r-plus", message="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        ev = events[0]
        # The pricing engine should have set a pricing_source
        assert ev.pricing_source is not None
        assert ev.pricing_source != "unknown"
        # cost_usd should be non-negative
        assert ev.cost_usd >= Decimal("0")


# ---------------------------------------------------------------------------
# Task aggregation integration tests
# ---------------------------------------------------------------------------


class TestTaskAggregation:
    """Auto-captured events are included in task cost aggregation."""

    def test_auto_captured_event_aggregated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        response = _make_response(
            model="command-r-plus",
            input_tokens=200,
            output_tokens=100,
        )
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_cohere(tracker)

        with tracker.task(task_type="agg_test") as task:
            Client.chat(model="command-r-plus", message="Hello")
            Client.chat(model="command-r-plus", message="World")

        tasks = storage.query_tasks(task_type="agg_test")
        t = tasks[0]
        assert t.total_input_tokens == 400
        assert t.total_output_tokens == 200
        assert t.llm_cost_usd >= Decimal("0")
        assert t.total_cost_usd >= Decimal("0")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 2


# ---------------------------------------------------------------------------
# Public API export tests
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """instrument_cohere / uninstrument_cohere accessible from top-level package."""

    def test_instrument_cohere_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "instrument_cohere")
        assert callable(dexcost.instrument_cohere)

    def test_uninstrument_cohere_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "uninstrument_cohere")
        assert callable(dexcost.uninstrument_cohere)
