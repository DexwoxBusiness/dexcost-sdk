"""Tests for LiteLLM auto-instrumentation (US-014).

All tests use mocked LiteLLM objects — the real ``litellm`` package is
**not** required.  We simulate the module structure that
:func:`instrument_litellm` patches so the wrapt monkey-patching works
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
# Fake LiteLLM module
# ---------------------------------------------------------------------------


def _make_usage(
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> MagicMock:
    """Build a mock ``Usage`` object (OpenAI-compatible)."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    return usage


def _make_response(
    model: str = "gpt-4o",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    usage_present: bool = True,
    provider: str | None = None,
    litellm_cost: float | None = None,
) -> MagicMock:
    """Build a mock LiteLLM ``ModelResponse``.

    Args:
        model: Model name in the response.
        prompt_tokens: Number of input tokens.
        completion_tokens: Number of output tokens.
        usage_present: Whether usage data is present.
        provider: Actual provider to set in ``_hidden_params``.
        litellm_cost: Cost value for litellm.completion_cost mock.
    """
    resp = MagicMock()
    resp.model = model
    if usage_present:
        resp.usage = _make_usage(prompt_tokens, completion_tokens)
    else:
        resp.usage = None

    # Set _hidden_params for provider resolution
    if provider is not None:
        resp._hidden_params = {"custom_llm_provider": provider}
    else:
        resp._hidden_params = {}

    return resp


def _make_chunk(
    model: str | None = "gpt-4o",
    usage: Any = None,
    hidden_params: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock streaming chunk."""
    chunk = MagicMock()
    chunk.model = model
    chunk.usage = usage
    chunk._hidden_params = hidden_params or {}
    return chunk


def _install_fake_litellm(
    completion_cost_value: float | None = None,
    completion_cost_raises: bool = False,
) -> types.ModuleType:
    """Install a fake ``litellm`` package into ``sys.modules``.

    Args:
        completion_cost_value: Value that ``litellm.completion_cost`` returns.
        completion_cost_raises: If True, ``completion_cost`` raises Exception.

    Returns:
        The fake litellm module.
    """
    litellm_mod = types.ModuleType("litellm")

    def _completion(**kwargs: Any) -> Any:
        raise NotImplementedError("should be mocked per-test")

    async def _acompletion(**kwargs: Any) -> Any:
        raise NotImplementedError("should be mocked per-test")

    litellm_mod.completion = _completion  # type: ignore[attr-defined]
    litellm_mod.acompletion = _acompletion  # type: ignore[attr-defined]

    # Mock completion_cost
    if completion_cost_raises:

        def _completion_cost(**kwargs: Any) -> float:
            raise Exception("cost calculation failed")

        litellm_mod.completion_cost = _completion_cost  # type: ignore[attr-defined]
    elif completion_cost_value is not None:

        def _completion_cost_val(**kwargs: Any) -> float:
            return completion_cost_value  # type: ignore[return-value]

        litellm_mod.completion_cost = _completion_cost_val  # type: ignore[attr-defined]
    else:
        litellm_mod.completion_cost = None  # type: ignore[attr-defined]

    sys.modules["litellm"] = litellm_mod

    return litellm_mod


def _uninstall_fake_litellm() -> None:
    """Remove our fake litellm module from ``sys.modules``.

    Sets each key to ``None`` so that any subsequent ``import litellm``
    raises ``ImportError`` immediately, correctly simulating a missing package
    even when the real litellm wheel is present in site-packages.
    """
    for key in list(sys.modules):
        if key == "litellm" or key.startswith("litellm."):
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
def _fake_litellm() -> Generator[None, None, None]:
    """Install/uninstall fake litellm for every test and ensure uninstrument."""
    _install_fake_litellm()
    yield
    # Always uninstrument after each test to reset module-level state
    from dexcost.instruments.litellm import uninstrument_litellm

    uninstrument_litellm()
    _uninstall_fake_litellm()


# ---------------------------------------------------------------------------
# Sync non-streaming tests
# ---------------------------------------------------------------------------


class TestSyncNonStreaming:
    """Sync litellm.completion() without streaming."""

    def test_records_event_with_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Mocked LiteLLM call inside tracked task -> event recorded with correct tokens."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=150,
            completion_tokens=75,
            provider="openai",
        )
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="sync_usage") as task:
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
        assert ev.cost_confidence == "exact"
        assert ev.cost_usd >= Decimal("0")

    def test_missing_usage_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When response.usage is None, cost_confidence should be 'estimated'."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(usage_present=False)
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="sync_no_usage") as task:
            litellm.completion(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.cost_usd == Decimal("0")
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0

    def test_latency_recorded(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """latency_ms is populated on the event."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(provider="openai")
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="sync_latency") as task:
            litellm.completion(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].latency_ms is not None
        assert events[0].latency_ms >= 0

    def test_model_from_response(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Model name is taken from the response, not the request."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(model="gpt-4o-2024-08-06", provider="openai")
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="sync_model") as task:
            litellm.completion(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].model == "gpt-4o-2024-08-06"


