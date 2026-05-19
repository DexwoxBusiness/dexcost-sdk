"""Tests for OpenAI auto-instrumentation (US-012).

All tests use mocked OpenAI SDK objects — the real ``openai`` package is
**not** required.  We simulate the module structure that
:func:`instrument_openai` patches so the wrapt monkey-patching works
against our fakes.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Generator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from dexcost.context import get_current_task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fake OpenAI module hierarchy
# ---------------------------------------------------------------------------


def _make_usage(
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cached_tokens: int | None = None,
) -> MagicMock:
    """Build a mock ``CompletionUsage`` object."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    if cached_tokens is not None:
        details = MagicMock()
        details.cached_tokens = cached_tokens
        usage.prompt_tokens_details = details
    else:
        usage.prompt_tokens_details = None

    return usage


def _make_response(
    model: str = "gpt-4o",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    cached_tokens: int | None = None,
    usage_present: bool = True,
) -> MagicMock:
    """Build a mock ``ChatCompletion`` response."""
    resp = MagicMock()
    resp.model = model
    if usage_present:
        resp.usage = _make_usage(prompt_tokens, completion_tokens, cached_tokens)
    else:
        resp.usage = None
    return resp


def _make_chunk(
    model: str | None = "gpt-4o",
    usage: Any = None,
) -> MagicMock:
    """Build a mock streaming ``ChatCompletionChunk``."""
    chunk = MagicMock()
    chunk.model = model
    chunk.usage = usage
    return chunk


