"""Provider errors raised *while a stream is consumed* become failed operations.

A streaming call has two distinct failure points, and only the first was
covered before:

1. **Stream creation** — the provider SDK raises from the ``create``/
   ``chat_stream`` call itself. The ``try`` around ``wrapped(...)`` handles it.
2. **Stream consumption** — the SDK hands back a stream and the error surfaces
   later, from ``next()`` / ``__anext__()``, as the response is pulled over the
   wire. This is the *normal* failure mode for long generations, and it used to
   be lost entirely: no failed event was persisted and an auto-task started for
   the call was left open forever.

These tests pin (2) for the sync and async OpenAI paths, and separately pin the
Cohere stream wrappers against an ``UnboundLocalError`` that used to replace the
provider's own exception when a stream failed to open inside an explicit task.

The provider SDKs are faked here exactly as in ``test_openai_instrument.py`` and
``test_cohere_instrument.py``; neither real wheel is required.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


class _StreamBoom(Exception):
    """Shaped like a provider transport error surfaced mid-generation."""

    def __init__(self, message: str = "connection reset", status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Fake OpenAI
# ---------------------------------------------------------------------------


def _install_fake_openai() -> tuple[type[Any], type[Any]]:
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
    return Completions, AsyncCompletions


def _install_fake_cohere() -> tuple[type[Any], type[Any]]:
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


def _uninstall(prefix: str) -> None:
    for key in list(sys.modules):
        if key == prefix or key.startswith(f"{prefix}."):
            sys.modules[key] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    s = SQLiteStorage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _fake_sdks() -> Generator[None, None, None]:
    prefixes = ("openai", "cohere")
    installed_modules = {
        key: module
        for key, module in sys.modules.items()
        if any(key == prefix or key.startswith(f"{prefix}.") for prefix in prefixes)
    }
    _install_fake_openai()
    _install_fake_cohere()
    yield
    from dexcost.instruments.cohere import uninstrument_cohere
    from dexcost.instruments.openai import uninstrument_openai

    uninstrument_openai()
    uninstrument_cohere()
    _uninstall("openai")
    _uninstall("cohere")
    for key in list(sys.modules):
        if any(key == prefix or key.startswith(f"{prefix}.") for prefix in prefixes):
            sys.modules.pop(key, None)
    sys.modules.update(installed_modules)


def _chunk(model: str = "gpt-4o") -> MagicMock:
    chunk = MagicMock()
    chunk.model = model
    chunk.usage = None
    chunk.id = None
    return chunk


# ---------------------------------------------------------------------------
# 1. A failure raised while consuming a stream is recorded
# ---------------------------------------------------------------------------


class TestSyncStreamConsumptionFailure:
    def test_records_failed_event_and_reraises(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """The stream opens, yields, then dies. The dead call is still recorded."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        boom = _StreamBoom()

        def _stream(**kwargs: Any) -> Any:
            yield _chunk()
            raise boom

        Completions.create = staticmethod(lambda **kw: _stream(**kw))  # type: ignore[assignment]
        instrument_openai(tracker)

        with tracker.task(task_type="stream_failure") as task:
            stream = Completions.create(model="gpt-4o", messages=[], stream=True)
            with pytest.raises(_StreamBoom) as raised:
                list(stream)

        # The user's own exception object propagates, unwrapped and unaltered.
        assert raised.value is boom

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "llm_call"
        assert event.provider == "openai"
        assert event.model == "gpt-4o"
        assert event.details["error_type"] == "streamboom"
        assert event.details["error_code"] == "502"
        # Nothing is fabricated for a call that never delivered a usage total.
        assert event.cost_confidence == "unknown"

        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["operation"]["status"] == "failed"
        assert converted["operation"]["error"]["type"] == "streamboom"

    def test_failure_before_any_chunk_still_names_the_requested_model(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """No chunk ever arrives, so the model can only come from the request."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        def _stream(**kwargs: Any) -> Any:
            raise _StreamBoom("died before first token")
            yield  # pragma: no cover - unreachable, makes this a generator

        Completions.create = staticmethod(lambda **kw: _stream(**kw))  # type: ignore[assignment]
        instrument_openai(tracker)

        with tracker.task(task_type="stream_failure") as task:
            stream = Completions.create(model="gpt-4o-mini", messages=[], stream=True)
            with pytest.raises(_StreamBoom):
                list(stream)

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].model == "gpt-4o-mini"

    def test_a_dead_stream_does_not_also_record_a_success(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Closing the stream after it died must not add a second, priced event."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        def _stream(**kwargs: Any) -> Any:
            yield _chunk()
            raise _StreamBoom()

        Completions.create = staticmethod(lambda **kw: _stream(**kw))  # type: ignore[assignment]
        instrument_openai(tracker)

        with tracker.task(task_type="stream_failure") as task:
            stream = Completions.create(model="gpt-4o", messages=[], stream=True)
            with pytest.raises(_StreamBoom):
                list(stream)
            stream.close()

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details["error_type"] == "streamboom"


class TestAsyncStreamConsumptionFailure:
    def test_records_failed_event_and_reraises(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        boom = _StreamBoom()

        class _AsyncStream:
            def __init__(self) -> None:
                self._sent = False

            def __aiter__(self) -> _AsyncStream:
                return self

            async def __anext__(self) -> Any:
                if not self._sent:
                    self._sent = True
                    return _chunk()
                raise boom

        async def _create(**kwargs: Any) -> Any:
            return _AsyncStream()

        AsyncCompletions.create = staticmethod(_create)  # type: ignore[assignment]
        instrument_openai(tracker)

        async def _run(task_obj: Any) -> None:
            stream = await AsyncCompletions.create(model="gpt-4o", messages=[], stream=True)
            with pytest.raises(_StreamBoom) as raised:
                async for _ in stream:
                    pass
            assert raised.value is boom

        with tracker.task(task_type="async_stream_failure") as task:
            asyncio.run(_run(task))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].provider == "openai"
        assert events[0].details["error_type"] == "streamboom"


class TestAutoTaskIsClosedByAStreamFailure:
    def test_auto_task_is_finalized_as_failed(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Without an explicit task the wrapper opens one — a dead stream must close it."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        def _stream(**kwargs: Any) -> Any:
            yield _chunk()
            raise _StreamBoom()

        Completions.create = staticmethod(lambda **kw: _stream(**kw))  # type: ignore[assignment]
        instrument_openai(tracker)

        stream = Completions.create(model="gpt-4o", messages=[], stream=True)
        with pytest.raises(_StreamBoom):
            list(stream)

        tasks = storage.query_tasks()
        assert len(tasks) == 1
        # The auto-task is closed, not left dangling, and it is marked failed.
        assert tasks[0].status == "failed"
        assert tasks[0].ended_at is not None


# ---------------------------------------------------------------------------
# 2. Cohere: a stream that fails to open inside an explicit task
# ---------------------------------------------------------------------------


class TestCohereStreamCreationFailureInsideExplicitTask:
    """Regression: ``auto_task_obj`` was unbound on this path.

    Inside an explicit task no auto-task is created, so the name was never
    assigned; the failure handler then raised ``UnboundLocalError``, which
    *replaced* the provider's own exception and skipped recording entirely.
    """

    def test_sync_stream_creation_failure_records_and_reraises(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        boom = _StreamBoom("cohere is down")

        def _raise(**kwargs: Any) -> Any:
            raise boom

        Client.chat_stream = staticmethod(_raise)  # type: ignore[assignment]
        instrument_cohere(tracker)

        with tracker.task(task_type="cohere_stream_failure") as task:
            with pytest.raises(_StreamBoom) as raised:
                Client.chat_stream(model="command-r-plus", message="hi")

        assert raised.value is boom
        assert not isinstance(raised.value, UnboundLocalError)

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].provider == "cohere"
        assert events[0].details["error_type"] == "streamboom"

    def test_async_stream_creation_failure_records_and_reraises(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from cohere import AsyncClient

        from dexcost.instruments.cohere import instrument_cohere

        boom = _StreamBoom("cohere is down")

        def _raise(**kwargs: Any) -> Any:
            raise boom

        AsyncClient.chat_stream = staticmethod(_raise)  # type: ignore[assignment]
        instrument_cohere(tracker)

        with tracker.task(task_type="cohere_async_stream_failure") as task:
            with pytest.raises(_StreamBoom) as raised:
                AsyncClient.chat_stream(model="command-r-plus", message="hi")

        assert raised.value is boom

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].provider == "cohere"

    def test_cohere_mid_stream_failure_is_recorded(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """The stream opens, yields, then dies partway through."""
        from cohere import Client

        from dexcost.instruments.cohere import instrument_cohere

        def _stream(**kwargs: Any) -> Any:
            event = MagicMock()
            event.event_type = "text-generation"
            yield event
            raise _StreamBoom()

        Client.chat_stream = staticmethod(lambda **kw: _stream(**kw))  # type: ignore[assignment]
        instrument_cohere(tracker)

        with tracker.task(task_type="cohere_mid_stream") as task:
            stream = Client.chat_stream(model="command-r-plus", message="hi")
            with pytest.raises(_StreamBoom):
                list(stream)

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].provider == "cohere"
        assert events[0].details["error_type"] == "streamboom"
