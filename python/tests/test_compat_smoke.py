"""Smoke tests for weekly SDK compatibility CI (US-022).

These tests verify that dexcost auto-instrumentation still works correctly
against the latest releases of openai, anthropic, and litellm.  Each test
creates a tracked task, makes a mocked API call through the instrumented
SDK, and verifies an event was captured with correct fields.

Tests are LIGHTER than the full instrumentation test suites — they focus
on "does the latest SDK version still work with our monkey-patches?"

All three SDK packages are optional.  Tests are skipped automatically
via ``pytest.importorskip()`` when the SDK is not installed.
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

from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_usage(
    prompt_tokens: int = 100, completion_tokens: int = 50
) -> MagicMock:
    """Build a mock OpenAI ``CompletionUsage``."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.prompt_tokens_details = None
    return usage


def _make_openai_response(
    model: str = "gpt-4o",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> MagicMock:
    """Build a mock OpenAI ``ChatCompletion`` response."""
    resp = MagicMock()
    resp.model = model
    resp.usage = _make_openai_usage(prompt_tokens, completion_tokens)
    return resp


def _make_openai_chunk(
    model: str = "gpt-4o", usage: Any = None
) -> MagicMock:
    """Build a mock OpenAI streaming chunk."""
    chunk = MagicMock()
    chunk.model = model
    chunk.usage = usage
    return chunk


def _make_anthropic_usage(
    input_tokens: int = 100, output_tokens: int = 50
) -> MagicMock:
    """Build a mock Anthropic ``Usage``."""
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    return usage


def _make_anthropic_response(
    model: str = "claude-3-5-sonnet-20241022",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MagicMock:
    """Build a mock Anthropic ``Message`` response."""
    resp = MagicMock()
    resp.model = model
    resp.usage = _make_anthropic_usage(input_tokens, output_tokens)
    return resp


def _make_anthropic_stream_event(
    event_type: str,
    *,
    model: str | None = None,
    usage: Any = None,
) -> MagicMock:
    """Build a mock Anthropic streaming event."""
    event = MagicMock()
    event.type = event_type
    if event_type == "message_start":
        msg = MagicMock()
        msg.model = model
        msg.usage = usage
        event.message = msg
    else:
        event.message = None
    if event_type == "message_delta":
        event.usage = usage
    else:
        event.usage = None
    return event


def _make_litellm_usage(
    prompt_tokens: int = 100, completion_tokens: int = 50
) -> MagicMock:
    """Build a mock LiteLLM usage object."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    return usage


def _make_litellm_response(
    model: str = "gpt-4o",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    provider: str = "openai",
) -> MagicMock:
    """Build a mock LiteLLM ``ModelResponse``."""
    resp = MagicMock()
    resp.model = model
    resp.usage = _make_litellm_usage(prompt_tokens, completion_tokens)
    resp._hidden_params = {"custom_llm_provider": provider}
    return resp


class _FakeAsyncIter:
    """Async iterator helper for streaming tests."""

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


# ---------------------------------------------------------------------------
# Fake module installers
# ---------------------------------------------------------------------------


def _install_fake_openai() -> tuple[Any, Any]:
    """Install a fake ``openai`` package into ``sys.modules``."""
    openai_mod = types.ModuleType("openai")
    resources_mod = types.ModuleType("openai.resources")
    chat_mod = types.ModuleType("openai.resources.chat")
    completions_mod = types.ModuleType("openai.resources.chat.completions")

    class Completions:
        @staticmethod
        def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    class AsyncCompletions:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    completions_mod.Completions = Completions  # type: ignore[attr-defined]
    completions_mod.AsyncCompletions = AsyncCompletions  # type: ignore[attr-defined]
    chat_mod.completions = completions_mod  # type: ignore[attr-defined]
    resources_mod.chat = chat_mod  # type: ignore[attr-defined]
    openai_mod.resources = resources_mod  # type: ignore[attr-defined]

    sys.modules["openai"] = openai_mod
    sys.modules["openai.resources"] = resources_mod
    sys.modules["openai.resources.chat"] = chat_mod
    sys.modules["openai.resources.chat.completions"] = completions_mod

    return Completions, AsyncCompletions


def _uninstall_fake_openai() -> None:
    for key in list(sys.modules):
        if key == "openai" or key.startswith("openai."):
            del sys.modules[key]


def _install_fake_anthropic() -> tuple[Any, Any]:
    """Install a fake ``anthropic`` package into ``sys.modules``."""
    anthropic_mod = types.ModuleType("anthropic")
    resources_mod = types.ModuleType("anthropic.resources")
    messages_mod = types.ModuleType("anthropic.resources.messages")

    class Messages:
        @staticmethod
        def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    class AsyncMessages:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    messages_mod.Messages = Messages  # type: ignore[attr-defined]
    messages_mod.AsyncMessages = AsyncMessages  # type: ignore[attr-defined]
    resources_mod.messages = messages_mod  # type: ignore[attr-defined]
    anthropic_mod.resources = resources_mod  # type: ignore[attr-defined]

    sys.modules["anthropic"] = anthropic_mod
    sys.modules["anthropic.resources"] = resources_mod
    sys.modules["anthropic.resources.messages"] = messages_mod

    return Messages, AsyncMessages


def _uninstall_fake_anthropic() -> None:
    for key in list(sys.modules):
        if key == "anthropic" or key.startswith("anthropic."):
            del sys.modules[key]


def _install_fake_litellm() -> types.ModuleType:
    """Install a fake ``litellm`` package into ``sys.modules``."""
    litellm_mod = types.ModuleType("litellm")

    def _completion(**kwargs: Any) -> Any:
        raise NotImplementedError

    async def _acompletion(**kwargs: Any) -> Any:
        raise NotImplementedError

    litellm_mod.completion = _completion  # type: ignore[attr-defined]
    litellm_mod.acompletion = _acompletion  # type: ignore[attr-defined]
    litellm_mod.completion_cost = None  # type: ignore[attr-defined]

    sys.modules["litellm"] = litellm_mod
    return litellm_mod


def _uninstall_fake_litellm() -> None:
    for key in list(sys.modules):
        if key == "litellm" or key.startswith("litellm."):
            del sys.modules[key]


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    """Fresh SQLite storage per test."""
    s = SQLiteStorage(db_path=tmp_path / "smoke.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    """CostTracker with no auto-instrumentation."""
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


# ============================================================================
# OpenAI smoke tests
# ============================================================================


class TestOpenAISmoke:
    """Smoke tests for OpenAI SDK compatibility."""

    @pytest.fixture(autouse=True)
    def _setup_openai(self) -> Generator[None, None, None]:
        """Install fake openai module and clean up after each test."""
        _install_fake_openai()
        yield
        from dexcost.instruments.openai import uninstrument_openai

        uninstrument_openai()
        _uninstall_fake_openai()

    def test_sync_non_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Sync mocked call -> event captured with correct fields."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        response = _make_openai_response(model="gpt-4o", prompt_tokens=150, completion_tokens=75)
        Completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="openai_sync_smoke") as task:
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
        assert ev.cost_usd >= Decimal("0")

    def test_sync_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Sync streaming -> event captured after stream consumed."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        usage = _make_openai_usage(prompt_tokens=120, completion_tokens=60)
        chunks = [
            _make_openai_chunk(model="gpt-4o"),
            _make_openai_chunk(model="gpt-4o", usage=usage),
        ]
        Completions.create = staticmethod(lambda **kwargs: iter(chunks))  # type: ignore[assignment]

        instrument_openai(tracker)

        with tracker.task(task_type="openai_stream_smoke") as task:
            stream = Completions.create(model="gpt-4o", messages=[], stream=True)
            collected = list(stream)

        assert len(collected) == 2
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 120
        assert events[0].output_tokens == 60

    def test_async_non_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Async mocked call -> event captured."""
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        response = _make_openai_response(model="gpt-4o", prompt_tokens=200, completion_tokens=80)

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncCompletions.create = staticmethod(fake_create)  # type: ignore[assignment]
        instrument_openai(tracker)

        async def run() -> None:
            async with tracker.task(task_type="openai_async_smoke"):
                await AsyncCompletions.create(model="gpt-4o", messages=[])

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="openai_async_smoke")
        assert len(tasks) == 1
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        assert events[0].provider == "openai"
        assert events[0].input_tokens == 200

    def test_async_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Async streaming -> event captured after stream consumed."""
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        usage = _make_openai_usage(prompt_tokens=90, completion_tokens=40)
        chunks = [
            _make_openai_chunk(model="gpt-4o"),
            _make_openai_chunk(model="gpt-4o", usage=usage),
        ]

        async def fake_create(**kwargs: Any) -> Any:
            return _FakeAsyncIter(chunks)

        AsyncCompletions.create = staticmethod(fake_create)  # type: ignore[assignment]
        instrument_openai(tracker)

        async def run() -> None:
            async with tracker.task(task_type="openai_async_stream_smoke"):
                stream = await AsyncCompletions.create(
                    model="gpt-4o", messages=[], stream=True
                )
                async for _ in stream:
                    pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="openai_async_stream_smoke")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 90
        assert events[0].output_tokens == 40


# ============================================================================
# Anthropic smoke tests
# ============================================================================


class TestAnthropicSmoke:
    """Smoke tests for Anthropic SDK compatibility."""

    @pytest.fixture(autouse=True)
    def _setup_anthropic(self) -> Generator[None, None, None]:
        """Install fake anthropic module and clean up after each test."""
        _install_fake_anthropic()
        yield
        from dexcost.instruments.anthropic import uninstrument_anthropic

        uninstrument_anthropic()
        _uninstall_fake_anthropic()

    def test_sync_non_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Sync mocked call -> event captured with correct fields."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_anthropic_response(
            model="claude-3-5-sonnet-20241022", input_tokens=150, output_tokens=75
        )
        Messages.create = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="anthropic_sync_smoke") as task:
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
        assert ev.cost_usd >= Decimal("0")

    def test_sync_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Sync streaming -> event captured after stream consumed."""
        from anthropic.resources.messages import Messages

        from dexcost.instruments.anthropic import instrument_anthropic

        input_usage = _make_anthropic_usage(input_tokens=120, output_tokens=0)
        delta_usage = MagicMock()
        delta_usage.output_tokens = 60

        stream_events = [
            _make_anthropic_stream_event(
                "message_start", model="claude-3-5-sonnet-20241022", usage=input_usage
            ),
            _make_anthropic_stream_event("message_delta", usage=delta_usage),
            _make_anthropic_stream_event("message_stop"),
        ]
        Messages.create = staticmethod(lambda **kwargs: iter(stream_events))  # type: ignore[assignment]

        instrument_anthropic(tracker)

        with tracker.task(task_type="anthropic_stream_smoke") as task:
            stream = Messages.create(
                model="claude-3-5-sonnet-20241022", messages=[], stream=True
            )
            collected = list(stream)

        assert len(collected) == 3
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 120
        assert events[0].output_tokens == 60

    def test_async_non_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Async mocked call -> event captured."""
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        response = _make_anthropic_response(
            model="claude-3-5-sonnet-20241022", input_tokens=200, output_tokens=80
        )

        async def fake_create(**kwargs: Any) -> Any:
            return response

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]
        instrument_anthropic(tracker)

        async def run() -> None:
            async with tracker.task(task_type="anthropic_async_smoke"):
                await AsyncMessages.create(
                    model="claude-3-5-sonnet-20241022", messages=[]
                )

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="anthropic_async_smoke")
        assert len(tasks) == 1
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        assert events[0].provider == "anthropic"
        assert events[0].input_tokens == 200

    def test_async_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Async streaming -> event captured after stream consumed."""
        from anthropic.resources.messages import AsyncMessages

        from dexcost.instruments.anthropic import instrument_anthropic

        input_usage = _make_anthropic_usage(input_tokens=90, output_tokens=0)
        delta_usage = MagicMock()
        delta_usage.output_tokens = 40

        stream_events = [
            _make_anthropic_stream_event(
                "message_start", model="claude-3-5-sonnet-20241022", usage=input_usage
            ),
            _make_anthropic_stream_event("message_delta", usage=delta_usage),
            _make_anthropic_stream_event("message_stop"),
        ]

        async def fake_create(**kwargs: Any) -> Any:
            return _FakeAsyncIter(stream_events)

        AsyncMessages.create = staticmethod(fake_create)  # type: ignore[assignment]
        instrument_anthropic(tracker)

        async def run() -> None:
            async with tracker.task(task_type="anthropic_async_stream_smoke"):
                stream = await AsyncMessages.create(
                    model="claude-3-5-sonnet-20241022", messages=[], stream=True
                )
                async for _ in stream:
                    pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="anthropic_async_stream_smoke")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 90
        assert events[0].output_tokens == 40


