"""Shared provider streams retain usage observed before terminal failures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


class _StreamFailureError(RuntimeError):
    pass


class _FailingSyncStream(Iterator[int]):
    def __init__(self, *, exit_failure: bool = False) -> None:
        self._sent = False
        self._exit_failure = exit_failure

    def __iter__(self) -> _FailingSyncStream:
        return self

    def __next__(self) -> int:
        if not self._sent:
            self._sent = True
            return 7
        raise _StreamFailureError("sync stream failed")

    def __enter__(self) -> _FailingSyncStream:
        return self

    def __exit__(self, *_: Any) -> None:
        if self._exit_failure:
            raise _StreamFailureError("sync stream exit failed")


class _FailingAsyncStream(AsyncIterator[int]):
    def __init__(self, *, exit_failure: bool = False) -> None:
        self._sent = False
        self._exit_failure = exit_failure

    def __aiter__(self) -> _FailingAsyncStream:
        return self

    async def __anext__(self) -> int:
        if not self._sent:
            self._sent = True
            return 9
        raise _StreamFailureError("async stream failed")

    async def __aenter__(self) -> _FailingAsyncStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._exit_failure:
            raise _StreamFailureError("async stream exit failed")


class _ValuesSyncStream(Iterator[int]):
    def __init__(self) -> None:
        self._values = iter((2, 3))

    def __iter__(self) -> _ValuesSyncStream:
        return self

    def __next__(self) -> int:
        return next(self._values)

    def close(self) -> None:
        return None


class _ValuesAsyncStream(AsyncIterator[int]):
    def __init__(self) -> None:
        self._values = iter((4, 5))

    def __aiter__(self) -> _ValuesAsyncStream:
        return self

    async def __anext__(self) -> int:
        try:
            return next(self._values)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        return None


def _measurement(quantity: int) -> OperationMeasurement:
    return OperationMeasurement(
        pricing_usage={"output_tokens": quantity},
        usage_lines=(ProviderUsageLine("output_tokens", quantity, "Tokens"),),
        task_output_tokens=quantity,
    )


def _session(tracker: CostTracker) -> ProviderOperationSession:
    return ProviderOperationSession(
        tracker=tracker,
        task_type="provider.partial-stream",
        provider="openrouter",
        service="chat",
        operation="openrouter.chat.completions.create",
        component="llm",
        model="openai/gpt-4o",
        event_type="llm_call",
    )


def _assert_partial_failure(storage: SQLiteStorage, task_id: str, quantity: int) -> None:
    events = storage.query_events(task_id=task_id)
    assert len(events) == 1
    event = events[0]
    assert event.output_tokens == quantity
    assert event.details["attribution_operation_status"] == "failed"
    assert event.details["error_type"] == "streamfailureerror"
    assert event.details["attribution_usage_lines"] == [
        {"metric": "output_tokens", "quantity": str(quantity), "unit": "Tokens"}
    ]
    converted = to_attribution_observation_v3(event)
    assert converted is not None
    assert converted["operation"]["status"] == "failed"
    assert converted["usage"][0]["quantity"] == str(quantity)


def test_sync_iteration_failure_retains_partial_usage(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "sync-partial.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    observed = 0

    def observe(value: int) -> None:
        nonlocal observed
        observed += value

    try:
        with tracker.task(task_type="sync-partial") as task:
            stream = SyncProviderStream(
                _FailingSyncStream(),
                _session(tracker),
                observe=observe,
                measurement=lambda: _measurement(observed),
            )
            with pytest.raises(_StreamFailureError, match="sync stream failed"):
                list(stream)
        _assert_partial_failure(storage, str(task.task_id), 7)
    finally:
        storage.close()


def test_sync_context_exit_failure_retains_partial_usage(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "sync-exit-partial.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    observed = 0

    def observe(value: int) -> None:
        nonlocal observed
        observed += value

    try:
        with tracker.task(task_type="sync-exit-partial") as task:
            stream = SyncProviderStream(
                _FailingSyncStream(exit_failure=True),
                _session(tracker),
                observe=observe,
                measurement=lambda: _measurement(observed),
            )
            with pytest.raises(_StreamFailureError, match="exit failed"), stream:
                assert next(stream) == 7
        _assert_partial_failure(storage, str(task.task_id), 7)
    finally:
        storage.close()


def test_sync_caller_exception_is_cancellation_not_provider_failure(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "sync-caller-cancel.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    observed = 0

    def observe(value: int) -> None:
        nonlocal observed
        observed += value

    try:
        with tracker.task(task_type="sync-caller-cancel") as task:
            stream = SyncProviderStream(
                _FailingSyncStream(),
                _session(tracker),
                observe=observe,
                measurement=lambda: _measurement(observed),
            )
            with pytest.raises(ValueError, match="caller stopped"), stream:
                assert next(stream) == 7
                raise ValueError("caller stopped")
        event = storage.query_events(task_id=str(task.task_id))[0]
        assert event.output_tokens == 7
        assert event.details["attribution_operation_status"] == "cancelled"
        assert "error_type" not in event.details
    finally:
        storage.close()


def test_async_iteration_failure_retains_partial_usage(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "async-partial.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    observed = 0

    def observe(value: int) -> None:
        nonlocal observed
        observed += value

    async def run() -> str:
        async with tracker.task(task_type="async-partial") as task:
            stream = AsyncProviderStream(
                _FailingAsyncStream(),
                _session(tracker),
                observe=observe,
                measurement=lambda: _measurement(observed),
            )
            with pytest.raises(_StreamFailureError, match="async stream failed"):
                async for _ in stream:
                    pass
        return str(task.task_id)

    try:
        task_id = asyncio.run(run())
        _assert_partial_failure(storage, task_id, 9)
    finally:
        storage.close()


def test_async_context_exit_failure_retains_partial_usage(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "async-exit-partial.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    observed = 0

    def observe(value: int) -> None:
        nonlocal observed
        observed += value

    async def run() -> str:
        async with tracker.task(task_type="async-exit-partial") as task:
            stream = AsyncProviderStream(
                _FailingAsyncStream(exit_failure=True),
                _session(tracker),
                observe=observe,
                measurement=lambda: _measurement(observed),
            )
            with pytest.raises(_StreamFailureError, match="exit failed"):
                async with stream:
                    assert await stream.__anext__() == 9
        return str(task.task_id)

    try:
        task_id = asyncio.run(run())
        _assert_partial_failure(storage, task_id, 9)
    finally:
        storage.close()


def test_async_caller_exception_is_cancellation_not_provider_failure(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "async-caller-cancel.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    observed = 0

    def observe(value: int) -> None:
        nonlocal observed
        observed += value

    async def run() -> str:
        async with tracker.task(task_type="async-caller-cancel") as task:
            stream = AsyncProviderStream(
                _FailingAsyncStream(),
                _session(tracker),
                observe=observe,
                measurement=lambda: _measurement(observed),
            )
            with pytest.raises(ValueError, match="caller stopped"):
                async with stream:
                    assert await stream.__anext__() == 9
                    raise ValueError("caller stopped")
        return str(task.task_id)

    try:
        task_id = asyncio.run(run())
        event = storage.query_events(task_id=task_id)[0]
        assert event.output_tokens == 9
        assert event.details["attribution_operation_status"] == "cancelled"
        assert "error_type" not in event.details
    finally:
        storage.close()


def test_sync_extraction_failures_never_replace_provider_results(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "sync-extraction-failure.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])

    def extraction_failure(*_: Any) -> Any:
        raise ValueError("unsupported provider shape")

    try:
        with tracker.task(task_type="sync-extraction-failure") as task:
            stream = SyncProviderStream(
                _ValuesSyncStream(),
                _session(tracker),
                observe=extraction_failure,
                measurement=extraction_failure,
                completion_status=extraction_failure,
            )
            assert list(stream) == [2, 3]

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details["attribution_operation_status"] == "unknown"
        assert events[0].details["attribution_usage_lines"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
    finally:
        storage.close()


def test_sync_close_survives_terminal_measurement_failure(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "sync-close-extraction-failure.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])

    def extraction_failure() -> OperationMeasurement:
        raise ValueError("unsupported provider shape")

    try:
        with tracker.task(task_type="sync-close-extraction-failure") as task:
            stream = SyncProviderStream(
                _ValuesSyncStream(),
                _session(tracker),
                observe=lambda _: None,
                measurement=extraction_failure,
            )
            assert next(stream) == 2
            assert stream.close() is None

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].details["attribution_operation_status"] == "cancelled"
    finally:
        storage.close()


def test_async_extraction_failures_never_replace_provider_results(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "async-extraction-failure.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])

    def extraction_failure(*_: Any) -> Any:
        raise ValueError("unsupported provider shape")

    async def run() -> tuple[str, list[int]]:
        values: list[int] = []
        async with tracker.task(task_type="async-extraction-failure") as task:
            stream = AsyncProviderStream(
                _ValuesAsyncStream(),
                _session(tracker),
                observe=extraction_failure,
                measurement=extraction_failure,
                completion_status=extraction_failure,
            )
            async for item in stream:
                values.append(item)
        return str(task.task_id), values

    try:
        task_id, values = asyncio.run(run())
        assert values == [4, 5]
        events = storage.query_events(task_id=task_id)
        assert len(events) == 1
        assert events[0].details["attribution_operation_status"] == "unknown"
        assert events[0].details["attribution_usage_lines"] == [
            {"metric": "request_count", "quantity": "1", "unit": "Requests"}
        ]
    finally:
        storage.close()


def test_async_close_survives_terminal_measurement_failure(tmp_path: Any) -> None:
    storage = SQLiteStorage(tmp_path / "async-close-extraction-failure.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])

    def extraction_failure() -> OperationMeasurement:
        raise ValueError("unsupported provider shape")

    async def run() -> str:
        async with tracker.task(task_type="async-close-extraction-failure") as task:
            stream = AsyncProviderStream(
                _ValuesAsyncStream(),
                _session(tracker),
                observe=lambda _: None,
                measurement=extraction_failure,
            )
            assert await stream.__anext__() == 4
            assert await stream.aclose() is None
        return str(task.task_id)

    try:
        task_id = asyncio.run(run())
        events = storage.query_events(task_id=task_id)
        assert len(events) == 1
        assert events[0].details["attribution_operation_status"] == "cancelled"
    finally:
        storage.close()
