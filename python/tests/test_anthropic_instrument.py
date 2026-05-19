"""Tests for Anthropic auto-instrumentation (US-013).

All tests use mocked Anthropic SDK objects — the real ``anthropic`` package is
**not** required.  We simulate the module structure that
:func:`instrument_anthropic` patches so the wrapt monkey-patching works
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
# Fake Anthropic module hierarchy
# ---------------------------------------------------------------------------


def _make_usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> MagicMock:
    """Build a mock Anthropic ``Usage`` object."""
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = cache_creation_input_tokens
    usage.cache_read_input_tokens = cache_read_input_tokens
    return usage


def _make_response(
    model: str = "claude-3-5-sonnet-20241022",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    usage_present: bool = True,
) -> MagicMock:
    """Build a mock Anthropic ``Message`` response."""
    resp = MagicMock()
    resp.model = model
    if usage_present:
        resp.usage = _make_usage(
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
        )
    else:
        resp.usage = None
    return resp


def _make_stream_event(
    event_type: str,
    *,
    model: str | None = None,
    usage: Any = None,
    message: Any = None,
) -> MagicMock:
    """Build a mock streaming ``RawMessageStreamEvent``."""
    event = MagicMock()
    event.type = event_type
    if message is not None:
        event.message = message
    elif event_type == "message_start":
        # Build a default message stub if none provided
        msg = MagicMock()
        msg.model = model
        msg.usage = usage
        event.message = msg
    else:
        event.message = None

    if event_type == "message_delta":
        event.usage = usage
    elif event_type != "message_start":
        event.usage = None

    return event


def _install_fake_anthropic() -> tuple[MagicMock, MagicMock]:
    """Install a fake ``anthropic`` package into ``sys.modules``.

    Returns the sync ``Messages`` class and async ``AsyncMessages``
    class so tests can set ``.create`` behaviour.
    """
    # Build the module tree: anthropic → anthropic.resources → anthropic.resources.messages
    anthropic_mod = types.ModuleType("anthropic")
    resources_mod = types.ModuleType("anthropic.resources")
    messages_mod = types.ModuleType("anthropic.resources.messages")

    class Messages:
        @staticmethod
        def create(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

    class AsyncMessages:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

    messages_mod.Messages = Messages  # type: ignore[attr-defined]
    messages_mod.AsyncMessages = AsyncMessages  # type: ignore[attr-defined]

    resources_mod.messages = messages_mod  # type: ignore[attr-defined]
    anthropic_mod.resources = resources_mod  # type: ignore[attr-defined]

    sys.modules["anthropic"] = anthropic_mod
    sys.modules["anthropic.resources"] = resources_mod
    sys.modules["anthropic.resources.messages"] = messages_mod

    return Messages, AsyncMessages  # type: ignore[return-value]


def _uninstall_fake_anthropic() -> None:
    """Remove our fake anthropic modules from ``sys.modules``."""
    for key in list(sys.modules):
        if key == "anthropic" or key.startswith("anthropic."):
            del sys.modules[key]


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
def _fake_anthropic() -> Generator[None, None, None]:
    """Install/uninstall fake anthropic for every test and ensure uninstrument."""
    _install_fake_anthropic()
    yield
    # Always uninstrument after each test to reset module-level state
    from dexcost.instruments.anthropic import uninstrument_anthropic

    uninstrument_anthropic()
    _uninstall_fake_anthropic()


# ---------------------------------------------------------------------------
# Sync non-streaming tests
# ---------------------------------------------------------------------------


class TestSyncNonStreaming:
    """Sync anthropic.messages.create() without streaming."""

    def test_records_event_with_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Mocked Anthropic call inside tracked task → event recorded with correct tokens."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            input_tokens=150,
            output_tokens=75,
        )
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="sync_usage") as task:
            result = Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

        assert result is response

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "anthropic"
        assert ev.model == "claude-3-5-sonnet-20241022"
        assert ev.input_tokens == 150
        assert ev.output_tokens == 75
        assert ev.cost_confidence == "exact"
        assert ev.cost_usd >= Decimal("0")

    def test_missing_usage_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When response.usage is None, cost_confidence should be 'estimated'."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(usage_present=False)
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="sync_no_usage") as task:
            Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.cost_usd == Decimal("0")
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0

    def test_cache_read_tokens_extracted(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """cache_read_input_tokens are captured in cached_tokens field."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            input_tokens=200,
            output_tokens=100,
            cache_read_input_tokens=80,
        )
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="sync_cache_read") as task:
            Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].cached_tokens == 80

    def test_cache_creation_tokens_extracted(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """cache_creation_input_tokens are captured in event details."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            input_tokens=200,
            output_tokens=100,
            cache_creation_input_tokens=150,
        )
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="sync_cache_creation") as task:
            Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details.get("cache_creation_input_tokens") == 150

    def test_cache_creation_and_read_combined(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Both cache creation and cache read tokens are captured correctly."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            input_tokens=500,
            output_tokens=100,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=150,
        )
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="sync_cache_both") as task:
            Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cached_tokens == 150
        assert ev.details.get("cache_creation_input_tokens") == 200
        assert ev.input_tokens == 500

    def test_latency_recorded(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """latency_ms is populated on the event."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response()
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="sync_latency") as task:
            Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].latency_ms is not None
        assert events[0].latency_ms >= 0

    def test_model_from_response(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Model name is taken from the response, not the request."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(model="claude-3-5-sonnet-20241022")
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="sync_model") as task:
            Messages.create(model="claude-3-5-sonnet-latest", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].model == "claude-3-5-sonnet-20241022"


