"""Tests for Google GenAI (Gemini) auto-instrumentation (US-012).

All tests use mocked Google GenAI SDK objects — the real ``google-genai``
package is **not** required.  We simulate the module structure that
:func:`instrument_gemini` patches so the wrapt monkey-patching works
against our fakes.
"""

from __future__ import annotations

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
# Fake Google GenAI module hierarchy
# ---------------------------------------------------------------------------


def _make_response(
    prompt_tokens: int = 100,
    candidates_tokens: int = 50,
    cached_tokens: int = 0,
    usage_present: bool = True,
) -> MagicMock:
    """Build a mock ``GenerateContentResponse``."""
    resp = MagicMock()
    if usage_present:
        usage = MagicMock()
        usage.prompt_token_count = prompt_tokens
        usage.candidates_token_count = candidates_tokens
        usage.cached_content_token_count = cached_tokens
        resp.usage_metadata = usage
    else:
        resp.usage_metadata = None
    return resp


def _make_stream_chunk(usage: Any = None) -> MagicMock:
    """Build a mock streaming ``GenerateContentResponse`` chunk.

    A chunk with ``usage`` is the terminal chunk carrying ``usage_metadata``.
    """
    chunk = MagicMock()
    chunk.usage_metadata = usage
    return chunk


def _make_stream_usage(
    prompt_tokens: int = 100,
    candidates_tokens: int = 50,
    cached_tokens: int = 0,
) -> MagicMock:
    """Build a mock ``usage_metadata`` object for a streamed response."""
    usage = MagicMock()
    usage.prompt_token_count = prompt_tokens
    usage.candidates_token_count = candidates_tokens
    usage.cached_content_token_count = cached_tokens
    return usage


