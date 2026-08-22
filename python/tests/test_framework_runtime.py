"""Framework-neutral task, context, and streaming lifecycle tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dexcost.capabilities import get_capability
from dexcost.context import get_current_task
from dexcost.integrations._framework_runtime import FrameworkExecutionProxy
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _proxy(
    execution: Any,
    tracker: CostTracker,
    *,
    methods: tuple[str, ...] = ("kickoff",),
    force_stream_methods: tuple[str, ...] = (),
) -> Any:
    return FrameworkExecutionProxy(
        execution,
        tracker=tracker,
        framework="example",
        methods=methods,
        force_stream_methods=force_stream_methods,
    )


def test_sync_execution_creates_canonical_task_and_preserves_proxy_type(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "framework-sync.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    observed: list[tuple[object, object]] = []

    class Workflow:
        def kickoff(self, value: int = 1) -> int:
            observed.append((get_current_task(), get_capability()))
            return value + 1

    try:
        workflow = Workflow()
        tracked = _proxy(workflow, tracker)
        assert isinstance(tracked, Workflow)
        assert tracked.kickoff(4) == 5
        assert get_current_task() is None
        assert get_capability() is None

        tasks = storage.query_tasks(task_type="example.kickoff")
        assert len(tasks) == 1
        assert tasks[0].status == "success"
        assert tasks[0].metadata["_dexcost_framework"] == "example"
        active_task, capability = observed[0]
        assert active_task is not None
        assert capability is not None
        assert capability.to_dict() == {
            "name": "example.kickoff",
            "kind": "workflow",
            "namespace": "example",
            "source": "other",
            "invocation": "automatic",
        }
    finally:
        storage.close()


def test_existing_task_is_reused_and_not_ended_by_framework(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "framework-existing.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Workflow:
        def kickoff(self) -> str:
            assert get_current_task() is not None
            return "done"

    try:
        tracked = _proxy(Workflow(), tracker)
        with tracker.task(task_type="caller.task") as caller:
            assert tracked.kickoff() == "done"
            assert caller.task.status == "pending"
        tasks = storage.query_tasks()
        assert len(tasks) == 1
        assert tasks[0].task_type == "caller.task"
        assert tasks[0].status == "success"
    finally:
        storage.close()


def test_sync_stream_context_does_not_leak_between_chunks(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "framework-stream.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    observed: list[tuple[object, object]] = []

    class Workflow:
        def kickoff(self) -> Any:
            def chunks() -> Any:
                observed.append((get_current_task(), get_capability()))
                yield "one"
                observed.append((get_current_task(), get_capability()))
                yield "two"

            return chunks()

    try:
        stream = _proxy(Workflow(), tracker).kickoff()
        assert next(stream) == "one"
        assert get_current_task() is None
        assert get_capability() is None
        assert list(stream) == ["two"]
        assert all(task is not None and capability is not None for task, capability in observed)
        task = storage.query_tasks(task_type="example.kickoff")[0]
        assert task.status == "success"
    finally:
        storage.close()


def test_stream_close_marks_auto_task_failed(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "framework-close.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Workflow:
        def kickoff(self) -> Any:
            yield "one"
            yield "two"

    try:
        stream = _proxy(Workflow(), tracker).kickoff()
        assert next(stream) == "one"
        stream.close()
        task = storage.query_tasks(task_type="example.kickoff")[0]
        assert task.status == "failed"
        assert task.failure_count == 1
    finally:
        storage.close()


def test_multiple_streams_share_one_task_until_all_finish(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "framework-multi.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Workflow:
        def kickoff(self) -> list[Any]:
            return [(value for value in (1,)), (value for value in (2,))]

    try:
        streams = _proxy(Workflow(), tracker).kickoff()
        assert list(streams[0]) == [1]
        pending = storage.query_tasks(task_type="example.kickoff")[0]
        assert pending.status == "pending"
        assert list(streams[1]) == [2]
        complete = storage.query_tasks(task_type="example.kickoff")[0]
        assert complete.status == "success"
    finally:
        storage.close()


def test_filtered_child_stream_completes_root_lifecycle(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "framework-child-stream.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class StreamSession:
        def __init__(self) -> None:
            self._values = iter(("a", "b"))

        def __iter__(self) -> Any:
            return self._values

        @property
        def events(self) -> Any:
            return self._values

        def close(self) -> None:
            pass

    class Workflow:
        def stream_events(self) -> StreamSession:
            return StreamSession()

    try:
        events = _proxy(
            Workflow(),
            tracker,
            methods=("stream_events",),
            force_stream_methods=("stream_events",),
        ).stream_events().events
        assert list(events) == ["a", "b"]
        task = storage.query_tasks(task_type="example.stream_events")[0]
        assert task.status == "success"
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_native_async_and_async_stream_lifecycles(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "framework-async.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    observed: list[object] = []

    class Workflow:
        async def kickoff_async(self) -> Any:
            await asyncio.sleep(0)

            async def chunks() -> Any:
                observed.append(get_current_task())
                yield 1
                observed.append(get_current_task())
                yield 2

            return chunks()

    try:
        tracked = _proxy(Workflow(), tracker, methods=("kickoff_async",))
        stream = await tracked.kickoff_async()
        values = [value async for value in stream]
        assert values == [1, 2]
        assert all(task is not None for task in observed)
        assert get_current_task() is None
        task = storage.query_tasks(task_type="example.kickoff_async")[0]
        assert task.status == "success"
    finally:
        storage.close()


def test_execution_failure_is_reraised_and_marks_task_failed(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "framework-failed.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    class Workflow:
        def kickoff(self) -> None:
            raise LookupError("private execution failure")

    try:
        with pytest.raises(LookupError, match="private execution failure"):
            _proxy(Workflow(), tracker).kickoff()
        task = storage.query_tasks(task_type="example.kickoff")[0]
        assert task.status == "failed"
        assert "private execution failure" not in str(task.to_dict())
    finally:
        storage.close()
