"""Auto-instrumentation for the OpenAI Python SDK.

Monkey-patches Chat Completions and Responses ``create`` calls (sync and
async) using :pypi:`wrapt` so that every call made inside an active
:class:`~dexcost.tracker.CostTracker` task is automatically recorded as an
``llm_call`` event.

Usage::

    from dexcost import CostTracker, instrument_openai

    tracker = CostTracker()
    instrument_openai(tracker)

    # Subsequent Chat Completions and Responses calls inside a tracked task
    # are captured automatically.

Implements US-012.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import suppress
from decimal import Decimal
from typing import Any

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.context import (
    _current_task,
    get_current_task,
    set_current_task,
    suppress_network_event,
)
from dexcost.instruments.openai_usage import OpenAIUsageError, normalize_openai_usage
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


def instrument_openai(tracker: Any) -> None:
    """Monkey-patch the OpenAI SDK to capture LLM calls automatically.

    Patches Chat Completions and, when available, the Responses API (sync and
    async). Older OpenAI SDK versions continue to receive Chat instrumentation.

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            price calls and persist events.

    Raises:
        ImportError: If the ``openai`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched

    if _patched:
        raise RuntimeError(
            "OpenAI instrumentation is already active. "
            "Call uninstrument_openai() before re-instrumenting."
        )

    # Verify openai is importable
    try:
        import openai.resources.chat.completions as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for OpenAI auto-instrumentation. "
            "Install it with: pip install openai"
        ) from exc

    _active_tracker = tracker

    # Store originals for uninstrument
    from openai.resources.chat.completions import AsyncCompletions, Completions

    _originals["sync_create"] = Completions.create
    _originals["async_create"] = AsyncCompletions.create

    # Apply monkey-patches via wrapt
    wrapt.wrap_function_wrapper(
        "openai.resources.chat.completions",
        "Completions.create",
        _sync_create_wrapper,
    )
    wrapt.wrap_function_wrapper(
        "openai.resources.chat.completions",
        "AsyncCompletions.create",
        _async_create_wrapper,
    )

    try:
        from openai.resources.responses.responses import AsyncResponses, Responses

        _originals["responses_sync_create"] = Responses.create
        _originals["responses_async_create"] = AsyncResponses.create
        wrapt.wrap_function_wrapper(
            "openai.resources.responses.responses",
            "Responses.create",
            _sync_responses_create_wrapper,
        )
        wrapt.wrap_function_wrapper(
            "openai.resources.responses.responses",
            "AsyncResponses.create",
            _async_responses_create_wrapper,
        )
    except ImportError:
        _log.debug("dexcost: installed OpenAI SDK has no Responses API")

    _patched = True


def uninstrument_openai() -> None:
    """Remove OpenAI monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched

    if not _patched:
        return

    from openai.resources.chat.completions import AsyncCompletions, Completions

    if "sync_create" in _originals:
        Completions.create = _originals["sync_create"]
    if "async_create" in _originals:
        AsyncCompletions.create = _originals["async_create"]
    try:
        from openai.resources.responses.responses import AsyncResponses, Responses

        if "responses_sync_create" in _originals:
            Responses.create = _originals["responses_sync_create"]
        if "responses_async_create" in _originals:
            AsyncResponses.create = _originals["responses_async_create"]
    except ImportError:
        pass

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _sync_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Completions.create``."""
    return _sync_create_common(wrapped, args, kwargs, "openai.chat", False)


def _sync_responses_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Responses.create``."""
    return _sync_create_common(wrapped, args, kwargs, "openai.responses", True)


def _sync_create_common(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    task_type: str,
    responses_stream: bool,
) -> Any:
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task(task_type)
        task = auto_task_obj
        auto_token = set_current_task(auto_task_obj)

    try:
        stream = kwargs.get("stream", False)
        start_time = time.perf_counter()

        if stream:
            with suppress_network_event():
                raw_stream = wrapped(*args, **kwargs)
            return _SyncStreamWrapper(
                raw_stream,
                start_time,
                responses_stream,
                task,
                auto_task_obj,
            )

        with suppress_network_event():
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
            with suppress(Exception):
                _log.debug("dexcost: auto-task call failed", exc_info=True)
        raise
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


def _async_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``AsyncCompletions.create``."""
    return _async_create_common(wrapped, args, kwargs, "openai.chat", False)


def _async_responses_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``AsyncResponses.create``."""
    return _async_create_common(wrapped, args, kwargs, "openai.responses", True)


def _async_create_common(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    task_type: str,
    responses_stream: bool,
) -> Any:
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task(task_type)
        task = auto_task_obj
        auto_token = set_current_task(auto_task_obj)

    stream = kwargs.get("stream", False)
    start_time = time.perf_counter()

    if stream:
        return _async_stream_handler(
            wrapped,
            args,
            kwargs,
            start_time,
            auto_task_obj,
            auto_token,
            responses_stream,
            task,
        )

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
        with suppress_network_event():
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
    responses_stream: bool = False,
    task: Any = None,
) -> Any:
    """Wrap async streaming to capture usage from the final chunk."""
    try:
        with suppress_network_event():
            raw_stream = await wrapped(*args, **kwargs)
        return _AsyncStreamWrapper(
            raw_stream,
            start_time,
            responses_stream,
            task,
            auto_task_obj,
        )
    finally:
        if auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Stream wrappers
# ---------------------------------------------------------------------------


class _SyncStreamWrapper(Iterator[Any]):
    """Wraps a sync OpenAI stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        responses_stream: bool = False,
        task: Any = None,
        auto_task_obj: Any = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model: str | None = None
        self._usage: Any | None = None
        self._record_id: str | None = None
        self._finalized: bool = False
        self._responses_stream = responses_stream
        self._task = task
        self._auto_task_obj = auto_task_obj

    def __iter__(self) -> _SyncStreamWrapper:
        return self

    def __next__(self) -> Any:
        try:
            chunk = next(self._stream)
            self._process_chunk(chunk)
            return chunk
        except StopIteration:
            self._finalize()
            raise

    def _process_chunk(self, chunk: Any) -> None:
        """Extract model and usage info from streaming chunks."""
        if self._responses_stream and getattr(chunk, "type", None) == "response.completed":
            response = getattr(chunk, "response", None)
            if response is not None:
                chunk = response
        if hasattr(chunk, "model") and chunk.model:
            self._model = chunk.model
        if hasattr(chunk, "id") and chunk.id:
            self._record_id = chunk.id
        if hasattr(chunk, "usage") and chunk.usage is not None:
            self._usage = chunk.usage

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                self._model,
                self._usage,
                latency_ms,
                self._record_id,
                self._task,
            )
            _finalize_stream_auto_task(self._auto_task_obj, event)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    # Forward close/context-manager to the underlying stream
    def close(self) -> None:
        self._finalize()
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
    """Wraps an async OpenAI stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        responses_stream: bool = False,
        task: Any = None,
        auto_task_obj: Any = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model: str | None = None
        self._usage: Any | None = None
        self._record_id: str | None = None
        self._finalized: bool = False
        self._responses_stream = responses_stream
        self._task = task
        self._auto_task_obj = auto_task_obj

    def __aiter__(self) -> _AsyncStreamWrapper:
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._stream.__anext__()
            self._process_chunk(chunk)
            return chunk
        except StopAsyncIteration:
            self._finalize()
            raise

    def _process_chunk(self, chunk: Any) -> None:
        """Extract model and usage info from streaming chunks."""
        if self._responses_stream and getattr(chunk, "type", None) == "response.completed":
            response = getattr(chunk, "response", None)
            if response is not None:
                chunk = response
        if hasattr(chunk, "model") and chunk.model:
            self._model = chunk.model
        if hasattr(chunk, "id") and chunk.id:
            self._record_id = chunk.id
        if hasattr(chunk, "usage") and chunk.usage is not None:
            self._usage = chunk.usage

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                self._model,
                self._usage,
                latency_ms,
                self._record_id,
                self._task,
            )
            _finalize_stream_auto_task(self._auto_task_obj, event)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    async def aclose(self) -> None:
        self._finalize()
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
    """Extract fields from a Chat Completion or Responses response."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    model = getattr(response, "model", None) or "unknown"
    usage = getattr(response, "usage", None)
    record_id = getattr(response, "id", None)

    if usage is not None:
        try:
            normalized = normalize_openai_usage(usage)
            input_tokens = normalized.total_input_tokens
            output_tokens = normalized.total_output_tokens
            cached_tokens = normalized.cache_read_input_tokens
            cache_write_tokens = normalized.cache_write_input_tokens
            reasoning_tokens = normalized.reasoning_output_tokens
            usage_error = None
            has_usage = True
        except OpenAIUsageError as exc:
            input_tokens = output_tokens = cached_tokens = 0
            cache_write_tokens = reasoning_tokens = 0
            usage_error = str(exc)
            has_usage = False
    else:
        input_tokens = output_tokens = cached_tokens = cache_write_tokens = reasoning_tokens = 0
        usage_error = None
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        record_id=record_id,
        usage_error=usage_error,
    )


def _record_from_stream_usage(
    model: str | None,
    usage: Any | None,
    latency_ms: int,
    record_id: str | None = None,
    task: Any = None,
) -> Event | None:
    """Record an event from accumulated stream data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    resolved_task = task or get_current_task()
    if resolved_task is None:
        return None

    resolved_model = model or "unknown"

    if usage is not None:
        try:
            normalized = normalize_openai_usage(usage)
            input_tokens = normalized.total_input_tokens
            output_tokens = normalized.total_output_tokens
            cached_tokens = normalized.cache_read_input_tokens
            cache_write_tokens = normalized.cache_write_input_tokens
            reasoning_tokens = normalized.reasoning_output_tokens
            usage_error = None
            has_usage = True
        except OpenAIUsageError as exc:
            input_tokens = output_tokens = cached_tokens = 0
            cache_write_tokens = reasoning_tokens = 0
            usage_error = str(exc)
            has_usage = False
    else:
        input_tokens = output_tokens = cached_tokens = cache_write_tokens = reasoning_tokens = 0
        usage_error = None
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=resolved_task.task_id,
        model=resolved_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        record_id=record_id,
        usage_error=usage_error,
    )


def _finalize_stream_auto_task(auto_task_obj: Any, event: Event | None) -> None:
    if auto_task_obj is None or event is None:
        return
    finalize_auto_task(auto_task_obj, event, status="success")
    if _active_tracker is not None:
        _active_tracker._storage.insert_task(auto_task_obj)


def _insert_llm_event(
    *,
    tracker: Any,
    task_id: Any,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    reasoning_tokens: int,
    latency_ms: int,
    has_usage: bool,
    record_id: str | None = None,
    usage_error: str | None = None,
) -> Event:
    """Create and persist an llm_call Event."""
    if has_usage:
        cost_result = tracker._pricing.get_cost(
            model,
            input_tokens,
            output_tokens,
            cached_tokens,
            cache_write_tokens,
        )
        cost_usd = cost_result.cost_usd
        cost_confidence = cost_result.cost_confidence
        pricing_source = cost_result.pricing_source
        pricing_version = cost_result.pricing_version
    else:
        cost_usd = Decimal("0")
        cost_confidence = "unknown" if usage_error is not None else "estimated"
        pricing_source = "unknown"
        pricing_version = None

    details: dict[str, Any] = {}
    if cache_write_tokens > 0:
        details["cache_write_input_tokens"] = cache_write_tokens
    if reasoning_tokens > 0:
        details["reasoning_output_tokens"] = reasoning_tokens
    if isinstance(record_id, str) and record_id:
        details["provider_record_id"] = record_id
    if usage_error is not None:
        details["openai_usage_error"] = usage_error

    event = Event(
        task_id=task_id,
        event_type="llm_call",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        provider="openai",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    tracker._storage.insert_event(event)
    return event