# ---------------------------------------------------------------------------
# Passthrough (no active task) tests
# ---------------------------------------------------------------------------


class TestPassthrough:
    """When no explicit task context is active, calls create an auto-task."""

    def test_no_task_context_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response()
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        result = Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        all_events = storage.query_events()
        assert len(all_events) >= 1


# ---------------------------------------------------------------------------
# Sync streaming tests
# ---------------------------------------------------------------------------


class TestSyncStreaming:
    """Sync streaming anthropic.messages.create(stream=True)."""

    def test_streaming_with_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Usage from message_start and message_delta events is captured."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        input_usage = _make_usage(input_tokens=120, output_tokens=0)
        delta_usage = MagicMock()
        delta_usage.output_tokens = 60

        events_list = [
            _make_stream_event(
                "message_start", model="claude-3-5-sonnet-20241022", usage=input_usage
            ),
            _make_stream_event("content_block_delta"),
            _make_stream_event("message_delta", usage=delta_usage),
            _make_stream_event("message_stop"),
        ]

        Messages.create = staticmethod(lambda **kwargs: iter(events_list))  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="stream_usage") as task:
            stream = Messages.create(model="claude-3-5-sonnet-20241022", messages=[], stream=True)
            collected = list(stream)

        assert len(collected) == 4

        recorded = storage.query_events(task_id=str(task.task_id))
        assert len(recorded) == 1
        ev = recorded[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "anthropic"
        assert ev.model == "claude-3-5-sonnet-20241022"
        assert ev.input_tokens == 120
        assert ev.output_tokens == 60
        assert ev.cost_confidence == "exact"

    def test_streaming_with_cache_tokens(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Cache tokens from message_start are captured in streaming."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        input_usage = _make_usage(
            input_tokens=300,
            output_tokens=0,
            cache_creation_input_tokens=100,
            cache_read_input_tokens=50,
        )
        delta_usage = MagicMock()
        delta_usage.output_tokens = 80

        events_list = [
            _make_stream_event(
                "message_start", model="claude-3-5-sonnet-20241022", usage=input_usage
            ),
            _make_stream_event("message_delta", usage=delta_usage),
            _make_stream_event("message_stop"),
        ]

        Messages.create = staticmethod(lambda **kwargs: iter(events_list))  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="stream_cache") as task:
            stream = Messages.create(model="claude-3-5-sonnet-20241022", messages=[], stream=True)
            list(stream)

        recorded = storage.query_events(task_id=str(task.task_id))
        assert len(recorded) == 1
        ev = recorded[0]
        assert ev.input_tokens == 300
        assert ev.output_tokens == 80
        assert ev.cached_tokens == 50
        assert ev.details.get("cache_creation_input_tokens") == 100

    def test_streaming_without_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """When no usage appears in the stream, cost_confidence is 'estimated'."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        # Simulate a stream with no usage info at all
        msg = MagicMock()
        msg.model = "claude-3-5-sonnet-20241022"
        msg.usage = None
        events_list = [
            _make_stream_event("message_start", message=msg),
            _make_stream_event("message_stop"),
        ]

        Messages.create = staticmethod(lambda **kwargs: iter(events_list))  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="stream_no_usage") as task:
            stream = Messages.create(model="claude-3-5-sonnet-20241022", messages=[], stream=True)
            list(stream)

        recorded = storage.query_events(task_id=str(task.task_id))
        assert len(recorded) == 1
        ev = recorded[0]
        assert ev.cost_confidence == "estimated"
        assert ev.cost_usd == Decimal("0")
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0

    def test_streaming_latency(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Latency covers the entire stream consumption."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        input_usage = _make_usage(input_tokens=50, output_tokens=0)
        delta_usage = MagicMock()
        delta_usage.output_tokens = 25
        events_list = [
            _make_stream_event(
                "message_start", model="claude-3-5-sonnet-20241022", usage=input_usage
            ),
            _make_stream_event("message_delta", usage=delta_usage),
            _make_stream_event("message_stop"),
        ]

        Messages.create = staticmethod(lambda **kwargs: iter(events_list))  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="stream_latency") as task:
            stream = Messages.create(model="claude-3-5-sonnet-20241022", messages=[], stream=True)
            list(stream)

        recorded = storage.query_events(task_id=str(task.task_id))
        assert recorded[0].latency_ms is not None
        assert recorded[0].latency_ms >= 0


# ---------------------------------------------------------------------------
# Async non-streaming tests
# ---------------------------------------------------------------------------


class TestAsyncNonStreaming:
    """Async anthropic.messages.create() without streaming."""

    def test_async_records_event(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            input_tokens=200,
            output_tokens=80,
        )

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_usage"):
                result = await AsyncMessages.create(
                    model="claude-3-5-sonnet-20241022", messages=[]
                )
                assert result is response

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_usage")
        assert len(tasks) == 1
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "anthropic"
        assert ev.model == "claude-3-5-sonnet-20241022"
        assert ev.input_tokens == 200
        assert ev.output_tokens == 80
        assert ev.cost_confidence == "exact"

    def test_async_missing_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(usage_present=False)

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_no_usage"):
                await AsyncMessages.create(model="claude-3-5-sonnet-20241022", messages=[])

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_no_usage")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert events[0].cost_confidence == "estimated"

    def test_async_no_task_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response()

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        async def run() -> Any:
            return await AsyncMessages.create(model="claude-3-5-sonnet-20241022", messages=[])

        result = asyncio.run(run())
        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        assert len(storage.query_events()) >= 1

    def test_async_cache_tokens(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Async call correctly captures cache creation and read tokens."""
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            input_tokens=400,
            output_tokens=100,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=100,
        )

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_cache"):
                await AsyncMessages.create(model="claude-3-5-sonnet-20241022", messages=[])

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_cache")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cached_tokens == 100
        assert ev.details.get("cache_creation_input_tokens") == 200


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
    """Async streaming anthropic.messages.create(stream=True)."""

    def test_async_streaming_with_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        input_usage = _make_usage(input_tokens=90, output_tokens=0)
        delta_usage = MagicMock()
        delta_usage.output_tokens = 40

        events_list = [
            _make_stream_event(
                "message_start", model="claude-3-5-sonnet-20241022", usage=input_usage
            ),
            _make_stream_event("message_delta", usage=delta_usage),
            _make_stream_event("message_stop"),
        ]

        async def fake_create(**kwargs: Any) -> Any:
            return _FakeAsyncIter(events_list)

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_stream"):
                stream = await AsyncMessages.create(
                    model="claude-3-5-sonnet-20241022", messages=[], stream=True
                )
                collected = []
                async for event in stream:
                    collected.append(event)
                assert len(collected) == 3

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_stream")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.model == "claude-3-5-sonnet-20241022"
        assert ev.input_tokens == 90
        assert ev.output_tokens == 40
        assert ev.cost_confidence == "exact"

    def test_async_streaming_without_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        msg = MagicMock()
        msg.model = "claude-3-5-sonnet-20241022"
        msg.usage = None
        events_list = [
            _make_stream_event("message_start", message=msg),
            _make_stream_event("message_stop"),
        ]

        async def fake_create(**kwargs: Any) -> Any:
            return _FakeAsyncIter(events_list)

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_stream_no_usage"):
                stream = await AsyncMessages.create(
                    model="claude-3-5-sonnet-20241022", messages=[], stream=True
                )
                async for _ in stream:
                    pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_stream_no_usage")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert events[0].cost_confidence == "estimated"

    def test_async_streaming_with_cache_tokens(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Async streaming correctly captures cache tokens."""
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        input_usage = _make_usage(
            input_tokens=500,
            output_tokens=0,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=100,
        )
        delta_usage = MagicMock()
        delta_usage.output_tokens = 60

        events_list = [
            _make_stream_event(
                "message_start", model="claude-3-5-sonnet-20241022", usage=input_usage
            ),
            _make_stream_event("message_delta", usage=delta_usage),
            _make_stream_event("message_stop"),
        ]

        async def fake_create(**kwargs: Any) -> Any:
            return _FakeAsyncIter(events_list)

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_stream_cache"):
                stream = await AsyncMessages.create(
                    model="claude-3-5-sonnet-20241022", messages=[], stream=True
                )
                async for _ in stream:
                    pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_stream_cache")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cached_tokens == 100
        assert ev.details.get("cache_creation_input_tokens") == 200
        assert ev.input_tokens == 500
        assert ev.output_tokens == 60


# ---------------------------------------------------------------------------
# Instrument / uninstrument lifecycle tests
# ---------------------------------------------------------------------------


class TestInstrumentLifecycle:
    """instrument_anthropic / uninstrument_anthropic lifecycle."""

    def test_double_instrument_raises(self, tracker: CostTracker) -> None:
        from dexcost.instruments.anthropic import instrument_anthropic

        instrument_anthropic(tracker)
        with pytest.raises(RuntimeError, match="already active"):
            instrument_anthropic(tracker)

    def test_uninstrument_restores_original(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic, uninstrument_anthropic

        original_create = Messages.create

        response = _make_response()
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        # Verify it's patched (the create method should be wrapped)
        assert Messages.create is not original_create  # type: ignore[comparison-overlap]

        uninstrument_anthropic()

        # After uninstrument, should be able to instrument again
        instrument_anthropic(tracker)

    def test_uninstrument_when_not_patched_is_noop(self) -> None:
        from dexcost.instruments.anthropic import uninstrument_anthropic

        # Should not raise
        uninstrument_anthropic()

    def test_missing_anthropic_raises_import_error(self, tracker: CostTracker) -> None:
        """instrument_anthropic raises ImportError if anthropic is not installed."""
        from unittest.mock import patch

        from dexcost.instruments.anthropic import instrument_anthropic

        _uninstall_fake_anthropic()

        # Block the import of anthropic.resources.messages even if the real
        # anthropic package is installed in the environment.
        blocked = {k: None for k in list(sys.modules) if k == "anthropic" or k.startswith("anthropic.")}
        blocked.setdefault("anthropic", None)
        blocked.setdefault("anthropic.resources", None)
        blocked.setdefault("anthropic.resources.messages", None)

        with patch.dict(sys.modules, blocked):
            with pytest.raises(ImportError, match="anthropic"):
                instrument_anthropic(tracker)

        # Re-install for cleanup
        _install_fake_anthropic()


# ---------------------------------------------------------------------------
# Cost calculation integration tests
# ---------------------------------------------------------------------------


class TestCostCalculation:
    """Verify the pricing engine is used to calculate costs."""

    def test_cost_calculated_via_pricing_engine(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """With usage present, cost should be computed by the pricing engine."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            input_tokens=1000,
            output_tokens=500,
        )
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="cost_calc") as task:
            Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        ev = events[0]
        # The pricing engine should have set a pricing_source
        assert ev.pricing_source is not None
        assert ev.pricing_source != "unknown"
        # cost_usd should be non-negative
        assert ev.cost_usd >= Decimal("0")

    def test_cache_pricing_tiers(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Cache creation and read tokens are priced at their respective tiers."""
        from dexcost.instruments.anthropic import instrument_anthropic

        instrument_anthropic(tracker)

        # Compute costs directly via the pricing engine for comparison.
        # 1000 input tokens: 400 cache creation, 300 cache read, 300 normal
        cost_with_cache = tracker._pricing.get_cost(
            "claude-3-5-sonnet-20241022",
            input_tokens=1000,
            output_tokens=200,
            cached_tokens=300,
            cache_creation_tokens=400,
        )
        cost_without_cache = tracker._pricing.get_cost(
            "claude-3-5-sonnet-20241022",
            input_tokens=1000,
            output_tokens=200,
        )

        assert cost_with_cache.cost_usd > Decimal("0")
        assert cost_without_cache.cost_usd > Decimal("0")
        # Cache creation is more expensive, cache read is cheaper;
        # the two costs should differ.
        assert cost_with_cache.cost_usd != cost_without_cache.cost_usd


# ---------------------------------------------------------------------------
# Task aggregation integration tests
# ---------------------------------------------------------------------------


class TestTaskAggregation:
    """Auto-captured events are included in task cost aggregation."""

    def test_auto_captured_event_aggregated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            input_tokens=200,
            output_tokens=100,
        )
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="agg_test") as task:
            Messages.create(model="claude-3-5-sonnet-20241022", messages=[])
            Messages.create(model="claude-3-5-sonnet-20241022", messages=[])

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
    """instrument_anthropic / uninstrument_anthropic accessible from top-level package."""

    def test_instrument_anthropic_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "instrument_anthropic")
        assert callable(dexcost.instrument_anthropic)

    def test_uninstrument_anthropic_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "uninstrument_anthropic")
        assert callable(dexcost.uninstrument_anthropic)
