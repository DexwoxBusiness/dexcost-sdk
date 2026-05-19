"""Task context propagation via contextvars.

Provides automatic task association for async/sync call stacks so that
any cost event recorded inside a tracked task is linked without manual
ID threading.
"""

from __future__ import annotations

import contextvars
import functools
import threading
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from dexcost.models.task import Task


@dataclass
class DexcostContext:
    """Attribution context for cost tracking.

    Set via set_context(), read by auto-instrumentation and dexcost.task().
    Thread-safe and async-safe via contextvars.
    """

    customer_id: str | None = None
    project_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    agent: str | None = None


_current_context: contextvars.ContextVar[DexcostContext | None] = contextvars.ContextVar(
    "_current_context", default=None
)


def set_context(
    customer_id: str | None = None,
    project_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    agent: str | None = None,
) -> None:
    """Set the attribution context for subsequent LLM calls and tasks."""
    _current_context.set(
        DexcostContext(
            customer_id=customer_id,
            project_id=project_id,
            metadata=metadata or {},
            agent=agent,
        )
    )


def get_context() -> DexcostContext | None:
    """Return the current attribution context, or None if not set."""
    return _current_context.get()


def clear_context() -> None:
    """Remove the current attribution context."""
    _current_context.set(None)


_current_task: contextvars.ContextVar[Task | None] = contextvars.ContextVar(
    "_current_task", default=None
)


def get_current_task() -> Task | None:
    """Return the active task in the current context, or ``None``."""
    return _current_task.get()


def set_current_task(task: Task | None) -> contextvars.Token[Task | None]:
    """Set the active task and return a token for later restoration."""
    return _current_task.set(task)


# ---------------------------------------------------------------------------
# Per-call network-event suppression flag
# ---------------------------------------------------------------------------
# When set, the HTTP adapter records bytes for the call but does NOT emit a
# standalone `network` event — used by the LLM instruments so an LLM API call
# does not produce both an `llm_call` event and a `network` event.

_suppress_network: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_suppress_network", default=False
)


def is_network_event_suppressed() -> bool:
    """Return True when the current call must not emit a `network` event."""
    return _suppress_network.get()


@contextmanager
def suppress_network_event() -> Generator[None, None, None]:
    """Within this block, the HTTP adapter suppresses standalone network events.

    Bytes are still recorded into the task counters; only the per-call
    `network` event is withheld. Used by LLM instruments around their HTTP
    call so it does not double-emit (`llm_call` + `network`).
    """
    token = _suppress_network.set(True)
    try:
        yield
    finally:
        _suppress_network.reset(token)


# ---------------------------------------------------------------------------
# ThreadPoolExecutor monkey-patch — propagate contextvars to child threads
# ---------------------------------------------------------------------------
# Python's ThreadPoolExecutor does NOT propagate contextvars to worker
# threads by default. Libraries like LangExtract, OpenAI, and others
# use ThreadPoolExecutor for parallel work. Without this patch, LLM
# calls in those threads can't find the active task.
#
# This patches ThreadPoolExecutor.submit() to capture the current
# context at submit time and run the callable within that context.
# ---------------------------------------------------------------------------

_original_tpe_submit = ThreadPoolExecutor.submit
_tpe_patched = False


def _patched_submit(
    self: ThreadPoolExecutor, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    """Wrap submitted callable to run within the parent's context."""
    ctx = contextvars.copy_context()
    return _original_tpe_submit(self, ctx.run, fn, *args, **kwargs)


def patch_thread_context() -> None:
    """Patch ThreadPoolExecutor to propagate contextvars to child threads.

    Safe to call multiple times — only patches once.
    """
    global _tpe_patched
    if _tpe_patched:
        return
    ThreadPoolExecutor.submit = _patched_submit  # type: ignore[assignment]
    _tpe_patched = True


def unpatch_thread_context() -> None:
    """Restore the original ThreadPoolExecutor.submit."""
    global _tpe_patched
    if not _tpe_patched:
        return
    ThreadPoolExecutor.submit = _original_tpe_submit  # type: ignore[method-assign]
    _tpe_patched = False


@contextmanager
def task_context(task: Task) -> Generator[Task, None, None]:
    """Context manager that sets *task* as the current task.

    Nesting is supported: if there is already an active task and *task* does
    not have ``parent_task_id`` set, it is automatically set to the current
    task's ``task_id``.

    On exit the previous task (or ``None``) is restored via token reset.
    """
    parent = get_current_task()
    if parent is not None and task.parent_task_id is None:
        task.parent_task_id = parent.task_id
    token = set_current_task(task)
    try:
        yield task
    finally:
        _current_task.reset(token)


@asynccontextmanager
async def async_task_context(task: Task) -> AsyncGenerator[Task, None]:
    """Async variant of :func:`task_context`.

    Identical nesting and parent-linking behaviour, for use in
    ``async with`` blocks.
    """
    parent = get_current_task()
    if parent is not None and task.parent_task_id is None:
        task.parent_task_id = parent.task_id
    token = set_current_task(task)
    try:
        yield task
    finally:
        _current_task.reset(token)
