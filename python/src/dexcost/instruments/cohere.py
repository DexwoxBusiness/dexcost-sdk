"""Auto-instrumentation for the Cohere Python SDK.

Monkey-patches ``cohere.Client.chat`` and ``cohere.AsyncClient.chat`` using
:pypi:`wrapt` so that every call made inside an active
:class:`~dexcost.tracker.CostTracker` task is automatically recorded as an
``llm_call`` event.

Token usage is extracted from ``response.meta.billed_units``.

Usage::

    from dexcost import CostTracker, instrument_cohere

    tracker = CostTracker()
    instrument_cohere(tracker)

    # All subsequent cohere.Client.chat() calls inside a
    # tracked task are captured automatically.
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


def instrument_cohere(tracker: Any) -> None:
    """Monkey-patch the Cohere SDK to capture LLM calls automatically.

    Patches ``cohere.Client.chat`` (sync) and ``cohere.AsyncClient.chat``
    (async).  When the SDK exposes the streaming method ``chat_stream``
    (sync and async) it is patched too so streamed responses are captured
    with token usage.

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            price calls and persist events.

    Raises:
        ImportError: If the ``cohere`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched

    if _patched:
        raise RuntimeError(
            "Cohere instrumentation is already active. "
            "Call uninstrument_cohere() before re-instrumenting."
        )

    # Verify cohere is importable
    try:
        import cohere as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'cohere' package is required for Cohere auto-instrumentation. "
            "Install it with: pip install cohere"
        ) from exc

    _active_tracker = tracker

    # Store originals for uninstrument
    import cohere

    _originals["sync_chat"] = cohere.Client.chat
    _originals["async_chat"] = cohere.AsyncClient.chat

    # Apply monkey-patches via wrapt
    wrapt.wrap_function_wrapper(
        "cohere",
        "Client.chat",
        _sync_chat_wrapper,
    )
    wrapt.wrap_function_wrapper(
        "cohere",
        "AsyncClient.chat",
        _async_chat_wrapper,
    )

    # Streaming path — Cohere exposes ``chat_stream`` as a separate method
    # that returns an iterator of streaming events.  Patch it when present.
    if hasattr(cohere.Client, "chat_stream"):
        _originals["sync_chat_stream"] = cohere.Client.chat_stream
        wrapt.wrap_function_wrapper(
            "cohere",
            "Client.chat_stream",
            _sync_chat_stream_wrapper,
        )
    if hasattr(cohere.AsyncClient, "chat_stream"):
        _originals["async_chat_stream"] = cohere.AsyncClient.chat_stream
        wrapt.wrap_function_wrapper(
            "cohere",
            "AsyncClient.chat_stream",
            _async_chat_stream_wrapper,
        )

    _patched = True


def uninstrument_cohere() -> None:
    """Remove Cohere monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched

    if not _patched:
        return

    try:
        import cohere

        if "sync_chat" in _originals:
            cohere.Client.chat = _originals["sync_chat"]
        if "async_chat" in _originals:
            cohere.AsyncClient.chat = _originals["async_chat"]
        if "sync_chat_stream" in _originals:
            cohere.Client.chat_stream = _originals["sync_chat_stream"]
        if "async_chat_stream" in _originals:
            cohere.AsyncClient.chat_stream = _originals["async_chat_stream"]
    except ImportError:
        pass

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _sync_chat_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Client.chat``."""
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("cohere.chat")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()

        response = wrapped(*args, **kwargs)
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(response, latency_ms, kwargs)
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


def _async_chat_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``AsyncClient.chat``."""
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("cohere.chat")
        auto_token = set_current_task(auto_task_obj)

    start_time = time.perf_counter()
    return _async_chat_handler(wrapped, args, kwargs, start_time, auto_task_obj, auto_token)


async def _async_chat_handler(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    start_time: float,
    auto_task_obj: Any = None,
    auto_token: Any = None,
) -> Any:
    """Await the async chat call and record the response."""
    try:
        response = await wrapped(*args, **kwargs)
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(response, latency_ms, kwargs)
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


# ---------------------------------------------------------------------------
# Streaming wrappers
# ---------------------------------------------------------------------------


def _sync_chat_stream_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Client.chat_stream``.

    ``chat_stream`` returns an iterator of streaming events; the wrapper
    accumulates token usage from the terminal ``stream-end`` event and
    records an ``llm_call`` event once the stream is fully consumed.
    """
    task = get_current_task()
    auto = task is None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("cohere.chat")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()
        model = kwargs.get("model") or "command-r-plus"
        raw_stream = wrapped(*args, **kwargs)
        return _SyncStreamWrapper(raw_stream, start_time, str(model))
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


def _async_chat_stream_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``AsyncClient.chat_stream``.

    ``AsyncClient.chat_stream`` returns an async iterator directly, so the
    wrapper simply wraps it; usage is captured as the stream is consumed.
    """
    task = get_current_task()
    auto = task is None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("cohere.chat")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()
        model = kwargs.get("model") or "command-r-plus"
        raw_stream = wrapped(*args, **kwargs)
        return _AsyncStreamWrapper(raw_stream, start_time, str(model))
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


def _extract_stream_usage(event: Any) -> Any | None:
    """Extract a ``billed_units`` usage object from a Cohere stream event.

    The terminal ``stream-end`` event carries the full response under
    ``event.response``; token counts live in ``response.meta.billed_units``.
    """
    event_type = getattr(event, "event_type", None) or getattr(event, "type", None)
    if event_type not in ("stream-end", "message-end"):
        return None
    response = getattr(event, "response", None)
    if response is None:
        return None
    meta = getattr(response, "meta", None)
    if meta is None:
        return None
    return getattr(meta, "billed_units", None)


class _SyncStreamWrapper(Iterator[Any]):
    """Wraps a sync Cohere chat stream to capture usage on completion."""

    def __init__(self, stream: Any, start_time: float, model: str) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model = model
        self._billed_units: Any | None = None
        self._finalized: bool = False

    def __iter__(self) -> _SyncStreamWrapper:
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._stream)
            usage = _extract_stream_usage(event)
            if usage is not None:
                self._billed_units = usage
            return event
        except StopIteration:
            self._finalize()
            raise

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            _record_from_stream_usage(self._model, self._billed_units, latency_ms)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

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
    """Wraps an async Cohere chat stream to capture usage on completion."""

    def __init__(self, stream: Any, start_time: float, model: str) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model = model
        self._billed_units: Any | None = None
        self._finalized: bool = False

    def __aiter__(self) -> _AsyncStreamWrapper:
        return self

    async def __anext__(self) -> Any:
        try:
            event = await self._stream.__anext__()
            usage = _extract_stream_usage(event)
            if usage is not None:
                self._billed_units = usage
            return event
        except StopAsyncIteration:
            self._finalize()
            raise

    def _finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            _record_from_stream_usage(self._model, self._billed_units, latency_ms)
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


def _record_from_stream_usage(
    model: str, billed_units: Any | None, latency_ms: int
) -> Event | None:
    """Record an event from accumulated Cohere stream usage data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    if billed_units is not None:
        input_tokens = getattr(billed_units, "input_tokens", 0) or 0
        output_tokens = getattr(billed_units, "output_tokens", 0) or 0
        has_usage = True
    else:
        input_tokens = 0
        output_tokens = 0
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
    )


def _record_from_response(
    response: Any, latency_ms: int, kwargs: dict[str, Any]
) -> Event | None:
    """Extract fields from a Cohere chat response and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    model = kwargs.get("model") or "command-r-plus"
    if not isinstance(model, str):
        model = str(model)

    # Extract token usage from response.meta.billed_units
    meta = getattr(response, "meta", None)
    billed_units = getattr(meta, "billed_units", None) if meta is not None else None

    if billed_units is not None:
        input_tokens = getattr(billed_units, "input_tokens", 0) or 0
        output_tokens = getattr(billed_units, "output_tokens", 0) or 0
        has_usage = True
    else:
        input_tokens = 0
        output_tokens = 0
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
    latency_ms: int,
    has_usage: bool,
) -> Event:
    """Create and persist an llm_call Event."""
    if has_usage:
        cost_result = tracker._pricing.get_cost(model, input_tokens, output_tokens)
        cost_usd = cost_result.cost_usd
        cost_confidence = "exact"
        pricing_source = cost_result.pricing_source
        pricing_version = cost_result.pricing_version
    else:
        cost_usd = Decimal("0")
        cost_confidence = "estimated"
        pricing_source = "unknown"
        pricing_version = None

    event = Event(
        task_id=task_id,
        event_type="llm_call",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        provider="cohere",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )
    tracker._storage.insert_event(event)
    return event