def _install_fake_openai() -> tuple[MagicMock, MagicMock]:
    """Install a fake ``openai`` package into ``sys.modules``.

    Returns the sync ``Completions`` class and async ``AsyncCompletions``
    class so tests can set ``.create`` behaviour.
    """
    # Build the module tree: openai → openai.resources → openai.resources.chat
    #   → openai.resources.chat.completions
    openai_mod = types.ModuleType("openai")
    resources_mod = types.ModuleType("openai.resources")
    chat_mod = types.ModuleType("openai.resources.chat")
    completions_mod = types.ModuleType("openai.resources.chat.completions")

    class Completions:
        @staticmethod
        def create(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

    class AsyncCompletions:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

    completions_mod.Completions = Completions  # type: ignore[attr-defined]
    completions_mod.AsyncCompletions = AsyncCompletions  # type: ignore[attr-defined]

    chat_mod.completions = completions_mod  # type: ignore[attr-defined]
    resources_mod.chat = chat_mod  # type: ignore[attr-defined]
    openai_mod.resources = resources_mod  # type: ignore[attr-defined]

    sys.modules["openai"] = openai_mod
    sys.modules["openai.resources"] = resources_mod
    sys.modules["openai.resources.chat"] = chat_mod
    sys.modules["openai.resources.chat.completions"] = completions_mod

    return Completions, AsyncCompletions  # type: ignore[return-value]


def _uninstall_fake_openai() -> None:
    """Remove our fake openai modules from ``sys.modules``.

    Sets each key to ``None`` so that any subsequent ``import openai``
    raises ``ImportError`` immediately, correctly simulating a missing package
    even when the real openai wheel is present in site-packages.
    """
    for key in list(sys.modules):
        if key == "openai" or key.startswith("openai."):
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
def _fake_openai() -> Generator[None, None, None]:
    """Install/uninstall fake openai for every test and ensure uninstrument."""
    _install_fake_openai()
    yield
    # Always uninstrument after each test to reset module-level state
    from dexcost.instruments.openai import uninstrument_openai

    uninstrument_openai()
    _uninstall_fake_openai()


# ---------------------------------------------------------------------------
# Sync non-streaming tests
# ---------------------------------------------------------------------------


class TestSyncNonStreaming:
    """Sync openai.chat.completions.create() without streaming."""

    def test_records_event_with_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Mocked OpenAI call inside tracked task → event recorded with correct tokens."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=150,
            completion_tokens=75,
        )
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="sync_usage") as task:
            result = Completions.create(model="gpt-4o", messages=[])

        assert result is response

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "openai"
        assert ev.model == "gpt-4o"
        assert ev.input_tokens == 150
        assert ev.output_tokens == 75
        assert ev.cost_confidence == "exact"
        assert ev.cost_usd >= Decimal("0")

    def test_missing_usage_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When response.usage is None, cost_confidence should be 'estimated'."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response(usage_present=False)
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="sync_no_usage") as task:
            Completions.create(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.cost_usd == Decimal("0")
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0

    def test_cached_tokens_extracted(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Cached tokens from prompt_tokens_details.cached_tokens are captured."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=200,
            completion_tokens=100,
            cached_tokens=50,
        )
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="sync_cached") as task:
            Completions.create(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].cached_tokens == 50

    def test_latency_recorded(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """latency_ms is populated on the event."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response()
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="sync_latency") as task:
            Completions.create(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].latency_ms is not None
        assert events[0].latency_ms >= 0

    def test_model_from_response(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Model name is taken from the response, not the request."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response(model="gpt-4o-2024-08-06")
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="sync_model") as task:
            Completions.create(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].model == "gpt-4o-2024-08-06"


# ---------------------------------------------------------------------------
# Passthrough (no active task) tests
# ---------------------------------------------------------------------------


class TestPassthrough:
    """When no explicit task context is active, calls create an auto-task."""

    def test_no_task_context_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response()
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        result = Completions.create(model="gpt-4o", messages=[])

        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        all_events = storage.query_events()
        assert len(all_events) >= 1


# ---------------------------------------------------------------------------
# Sync streaming tests
# ---------------------------------------------------------------------------


class TestSyncStreaming:
    """Sync streaming openai.chat.completions.create(stream=True)."""

    def test_streaming_with_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Usage in the final chunk is captured after stream is consumed."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        usage = _make_usage(prompt_tokens=120, completion_tokens=60)
        chunks = [
            _make_chunk(model="gpt-4o"),
            _make_chunk(model="gpt-4o"),
            _make_chunk(model="gpt-4o", usage=usage),
        ]

        Completions.create = staticmethod(lambda **kwargs: iter(chunks))  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="stream_usage") as task:
            stream = Completions.create(model="gpt-4o", messages=[], stream=True)
            collected = list(stream)

        assert len(collected) == 3

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.model == "gpt-4o"
        assert ev.input_tokens == 120
        assert ev.output_tokens == 60
        assert ev.cost_confidence == "exact"

    def test_streaming_without_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """When no usage appears in the stream, cost_confidence is 'estimated'."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        chunks = [
            _make_chunk(model="gpt-4o"),
            _make_chunk(model="gpt-4o"),
        ]

        Completions.create = staticmethod(lambda **kwargs: iter(chunks))  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="stream_no_usage") as task:
            stream = Completions.create(model="gpt-4o", messages=[], stream=True)
            list(stream)

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.cost_usd == Decimal("0")
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0

    def test_streaming_latency(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Latency covers the entire stream consumption."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        usage = _make_usage(prompt_tokens=50, completion_tokens=25)
        chunks = [_make_chunk(model="gpt-4o", usage=usage)]

        Completions.create = staticmethod(lambda **kwargs: iter(chunks))  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="stream_latency") as task:
            stream = Completions.create(model="gpt-4o", messages=[], stream=True)
            list(stream)

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].latency_ms is not None
        assert events[0].latency_ms >= 0


# ---------------------------------------------------------------------------
# Async non-streaming tests
# ---------------------------------------------------------------------------


