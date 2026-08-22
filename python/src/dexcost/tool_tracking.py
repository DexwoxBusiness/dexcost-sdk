"""Execution-shape-safe wrappers used by the public ``track_tool`` API."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])
_log = logging.getLogger(__name__)

ToolBegin = Callable[[], object | None]
ToolFinish = Callable[[object, str, int, BaseException | None], None]


def _duration_ms(start_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - start_ns + 500_000) // 1_000_000)


def _status(error: BaseException) -> str:
    return (
        "cancelled"
        if isinstance(error, (asyncio.CancelledError, GeneratorExit))
        else "failed"
    )


def _safe_begin(begin: ToolBegin) -> object | None:
    try:
        return begin()
    except Exception:
        _log.warning(
            "dexcost tool tracking could not start; call remains unblocked",
            exc_info=True,
        )
        return None


def _safe_finish(
    finish: ToolFinish,
    state: object | None,
    status: str,
    start_ns: int,
    error: BaseException | None,
) -> None:
    if state is None:
        return
    try:
        finish(state, status, _duration_ms(start_ns), error)
    except Exception:
        _log.warning(
            "dexcost tool tracking could not persist; call remains unblocked",
            exc_info=True,
        )


def decorate_tool(function: F, *, begin: ToolBegin, finish: ToolFinish) -> F:
    """Wrap sync, async, generator, and async-generator tool call lifecycles."""
    if inspect.isasyncgenfunction(function):

        @functools.wraps(function)
        async def async_generator_wrapper(*args: Any, **kwargs: Any) -> Any:
            state = _safe_begin(begin)
            started = time.perf_counter_ns()
            try:
                async for item in function(*args, **kwargs):
                    yield item
            except BaseException as exc:
                _safe_finish(finish, state, _status(exc), started, exc)
                raise
            else:
                _safe_finish(finish, state, "succeeded", started, None)

        return cast(F, async_generator_wrapper)

    if inspect.isgeneratorfunction(function):

        @functools.wraps(function)
        def generator_wrapper(*args: Any, **kwargs: Any) -> Any:
            state = _safe_begin(begin)
            started = time.perf_counter_ns()
            try:
                yield from function(*args, **kwargs)
            except BaseException as exc:
                _safe_finish(finish, state, _status(exc), started, exc)
                raise
            else:
                _safe_finish(finish, state, "succeeded", started, None)

        return cast(F, generator_wrapper)

    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            state = _safe_begin(begin)
            started = time.perf_counter_ns()
            try:
                result = await function(*args, **kwargs)
            except BaseException as exc:
                _safe_finish(finish, state, _status(exc), started, exc)
                raise
            _safe_finish(finish, state, "succeeded", started, None)
            return result

        return cast(F, async_wrapper)

    @functools.wraps(function)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        state = _safe_begin(begin)
        started = time.perf_counter_ns()
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            _safe_finish(finish, state, _status(exc), started, exc)
            raise
        _safe_finish(finish, state, "succeeded", started, None)
        return result

    return cast(F, sync_wrapper)


__all__ = ["ToolBegin", "ToolFinish", "decorate_tool"]