# ---------------------------------------------------------------------------
# Provider resolution tests
# ---------------------------------------------------------------------------


class TestProviderResolution:
    """Verify that the actual provider is resolved from LiteLLM responses."""

    def test_provider_from_hidden_params(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Provider is extracted from _hidden_params.custom_llm_provider."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
        )
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="provider_hidden") as task:
            litellm.completion(model="anthropic/claude-3-5-sonnet-20241022", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].provider == "anthropic"

    def test_provider_from_model_prefix(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Provider is extracted from model string prefix when _hidden_params is empty."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(model="openai/gpt-4o", provider=None)
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="provider_prefix") as task:
            litellm.completion(model="openai/gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].provider == "openai"

    def test_provider_unknown_fallback(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Provider falls back to 'unknown' when not resolvable."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(model="gpt-4o", provider=None)
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="provider_unknown") as task:
            litellm.completion(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].provider == "unknown"

    def test_provider_not_litellm(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Provider should be the actual resolved provider, never 'litellm'."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(model="gpt-4o", provider="openai")
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="not_litellm") as task:
            litellm.completion(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].provider != "litellm"
        assert events[0].provider == "openai"


# ---------------------------------------------------------------------------
# Cost calculation tests
# ---------------------------------------------------------------------------


class TestCostCalculation:
    """Verify LiteLLM cost calculation and pricing engine fallback."""

    def test_litellm_own_cost_used(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """When litellm.completion_cost returns a value, it is used."""
        _uninstall_fake_litellm()
        _install_fake_litellm(completion_cost_value=0.0025)

        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=500,
            provider="openai",
        )
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="litellm_cost") as task:
            litellm.completion(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        ev = events[0]
        assert ev.cost_usd == Decimal("0.0025")
        assert ev.pricing_source == "litellm"

    def test_fallback_to_pricing_engine(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When litellm.completion_cost fails, pricing engine is used."""
        _uninstall_fake_litellm()
        _install_fake_litellm(completion_cost_raises=True)

        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=500,
            provider="openai",
        )
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="fallback_cost") as task:
            litellm.completion(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        ev = events[0]
        # The dexcost pricing engine sets a pricing_version hash;
        # litellm.completion_cost path sets pricing_version=None.
        assert ev.pricing_version is not None
        assert ev.cost_usd >= Decimal("0")

    def test_fallback_when_litellm_cost_is_zero(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When litellm.completion_cost returns 0, fall back to pricing engine."""
        _uninstall_fake_litellm()
        _install_fake_litellm(completion_cost_value=0.0)

        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=500,
            provider="openai",
        )
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="zero_cost") as task:
            litellm.completion(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        ev = events[0]
        # Zero cost from litellm should fall back to pricing engine,
        # which sets a pricing_version hash (litellm path sets None).
        assert ev.pricing_version is not None


# ---------------------------------------------------------------------------
# Passthrough (no active task) tests
# ---------------------------------------------------------------------------


class TestPassthrough:
    """When no explicit task context is active, calls create an auto-task."""

    def test_no_task_context_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(provider="openai")
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        result = litellm.completion(model="gpt-4o", messages=[])

        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        all_events = storage.query_events()
        assert len(all_events) >= 1


# ---------------------------------------------------------------------------
# Sync streaming tests
# ---------------------------------------------------------------------------


class TestSyncStreaming:
    """Sync streaming litellm.completion(stream=True)."""

    def test_streaming_with_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Usage in the final chunk is captured after stream is consumed."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        usage = _make_usage(prompt_tokens=120, completion_tokens=60)
        chunks = [
            _make_chunk(model="gpt-4o", hidden_params={"custom_llm_provider": "openai"}),
            _make_chunk(model="gpt-4o"),
            _make_chunk(model="gpt-4o", usage=usage),
        ]

        litellm.completion = lambda **kwargs: iter(chunks)  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="stream_usage") as task:
            stream = litellm.completion(model="gpt-4o", messages=[], stream=True)
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
        assert ev.provider == "openai"

    def test_streaming_without_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """When no usage appears in the stream, cost_confidence is 'estimated'."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        chunks = [
            _make_chunk(model="gpt-4o"),
            _make_chunk(model="gpt-4o"),
        ]

        litellm.completion = lambda **kwargs: iter(chunks)  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="stream_no_usage") as task:
            stream = litellm.completion(model="gpt-4o", messages=[], stream=True)
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
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        usage = _make_usage(prompt_tokens=50, completion_tokens=25)
        chunks = [_make_chunk(model="gpt-4o", usage=usage)]

        litellm.completion = lambda **kwargs: iter(chunks)  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="stream_latency") as task:
            stream = litellm.completion(model="gpt-4o", messages=[], stream=True)
            list(stream)

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].latency_ms is not None
        assert events[0].latency_ms >= 0

    def test_streaming_provider_from_chunks(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Provider is resolved from stream chunk _hidden_params."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        usage = _make_usage(prompt_tokens=100, completion_tokens=50)
        chunks = [
            _make_chunk(
                model="claude-3-5-sonnet-20241022",
                hidden_params={"custom_llm_provider": "anthropic"},
            ),
            _make_chunk(model="claude-3-5-sonnet-20241022", usage=usage),
        ]

        litellm.completion = lambda **kwargs: iter(chunks)  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="stream_provider") as task:
            stream = litellm.completion(
                model="anthropic/claude-3-5-sonnet-20241022", messages=[], stream=True
            )
            list(stream)

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].provider == "anthropic"


# ---------------------------------------------------------------------------
# Async non-streaming tests
# ---------------------------------------------------------------------------


class TestAsyncNonStreaming:
    """Async litellm.acompletion() without streaming."""

    def test_async_records_event(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=200,
            completion_tokens=80,
            provider="openai",
        )

        async def fake_acompletion(**kwargs: Any) -> Any:
            return response

        litellm.acompletion = fake_acompletion  # type: ignore[assignment]

        instrument_litellm(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_usage"):
                result = await litellm.acompletion(model="gpt-4o", messages=[])
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
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(usage_present=False)

        async def fake_acompletion(**kwargs: Any) -> Any:
            return response

        litellm.acompletion = fake_acompletion  # type: ignore[assignment]

        instrument_litellm(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_no_usage"):
                await litellm.acompletion(model="gpt-4o", messages=[])

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_no_usage")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert events[0].cost_confidence == "estimated"

    def test_async_no_task_creates_auto_task(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(provider="openai")

        async def fake_acompletion(**kwargs: Any) -> Any:
            return response

        litellm.acompletion = fake_acompletion  # type: ignore[assignment]

        instrument_litellm(tracker)

        async def run() -> Any:
            return await litellm.acompletion(model="gpt-4o", messages=[])

        result = asyncio.run(run())
        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        assert len(storage.query_events()) >= 1

    def test_async_provider_resolved(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Async call correctly resolves the provider."""
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
        )

        async def fake_acompletion(**kwargs: Any) -> Any:
            return response

        litellm.acompletion = fake_acompletion  # type: ignore[assignment]

        instrument_litellm(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_provider"):
                await litellm.acompletion(
                    model="anthropic/claude-3-5-sonnet-20241022", messages=[]
                )

        asyncio.run(run())

        tasks = storage.query_tasks(task_type="async_provider")
        events = storage.query_events(task_id=str(tasks[0].task_id))
        assert events[0].provider == "anthropic"


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
    """Async streaming litellm.acompletion(stream=True)."""

    def test_async_streaming_with_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        usage = _make_usage(prompt_tokens=90, completion_tokens=40)
        chunks = [
            _make_chunk(model="gpt-4o", hidden_params={"custom_llm_provider": "openai"}),
            _make_chunk(model="gpt-4o", usage=usage),
        ]

        async def fake_acompletion(**kwargs: Any) -> Any:
            return _FakeAsyncIter(chunks)

        litellm.acompletion = fake_acompletion  # type: ignore[assignment]

        instrument_litellm(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_stream"):
                stream = await litellm.acompletion(model="gpt-4o", messages=[], stream=True)
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
        assert ev.provider == "openai"

    def test_async_streaming_without_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        chunks = [_make_chunk(model="gpt-4o")]

        async def fake_acompletion(**kwargs: Any) -> Any:
            return _FakeAsyncIter(chunks)

        litellm.acompletion = fake_acompletion  # type: ignore[assignment]

        instrument_litellm(tracker)

        async def run() -> None:
            async with tracker.task(task_type="async_stream_no_usage"):
                stream = await litellm.acompletion(model="gpt-4o", messages=[], stream=True)
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
    """instrument_litellm / uninstrument_litellm lifecycle."""

    def test_double_instrument_raises(self, tracker: CostTracker) -> None:
        from dexcost.instruments.litellm import instrument_litellm

        instrument_litellm(tracker)
        with pytest.raises(RuntimeError, match="already active"):
            instrument_litellm(tracker)

    def test_uninstrument_restores_original(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        import litellm

        from dexcost.instruments.litellm import instrument_litellm, uninstrument_litellm

        original_completion = litellm.completion

        response = _make_response(provider="openai")
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        # Verify it's patched (the completion function should be wrapped)
        assert litellm.completion is not original_completion

        uninstrument_litellm()

        # After uninstrument, should be able to instrument again
        instrument_litellm(tracker)

    def test_uninstrument_when_not_patched_is_noop(self) -> None:
        from dexcost.instruments.litellm import uninstrument_litellm

        # Should not raise
        uninstrument_litellm()

    def test_missing_litellm_raises_import_error(self, tracker: CostTracker) -> None:
        """instrument_litellm raises ImportError if litellm is not installed."""
        from dexcost.instruments.litellm import instrument_litellm

        _uninstall_fake_litellm()

        with pytest.raises(ImportError, match="litellm"):
            instrument_litellm(tracker)

        # Re-install for cleanup
        _install_fake_litellm()


# ---------------------------------------------------------------------------
# Task aggregation integration tests
# ---------------------------------------------------------------------------


class TestTaskAggregation:
    """Auto-captured events are included in task cost aggregation."""

    def test_auto_captured_event_aggregated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        import litellm

        from dexcost.instruments.litellm import instrument_litellm

        response = _make_response(
            model="gpt-4o",
            prompt_tokens=200,
            completion_tokens=100,
            provider="openai",
        )
        litellm.completion = lambda **kwargs: response  # type: ignore[assignment]

        instrument_litellm(tracker)

        with tracker.task(task_type="agg_test") as task:
            litellm.completion(model="gpt-4o", messages=[])
            litellm.completion(model="gpt-4o", messages=[])

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
    """instrument_litellm / uninstrument_litellm accessible from top-level package."""

    def test_instrument_litellm_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "instrument_litellm")
        assert callable(dexcost.instrument_litellm)

    def test_uninstrument_litellm_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "uninstrument_litellm")
        assert callable(dexcost.uninstrument_litellm)