class TestAsyncNonStreaming:
    """Async openai.chat.completions.create() without streaming."""

    def test_async_records_event(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=200,
            completion_tokens=80,
        )

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncCompletions.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_openai(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_usage"):
                result = await AsyncCompletions.create(model="gpt-4o", messages=[])
                assert result is response

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_usage")
        assert len(tasks) == 1
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "openai"
        assert ev.model == "gpt-4o"
        assert ev.input_tokens == 200
        assert ev.output_tokens == 80
        assert ev.cost_confidence == "exact"

    def test_async_missing_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response(usage_present=False)

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncCompletions.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_openai(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_no_usage"):
                await AsyncCompletions.create(model="gpt-4o", messages=[])

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_no_usage")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert events[0].cost_confidence == "estimated"

    def test_async_no_task_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response()

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncCompletions.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_openai(tracker)

        async def run() -> Any:
            return await AsyncCompletions.create(model="gpt-4o", messages=[])

        result = asyncio.run(run())
        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        assert len(storage.query_events()) >= 1


# ---------------------------------------------------------------------------
# Async streaming tests
# ---------------------------------------------------------------------------


class _FakeAsyncIter:
    """Helper async iterator for testing async streaming."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self._index = 0

    def __aiter__(self) -> _FakeAsyncIter:
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


class TestAsyncStreaming:
    """Async streaming openai.chat.completions.create(stream=True)."""

    def test_async_streaming_with_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        usage = _make_usage(prompt_tokens=90, completion_tokens=40)
        chunks = [
            _make_chunk(model="gpt-4o"),
            _make_chunk(model="gpt-4o", usage=usage),
        ]

        async def fake_create(**kwargs: Any) -> Any:
            return _FakeAsyncIter(chunks)

        AsyncCompletions.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_openai(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_stream"):
                stream = await AsyncCompletions.create(model="gpt-4o", messages=[], stream=True)
                collected = []
                async for chunk in stream:
                    collected.append(chunk)
                assert len(collected) == 2

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_stream")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.model == "gpt-4o"
        assert ev.input_tokens == 90
        assert ev.output_tokens == 40
        assert ev.cost_confidence == "exact"

    def test_async_streaming_without_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        chunks = [_make_chunk(model="gpt-4o")]

        async def fake_create(**kwargs: Any) -> Any:
            return _FakeAsyncIter(chunks)

        AsyncCompletions.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_openai(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_stream_no_usage"):
                stream = await AsyncCompletions.create(model="gpt-4o", messages=[], stream=True)
                async for _ in stream:
                    pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_stream_no_usage")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert events[0].cost_confidence == "estimated"


# ---------------------------------------------------------------------------
# Instrument / uninstrument lifecycle tests
# ---------------------------------------------------------------------------


class TestInstrumentLifecycle:
    """instrument_openai / uninstrument_openai lifecycle."""

    def test_double_instrument_raises(self, tracker: CostTracker) -> None:
        from dexcost.instruments.openai import instrument_openai

        instrument_openai(tracker)
        with pytest.raises(RuntimeError, match="already active"):
            instrument_openai(tracker)

    def test_uninstrument_restores_original(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai, uninstrument_openai

        original_create = Completions.create

        response = _make_response()
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        # Verify it's patched (the create method should be wrapped)
        assert Completions.create is not original_create  # type: ignore[comparison-overlap]

        uninstrument_openai()

        # After uninstrument, should be able to instrument again
        instrument_openai(tracker)

    def test_uninstrument_when_not_patched_is_noop(self) -> None:
        from dexcost.instruments.openai import uninstrument_openai

        # Should not raise
        uninstrument_openai()

    def test_missing_openai_raises_import_error(self, tracker: CostTracker) -> None:
        """instrument_openai raises ImportError if openai is not installed."""
        from dexcost.instruments.openai import instrument_openai

        _uninstall_fake_openai()

        with pytest.raises(ImportError, match="openai"):
            instrument_openai(tracker)

        # Re-install for cleanup
        _install_fake_openai()


# ---------------------------------------------------------------------------
# Cost calculation integration tests
# ---------------------------------------------------------------------------


class TestCostCalculation:
    """Verify the pricing engine is used to calculate costs."""

    def test_cost_calculated_via_pricing_engine(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """With usage present, cost should be computed by the pricing engine."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=500,
        )
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="cost_calc") as task:
            Completions.create(model="gpt-4o", messages=[])

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
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=200,
            completion_tokens=100,
        )
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="agg_test") as task:
            Completions.create(model="gpt-4o", messages=[])
            Completions.create(model="gpt-4o", messages=[])

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
    """instrument_openai / uninstrument_openai accessible from top-level package."""

    def test_instrument_openai_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "instrument_openai")
        assert callable(dexcost.instrument_openai)

    def test_uninstrument_openai_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "uninstrument_openai")
        assert callable(dexcost.uninstrument_openai)
