"""Auto-instrumentation for LiteLLM — a unified LLM gateway.

Monkey-patches ``litellm.completion`` and ``litellm.acompletion`` using
:pypi:`wrapt` so that every call made inside an active
:class:`~dexcost.tracker.CostTracker` task is automatically recorded as an
``llm_call`` event.

Usage::

    from dexcost import CostTracker, instrument_litellm

    tracker = CostTracker()
    instrument_litellm(tracker)

    # All subsequent litellm.completion() / litellm.acompletion() calls
    # inside a tracked task are captured automatically.

Implements US-014.
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


def instrument_litellm(tracker: Any) -> None:
    """Monkey-patch LiteLLM to capture LLM calls automatically.

    Patches ``litellm.completion`` (sync) and ``litellm.acompletion`` (async).

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            price calls and persist events.

    Raises:
        ImportError: If the ``litellm`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched

    if _patched:
        raise RuntimeError(
            "LiteLLM instrumentation is already active. "
            "Call uninstrument_litellm() before re-instrumenting."
        )

    # Verify litellm is importable
    try:
        import litellm as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'litellm' package is required for LiteLLM auto-instrumentation. "
            "Install it with: pip install litellm"
        ) from exc

    _active_tracker = tracker

    # Store originals for uninstrument
    import litellm

    _originals["completion"] = litellm.completion
    _originals["acompletion"] = litellm.acompletion

    # Apply monkey-patches via wrapt
    wrapt.wrap_function_wrapper(
        "litellm",
        "completion",
        _sync_completion_wrapper,
    )
    wrapt.wrap_function_wrapper(
        "litellm",
        "acompletion",
        _async_completion_wrapper,
    )

    _patched = True


def uninstrument_litellm() -> None:
    """Remove LiteLLM monkey-patches and restore original functions.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched

    if not _patched:
        return

    import litellm

    if "completion" in _originals:
        litellm.completion = _originals["completion"]
    if "acompletion" in _originals:
        litellm.acompletion = _originals["acompletion"]

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _sync_completion_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``litellm.completion``."""
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("litellm.completion")
        auto_token = set_current_task(auto_task_obj)

    try:
        stream = kwargs.get("stream", False)
        start_time = time.perf_counter()

        if stream:
            raw_stream = wrapped(*args, **kwargs)
            return _SyncStreamWrapper(raw_stream, start_time, kwargs.get("model"))

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


def _async_completion_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``litellm.acompletion``."""
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("litellm.completion")
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
    """Await the async acompletion call and record the response."""
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
    """Wrap async streaming to capture usage from the final chunk."""
    try:
        raw_stream = await wrapped(*args, **kwargs)
        return _AsyncStreamWrapper(raw_stream, start_time, kwargs.get("model"))
    finally:
        if auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Stream wrappers
# ---------------------------------------------------------------------------


class _SyncStreamWrapper(Iterator[Any]):
    """Wraps a sync LiteLLM stream to capture usage on completion."""

    def __init__(self, stream: Any, start_time: float, request_model: Any = None) -> None:
        self._stream = stream
        self._start_time = start_time
        self._request_model = request_model
        self._model: str | None = None
        self._usage: Any | None = None
        self._hidden_params: dict[str, Any] | None = None
        self._finalized: bool = False

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
        if hasattr(chunk, "model") and chunk.model:
            self._model = chunk.model
        if hasattr(chunk, "usage") and chunk.usage is not None:
            self._usage = chunk.usage
        if hasattr(chunk, "_hidden_params") and chunk._hidden_params:
            self._hidden_params = chunk._hidden_params

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                request_model=self._request_model,
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
    """Wraps an async LiteLLM stream to capture usage on completion."""

    def __init__(self, stream: Any, start_time: float, request_model: Any = None) -> None:
        self._stream = stream
        self._start_time = start_time
        self._request_model = request_model
        self._model: str | None = None
        self._usage: Any | None = None
        self._hidden_params: dict[str, Any] | None = None
        self._finalized: bool = False

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
        if hasattr(chunk, "model") and chunk.model:
            self._model = chunk.model
        if hasattr(chunk, "usage") and chunk.usage is not None:
            self._usage = chunk.usage
        if hasattr(chunk, "_hidden_params") and chunk._hidden_params:
            self._hidden_params = chunk._hidden_params

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                request_model=self._request_model,
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
# Provider resolution
# ---------------------------------------------------------------------------


def _resolve_provider(
    response: Any = None,
    hidden_params: dict[str, Any] | None = None,
    request_model: Any = None,
) -> str:
    """Resolve the actual LLM provider from LiteLLM response data.

    Resolution order:
    1. ``_hidden_params["custom_llm_provider"]`` from the response
    2. Prefix of the model string (e.g. ``"openai/gpt-4"`` -> ``"openai"``)
    3. ``"unknown"``
    """
    # Try _hidden_params on the response object itself
    hp: dict[str, Any] | None = hidden_params
    if hp is None and response is not None:
        hp = getattr(response, "_hidden_params", None)

    if hp and isinstance(hp, dict):
        provider = hp.get("custom_llm_provider")
        if provider and isinstance(provider, str):
            return str(provider)

    # Try extracting from model string prefix (e.g. "openai/gpt-4")
    model_str: str | None = None
    if response is not None:
        raw = getattr(response, "model", None)
        if raw is not None:
            model_str = str(raw)
    if not model_str and request_model is not None:
        model_str = str(request_model)

    if model_str and "/" in model_str:
        prefix = model_str.split("/", 1)[0]
        if prefix:
            return str(prefix)

    return "unknown"


# ---------------------------------------------------------------------------
# LiteLLM cost calculation
# ---------------------------------------------------------------------------


def _try_litellm_cost(response: Any) -> Decimal | None:
    """Attempt to use LiteLLM's own ``completion_cost`` for cost calculation.

    Returns the cost as a :class:`Decimal`, or ``None`` if LiteLLM cost
    calculation is unavailable or fails.
    """
    try:
        import litellm

        cost = litellm.completion_cost(completion_response=response)
        if cost is not None and cost > 0:
            return Decimal(str(cost))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _record_from_response(response: Any, latency_ms: int) -> Event | None:
    """Extract fields from a LiteLLM ModelResponse and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    model = getattr(response, "model", None) or "unknown"
    usage = getattr(response, "usage", None)
    provider = _resolve_provider(response)

    if usage is not None:
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        has_usage = True
    else:
        input_tokens = 0
        output_tokens = 0
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        response=response,
    )


def _record_from_stream_usage(
    *,
    model: str | None,
    usage: Any | None,
    hidden_params: dict[str, Any] | None,
    request_model: Any,
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
    provider = _resolve_provider(hidden_params=hidden_params, request_model=request_model)

    if usage is not None:
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        has_usage = True
    else:
        input_tokens = 0
        output_tokens = 0
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=resolved_model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        response=None,
    )


def _insert_llm_event(
    *,
    tracker: Any,
    task_id: Any,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    has_usage: bool,
    response: Any | None,
) -> Event:
    """Create and persist an llm_call Event.

    Tries LiteLLM's own ``completion_cost`` first; falls back to
    the dexcost pricing engine.
    """
    if has_usage:
        # Try LiteLLM cost calculation first
        litellm_cost = _try_litellm_cost(response) if response is not None else None

        if litellm_cost is not None:
            cost_usd = litellm_cost
            cost_confidence = "exact"
            pricing_source = "litellm"
            pricing_version: str | None = None
        else:
            # Fall back to dexcost pricing engine
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
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
    )
    tracker._storage.insert_event(event)
    return event
