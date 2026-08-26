"""Tests for Cohere auto-instrumentation (US-012).

All tests use mocked Cohere SDK objects — the real ``cohere`` package is
**not** required.  We simulate the module structure that
:func:`instrument_cohere` patches so the wrapt monkey-patching works
against our fakes.
"""

from __future__ import annotations

import asyncio
import gc
import json
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


class _FakeAsyncStream:
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self._index = 0
        self.closed = False

    def __aiter__(self) -> _FakeAsyncStream:
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item

    async def aclose(self) -> None:
        self.closed = True


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
        assert ev.cost_confidence == "computed"
        assert ev.cost_usd >= Decimal("0")

    def test_tokens_from_billed_units(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
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

    def test_response_id_tools_context_and_privacy_contract(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.capabilities import capability_context
        from dexcost.idempotency import idempotency_key
        from dexcost.instruments.cohere import instrument_cohere
        from dexcost.models.capability import CapabilityIdentity

        response = _make_response(input_tokens=19, output_tokens=6)
        response.response_id = "cohere-response-123"
        response.tool_calls = [
            types.SimpleNamespace(
                name="private_cohere_tool",
                parameters={"query": "private-cohere-tool-input"},
            )
        ]
        Client.chat = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]
        instrument_cohere(tracker)
        capability = CapabilityIdentity(name="research.answer", kind="workflow")

        with (
            tracker.task(task_type="cohere_contract") as task,
            capability_context(capability),
            idempotency_key("private-cohere-idempotency"),
        ):
            Client.chat(
                model="command-r-plus",
                message="private-cohere-prompt",
                tools=[{"name": "private_cohere_tool"}],
            )

        event = storage.query_events(task_id=str(task.task_id))[0]
        assert event.service_name == "chat"
        assert event.details["provider_record_id"] == "cohere-response-123"
        assert event.details["attribution_capability"] == capability.to_dict()
        assert len(event.details["_dexcost_idempotency_sha256"]) == 64
        usage = {
            line["metric"]: line["quantity"] for line in event.details["attribution_usage_lines"]
        }
        assert usage == {"input_tokens": "19", "output_tokens": "6", "tool_call_count": "1"}
        persisted = json.dumps(event.to_dict())
        for secret in (
            "private-cohere-prompt",
            "private_cohere_tool",
            "private-cohere-tool-input",
            "private-cohere-idempotency",
        ):
            assert secret not in persisted


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
        assert ev.cost_confidence == "computed"

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

    def test_stream_snapshots_context_and_terminal_provider_metadata(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.capabilities import capability_context
        from dexcost.idempotency import idempotency_key
        from dexcost.instruments.cohere import instrument_cohere
        from dexcost.models.capability import CapabilityIdentity

        events_stream = _make_stream_events(input_tokens=13, output_tokens=4)
        terminal = events_stream[-1].response
        terminal.response_id = "cohere-stream-response-1"
        terminal.tool_calls = [
            types.SimpleNamespace(
                name="private_stream_tool",
                parameters={"query": "private-stream-tool-input"},
            )
        ]
        Client.chat_stream = staticmethod(lambda **kwargs: iter(events_stream))  # type: ignore[assignment]
        instrument_cohere(tracker)
        capability = CapabilityIdentity(name="agent.tool_route", kind="workflow")

        with tracker.task(task_type="cohere_stream_contract") as task:
            with (
                capability_context(capability),
                idempotency_key("private-cohere-stream-idempotency"),
            ):
                stream = Client.chat_stream(
                    model="command-r-plus",
                    message="private-cohere-stream-prompt",
                )
            list(stream)

        event = storage.query_events(task_id=str(task.task_id))[0]
        assert event.details["provider_record_id"] == "cohere-stream-response-1"
        assert event.details["attribution_capability"] == capability.to_dict()
        usage = {
            line["metric"]: line["quantity"] for line in event.details["attribution_usage_lines"]
        }
        assert usage == {"input_tokens": "13", "output_tokens": "4", "tool_call_count": "1"}
        persisted = json.dumps(event.to_dict())
        for secret in (
            "private_stream_tool",
            "private-stream-tool-input",
            "private-cohere-stream-prompt",
            "private-cohere-stream-idempotency",
        ):
            assert secret not in persisted

    def test_streaming_without_explicit_task_finishes_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        events_stream = _make_stream_events(input_tokens=40, output_tokens=12)
        Client.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: iter(events_stream)
        )
        instrument_cohere(tracker)

        assert len(list(Client.chat_stream(model="command-r-plus", message="Hi"))) == 2

        events = storage.query_events()
        tasks = storage.query_tasks(task_type="cohere.chat")
        assert len(events) == 1
        assert len(tasks) == 1
        assert events[0].task_id == tasks[0].task_id
        assert events[0].input_tokens == 40
        assert events[0].details["attribution_operation_status"] == "succeeded"
        assert tasks[0].status == "success"

    def test_close_records_cancelled_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        Client.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: iter(_make_stream_events())
        )
        instrument_cohere(tracker)

        stream = Client.chat_stream(model="command-r-plus", message="Hi")
        next(stream)
        stream.close()

        events = storage.query_events()
        tasks = storage.query_tasks(task_type="cohere.chat")
        assert len(events) == 1
        assert len(tasks) == 1
        assert events[0].details["attribution_operation_status"] == "cancelled"
        assert tasks[0].status == "failed"

    def test_mid_stream_failure_preserves_observed_billed_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        terminal = _make_stream_events(input_tokens=23, output_tokens=7)[-1]

        def failing_stream() -> Generator[Any, None, None]:
            yield terminal
            raise RuntimeError("transport closed after usage")

        Client.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: failing_stream()
        )
        instrument_cohere(tracker)

        with (
            tracker.task(task_type="stream_partial_failure") as task,
            pytest.raises(RuntimeError, match="transport closed"),
        ):
            list(Client.chat_stream(model="command-r-plus", message="Hi"))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 23
        assert events[0].output_tokens == 7
        assert events[0].cost_confidence == "computed"
        assert events[0].details["attribution_operation_status"] == "failed"
        assert events[0].details["error_type"] == "runtimeerror"

    def test_garbage_collected_stream_records_cancelled_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        Client.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: iter(_make_stream_events(input_tokens=17, output_tokens=5))
        )
        instrument_cohere(tracker)

        stream = Client.chat_stream(model="command-r-plus", message="Hi")
        next(stream)
        next(stream)
        del stream
        gc.collect()

        events = storage.query_events()
        tasks = storage.query_tasks(task_type="cohere.chat")
        assert len(events) == 1
        assert len(tasks) == 1
        assert events[0].input_tokens == 17
        assert events[0].output_tokens == 5
        assert events[0].details["attribution_operation_status"] == "cancelled"
        assert tasks[0].status == "failed"


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
        assert ev.cost_confidence == "computed"

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

    def test_async_stream_without_explicit_task_finishes_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import AsyncClient

        from dexcost.instruments.cohere import instrument_cohere

        AsyncClient.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: _FakeAsyncStream(
                _make_stream_events(input_tokens=55, output_tokens=21)
            )
        )
        instrument_cohere(tracker)

        async def run() -> None:
            stream = AsyncClient.chat_stream(model="command-r-plus", message="Hi")
            assert len([event async for event in stream]) == 2

        asyncio.run(run())

        events = storage.query_events()
        tasks = storage.query_tasks(task_type="cohere.chat")
        assert len(events) == 1
        assert len(tasks) == 1
        assert events[0].task_id == tasks[0].task_id
        assert events[0].input_tokens == 55
        assert events[0].details["attribution_operation_status"] == "succeeded"

    def test_aclose_records_cancelled_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import AsyncClient

        from dexcost.instruments.cohere import instrument_cohere

        raw_stream = _FakeAsyncStream(_make_stream_events())
        AsyncClient.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: raw_stream
        )
        instrument_cohere(tracker)

        async def run() -> None:
            stream = AsyncClient.chat_stream(model="command-r-plus", message="Hi")
            await stream.__anext__()
            await stream.aclose()

        asyncio.run(run())

        events = storage.query_events()
        tasks = storage.query_tasks(task_type="cohere.chat")
        assert raw_stream.closed is True
        assert len(events) == 1
        assert len(tasks) == 1
        assert events[0].details["attribution_operation_status"] == "cancelled"
        assert tasks[0].status == "failed"

    def test_async_mid_stream_failure_preserves_observed_billed_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import AsyncClient

        from dexcost.instruments.cohere import instrument_cohere

        terminal = _make_stream_events(input_tokens=29, output_tokens=11)[-1]

        async def failing_stream() -> Any:
            yield terminal
            raise RuntimeError("async transport closed after usage")

        AsyncClient.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: failing_stream()
        )
        instrument_cohere(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_stream_partial_failure"):
                with pytest.raises(RuntimeError, match="transport closed"):
                    stream = AsyncClient.chat_stream(model="command-r-plus", message="Hi")
                    _ = [event async for event in stream]

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_stream_partial_failure")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 29
        assert events[0].output_tokens == 11
        assert events[0].cost_confidence == "computed"
        assert events[0].details["attribution_operation_status"] == "failed"
        assert events[0].details["error_type"] == "runtimeerror"

    def test_garbage_collected_async_stream_records_cancelled_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import AsyncClient

        from dexcost.instruments.cohere import instrument_cohere

        AsyncClient.chat_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: _FakeAsyncStream(
                _make_stream_events(input_tokens=13, output_tokens=6)
            )
        )
        instrument_cohere(tracker)

        async def run() -> None:
            stream = AsyncClient.chat_stream(model="command-r-plus", message="Hi")
            await stream.__anext__()
            await stream.__anext__()
            del stream
            gc.collect()

        asyncio.run(run())

        events = storage.query_events()
        tasks = storage.query_tasks(task_type="cohere.chat")
        assert len(events) == 1
        assert len(tasks) == 1
        assert events[0].input_tokens == 13
        assert events[0].output_tokens == 6
        assert events[0].details["attribution_operation_status"] == "cancelled"
        assert tasks[0].status == "failed"


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

        with patch.dict(sys.modules, blocked), pytest.raises(ImportError, match="cohere"):
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
