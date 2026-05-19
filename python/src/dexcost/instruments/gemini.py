"""Auto-instrumentation for the Google GenAI SDK (Gemini).

Monkey-patches ``google.genai.models.Models.generate_content`` using
:pypi:`wrapt` so that every call made inside an active
:class:`~dexcost.tracker.CostTracker` task is automatically recorded as an
``llm_call`` event.

Usage::

    from dexcost import CostTracker, instrument_gemini

    tracker = CostTracker()
    instrument_gemini(tracker)

    # All subsequent google.genai models generate_content() calls inside a
    # tracked task are captured automatically.

Implements US-012 (Gemini provider).
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


def instrument_gemini(tracker: Any) -> None:
    """Monkey-patch the Google GenAI SDK to capture LLM calls automatically.

    Patches ``google.genai.models.Models.generate_content`` (sync) and, when
    present, ``google.genai.models.Models.generate_content_stream`` so that
    streamed responses are also captured with token usage.

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance used to
            price calls and persist events.

    Raises:
        ImportError: If the ``google-genai`` package is not installed.
        RuntimeError: If instrumentation is already active.
    """
    global _active_tracker, _patched

    if _patched:
        raise RuntimeError(
            "Gemini instrumentation is already active. "
            "Call uninstrument_gemini() before re-instrumenting."
        )

    # Verify google.genai is importable
    try:
        import google.genai.models as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'google-genai' package is required for Gemini auto-instrumentation. "
            "Install it with: pip install google-genai"
        ) from exc

    _active_tracker = tracker

    # Store originals for uninstrument
    from google.genai import models

    _originals["generate_content"] = models.Models.generate_content

    # Apply monkey-patch via wrapt
    wrapt.wrap_function_wrapper(
        "google.genai.models",
        "Models.generate_content",
        _sync_generate_content_wrapper,
    )

    # Streaming path — google.genai exposes a separate streaming method,
    # ``generate_content_stream``, that returns an iterator of response
    # chunks.  Patch it when available (older SDK versions may lack it).
    if hasattr(models.Models, "generate_content_stream"):
        _originals["generate_content_stream"] = models.Models.generate_content_stream
        wrapt.wrap_function_wrapper(
            "google.genai.models",
            "Models.generate_content_stream",
            _sync_generate_content_stream_wrapper,
        )

    _patched = True


def uninstrument_gemini() -> None:
    """Remove Gemini monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched

    if not _patched:
        return

    try:
        from google.genai import models

        if "generate_content" in _originals:
            models.Models.generate_content = _originals["generate_content"]
        if "generate_content_stream" in _originals:
            models.Models.generate_content_stream = _originals["generate_content_stream"]
    except ImportError:
        pass

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _sync_generate_content_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Models.generate_content``."""
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("gemini.generate_content")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()

        response = wrapped(*args, **kwargs)
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(response, latency_ms, args, kwargs)
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


def _sync_generate_content_stream_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Models.generate_content_stream``.

    The underlying call returns an iterator of response chunks; the stream
    wrapper accumulates ``usage_metadata`` and records the ``llm_call``
    event once the stream is fully consumed.
    """
    task = get_current_task()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("gemini.generate_content")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()
        model = _resolve_model_name(args, kwargs)
        raw_stream = wrapped(*args, **kwargs)
        return _SyncStreamWrapper(raw_stream, start_time, model)
    finally:
        if auto and auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Stream wrapper
# ---------------------------------------------------------------------------


class _SyncStreamWrapper(Iterator[Any]):
    """Wraps a sync Gemini streaming response to capture usage on completion.

    Each streamed chunk is a ``GenerateContentResponse``; ``usage_metadata``
    is typically populated on the final chunk(s).  The most recent chunk
    that carries usage wins.
    """

    def __init__(self, stream: Any, start_time: float, model: str) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model = model
        self._usage: Any | None = None
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
        """Extract usage info from streaming chunks."""
        usage = getattr(chunk, "usage_metadata", None)
        if usage is not None:
            self._usage = usage

    def _finalize(self) -> None:
        """Record the event after the stream is fully consumed."""
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            _record_from_stream_usage(self._model, self._usage, latency_ms)
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


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _resolve_model_name(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str:
    """Extract the model name from generate_content arguments.

    The google.genai SDK takes ``model`` as the first positional argument
    or as a keyword argument.  Model strings may be prefixed with
    ``"models/"`` (e.g. ``"models/gemini-2.0-flash"``).
    """
    model_name: str | None = kwargs.get("model")
    if model_name is None and args:
        candidate = args[0]
        if isinstance(candidate, str):
            model_name = candidate

    if model_name is None:
        return "gemini-unknown"

    # Strip the "models/" prefix that google.genai often uses
    if isinstance(model_name, str) and model_name.startswith("models/"):
        model_name = model_name[len("models/"):]

    return str(model_name)


def _record_from_response(
    response: Any,
    latency_ms: int,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Event | None:
    """Extract fields from a Gemini GenerateContentResponse and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    model = _resolve_model_name(args, kwargs)

    # Extract token usage from response.usage_metadata
    usage = getattr(response, "usage_metadata", None)

    if usage is not None:
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0
        has_usage = True
    else:
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
    )


def _record_from_stream_usage(
    model: str,
    usage: Any | None,
    latency_ms: int,
) -> Event | None:
    """Record an event from accumulated Gemini stream usage data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    if usage is not None:
        input_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0
        has_usage = True
    else:
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        has_usage = False

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
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
    cached_tokens: int,
    latency_ms: int,
    has_usage: bool,
) -> Event:
    """Create and persist an llm_call Event."""
    if has_usage:
        cost_result = tracker._pricing.get_cost(model, input_tokens, output_tokens, cached_tokens)
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
        provider="google",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
    )
    tracker._storage.insert_event(event)
    return event
