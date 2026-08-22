"""Exceptions raised by instrumented provider calls become failed operations.

When an auto-instrumented provider call raises, the call still happened — it
just failed — so the SDK records an event that converts to attribution-v3 with
``operation.status == "failed"`` and an ``operation.error`` identity, and then
re-raises the user's exception untouched.

The OpenAI SDK is faked here exactly as in ``test_openai_instrument.py``; the
real ``openai`` package is not required.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

# ---------------------------------------------------------------------------
# Fake OpenAI module hierarchy
# ---------------------------------------------------------------------------


def _install_fake_openai() -> tuple[type[Any], type[Any]]:
    """Install a fake ``openai`` package and return (Completions, AsyncCompletions)."""
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


def _uninstall_fake_openai() -> None:
    for key in list(sys.modules):
        if key == "openai" or key.startswith("openai."):
            sys.modules[key] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Provider-shaped exceptions
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Shaped like ``openai.RateLimitError``: carries an HTTP status code."""

    def __init__(self, message: str, status_code: int = 429) -> None:
        super().__init__(message)
        self.status_code = status_code


class InsufficientQuotaError(Exception):
    """Shaped like a provider error whose ``code`` is a string."""

    def __init__(self, message: str, code: str = "insufficient_quota") -> None:
        super().__init__(message)
        self.code = code


class _Boom(Exception):
    """A plain exception with no code of any kind."""


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
def _fake_openai() -> Generator[None, None, None]:
    installed_modules = {
        key: module
        for key, module in sys.modules.items()
        if key == "openai" or key.startswith("openai.")
    }
    _install_fake_openai()
    yield
    from dexcost.instruments.openai import uninstrument_openai

    uninstrument_openai()
    _uninstall_fake_openai()
    for key in list(sys.modules):
        if key == "openai" or key.startswith("openai."):
            sys.modules.pop(key, None)
    sys.modules.update(installed_modules)


def _raise_on_create(exc: BaseException) -> Any:
    def _create(**kwargs: Any) -> Any:
        raise exc

    return _create


def _call_and_capture(
    tracker: CostTracker,
    storage: SQLiteStorage,
    exc: BaseException,
    *,
    model: str = "gpt-4o",
) -> Event:
    """Make one raising instrumented call; assert it re-raises; return the event."""
    from openai.resources.chat.completions import Completions

    from dexcost.instruments.openai import instrument_openai

    Completions.create = _raise_on_create(exc)  # type: ignore[assignment]
    instrument_openai(tracker)

    with tracker.task(task_type="failure") as task:
        with pytest.raises(type(exc)) as raised:
            Completions.create(model=model, messages=[])
    assert raised.value is exc

    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    return events[0]


# ---------------------------------------------------------------------------
# 1. A raising call records a failed event and re-raises
# ---------------------------------------------------------------------------


