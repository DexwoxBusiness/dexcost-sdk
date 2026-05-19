"""Auto-instrumentation for the Anthropic Python SDK.

Monkey-patches ``anthropic.resources.messages.Messages.create`` (sync and async)
using :pypi:`wrapt` so that every call made inside an active
:class:`~dexcost.tracker.CostTracker` task is automatically recorded as an
``llm_call`` event.

Usage::

    from dexcost import CostTracker, instrument_anthropic

    tracker = CostTracker()
    instrument_anthropic(tracker)

    # All subsequent anthropic.messages.create() calls inside a
    # tracked task are captured automatically.

Implements US-013.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.context import _current_task, get_current_task, set_current_task
from dexcost.models.event import Event

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def instrument_anthropic(tracker: Any) -> None:
    """Monkey-patch the Anthropic SDK to capture LLM calls automatically.

    Patches ``anthropic.resources.messages.Messages.create`` (sync)
    and ``anthropic.resources.messages.AsyncMessages.create`` (async).

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            price calls and persist events.

    Raises:
        ImportError: If the ``anthropic`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched

    if _patched:
        raise RuntimeError(
            "Anthropic instrumentation is already active. "
            "Call uninstrument_anthropic() before re-instrumenting."
        )

    # Verify anthropic is importable
    try:
        import anthropic.resources.messages as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'anthropic' package is required for Anthropic auto-instrumentation. "
            "Install it with: pip install anthropic"
        ) from exc

    _active_tracker = tracker

    # Store originals for uninstrument
    from anthropic.resources.messages import AsyncMessages, Messages

    _originals["sync_create"] = Messages.create
    _originals["async_create"] = AsyncMessages.create

    # Apply monkey-patches via wrapt
    wrapt.wrap_function_wrapper(
        "anthropic.resources.messages",
        "Messages.create",
        _sync_create_wrapper,
    )
    wrapt.wrap_function_wrapper(
        "anthropic.resources.messages",
        "AsyncMessages.create",
        _async_create_wrapper,
    )

    _patched = True


def uninstrument_anthropic() -> None:
    """Remove Anthropic monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched

    if not _patched:
        return

    from anthropic.resources.messages import AsyncMessages, Messages

    if "sync_create" in _originals:
        Messages.create = _originals["sync_create"]
    if "async_create" in _originals:
        AsyncMessages.create = _originals["async_create"]

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _sync_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Messages.create``."""
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("anthropic.messages")
        auto_token = set_current_task(auto_task_obj)

    try:
        stream = kwargs.get("stream", False)
        start_time = time.perf_counter()

        if stream:
            raw_stream = wrapped(*args, **kwargs)
            return _SyncStreamWrapper(raw_stream, start_time)

        response = wrapped(*args, **kwargs)
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(response, latency_ms)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

        if auto and auto_task_obj is not None and event is not None:
            try:
                finalize_auto_task(auto_task_obj, event, status="success")
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(auto_task_obj)
            except Exception:
                _log.debug("dexcost: failed to finalize auto-task", exc_info=True)

        return response
    except Exception:
        if auto and auto_task_obj is not None:
            try:
                _log.debug("dexcost: auto-task call failed", exc_info=True)
            except Exception:
                pass
        raise
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


def _async_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``AsyncMessages.create``."""
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("anthropic.messages")
        auto_token = set_current_task(auto_task_obj)

    stream = kwargs.get("stream", False)
    start_time = time.perf_counter()

    if stream:
        return _async_stream_handler(wrapped, args, kwargs, start_time, auto_task_obj, auto_token)

    return _async_non_stream_handler(wrapped, args, kwargs, start_time, auto_task_obj, auto_token)


async def _async_non_stream_handler(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    start_time: float,
    auto_task_obj: Any = None,
    auto_token: Any = None,
) -> Any:
    """Await the async create call and record the response."""
    try:
        response = await wrapped(*args, **kwargs)
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(response, latency_ms)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

        if auto_task_obj is not None and event is not None:
            try:
                finalize_auto_task(auto_task_obj, event, status="success")
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(auto_task_obj)
            except Exception:
                _log.debug("dexcost: failed to finalize auto-task", exc_info=True)

        return response
    finally:
        if auto_token is not None:
            _current_task.reset(auto_token)


async def _async_stream_handler(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    start_time: float,
    auto_task_obj: Any = None,
    auto_token: Any = None,
) -> Any:
    """Wrap async streaming to capture usage from the final events."""
    try:
        raw_stream = await wrapped(*args, **kwargs)
        return _AsyncStreamWrapper(raw_stream, start_time)
    finally:
        if auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Stream wrappers
# ---------------------------------------------------------------------------


class _SyncStreamWrapper(Iterator[Any]):
    """Wraps a sync Anthropic stream to capture usage on completion.

    Anthropic streaming distributes usage across events:
    - ``message_start``: ``message.model`` and ``message.usage`` (input tokens)
    - ``message_delta``: ``usage.output_tokens``
    - ``message_stop``: signals stream end
    """

    def __init__(self, stream: Any, start_time: float) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model: str | None = None
        self._input_tokens: int = 0
        self._output_tokens: int = 0
        self._cache_creation_input_tokens: int = 0
        self._cache_read_input_tokens: int = 0
        self._finalized: bool = False

    def __iter__(self) -> _SyncStreamWrapper:
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._stream)
            self._process_event(event)
            return event
        except StopIteration:
            self._finalize()
            raise

    def _process_event(self, event: Any) -> None:
        """Extract model and usage info from streaming events."""
        event_type = getattr(event, "type", None)

        if event_type == "message_start":
            message = getattr(event, "message", None)
            if message is not None:
                model = getattr(message, "model", None)
                if model:
                    self._model = model
                usage = getattr(message, "usage", None)
                if usage is not None:
                    self._input_tokens = getattr(usage, "input_tokens", 0) or 0
                    self._cache_creation_input_tokens = (
                        getattr(usage, "cache_creation_input_tokens", 0) or 0
                    )
                    self._cache_read_input_tokens = (
                        getattr(usage, "cache_read_input_tokens", 0) or 0
                    )

        elif event_type == "message_delta":
            usage = getattr(event, "usage", None)
            if usage is not None:
                self._output_tokens = getattr(usage, "output_tokens", 0) or 0

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            _record_from_stream_data(
                model=self._model,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                cache_creation_input_tokens=self._cache_creation_input_tokens,
                cache_read_input_tokens=self._cache_read_input_tokens,
                latency_ms=latency_ms,
            )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    # Forward close/context-manager to the underlying stream
    def close(self) -> None:
        if hasattr(self._stream, "close"):
            self._stream.close()

    def __enter__(self) -> _SyncStreamWrapper:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._finalize()
        if hasattr(self._stream, "__exit__"):
            self._stream.__exit__(*args)


class _AsyncStreamWrapper:
    """Wraps an async Anthropic stream to capture usage on completion."""

    def __init__(self, stream: Any, start_time: float) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model: str | None = None
        self._input_tokens: int = 0
        self._output_tokens: int = 0
        self._cache_creation_input_tokens: int = 0
        self._cache_read_input_tokens: int = 0
        self._finalized: bool = False

    def __aiter__(self) -> _AsyncStreamWrapper:
        return self

    async def __anext__(self) -> Any:
        try:
            event = await self._stream.__anext__()
            self._process_event(event)
            return event
        except StopAsyncIteration:
            self._finalize()
            raise

    def _process_event(self, event: Any) -> None:
        """Extract model and usage info from streaming events."""
        event_type = getattr(event, "type", None)

        if event_type == "message_start":
            message = getattr(event, "message", None)
            if message is not None:
                model = getattr(message, "model", None)
                if model:
                    self._model = model
                usage = getattr(message, "usage", None)
                if usage is not None:
                    self._input_tokens = getattr(usage, "input_tokens", 0) or 0
                    self._cache_creation_input_tokens = (
                        getattr(usage, "cache_creation_input_tokens", 0) or 0
                    )
                    self._cache_read_input_tokens = (
                        getattr(usage, "cache_read_input_tokens", 0) or 0
                    )

        elif event_type == "message_delta":
            usage = getattr(event, "usage", None)
            if usage is not None:
                self._output_tokens = getattr(usage, "output_tokens", 0) or 0

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            _record_from_stream_data(
                model=self._model,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                cache_creation_input_tokens=self._cache_creation_input_tokens,
                cache_read_input_tokens=self._cache_read_input_tokens,
                latency_ms=latency_ms,
            )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    async def aclose(self) -> None:
        if hasattr(self._stream, "aclose"):
            await self._stream.aclose()

    async def __aenter__(self) -> _AsyncStreamWrapper:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._finalize()
        if hasattr(self._stream, "__aexit__"):
            await self._stream.__aexit__(*args)


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _record_from_response(response: Any, latency_ms: int) -> Event | None:
    """Extract fields from an Anthropic Message response and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    model = getattr(response, "model", None) or "unknown"
    usage = getattr(response, "usage", None)

    if usage is not None:
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_creation_input_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read_input_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        has_usage = True
    else:
        input_tokens = 0
        output_tokens = 0
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
    )


def _record_from_stream_data(
    *,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    latency_ms: int,
) -> Event | None:
    """Record an event from accumulated stream data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    resolved_model = model or "unknown"
    has_usage = input_tokens > 0 or output_tokens > 0

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=resolved_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
    )


def _insert_llm_event(
    *,
    tracker: Any,
    task_id: Any,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    latency_ms: int,
    has_usage: bool,
) -> Event:
    """Create and persist an llm_call Event."""
    if has_usage:
        cost_result = tracker._pricing.get_cost(
            model,
            input_tokens,
            output_tokens,
            cached_tokens=cache_read_input_tokens,
            cache_creation_tokens=cache_creation_input_tokens,
        )
        cost_usd = cost_result.cost_usd
        cost_confidence = "exact"
        pricing_source = cost_result.pricing_source
        pricing_version = cost_result.pricing_version
    else:
        cost_usd = Decimal("0")
        cost_confidence = "estimated"
        pricing_source = "unknown"
        pricing_version = None

    # Store cache_read_input_tokens in the standard cached_tokens field.
    # Store cache_creation_input_tokens in details for full auditability.
    details: dict[str, Any] = {}
    if cache_creation_input_tokens > 0:
        details["cache_creation_input_tokens"] = cache_creation_input_tokens

    event = Event(
        task_id=task_id,
        event_type="llm_call",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        provider="anthropic",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cache_read_input_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    tracker._storage.insert_event(event)
    return event