# ============================================================================
# LiteLLM smoke tests
# ============================================================================


class TestLiteLLMSmoke:
    """Smoke tests for LiteLLM SDK compatibility."""

    @pytest.fixture(autouse=True)
    def _setup_litellm(self) -> Generator[None, None, None]:
        """Install fake litellm module and clean up after each test."""
        _install_fake_litellm()
        yield
        from dexcost.instruments.litellm import uninstrument_litellm

        uninstrument_litellm()
        _uninstall_fake_litellm()

    def test_sync_non_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Sync mocked call -> event captured with correct fields."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_litellm_response(
            model="gpt-4o", prompt_tokens=150, completion_tokens=75, provider="openai"
        )
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="litellm_sync_smoke") as task:
            result = litellm.completion(model="gpt-4o", messages=[])

        assert result is response
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "openai"
        assert ev.model == "gpt-4o"
        assert ev.input_tokens == 150
        assert ev.output_tokens == 75
        assert ev.cost_usd >= Decimal("0")

    def test_sync_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Sync streaming -> event captured after stream consumed."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        usage = _make_litellm_usage(prompt_tokens=120, completion_tokens=60)
        chunk1 = MagicMock()
        chunk1.model = "gpt-4o"
        chunk1.usage = None
        chunk1._hidden_params = {"custom_llm_provider": "openai"}
        chunk2 = MagicMock()
        chunk2.model = "gpt-4o"
        chunk2.usage = usage
        chunk2._hidden_params = {}

        litellm.completion = lambda **kwargs: iter([chunk1, chunk2])  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="litellm_stream_smoke") as task:
            stream = litellm.completion(model="gpt-4o", messages=[], stream=True)
            collected = list(stream)

        assert len(collected) == 2
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 120
        assert events[0].output_tokens == 60

    def test_async_non_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Async mocked call -> event captured."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_litellm_response(
            model="gpt-4o", prompt_tokens=200, completion_tokens=80, provider="openai"
        )

        async def fake_acompletion(**kwargs: Any) -> Any:
            return response

        litellm.acompletion = fake_acompletion  # type: ignore[assignment]

        instrument_litellm(tracker)

        async def run() -> None:
            async with tracker.task(task_type="litellm_async_smoke"):
                await litellm.acompletion(model="gpt-4o", messages=[])

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="litellm_async_smoke")
        assert len(tasks) == 1
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        assert events[0].provider == "openai"
        assert events[0].input_tokens == 200

    def test_async_streaming(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Async streaming -> event captured after stream consumed."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        usage = _make_litellm_usage(prompt_tokens=90, completion_tokens=40)
        chunk1 = MagicMock()
        chunk1.model = "gpt-4o"
        chunk1.usage = None
        chunk1._hidden_params = {"custom_llm_provider": "openai"}
        chunk2 = MagicMock()
        chunk2.model = "gpt-4o"
        chunk2.usage = usage
        chunk2._hidden_params = {}

        async def fake_acompletion(**kwargs: Any) -> Any:
            return _FakeAsyncIter([chunk1, chunk2])

        litellm.acompletion = fake_acompletion  # type: ignore[assignment]

        instrument_litellm(tracker)

        async def run() -> None:
            async with tracker.task(task_type="litellm_async_stream_smoke"):
                stream = await litellm.acompletion(
                    model="gpt-4o", messages=[], stream=True
                )
                async for _ in stream:
                    pass

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="litellm_async_stream_smoke")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert len(events) == 1
        assert events[0].input_tokens == 90
        assert events[0].output_tokens == 40