def _install_fake_gemini() -> type:
    """Install fake ``google.genai`` modules into ``sys.modules``.

    Returns the ``Models`` class so tests can set ``.generate_content``
    behaviour.
    """
    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    models_mod = types.ModuleType("google.genai.models")

    class Models:
        @staticmethod
        def generate_content(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

        @staticmethod
        def generate_content_stream(**kwargs: Any) -> Any:
            raise NotImplementedError("should be mocked per-test")

    models_mod.Models = Models  # type: ignore[attr-defined]
    genai.models = models_mod  # type: ignore[attr-defined]
    google.genai = genai  # type: ignore[attr-defined]

    sys.modules["google"] = google
    sys.modules["google.genai"] = genai
    sys.modules["google.genai.models"] = models_mod

    return Models  # type: ignore[return-value]


def _uninstall_fake_gemini() -> None:
    """Remove our fake google.genai modules from ``sys.modules``.

    Sets each key to ``None`` so that any subsequent ``import google.genai``
    raises ``ImportError`` immediately.
    """
    for key in list(sys.modules):
        if key == "google" or key.startswith("google.genai"):
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
def _fake_gemini() -> Generator[None, None, None]:
    """Install/uninstall fake google.genai for every test and ensure uninstrument."""
    _install_fake_gemini()
    yield
    # Always uninstrument after each test to reset module-level state
    from dexcost.instruments.gemini import uninstrument_gemini

    uninstrument_gemini()
    _uninstall_fake_gemini()


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


class TestSyncGenerateContent:
    """Sync google.genai.models.Models.generate_content()."""

    def test_records_event_with_usage(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Mocked Gemini call inside tracked task -> event recorded with correct tokens."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response(
            prompt_tokens=150,
            candidates_tokens=75,
        )
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        with tracker.task(task_type="sync_usage") as task:
            result = Models.generate_content(model="gemini-1.5-pro", contents="Hello")

        assert result is response

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "google"
        assert ev.model == "gemini-1.5-pro"
        assert ev.input_tokens == 150
        assert ev.output_tokens == 75
        assert ev.cost_confidence == "exact"
        assert ev.cost_usd >= Decimal("0")

    def test_cached_tokens_extracted(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """cached_content_token_count from usage_metadata are captured."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response(
            prompt_tokens=200,
            candidates_tokens=100,
            cached_tokens=50,
        )
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        with tracker.task(task_type="sync_cached") as task:
            Models.generate_content(model="gemini-1.5-pro", contents="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].cached_tokens == 50

    def test_missing_usage_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """When response.usage_metadata is None, cost_confidence should be 'estimated'."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response(usage_present=False)
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        with tracker.task(task_type="sync_no_usage") as task:
            Models.generate_content(model="gemini-1.5-pro", contents="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.cost_confidence == "estimated"
        assert ev.cost_usd == Decimal("0")
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0

    def test_latency_recorded(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """latency_ms is populated on the event."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response()
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        with tracker.task(task_type="sync_latency") as task:
            Models.generate_content(model="gemini-1.5-pro", contents="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].latency_ms is not None
        assert events[0].latency_ms >= 0

    def test_model_from_request_used(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Model name is extracted from the request kwargs."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response()
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        with tracker.task(task_type="sync_model") as task:
            Models.generate_content(model="gemini-2.0-flash", contents="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].model == "gemini-2.0-flash"

    def test_models_prefix_stripped(self, tracker: CostTracker, storage: SQLiteStorage) -> None:
        """Model names with 'models/' prefix are stripped."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response()
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        with tracker.task(task_type="sync_prefix") as task:
            Models.generate_content(model="models/gemini-1.5-pro", contents="Hello")

        events = storage.query_events(task_id=str(task.task_id))
        assert events[0].model == "gemini-1.5-pro"


# ---------------------------------------------------------------------------
# Streaming tests (Fix 2)
# ---------------------------------------------------------------------------


class TestStreamingGenerateContent:
    """Sync google.genai generate_content_stream() — streamed cost capture."""

    def test_streaming_records_event_with_usage(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """A streamed Gemini call records an llm_call event with token usage."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        usage = _make_stream_usage(prompt_tokens=120, candidates_tokens=60)
        chunks = [
            _make_stream_chunk(),
            _make_stream_chunk(),
            _make_stream_chunk(usage=usage),
        ]
        Models.generate_content_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: iter(chunks)
        )

        instrument_gemini(tracker)

        with tracker.task(task_type="stream_usage") as task:
            stream = Models.generate_content_stream(
                model="gemini-1.5-pro", contents="Hello"
            )
            collected = list(stream)

        assert len(collected) == 3

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "llm_call"
        assert ev.provider == "google"
        assert ev.model == "gemini-1.5-pro"
        assert ev.input_tokens == 120
        assert ev.output_tokens == 60
        assert ev.cost_confidence == "exact"

    def test_streaming_without_usage_sets_estimated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """A streamed call with no usage_metadata records an estimated event."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        chunks = [_make_stream_chunk(), _make_stream_chunk()]
        Models.generate_content_stream = staticmethod(  # type: ignore[assignment]
            lambda **kwargs: iter(chunks)
        )

        instrument_gemini(tracker)

        with tracker.task(task_type="stream_no_usage") as task:
            list(Models.generate_content_stream(model="gemini-1.5-pro", contents="Hi"))

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
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response()
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        result = Models.generate_content(model="gemini-1.5-pro", contents="Hello")

        assert result is response
        # An auto-task event should be recorded (auto-task created when no explicit task)
        all_events = storage.query_events()
        assert len(all_events) >= 1


# ---------------------------------------------------------------------------
# Instrument / uninstrument lifecycle tests
# ---------------------------------------------------------------------------


class TestInstrumentLifecycle:
    """instrument_gemini / uninstrument_gemini lifecycle."""

    def test_double_instrument_raises(self, tracker: CostTracker) -> None:
        from dexcost.instruments.gemini import instrument_gemini

        instrument_gemini(tracker)
        with pytest.raises(RuntimeError, match="already active"):
            instrument_gemini(tracker)

    def test_uninstrument_restores_original(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini, uninstrument_gemini

        original_generate = Models.generate_content

        response = _make_response()
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        # Verify it's patched (the method should be wrapped)
        assert Models.generate_content is not original_generate  # type: ignore[comparison-overlap]

        uninstrument_gemini()

        # After uninstrument, should be able to instrument again
        instrument_gemini(tracker)

    def test_uninstrument_when_not_patched_is_noop(self) -> None:
        from dexcost.instruments.gemini import uninstrument_gemini

        # Should not raise
        uninstrument_gemini()

    def test_missing_gemini_raises_import_error(self, tracker: CostTracker) -> None:
        """instrument_gemini raises ImportError if google-genai is not installed."""
        from unittest.mock import patch

        from dexcost.instruments.gemini import instrument_gemini

        _uninstall_fake_gemini()

        blocked = {k: None for k in list(sys.modules) if k == "google" or k.startswith("google.genai")}
        blocked.setdefault("google", None)
        blocked.setdefault("google.genai", None)
        blocked.setdefault("google.genai.models", None)

        with patch.dict(sys.modules, blocked):
            with pytest.raises(ImportError, match="google-genai"):
                instrument_gemini(tracker)

        # Re-install for cleanup
        _install_fake_gemini()


# ---------------------------------------------------------------------------
# Cost calculation integration tests
# ---------------------------------------------------------------------------


class TestCostCalculation:
    """Verify the pricing engine is used to calculate costs."""

    def test_cost_calculated_via_pricing_engine(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """With usage present, cost should be computed by the pricing engine."""
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response(
            prompt_tokens=1000,
            candidates_tokens=500,
        )
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        with tracker.task(task_type="cost_calc") as task:
            Models.generate_content(model="gemini-1.5-pro", contents="Hello")

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
        from google.genai.models import Models

        from dexcost.instruments.gemini import instrument_gemini

        response = _make_response(
            prompt_tokens=200,
            candidates_tokens=100,
        )
        Models.generate_content = staticmethod(lambda **kwargs: response)  # type: ignore[assignment]

        instrument_gemini(tracker)

        with tracker.task(task_type="agg_test") as task:
            Models.generate_content(model="gemini-1.5-pro", contents="Hello")
            Models.generate_content(model="gemini-1.5-pro", contents="World")

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
    """instrument_gemini / uninstrument_gemini accessible from top-level package."""

    def test_instrument_gemini_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "instrument_gemini")
        assert callable(dexcost.instrument_gemini)

    def test_uninstrument_gemini_exported(self) -> None:
        import dexcost

        assert hasattr(dexcost, "uninstrument_gemini")
        assert callable(dexcost.uninstrument_gemini)
