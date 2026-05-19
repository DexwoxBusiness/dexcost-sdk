"""Tests for task context propagation (US-006)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest

from dexcost.context import (
    async_task_context,
    get_current_task,
    set_current_task,
    task_context,
)
from dexcost.models.task import Task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(**kwargs: Any) -> Task:
    """Create a Task with sensible defaults for testing."""
    defaults: dict[str, Any] = {"task_type": "test"}
    defaults.update(kwargs)
    return Task(**defaults)


# ---------------------------------------------------------------------------
# Synchronous context tests
# ---------------------------------------------------------------------------


class TestGetCurrentTask:
    """get_current_task() returns None when no task is active."""

    def test_returns_none_by_default(self) -> None:
        assert get_current_task() is None


class TestSetCurrentTask:
    """set_current_task() makes the task retrievable."""

    def test_set_and_get(self) -> None:
        task = _make_task()
        set_current_task(task)
        try:
            assert get_current_task() is task
        finally:
            set_current_task(None)  # clean up

    def test_token_reset_restores_previous(self) -> None:
        task_a = _make_task(task_type="a")
        task_b = _make_task(task_type="b")

        token_a = set_current_task(task_a)
        assert get_current_task() is task_a

        set_current_task(task_b)
        assert get_current_task() is task_b

        # Use the ContextVar token to restore to task_a
        from dexcost.context import _current_task

        _current_task.reset(token_a)
        assert get_current_task() is None  # reset goes back to default (before token_a)

    def test_set_none_clears(self) -> None:
        task = _make_task()
        set_current_task(task)
        set_current_task(None)
        assert get_current_task() is None


# ---------------------------------------------------------------------------
# task_context() context manager tests
# ---------------------------------------------------------------------------


class TestTaskContext:
    """task_context() sets/restores task and handles nesting."""

    def test_sets_task_during_block(self) -> None:
        task = _make_task()
        with task_context(task) as t:
            assert t is task
            assert get_current_task() is task
        assert get_current_task() is None

    def test_restores_previous_task_on_exit(self) -> None:
        outer = _make_task(task_type="outer")
        inner = _make_task(task_type="inner")

        with task_context(outer):
            assert get_current_task() is outer
            with task_context(inner):
                assert get_current_task() is inner
            assert get_current_task() is outer
        assert get_current_task() is None

    def test_nested_task_gets_parent_id(self) -> None:
        parent = _make_task(task_type="parent")
        child = _make_task(task_type="child")
        assert child.parent_task_id is None

        with task_context(parent), task_context(child):
            assert child.parent_task_id == parent.task_id

    def test_explicit_parent_id_not_overwritten(self) -> None:
        parent = _make_task(task_type="parent")
        explicit_parent_id = uuid.uuid4()
        child = _make_task(task_type="child", parent_task_id=explicit_parent_id)

        with task_context(parent), task_context(child):
            assert child.parent_task_id == explicit_parent_id

    def test_three_level_nesting(self) -> None:
        grandparent = _make_task(task_type="grandparent")
        parent = _make_task(task_type="parent")
        child = _make_task(task_type="child")

        with task_context(grandparent):
            with task_context(parent):
                assert parent.parent_task_id == grandparent.task_id
                with task_context(child):
                    assert child.parent_task_id == parent.task_id
                assert get_current_task() is parent
            assert get_current_task() is grandparent
        assert get_current_task() is None

    def test_restores_on_exception(self) -> None:
        outer = _make_task(task_type="outer")
        inner = _make_task(task_type="inner")

        with task_context(outer):
            with pytest.raises(RuntimeError), task_context(inner):
                raise RuntimeError("boom")
            # Outer task must be restored
            assert get_current_task() is outer
        assert get_current_task() is None


# ---------------------------------------------------------------------------
# Async context isolation tests
# ---------------------------------------------------------------------------


class TestAsyncContextIsolation:
    """contextvars isolate task per asyncio task."""

    def test_async_tasks_isolated(self) -> None:
        """Two concurrent async tasks see their own task objects."""

        async def _run() -> None:
            task_a = _make_task(task_type="async_a")
            task_b = _make_task(task_type="async_b")
            results: dict[str, Task | None] = {}

            async def worker(name: str, task: Task) -> None:
                with task_context(task):
                    # Yield control so both workers interleave
                    await asyncio.sleep(0.01)
                    results[name] = get_current_task()

            await asyncio.gather(
                asyncio.create_task(worker("a", task_a)),
                asyncio.create_task(worker("b", task_b)),
            )

            assert results["a"] is task_a
            assert results["b"] is task_b

        asyncio.run(_run())

    def test_async_no_cross_contamination(self) -> None:
        """Setting a task in one async task does not affect another."""

        async def _run() -> None:
            task = _make_task(task_type="only_one")
            observed: list[Task | None] = []

            async def setter() -> None:
                with task_context(task):
                    await asyncio.sleep(0.02)

            async def observer() -> None:
                await asyncio.sleep(0.01)
                observed.append(get_current_task())

            await asyncio.gather(
                asyncio.create_task(setter()),
                asyncio.create_task(observer()),
            )
            assert observed[0] is None

        asyncio.run(_run())

    def test_nested_async_tasks(self) -> None:
        """Nesting works correctly inside an async function."""

        async def _run() -> None:
            parent = _make_task(task_type="async_parent")
            child = _make_task(task_type="async_child")

            with task_context(parent):
                assert get_current_task() is parent
                with task_context(child):
                    assert get_current_task() is child
                    assert child.parent_task_id == parent.task_id
                assert get_current_task() is parent

        asyncio.run(_run())


class TestAsyncTaskContext:
    """async_task_context() async context manager."""

    def test_sets_and_restores(self) -> None:
        async def _run() -> None:
            task = _make_task(task_type="async_cm")
            assert get_current_task() is None
            async with async_task_context(task) as t:
                assert t is task
                assert get_current_task() is task
            assert get_current_task() is None

        asyncio.run(_run())

    def test_nested_auto_parent(self) -> None:
        async def _run() -> None:
            parent = _make_task(task_type="async_parent")
            child = _make_task(task_type="async_child")

            async with async_task_context(parent):
                async with async_task_context(child) as c:
                    assert c.parent_task_id == parent.task_id
                    assert get_current_task() is child
                assert get_current_task() is parent

        asyncio.run(_run())

    def test_restores_on_exception(self) -> None:
        async def _run() -> None:
            task = _make_task(task_type="failing_async")
            with pytest.raises(ValueError):
                async with async_task_context(task):
                    raise ValueError("async boom")
            assert get_current_task() is None

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Thread context isolation tests
# ---------------------------------------------------------------------------


class TestThreadContextIsolation:
    """contextvars isolate task per thread."""

    def test_threads_isolated(self) -> None:
        """Tasks set in separate threads do not leak across threads."""
        task_a = _make_task(task_type="thread_a")
        task_b = _make_task(task_type="thread_b")
        results: dict[str, Task | None] = {}
        barrier = threading.Barrier(2)

        def worker(name: str, task: Task) -> None:
            with task_context(task):
                barrier.wait(timeout=5)
                results[name] = get_current_task()

        t1 = threading.Thread(target=worker, args=("a", task_a))
        t2 = threading.Thread(target=worker, args=("b", task_b))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert results["a"] is task_a
        assert results["b"] is task_b

    def test_thread_does_not_see_main_task(self) -> None:
        """A child thread does not inherit the main thread's task."""
        main_task = _make_task(task_type="main")
        observed: list[Task | None] = []

        def child_worker() -> None:
            observed.append(get_current_task())

        with task_context(main_task):
            t = threading.Thread(target=child_worker)
            t.start()
            t.join(timeout=10)

        assert observed[0] is None

    def test_nested_tasks_in_thread(self) -> None:
        """Nesting works correctly inside a thread."""
        parent = _make_task(task_type="thread_parent")
        child = _make_task(task_type="thread_child")
        results: dict[str, Any] = {}

        def worker() -> None:
            with task_context(parent):
                with task_context(child):
                    results["child_parent_id"] = child.parent_task_id
                    results["current"] = get_current_task()
                results["after_child"] = get_current_task()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)

        assert results["child_parent_id"] == parent.task_id
        assert results["current"] is child
        assert results["after_child"] is parent


# ---------------------------------------------------------------------------
# Public API re-export tests
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """Context functions are accessible from the top-level package."""

    def test_get_current_task_exported(self) -> None:
        import dexcost

        assert dexcost.get_current_task is get_current_task

    def test_set_current_task_exported(self) -> None:
        import dexcost

        assert dexcost.set_current_task is set_current_task

    def test_task_context_exported(self) -> None:
        import dexcost

        assert dexcost.task_context is task_context

    def test_async_task_context_exported(self) -> None:
        import dexcost

        assert dexcost.async_task_context is async_task_context
