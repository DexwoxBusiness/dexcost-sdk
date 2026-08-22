"""Shared, framework-neutral execution lifecycle for agent integrations.

This module deliberately wraps public execution methods instead of replacing
framework classes or provider drivers. Provider instrumentation remains the
authoritative source for LLM costs; framework integrations add canonical task,
capability, streaming, and native tool lifecycle attribution.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

import wrapt

from dexcost.auto_task import create_auto_task
from dexcost.capabilities import (
    canonical_tool_capability_name,
    capability_context,
    get_capability,
)
from dexcost.context import (
    _reset_current_task,
    get_current_task,
    set_current_task,
)
from dexcost.models.capability import CapabilityIdentity
from dexcost.tracker import CostTracker, TrackedTask

_log = logging.getLogger(__name__)

_ACTIVE_FRAMEWORK_SESSION: contextvars.ContextVar[FrameworkInvocation | None] = (
    contextvars.ContextVar("dexcost_framework_session", default=None)
)

ListenerFactory = Callable[[], AbstractContextManager[Any]]
BeforeFinalize = Callable[[], None]


def resolve_tracker(tracker: CostTracker | None) -> CostTracker:
    """Resolve an explicit tracker or the tracker configured by ``dexcost.init``."""
    if tracker is not None:
        return tracker
    import dexcost

    configured = getattr(dexcost, "_global_tracker", None)
    if not isinstance(configured, CostTracker):
        raise RuntimeError(
            "no DexCost tracker is configured; call dexcost.init() or pass tracker="
        )
    return configured


def current_framework_invocation(framework: str) -> FrameworkInvocation | None:
    """Return the active integration session when it belongs to *framework*."""
    session = _ACTIVE_FRAMEWORK_SESSION.get()
    if session is None or session.framework != framework:
        return None
    return session


class FrameworkInvocation:
    """One task/capability lifecycle shared by a framework execution and streams."""

    def __init__(
        self,
        *,
        tracker: CostTracker,
        framework: str,
        task_type: str,
        capability: CapabilityIdentity,
        capture_llm_events: bool,
        capture_tool_events: bool,
        listener_factory: ListenerFactory | None = None,
        before_finalize: BeforeFinalize | None = None,
    ) -> None:
        self.framework = framework
        self.capture_llm_events = capture_llm_events
        self.capture_tool_events = capture_tool_events
        self._tracker = tracker
        self._capability = capability
        self._listener_factory = listener_factory
        self._before_finalize = before_finalize
        self._lock = threading.RLock()
        self._claimed_events: set[str] = set()
        self._pending_streams = 0
        self._failed = False
        self._finalizing = False
        self._closed = False

        task = get_current_task()
        self._owns_task = task is None
        if task is None:
            task = create_auto_task(task_type)
            task.metadata = {
                **task.metadata,
                "_dexcost_framework": framework,
            }
            tracker._storage.insert_task(task)
        self.tracked_task = TrackedTask(task, tracker._storage, tracker)
        if not self._owns_task:
            # The caller owns the existing task lifecycle. This handle is only
            # used to attach framework-native events to it.
            self.tracked_task._ended = True

    @property
    def capability(self) -> CapabilityIdentity:
        """Capability propagated to provider instrumentation for this run."""
        return self._capability

    @contextmanager
    def activate(self) -> Iterator[FrameworkInvocation]:
        """Activate task, capability, and any framework-local event listener."""
        task_token = None
        if get_current_task() is not self.tracked_task.task:
            task_token = set_current_task(self.tracked_task.task)
        session_token = _ACTIVE_FRAMEWORK_SESSION.set(self)
        listener = (
            self._listener_factory() if self._listener_factory is not None else nullcontext()
        )
        try:
            with capability_context(self._capability), listener:
                yield self
        finally:
            _ACTIVE_FRAMEWORK_SESSION.reset(session_token)
            if task_token is not None:
                _reset_current_task(task_token)

    def claim_event(self, key: str) -> bool:
        """Claim a framework event exactly once across listeners and threads."""
        with self._lock:
            if self._closed or key in self._claimed_events:
                return False
            self._claimed_events.add(key)
            return True

    def add_streams(self, count: int) -> None:
        """Register stream roots before their wrappers are returned to callers."""
        if count <= 0:
            raise ValueError("stream count must be positive")
        with self._lock:
            if self._finalizing or self._closed:
                raise RuntimeError("cannot add streams to a finalized invocation")
            self._pending_streams += count

    def mark_failed(self) -> None:
        """Latch a framework-native failure even when its API does not raise."""
        with self._lock:
            if not self._closed:
                self._failed = True

    def stream_finished(self, status: str) -> None:
        """Finish one stream and close the invocation when every root is terminal."""
        should_finalize = False
        final_status = "success"
        with self._lock:
            if self._closed:
                return
            if status != "success":
                self._failed = True
            if self._pending_streams <= 0:
                return
            self._pending_streams -= 1
            if self._pending_streams == 0 and not self._finalizing:
                self._finalizing = True
                should_finalize = True
                final_status = "failed" if self._failed else "success"
        if should_finalize:
            self._finalize(final_status)

    def finish(self, status: str) -> None:
        """Close a non-streaming execution exactly once."""
        with self._lock:
            if self._closed or self._finalizing:
                return
            if status != "success":
                self._failed = True
            self._finalizing = True
            final_status = "failed" if self._failed else "success"
        self._finalize(final_status)

    def _finalize(self, status: str) -> None:
        # CrewAI dispatches handlers through an executor. Its public event-bus
        # flush runs before aggregation so native tool events are included.
        if self._before_finalize is not None:
            try:
                self._before_finalize()
            except Exception:
                _log.debug("dexcost: framework event flush failed", exc_info=True)
        with self._lock:
            self._closed = True
        if not self._owns_task:
            return
        try:
            self.tracked_task.end(status=status)
        except Exception:
            _log.debug("dexcost: framework task finalization failed", exc_info=True)
            self.tracked_task._ended = True


class _StreamTerminal:
    """A terminal shared by a root stream and any filtered child iterators."""

    def __init__(self, session: FrameworkInvocation) -> None:
        self._session = session
        self._lock = threading.Lock()
        self._done = False

    def finish(self, status: str) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._session.stream_finished(status)

    def __del__(self) -> None:
        # Abandoning the last reference to a non-terminal stream must not leave
        # an auto-created task permanently pending.
        with contextlib.suppress(Exception):
            self.finish("failed")


class FrameworkStreamProxy:
    """Hybrid sync/async stream proxy preserving framework result properties."""

    _CHILD_STREAM_PROPERTIES = frozenset({"events", "llm", "messages", "flow", "tools"})
    _CHILD_STREAM_METHODS = frozenset({"subscribe", "interleave"})

    def __init__(
        self,
        stream: Any,
        session: FrameworkInvocation,
        terminal: _StreamTerminal,
    ) -> None:
        self.__wrapped__ = stream
        self._stream = stream
        self._session = session
        self._terminal = terminal
        self._sync_iterator: Any = None
        self._async_iterator: Any = None

    def __iter__(self) -> FrameworkStreamProxy:
        if self._sync_iterator is None:
            self._sync_iterator = iter(self._stream)
        return self

    def __next__(self) -> Any:
        try:
            with self._session.activate():
                if self._sync_iterator is None:
                    self._sync_iterator = iter(self._stream)
                return next(self._sync_iterator)
        except StopIteration:
            self._terminal.finish("success")
            raise
        except BaseException:
            self._terminal.finish("failed")
            raise

    def __aiter__(self) -> FrameworkStreamProxy:
        if self._async_iterator is None:
            self._async_iterator = self._stream.__aiter__()
        return self

    async def __anext__(self) -> Any:
        try:
            with self._session.activate():
                if self._async_iterator is None:
                    self._async_iterator = self._stream.__aiter__()
                return await self._async_iterator.__anext__()
        except StopAsyncIteration:
            self._terminal.finish("success")
            raise
        except BaseException:
            self._terminal.finish("failed")
            raise

    def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                with self._session.activate():
                    close()
        finally:
            self._terminal.finish("failed")

    async def aclose(self) -> None:
        try:
            aclose = getattr(self._stream, "aclose", None)
            if aclose is not None:
                with self._session.activate():
                    await aclose()
            else:
                close = getattr(self._stream, "close", None)
                if close is not None:
                    with self._session.activate():
                        close()
        finally:
            self._terminal.finish("failed")

    def __enter__(self) -> FrameworkStreamProxy:
        enter = getattr(self._stream, "__enter__", None)
        if enter is not None:
            with self._session.activate():
                enter()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            exit_method = getattr(self._stream, "__exit__", None)
            if exit_method is not None:
                with self._session.activate():
                    return exit_method(*exc_info)
            return None
        finally:
            self._terminal.finish("failed")

    async def __aenter__(self) -> FrameworkStreamProxy:
        enter = getattr(self._stream, "__aenter__", None)
        if enter is not None:
            with self._session.activate():
                await enter()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        try:
            exit_method = getattr(self._stream, "__aexit__", None)
            if exit_method is not None:
                with self._session.activate():
                    return await exit_method(*exc_info)
            return None
        finally:
            self._terminal.finish("failed")

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._stream, name)
        if name in self._CHILD_STREAM_PROPERTIES and _is_stream(value, force=True):
            return FrameworkStreamProxy(value, self._session, self._terminal)
        if name in self._CHILD_STREAM_METHODS and callable(value):

            @functools.wraps(value)
            def child_stream(*args: Any, **kwargs: Any) -> Any:
                with self._session.activate():
                    result = value(*args, **kwargs)
                if _is_stream(result, force=True):
                    return FrameworkStreamProxy(result, self._session, self._terminal)
                return result

            return child_stream
        return value


def _is_stream(value: Any, *, force: bool = False) -> bool:
    if inspect.isgenerator(value) or inspect.isasyncgen(value):
        return True
    value_type = type(value)
    name = value_type.__name__.lower()
    module = value_type.__module__.lower()
    has_iteration = hasattr(value, "__iter__") or hasattr(value, "__aiter__")
    if module.startswith(("crewai", "griptape")) and "stream" in name and has_iteration:
        return True
    return force and has_iteration and not isinstance(value, (str, bytes, dict))


def _stream_values(result: Any, *, force: bool) -> list[Any]:
    if _is_stream(result, force=force):
        return [result]
    if isinstance(result, (list, tuple)):
        values: list[Any] = []
        for item in result:
            values.extend(_stream_values(item, force=False))
        return values
    return []


def _replace_streams(
    result: Any,
    session: FrameworkInvocation,
    wrappers: dict[int, FrameworkStreamProxy],
    *,
    force: bool,
) -> Any:
    if _is_stream(result, force=force):
        return wrappers[id(result)]
    if isinstance(result, list):
        return [
            _replace_streams(item, session, wrappers, force=False) for item in result
        ]
    if isinstance(result, tuple):
        return tuple(
            _replace_streams(item, session, wrappers, force=False) for item in result
        )
    return result


def _finish_result(
    result: Any,
    session: FrameworkInvocation,
    *,
    force_stream: bool,
) -> Any:
    stream_values = _stream_values(result, force=force_stream)
    unique_streams = {id(stream): stream for stream in stream_values}
    if not unique_streams:
        session.finish("success")
        return result
    session.add_streams(len(unique_streams))
    wrappers = {
        key: FrameworkStreamProxy(stream, session, _StreamTerminal(session))
        for key, stream in unique_streams.items()
    }
    return _replace_streams(result, session, wrappers, force=force_stream)


class FrameworkExecutionProxy(wrapt.ObjectProxy):  # type: ignore[misc]
    """Transparent proxy that tracks selected public execution methods."""

    def __init__(
        self,
        wrapped: Any,
        *,
        tracker: CostTracker,
        framework: str,
        methods: Sequence[str],
        force_stream_methods: Sequence[str] = (),
        task_type: str | None = None,
        capability: CapabilityIdentity | None = None,
        capture_llm_events: bool = False,
        capture_tool_events: bool = True,
        listener_factory: ListenerFactory | None = None,
        before_finalize: BeforeFinalize | None = None,
    ) -> None:
        super().__init__(wrapped)
        self._self_tracker = tracker
        self._self_framework = framework
        self._self_methods = frozenset(methods)
        self._self_force_stream_methods = frozenset(force_stream_methods)
        self._self_task_type = task_type
        self._self_capability = capability
        self._self_capture_llm_events = capture_llm_events
        self._self_capture_tool_events = capture_tool_events
        self._self_listener_factory = listener_factory
        self._self_before_finalize = before_finalize

    def _self_new_session(self, method_name: str) -> FrameworkInvocation:
        operation_name = canonical_tool_capability_name(
            f"{self._self_framework}.{method_name}"
        )
        task_type = self._self_task_type or operation_name
        capability = self._self_capability or get_capability()
        if capability is None:
            capability = CapabilityIdentity(
                name=operation_name,
                kind="workflow",
                namespace=self._self_framework,
                source="other",
                invocation="automatic",
            )
        return FrameworkInvocation(
            tracker=self._self_tracker,
            framework=self._self_framework,
            task_type=task_type,
            capability=capability,
            capture_llm_events=self._self_capture_llm_events,
            capture_tool_events=self._self_capture_tool_events,
            listener_factory=self._self_listener_factory,
            before_finalize=self._self_before_finalize,
        )

    def __getattr__(self, name: str) -> Any:
        value = super().__getattr__(name)
        if name not in self._self_methods or not callable(value):
            return value

        @functools.wraps(value)
        def tracked_method(*args: Any, **kwargs: Any) -> Any:
            force_stream = name in self._self_force_stream_methods
            if inspect.iscoroutinefunction(value):

                async def invoke_async() -> Any:
                    try:
                        session = self._self_new_session(name)
                    except Exception:
                        _log.debug("dexcost: framework task setup failed", exc_info=True)
                        return await value(*args, **kwargs)
                    try:
                        with session.activate():
                            result = await value(*args, **kwargs)
                    except BaseException:
                        session.finish("failed")
                        raise
                    return _finish_result(result, session, force_stream=force_stream)

                return invoke_async()

            try:
                session = self._self_new_session(name)
            except Exception:
                _log.debug("dexcost: framework task setup failed", exc_info=True)
                return value(*args, **kwargs)
            try:
                with session.activate():
                    result = value(*args, **kwargs)
            except BaseException:
                session.finish("failed")
                raise

            if inspect.isawaitable(result):

                async def await_result() -> Any:
                    try:
                        with session.activate():
                            awaited = await result
                    except BaseException:
                        session.finish("failed")
                        raise
                    return _finish_result(
                        awaited,
                        session,
                        force_stream=force_stream,
                    )

                return await_result()
            return _finish_result(result, session, force_stream=force_stream)

        return tracked_method


__all__ = [
    "FrameworkExecutionProxy",
    "FrameworkInvocation",
    "FrameworkStreamProxy",
    "current_framework_invocation",
    "resolve_tracker",
]