class TestRaisingCallRecordsFailure:
    def test_records_failed_event_and_reraises(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """The original exception type propagates, and a failed event lands."""
        event = _call_and_capture(tracker, storage, RateLimitError("slow down"))

        assert event.event_type == "llm_call"
        assert event.provider == "openai"
        assert event.model == "gpt-4o"
        assert event.details["error_type"] == "ratelimiterror"

        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["operation"]["status"] == "failed"
        assert converted["operation"]["error"]["type"] == "ratelimiterror"

    def test_original_traceback_is_preserved(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Instrumentation re-raises with ``raise``, so the frame chain survives."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        def _create(**kwargs: Any) -> Any:
            raise _Boom("kaboom")

        Completions.create = _create  # type: ignore[assignment]
        instrument_openai(tracker)

        with tracker.task(task_type="failure"):
            with pytest.raises(_Boom) as raised:
                Completions.create(model="gpt-4o", messages=[])

        frames = []
        tb = raised.value.__traceback__
        while tb is not None:
            frames.append(tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        assert "_create" in frames
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__ is False

    def test_no_usage_or_cost_is_fabricated(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """Usage is unknown on a failure — nothing is invented."""
        event = _call_and_capture(tracker, storage, _Boom("nope"))

        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "unknown"
        assert event.pricing_source == "unknown"
        assert event.input_tokens is None
        assert event.output_tokens is None

        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert "cost_evidence" not in converted
        # Only the request itself is asserted, with no token counts.
        assert [line["metric"] for line in converted["usage"]] == ["request_count"]

    def test_latency_is_still_measured(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """A failed call is still timed, so latency_ms reaches the wire."""
        event = _call_and_capture(tracker, storage, _Boom("nope"))
        assert event.latency_ms is not None
        assert event.latency_ms >= 0

        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["operation"]["latency_ms"] >= 0

    def test_async_raising_call_records_failure(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """The async wrapper records failures too."""
        from openai.resources.chat.completions import AsyncCompletions

        from dexcost.instruments.openai import instrument_openai

        async def _create(**kwargs: Any) -> Any:
            raise RateLimitError("slow down")

        AsyncCompletions.create = _create  # type: ignore[assignment]
        instrument_openai(tracker)

        with tracker.task(task_type="async_failure") as task:
            with pytest.raises(RateLimitError):
                asyncio.run(AsyncCompletions.create(model="gpt-4o", messages=[]))

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details["error_type"] == "ratelimiterror"

    def test_successful_call_records_no_error(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """The happy path is untouched: no error marker, status succeeded."""
        from types import SimpleNamespace

        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        def _create(**kwargs: Any) -> Any:
            return SimpleNamespace(
                model="gpt-4o",
                id="chatcmpl_ok",
                usage=SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=5,
                    prompt_tokens_details=None,
                    completion_tokens_details=None,
                ),
            )

        Completions.create = _create  # type: ignore[assignment]
        instrument_openai(tracker)

        with tracker.task(task_type="ok") as task:
            Completions.create(model="gpt-4o", messages=[])

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert "error_type" not in events[0].details

        converted = to_attribution_observation_v3(events[0])
        assert converted is not None
        assert converted["operation"]["status"] == "succeeded"
        assert "error" not in converted["operation"]


# ---------------------------------------------------------------------------
# 2. Error codes
# ---------------------------------------------------------------------------


class TestErrorCodeCapture:
    def test_status_code_is_captured(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """``exc.status_code`` becomes ``operation.error.code``."""
        event = _call_and_capture(tracker, storage, RateLimitError("slow down", 429))
        assert event.details["error_code"] == "429"

        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["operation"]["error"] == {
            "type": "ratelimiterror",
            "code": "429",
        }

    def test_string_code_is_captured(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """``exc.code`` wins over anything else and is carried verbatim."""
        event = _call_and_capture(tracker, storage, InsufficientQuotaError("no funds"))
        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["operation"]["error"] == {
            "type": "insufficientquotaerror",
            "code": "insufficient_quota",
        }

    def test_code_is_omitted_when_absent(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """No code on the exception → the optional wire field is omitted."""
        event = _call_and_capture(tracker, storage, _Boom("nope"))
        assert "error_code" not in event.details

        converted = to_attribution_observation_v3(event)
        assert converted is not None
        # A leading underscore cannot start a canonical token, so it is dropped.
        assert converted["operation"]["error"] == {"type": "boom"}

    def test_code_is_truncated_to_64_characters(self) -> None:
        from dexcost.instruments._errors import error_code_of

        exc = InsufficientQuotaError("no funds", code="x" * 200)
        code = error_code_of(exc)
        assert code is not None
        assert len(code) == 64

    def test_botocore_style_error_envelope(self) -> None:
        """A botocore-shaped ``response['Error']['Code']`` is picked up."""
        from dexcost.instruments._errors import error_code_of

        class _ClientError(Exception):
            def __init__(self) -> None:
                super().__init__("boom")
                self.response = {"Error": {"Code": "ThrottlingException"}}

        assert error_code_of(_ClientError()) == "ThrottlingException"

    def test_non_scalar_code_is_ignored(self) -> None:
        """A ``code`` that is not a string or int is dropped, not stringified."""
        from dexcost.instruments._errors import error_code_of

        class _Weird(Exception):
            code = {"nested": "object"}

        assert error_code_of(_Weird()) is None


# ---------------------------------------------------------------------------
# 3. Canonicalisation of the error type
# ---------------------------------------------------------------------------


class TestErrorTypeCanonicalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("RateLimitError", "ratelimiterror"),
            ("APIConnectionError", "apiconnectionerror"),
            ("Weird Error!Name", "weird_error_name"),
            ("rate_limit", "rate_limit"),
            ("api.timeout-error", "api.timeout-error"),
            ("___leading", "leading"),
            ("", "unknown_error"),
            ("!!!", "unknown_error"),
        ],
    )
    def test_canonical_error_type(self, raw: str, expected: str) -> None:
        from dexcost.instruments._errors import canonical_error_type

        assert canonical_error_type(raw) == expected

    def test_long_name_is_truncated_to_127(self) -> None:
        from dexcost.instruments._errors import canonical_error_type

        assert len(canonical_error_type("E" * 500)) == 127

    def test_converter_drops_an_uncanonicalisable_error_type(self) -> None:
        """A details value that cannot be canonicalised yields no error object."""
        event = Event(
            task_id=uuid.uuid4(),
            event_type="llm_call",
            cost_usd=Decimal("0"),
            cost_confidence="unknown",
            pricing_source="unknown",
            provider="openai",
            model="gpt-4o",
            details={"error_type": "!!!"},
        )
        converted = to_attribution_observation_v3(event)
        assert converted is not None
        # The failure marker still forces the status, but no invalid error
        # identity is put on the wire.
        assert converted["operation"]["status"] == "failed"
        assert "error" not in converted["operation"]


# ---------------------------------------------------------------------------
# 4. The converter refuses to attach an error to a succeeded operation
# ---------------------------------------------------------------------------


class TestSucceededOperationsNeverCarryError:
    def _event(self, **details: Any) -> Event:
        return Event(
            task_id=uuid.uuid4(),
            event_type="llm_call",
            cost_usd=Decimal("0"),
            cost_confidence="unknown",
            pricing_source="unknown",
            provider="openai",
            model="gpt-4o",
            input_tokens=10,
            output_tokens=5,
            details=details,
        )

    def test_explicit_succeeded_status_strips_the_error(self) -> None:
        """The server rejects error-on-succeeded, so the converter must not emit it."""
        event = self._event(
            attribution_operation_status="succeeded",
            error_type="ratelimiterror",
            error_code="429",
        )
        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["operation"]["status"] == "succeeded"
        assert "error" not in converted["operation"]

    # ``in_progress`` is excluded: a final-lifecycle observation cannot carry it.
    @pytest.mark.parametrize("status", ["failed", "cancelled", "unknown"])
    def test_non_succeeded_statuses_keep_the_error(self, status: str) -> None:
        event = self._event(
            attribution_operation_status=status,
            error_type="ratelimiterror",
        )
        converted = to_attribution_observation_v3(event)
        assert converted is not None
        assert converted["operation"]["status"] == status
        assert converted["operation"]["error"] == {"type": "ratelimiterror"}


# ---------------------------------------------------------------------------
# 5. A failure inside our own recording never propagates
# ---------------------------------------------------------------------------


class TestRecordingFailuresAreContained:
    def test_broken_storage_does_not_mask_the_user_exception(
        self, tracker: CostTracker, storage: SQLiteStorage
    ) -> None:
        """If our insert blows up, the user still sees *their* exception."""
        from openai.resources.chat.completions import Completions

        from dexcost.instruments.openai import instrument_openai

        def _create(**kwargs: Any) -> Any:
            raise RateLimitError("slow down")

        def _explode(event: Event) -> None:
            raise RuntimeError("storage is on fire")

        Completions.create = _create  # type: ignore[assignment]
        instrument_openai(tracker)
        storage.insert_event = _explode  # type: ignore[assignment]

        with tracker.task(task_type="broken_storage"):
            with pytest.raises(RateLimitError):
                Completions.create(model="gpt-4o", messages=[])

    def test_recording_failure_is_logged_not_raised(
        self, tracker: CostTracker, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``record_call_failure`` swallows and logs its own errors."""
        from dexcost.instruments._errors import record_call_failure

        class _BrokenStorage:
            def insert_event(self, event: Event) -> None:
                raise RuntimeError("storage is on fire")

        class _BrokenTracker:
            _storage = _BrokenStorage()

        task = Task(task_type="broken")
        with caplog.at_level(logging.DEBUG, logger="dexcost.instruments._errors"):
            result = record_call_failure(
                tracker=_BrokenTracker(),
                exc=RateLimitError("slow down"),
                provider="openai",
                model="gpt-4o",
                latency_ms=1,
                task=task,
            )
        assert result is None
        assert "failed to record call failure" in caplog.text

    def test_no_tracker_is_a_silent_no_op(self) -> None:
        from dexcost.instruments._errors import record_call_failure

        assert (
            record_call_failure(
                tracker=None,
                exc=RateLimitError("slow down"),
                provider="openai",
                task=Task(task_type="no_tracker"),
            )
            is None
        )

    def test_no_active_task_is_a_silent_no_op(self, tracker: CostTracker) -> None:
        from dexcost.context import _current_task
        from dexcost.instruments._errors import record_call_failure

        token = _current_task.set(None)
        try:
            assert (
                record_call_failure(
                    tracker=tracker,
                    exc=RateLimitError("slow down"),
                    provider="openai",
                )
                is None
            )
        finally:
            _current_task.reset(token)

    def test_finalize_failed_auto_task_never_raises(self) -> None:
        from dexcost.instruments._errors import finalize_failed_auto_task

        class _BrokenTracker:
            @property
            def _storage(self) -> Any:
                raise RuntimeError("storage is on fire")

        event = Event(
            task_id=uuid.uuid4(),
            event_type="llm_call",
            cost_usd=Decimal("0"),
            cost_confidence="unknown",
            pricing_source="unknown",
        )
        finalize_failed_auto_task(_BrokenTracker(), object(), event)


# ---------------------------------------------------------------------------
# 6. Every provider instrument is wired the same way
# ---------------------------------------------------------------------------


class TestAllInstrumentsShareTheFailurePath:
    @pytest.mark.parametrize(
        ("module", "session_factory"),
        [
            ("openai", None),
            ("anthropic", None),
            ("bedrock", None),
            ("cohere", None),
            ("litellm", None),
            ("gemini", "_session"),
            ("ollama", "_new_session"),
            ("openrouter", "_session"),
            ("perplexity", "_session"),
            ("fal", "_operation_session"),
        ],
    )
    def test_instrument_records_call_failures(
        self, module: str, session_factory: str | None
    ) -> None:
        """Each provider instrument routes raised calls through the shared helper."""
        import importlib

        instrument = importlib.import_module(f"dexcost.instruments.{module}")
        if session_factory is not None:
            # Current adapters use the shared provider operation session,
            # whose fail() path preserves the native exception/error identity.
            assert hasattr(instrument, "ProviderOperationSession")
            assert hasattr(instrument, session_factory)
        else:
            assert hasattr(instrument, "_record_call_failure")
            assert hasattr(instrument, "record_call_failure")
