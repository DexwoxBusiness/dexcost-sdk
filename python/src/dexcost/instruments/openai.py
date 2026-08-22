"""Auto-instrumentation for the OpenAI Python SDK.

Monkey-patches Chat Completions ``create`` calls and Responses ``create`` and
``parse`` calls (sync and async) using :pypi:`wrapt` so that every call made inside an active
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
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
from inspect import isawaitable
from typing import Any
from urllib.parse import urlparse

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.capabilities import get_capability
from dexcost.context import (
    _current_task,
    get_current_task,
    set_current_task,
    suppress_network_event,
)
from dexcost.idempotency import get_idempotency_key
from dexcost.instruments._errors import (
    finalize_failed_auto_task,
    record_call_failure,
    record_stream_failure,
    requested_model,
)
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    OperationStatus,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
    record_provider_operation,
)
from dexcost.instruments.openai_usage import OpenAIUsageError, normalize_openai_usage
from dexcost.models.event import Event
from dexcost.models.provider_job import ProviderJobEventType, ProviderJobStatus
from dexcost.provider_jobs import (
    AsyncProviderJobStream,
    ProviderJobSession,
    SyncProviderJobStream,
    reconcile_provider_job,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}
_patched_owner: tuple[Any, Any] | None = None
_provider_identity: ContextVar[str] = ContextVar(
    "dexcost_openai_compatible_provider", default="openai"
)


def _current_provider() -> str:
    return _provider_identity.get()


def _provider_for_instance(instance: Any) -> str:
    client = getattr(instance, "_client", None)
    base_url = getattr(client, "base_url", None)
    if base_url is not None:
        with suppress(ValueError):
            hostname = (urlparse(str(base_url)).hostname or "").lower()
            if hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai"):
                return "openrouter"
            if hostname == "api.perplexity.ai" or hostname.endswith(
                ".perplexity.ai"
            ):
                return "perplexity"
            if hostname.endswith(".openai.azure.com") or hostname.endswith(
                ".services.ai.azure.com"
            ):
                return "azure_openai"
    return "openai"


def _routed_wrapper(wrapper: Any) -> Any:
    """Bind OpenAI-compatible calls to their actual billing provider."""

    def routed(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        provider = _provider_for_instance(instance)
        token = _provider_identity.set(provider)
        try:
            result = wrapper(wrapped, instance, args, kwargs)
        finally:
            _provider_identity.reset(token)
        if not isawaitable(result):
            return result

        async def await_result() -> Any:
            async_token = _provider_identity.set(provider)
            try:
                return await result
            finally:
                _provider_identity.reset(async_token)

        return await_result()

    return routed


def _patch_optional_method(
    module_name: str,
    class_name: str,
    method_name: str,
    wrapper: Any,
) -> None:
    """Patch one public OpenAI SDK method when the installed version has it."""
    try:
        module = import_module(module_name)
        resource_class = getattr(module, class_name)
        original = getattr(resource_class, method_name)
    except (AttributeError, ImportError):
        return
    key = f"optional:{module_name}:{class_name}:{method_name}"
    _originals[key] = (resource_class, method_name, original)
    wrapt.wrap_function_wrapper(
        module_name,
        f"{class_name}.{method_name}",
        _routed_wrapper(wrapper),
    )


def _restore_optional_methods() -> None:
    for stored in list(_originals.values()):
        if not isinstance(stored, tuple) or len(stored) != 3:
            continue
        resource_class, method_name, original = stored
        setattr(resource_class, method_name, original)


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
    global _active_tracker, _patched, _patched_owner

    # Verify openai is importable
    try:
        import openai.resources.chat.completions as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for OpenAI auto-instrumentation. "
            "Install it with: pip install openai"
        ) from exc

    from openai.resources.chat.completions import AsyncCompletions, Completions

    current_owner = (Completions, AsyncCompletions)
    if _patched:
        if _patched_owner == current_owner:
            raise RuntimeError(
                "OpenAI instrumentation is already active. "
                "Call uninstrument_openai() before re-instrumenting."
            )
        # A plugin/test reload can replace ``sys.modules`` while the exact
        # classes patched earlier remain live in clients that were already
        # constructed. Restore those owner objects directly before switching
        # module graphs; clearing bookkeeping here would orphan wrappers and
        # make future instrumentation stack them.
        _restore_optional_methods()
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None

    _active_tracker = tracker
    _patched_owner = current_owner

    # Store originals for uninstrument

    _originals["sync_create"] = (Completions, "create", Completions.create)
    _originals["async_create"] = (
        AsyncCompletions,
        "create",
        AsyncCompletions.create,
    )

    # Apply monkey-patches via wrapt
    wrapt.wrap_function_wrapper(
        "openai.resources.chat.completions",
        "Completions.create",
        _routed_wrapper(_sync_create_wrapper),
    )
    wrapt.wrap_function_wrapper(
        "openai.resources.chat.completions",
        "AsyncCompletions.create",
        _routed_wrapper(_async_create_wrapper),
    )

    try:
        from openai.resources.responses.responses import AsyncResponses, Responses

        _originals["responses_sync_create"] = (
            Responses,
            "create",
            Responses.create,
        )
        _originals["responses_async_create"] = (
            AsyncResponses,
            "create",
            AsyncResponses.create,
        )
        wrapt.wrap_function_wrapper(
            "openai.resources.responses.responses",
            "Responses.create",
            _routed_wrapper(_sync_responses_create_wrapper),
        )
        wrapt.wrap_function_wrapper(
            "openai.resources.responses.responses",
            "AsyncResponses.create",
            _routed_wrapper(_async_responses_create_wrapper),
        )
        # Responses.parse() performs its own POST request; it does not call
        # Responses.create(). Patch it independently when the installed
        # OpenAI SDK exposes the structured-output helper.
        if callable(getattr(Responses, "parse", None)):
            _originals["responses_sync_parse"] = (
                Responses,
                "parse",
                Responses.parse,
            )
            wrapt.wrap_function_wrapper(
                "openai.resources.responses.responses",
                "Responses.parse",
                _routed_wrapper(_sync_responses_parse_wrapper),
            )
        if callable(getattr(AsyncResponses, "parse", None)):
            _originals["responses_async_parse"] = (
                AsyncResponses,
                "parse",
                AsyncResponses.parse,
            )
            wrapt.wrap_function_wrapper(
                "openai.resources.responses.responses",
                "AsyncResponses.parse",
                _routed_wrapper(_async_responses_parse_wrapper),
            )
    except ImportError:
        _log.debug("dexcost: installed OpenAI SDK has no Responses API")

    # Public cost-causing methods outside Chat/Responses.  Each is optional so
    # older OpenAI SDK releases retain the surface they actually expose.
    _patch_optional_method(
        "openai.resources.chat.completions",
        "Completions",
        "parse",
        _sync_chat_parse_wrapper,
    )
    _patch_optional_method(
        "openai.resources.chat.completions",
        "AsyncCompletions",
        "parse",
        _async_chat_parse_wrapper,
    )
    _patch_optional_method(
        "openai.resources.completions",
        "Completions",
        "create",
        _sync_legacy_completion_wrapper,
    )
    _patch_optional_method(
        "openai.resources.completions",
        "AsyncCompletions",
        "create",
        _async_legacy_completion_wrapper,
    )
    _patch_optional_method(
        "openai.resources.embeddings",
        "Embeddings",
        "create",
        _sync_embeddings_wrapper,
    )
    _patch_optional_method(
        "openai.resources.embeddings",
        "AsyncEmbeddings",
        "create",
        _async_embeddings_wrapper,
    )
    for method_name, sync_wrapper, async_wrapper in (
        ("generate", _sync_image_generate_wrapper, _async_image_generate_wrapper),
        ("edit", _sync_image_edit_wrapper, _async_image_edit_wrapper),
        (
            "create_variation",
            _sync_image_variation_wrapper,
            _async_image_variation_wrapper,
        ),
    ):
        _patch_optional_method(
            "openai.resources.images", "Images", method_name, sync_wrapper
        )
        _patch_optional_method(
            "openai.resources.images", "AsyncImages", method_name, async_wrapper
        )
    for module_name, class_name, async_class_name, sync_wrapper, async_wrapper in (
        (
            "openai.resources.audio.transcriptions",
            "Transcriptions",
            "AsyncTranscriptions",
            _sync_audio_transcription_wrapper,
            _async_audio_transcription_wrapper,
        ),
        (
            "openai.resources.audio.translations",
            "Translations",
            "AsyncTranslations",
            _sync_audio_translation_wrapper,
            _async_audio_translation_wrapper,
        ),
        (
            "openai.resources.audio.speech",
            "Speech",
            "AsyncSpeech",
            _sync_audio_speech_wrapper,
            _async_audio_speech_wrapper,
        ),
    ):
        _patch_optional_method(module_name, class_name, "create", sync_wrapper)
        _patch_optional_method(module_name, async_class_name, "create", async_wrapper)

    for method_name, sync_wrapper, async_wrapper in (
        (
            "retrieve",
            _sync_response_job_reconcile_wrapper,
            _async_response_job_reconcile_wrapper,
        ),
        (
            "cancel",
            _sync_response_job_reconcile_wrapper,
            _async_response_job_reconcile_wrapper,
        ),
    ):
        _patch_optional_method(
            "openai.resources.responses.responses",
            "Responses",
            method_name,
            sync_wrapper,
        )
        _patch_optional_method(
            "openai.resources.responses.responses",
            "AsyncResponses",
            method_name,
            async_wrapper,
        )

    for method_name, sync_wrapper, async_wrapper in (
        ("create", _sync_batch_create_wrapper, _async_batch_create_wrapper),
        ("retrieve", _sync_batch_reconcile_wrapper, _async_batch_reconcile_wrapper),
        ("cancel", _sync_batch_reconcile_wrapper, _async_batch_reconcile_wrapper),
    ):
        _patch_optional_method(
            "openai.resources.batches", "Batches", method_name, sync_wrapper
        )
        _patch_optional_method(
            "openai.resources.batches", "AsyncBatches", method_name, async_wrapper
        )

    for method_name in ("create", "remix", "edit", "extend"):
        _patch_optional_method(
            "openai.resources.videos",
            "Videos",
            method_name,
            _sync_video_submit_wrapper(method_name),
        )
        _patch_optional_method(
            "openai.resources.videos",
            "AsyncVideos",
            method_name,
            _async_video_submit_wrapper(method_name),
        )
    _patch_optional_method(
        "openai.resources.videos",
        "Videos",
        "retrieve",
        _sync_video_reconcile_wrapper,
    )
    _patch_optional_method(
        "openai.resources.videos",
        "AsyncVideos",
        "retrieve",
        _async_video_reconcile_wrapper,
    )

    for method_name, sync_wrapper, async_wrapper in (
        ("create", _sync_fine_tuning_create_wrapper, _async_fine_tuning_create_wrapper),
        (
            "retrieve",
            _sync_fine_tuning_reconcile_wrapper,
            _async_fine_tuning_reconcile_wrapper,
        ),
        (
            "cancel",
            _sync_fine_tuning_reconcile_wrapper,
            _async_fine_tuning_reconcile_wrapper,
        ),
        (
            "pause",
            _sync_fine_tuning_reconcile_wrapper,
            _async_fine_tuning_reconcile_wrapper,
        ),
        (
            "resume",
            _sync_fine_tuning_reconcile_wrapper,
            _async_fine_tuning_reconcile_wrapper,
        ),
    ):
        _patch_optional_method(
            "openai.resources.fine_tuning.jobs.jobs",
            "Jobs",
            method_name,
            sync_wrapper,
        )
        _patch_optional_method(
            "openai.resources.fine_tuning.jobs.jobs",
            "AsyncJobs",
            method_name,
            async_wrapper,
        )

    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "Realtime",
        "connect",
        _sync_realtime_connect_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "AsyncRealtime",
        "connect",
        _async_realtime_connect_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "RealtimeConnectionManager",
        "__enter__",
        _sync_realtime_enter_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "AsyncRealtimeConnectionManager",
        "__aenter__",
        _async_realtime_enter_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "RealtimeResponseResource",
        "create",
        _sync_realtime_response_create_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "AsyncRealtimeResponseResource",
        "create",
        _async_realtime_response_create_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "RealtimeConnection",
        "recv",
        _sync_realtime_recv_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "AsyncRealtimeConnection",
        "recv",
        _async_realtime_recv_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "RealtimeConnection",
        "_reconnect",
        _sync_realtime_reconnect_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "AsyncRealtimeConnection",
        "_reconnect",
        _async_realtime_reconnect_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "RealtimeConnection",
        "close",
        _sync_realtime_close_wrapper,
    )
    _patch_optional_method(
        "openai.resources.realtime.realtime",
        "AsyncRealtimeConnection",
        "close",
        _async_realtime_close_wrapper,
    )

    _patched = True


def uninstrument_openai() -> None:
    """Remove OpenAI monkey-patches and restore original methods.

    Safe to call even if instrumentation is not active (no-op).
    """
    global _active_tracker, _patched, _patched_owner

    if not _patched:
        return

    # Restore the precise class objects captured at patch time. Imports may now
    # resolve to a different SDK module graph after hot reload or plugin unload.
    _restore_optional_methods()

    _originals.clear()
    _active_tracker = None
    _patched = False
    _patched_owner = None


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
    if kwargs.get("background") is True:
        return _sync_response_job_create(wrapped, args, kwargs)
    return _sync_create_common(wrapped, args, kwargs, "openai.responses", True)


def _sync_responses_parse_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Responses.parse``."""
    return _sync_create_common(wrapped, args, kwargs, "openai.responses", True)


def _sync_chat_parse_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """Capture ``chat.completions.parse`` which performs its own POST."""
    return _sync_create_common(wrapped, args, kwargs, "openai.chat", False)


def _sync_legacy_completion_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _sync_create_common(wrapped, args, kwargs, "openai.completions", False)


def _record_call_failure(
    exc: BaseException,
    start_time: float,
    kwargs: dict[str, Any],
    task: Any = None,
    auto_task_obj: Any = None,
) -> Event | None:
    """Record a raised OpenAI call as a failed operation.

    Never raises: the caller re-raises the user's original exception with a
    bare ``raise`` immediately after.
    """
    try:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
    except Exception:  # pragma: no cover - defensive
        latency_ms = None
    event = record_call_failure(
        tracker=_active_tracker,
        exc=exc,
        provider=_current_provider(),
        model=requested_model(kwargs),
        latency_ms=latency_ms,
        task=task,
    )
    finalize_failed_auto_task(_active_tracker, auto_task_obj, event)
    return event


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
            try:
                with suppress_network_event():
                    raw_stream = wrapped(*args, **kwargs)
            except Exception as exc:
                _record_call_failure(exc, start_time, kwargs, task, auto_task_obj)
                raise
            return _SyncStreamWrapper(
                raw_stream,
                start_time,
                responses_stream,
                task,
                auto_task_obj,
                requested_model(kwargs),
            )

        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(exc, start_time, kwargs, task, auto_task_obj)
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(
                response, latency_ms, requested=requested_model(kwargs)
            )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

        if auto and auto_task_obj is not None and event is not None:
            try:
                finalize_auto_task(auto_task_obj, event, status="success")
                if _active_tracker is not None:
                    _active_tracker._aggregate_costs(auto_task_obj)
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
    if kwargs.get("background") is True:
        return _async_response_job_create(wrapped, args, kwargs)
    return _async_create_common(wrapped, args, kwargs, "openai.responses", True)


def _async_responses_parse_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``AsyncResponses.parse``."""
    return _async_create_common(wrapped, args, kwargs, "openai.responses", True)


def _async_chat_parse_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _async_create_common(wrapped, args, kwargs, "openai.chat", False)


def _async_legacy_completion_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _async_create_common(wrapped, args, kwargs, "openai.completions", False)


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
        try:
            with suppress_network_event():
                response = await wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(exc, start_time, kwargs, None, auto_task_obj)
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(
                response, latency_ms, requested=requested_model(kwargs)
            )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

        if auto_task_obj is not None and event is not None:
            try:
                finalize_auto_task(auto_task_obj, event, status="success")
                if _active_tracker is not None:
                    _active_tracker._aggregate_costs(auto_task_obj)
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
        try:
            with suppress_network_event():
                raw_stream = await wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(exc, start_time, kwargs, task, auto_task_obj)
            raise
        return _AsyncStreamWrapper(
            raw_stream,
            start_time,
            responses_stream,
            task,
            auto_task_obj,
            requested_model(kwargs),
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
        requested: str | None = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._requested = requested
        self._model: str | None = None
        self._usage: Any | None = None
        self._record_id: str | None = None
        self._completed_response: Any | None = None
        self._finalized: bool = False
        self._responses_stream = responses_stream
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._provider = _current_provider()

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
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _record_failure(self, exc: BaseException) -> None:
        """Persist a provider error raised while the stream was being consumed.

        Marks the wrapper finalized so the success path can no longer fire: a
        stream that died mid-flight has no trustworthy usage total, and
        recording one would overstate what the provider actually delivered.
        """
        if self._finalized:
            return
        self._finalized = True
        record_stream_failure(
            tracker=_active_tracker,
            exc=exc,
            start_time=self._start_time,
            provider=self._provider,
            model=self._model or self._requested,
            task=self._task,
            auto_task_obj=self._auto_task_obj,
        )

    def _process_chunk(self, chunk: Any) -> None:
        """Extract model and usage info from streaming chunks."""
        if self._responses_stream and getattr(chunk, "type", None) == "response.completed":
            response = getattr(chunk, "response", None)
            if response is not None:
                self._completed_response = response
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
                self._completed_response,
                provider=self._provider,
                requested=self._requested,
            )
            _finalize_stream_auto_task(self._auto_task_obj, event)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def _cancel(self) -> None:
        """Record an explicitly closed, non-exhausted stream as cancelled."""
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
                status="cancelled",
                provider=self._provider,
                requested=self._requested,
            )
            _finalize_stream_auto_task(self._auto_task_obj, event, succeeded=False)
        except Exception:
            _log.debug("dexcost: failed to record stream cancellation", exc_info=True)

    # Forward close/context-manager to the underlying stream
    def close(self) -> None:
        self._cancel()
        if hasattr(self._stream, "close"):
            self._stream.close()

    def __enter__(self) -> _SyncStreamWrapper:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._cancel()
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
        requested: str | None = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._requested = requested
        self._model: str | None = None
        self._usage: Any | None = None
        self._record_id: str | None = None
        self._completed_response: Any | None = None
        self._finalized: bool = False
        self._responses_stream = responses_stream
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._provider = _current_provider()

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
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _record_failure(self, exc: BaseException) -> None:
        """Persist a provider error raised while the stream was being consumed.

        Marks the wrapper finalized so the success path can no longer fire: a
        stream that died mid-flight has no trustworthy usage total, and
        recording one would overstate what the provider actually delivered.
        """
        if self._finalized:
            return
        self._finalized = True
        record_stream_failure(
            tracker=_active_tracker,
            exc=exc,
            start_time=self._start_time,
            provider=self._provider,
            model=self._model or self._requested,
            task=self._task,
            auto_task_obj=self._auto_task_obj,
        )

    def _process_chunk(self, chunk: Any) -> None:
        """Extract model and usage info from streaming chunks."""
        if self._responses_stream and getattr(chunk, "type", None) == "response.completed":
            response = getattr(chunk, "response", None)
            if response is not None:
                self._completed_response = response
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
                self._completed_response,
                provider=self._provider,
                requested=self._requested,
            )
            _finalize_stream_auto_task(self._auto_task_obj, event)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def _cancel(self) -> None:
        """Record an explicitly closed, non-exhausted stream as cancelled."""
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
                status="cancelled",
                provider=self._provider,
                requested=self._requested,
            )
            _finalize_stream_auto_task(self._auto_task_obj, event, succeeded=False)
        except Exception:
            _log.debug("dexcost: failed to record stream cancellation", exc_info=True)

    async def aclose(self) -> None:
        await self.close()

    async def close(self) -> None:
        """Cancel metering and forward both OpenAI async close conventions."""
        self._cancel()
        closer = getattr(self._stream, "aclose", None)
        if not callable(closer):
            closer = getattr(self._stream, "close", None)
        if callable(closer):
            result = closer()
            if isawaitable(result):
                await result

    async def __aenter__(self) -> _AsyncStreamWrapper:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._cancel()
        if hasattr(self._stream, "__aexit__"):
            await self._stream.__aexit__(*args)


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _response_tool_counts(response: Any) -> dict[str, int]:
    """Return terminal tool-call counts and discard all tool payload data."""
    output = _value(response, "output")
    if not isinstance(output, Sequence) or isinstance(
        output, (str, bytes, bytearray)
    ):
        return {}

    counts: dict[str, int] = {}
    container_ids: set[str] = set()
    pending_statuses = {
        "in_progress",
        "searching",
        "generating",
        "calling",
        "interpreting",
    }
    for item in output:
        item_type = _value(item, "type")
        status = _value(item, "status")
        if status in pending_statuses:
            continue
        if item_type == "web_search_call":
            counts["web_search_calls"] = counts.get("web_search_calls", 0) + 1
        elif item_type == "file_search_call":
            counts["file_search_calls"] = counts.get("file_search_calls", 0) + 1
        elif item_type == "code_interpreter_call":
            container_id = _value(item, "container_id")
            if isinstance(container_id, str) and container_id:
                container_ids.add(container_id)
        elif item_type == "shell_call":
            counts["hosted_shell_calls"] = counts.get("hosted_shell_calls", 0) + 1
            environment = _value(item, "environment")
            container_id = _value(environment, "container_id")
            if isinstance(container_id, str) and container_id:
                container_ids.add(container_id)
        elif item_type == "image_generation_call" and status == "completed":
            counts["output_image_count"] = counts.get("output_image_count", 0) + 1
        elif item_type == "computer_call":
            counts["computer_tool_calls"] = counts.get("computer_tool_calls", 0) + 1
        elif item_type == "mcp_call":
            counts["mcp_tool_calls"] = counts.get("mcp_tool_calls", 0) + 1
        elif item_type == "tool_search_call":
            counts["tool_search_calls"] = counts.get("tool_search_calls", 0) + 1
        elif item_type == "apply_patch_call":
            counts["apply_patch_calls"] = counts.get("apply_patch_calls", 0) + 1

    if container_ids:
        # A reference proves container use but does not prove a new billable
        # session: the same container may span multiple Responses calls.
        counts["container_reference_count"] = len(container_ids)
    return counts


def _record_response_tool_events(
    response: Any, task: Any, latency_ms: int
) -> None:
    """Persist provider-observed built-in tool calls without tool payloads."""
    tracker = _active_tracker
    if tracker is None:
        return
    counts = _response_tool_counts(response)

    response_model = _value(response, "model")
    model = response_model if isinstance(response_model, str) else "unknown"
    response_id = _resource_id(response)
    specs: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
        "web_search_calls": (
            "web_search",
            "openai.responses.web_search",
            model,
            _openai_candidates(model),
        ),
        "file_search_calls": (
            "file_search",
            "openai.responses.file_search",
            "openai/file-search",
            ("file-search",),
        ),
        "container_reference_count": (
            "containers",
            "openai.responses.container",
            "openai/container",
            ("container",),
        ),
        "hosted_shell_calls": (
            "hosted_shell",
            "openai.responses.hosted_shell",
            "openai/container",
            ("container",),
        ),
        "output_image_count": (
            "image_generation",
            "openai.responses.image_generation",
            "openai/image-generation-tool",
            (),
        ),
        "computer_tool_calls": (
            "computer_use",
            "openai.responses.computer_use",
            model,
            _openai_candidates(model),
        ),
        "mcp_tool_calls": (
            "mcp",
            "openai.responses.mcp",
            "openai/mcp",
            (),
        ),
        "tool_search_calls": (
            "tool_search",
            "openai.responses.tool_search",
            model,
            _openai_candidates(model),
        ),
        "apply_patch_calls": (
            "apply_patch",
            "openai.responses.apply_patch",
            model,
            _openai_candidates(model),
        ),
    }
    for metric, quantity in counts.items():
        service, operation, resource, candidates = specs[metric]
        unit = "Images" if metric == "output_image_count" else "Calls"
        try:
            record_provider_operation(
                tracker=tracker,
                task=task,
                provider=_current_provider(),
                service=service,
                operation=operation,
                component="external",
                event_type="external_cost",
                model=resource,
                measurement=OperationMeasurement(
                    pricing_usage={metric: quantity},
                    usage_lines=(ProviderUsageLine(metric, quantity, unit),),
                    provider_record_id=response_id,
                    response_model=resource,
                    model_candidates=candidates,
                ),
                latency_ms=latency_ms,
                capability=get_capability(),
                idempotency_key=get_idempotency_key(),
            )
        except Exception:
            _log.debug(
                "dexcost: failed to record OpenAI built-in tool usage",
                exc_info=True,
            )


def _provider_model(model: str, provider: str) -> str:
    prefixes = {
        "openrouter": "openrouter/",
        "perplexity": "perplexity/",
        "azure_openai": "azure/",
    }
    prefix = prefixes.get(provider)
    if prefix is None or model.startswith(prefix):
        return model
    return f"{prefix}{model}"


def _openrouter_costs(
    usage: Any, provider: str | None = None
) -> tuple[Decimal | None, Decimal | None, bool | None]:
    if (provider or _current_provider()) != "openrouter":
        return None, None, None
    cost = _decimal_quantity(_value(usage, "cost"))
    cost_details = _value(usage, "cost_details")
    upstream = _decimal_quantity(_value(cost_details, "upstream_inference_cost"))
    is_byok = _value(usage, "is_byok")
    return cost, upstream, is_byok if isinstance(is_byok, bool) else None


def _perplexity_cost(usage: Any, provider: str | None = None) -> Decimal | None:
    if (provider or _current_provider()) != "perplexity":
        return None
    return _decimal_quantity(_value(_value(usage, "cost"), "total_cost"))


def _perplexity_usage_details(usage: Any, provider: str) -> dict[str, Any]:
    """Keep Perplexity billing quantities while discarding query/result content."""
    if provider != "perplexity" or usage is None:
        return {}
    lines: list[dict[str, str]] = []

    def add(metric: str, value: Any, unit: str) -> None:
        quantity = _decimal_quantity(value)
        if quantity is not None and quantity > 0:
            lines.append({"metric": metric, "quantity": str(quantity), "unit": unit})

    add("search_query_count", _value(usage, "num_search_queries"), "Queries")
    add("citation_token_count", _value(usage, "citation_tokens"), "Tokens")
    tool_calls = _value(usage, "tool_calls_details")
    if isinstance(tool_calls, Mapping):
        for raw_name, raw_details in tool_calls.items():
            name = re.sub(r"[^a-z0-9_]+", "_", str(raw_name).lower()).strip("_")
            if name:
                add(
                    f"tool_{name}_invocation_count",
                    _value(raw_details, "invocation"),
                    "Calls",
                )
    details: dict[str, Any] = {}
    if lines:
        details["attribution_usage_lines"] = lines
    cost = _value(usage, "cost")
    if cost is not None:
        breakdown: dict[str, str] = {}
        for name in (
            "input_cost",
            "output_cost",
            "cache_creation_cost",
            "cache_read_cost",
            "tool_calls_cost",
            "input_tokens_cost",
            "output_tokens_cost",
            "reasoning_tokens_cost",
            "request_cost",
            "citation_tokens_cost",
            "search_queries_cost",
        ):
            value = _decimal_quantity(_value(cost, name))
            if value is not None:
                breakdown[name] = str(value)
        if breakdown:
            details["provider_cost_breakdown_usd"] = breakdown
    return details


def _record_from_response(
    response: Any, latency_ms: int, *, requested: str | None = None
) -> Event | None:
    """Extract fields from a Chat Completion or Responses response."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    provider = _current_provider()
    model = _provider_model(getattr(response, "model", None) or "unknown", provider)
    usage = getattr(response, "usage", None)
    record_id = getattr(response, "id", None)
    provider_cost, upstream_cost, is_byok = _openrouter_costs(usage, provider)
    if provider_cost is None:
        provider_cost = _perplexity_cost(usage, provider)

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

    event = _insert_llm_event(
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
        provider=provider,
        provider_cost_usd=provider_cost,
        provider_upstream_cost_usd=upstream_cost,
        provider_is_byok=is_byok,
        requested_model=requested,
        provider_details=_perplexity_usage_details(usage, provider),
    )
    _record_response_tool_events(response, task, latency_ms)
    return event


def _record_from_stream_usage(
    model: str | None,
    usage: Any | None,
    latency_ms: int,
    record_id: str | None = None,
    task: Any = None,
    response: Any = None,
    status: str = "succeeded",
    provider: str | None = None,
    requested: str | None = None,
) -> Event | None:
    """Record an event from accumulated stream data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    resolved_task = task or get_current_task()
    if resolved_task is None:
        return None

    resolved_provider = provider or _current_provider()
    resolved_model = _provider_model(model or "unknown", resolved_provider)
    provider_cost, upstream_cost, is_byok = _openrouter_costs(
        usage, resolved_provider
    )
    if provider_cost is None:
        provider_cost = _perplexity_cost(usage, resolved_provider)

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

    event = _insert_llm_event(
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
        provider=resolved_provider,
        provider_cost_usd=provider_cost,
        provider_upstream_cost_usd=upstream_cost,
        provider_is_byok=is_byok,
        operation_status=status,
        requested_model=requested,
        provider_details=_perplexity_usage_details(usage, resolved_provider),
    )
    if response is not None:
        _record_response_tool_events(response, resolved_task, latency_ms)
    return event


def _finalize_stream_auto_task(
    auto_task_obj: Any,
    event: Event | None,
    *,
    succeeded: bool = True,
) -> None:
    if auto_task_obj is None or event is None:
        return
    finalize_auto_task(auto_task_obj, event, status="success" if succeeded else "failed")
    if _active_tracker is not None:
        _active_tracker._aggregate_costs(auto_task_obj)
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
    operation_status: str = "succeeded",
    provider: str = "openai",
    provider_cost_usd: Decimal | None = None,
    provider_upstream_cost_usd: Decimal | None = None,
    provider_is_byok: bool | None = None,
    requested_model: str | None = None,
    provider_details: Mapping[str, Any] | None = None,
) -> Event:
    """Create and persist an llm_call Event."""
    if provider_cost_usd is not None:
        cost_usd = provider_cost_usd
        cost_confidence = "exact"
        pricing_source = "provider_response"
        pricing_version = None
    elif has_usage:
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
        cost_confidence = (
            "unknown"
            if usage_error is not None or operation_status != "succeeded"
            else "estimated"
        )
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
    if operation_status != "succeeded":
        details["attribution_operation_status"] = operation_status
    if provider_cost_usd is not None:
        details["provider_reported_cost_usd"] = str(provider_cost_usd)
    if provider_upstream_cost_usd is not None:
        details["provider_upstream_cost_usd"] = str(provider_upstream_cost_usd)
    if provider_is_byok is not None:
        details["attribution_dimensions"] = [
            {
                "key": "is_byok",
                "value": {
                    "type": "string",
                    "value": "true" if provider_is_byok else "false",
                },
            }
        ]
    dimensions = list(details.get("attribution_dimensions", []))
    if provider == "azure_openai" and isinstance(requested_model, str) and requested_model:
        dimensions.append(
            {
                "key": "azure_deployment",
                "value": {"type": "string", "value": requested_model[:256]},
            }
        )
    if provider == "perplexity":
        dimensions.append(
            {
                "key": "gateway",
                "value": {"type": "string", "value": "perplexity"},
            }
        )
    if dimensions:
        details["attribution_dimensions"] = dimensions
    if provider_details:
        details.update(provider_details)

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
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    tracker._storage.insert_event(event)
    return event


# ---------------------------------------------------------------------------
# Embeddings, images, and audio
# ---------------------------------------------------------------------------


def _value(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        if name in attributes:
            return attributes[name]
        model_extra = getattr(value, "model_extra", None)
        if isinstance(model_extra, Mapping):
            return model_extra.get(name)
        return None
    return getattr(value, name, None)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _decimal_quantity(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() and result >= 0 else None


def _openrouter_record_id(response: object) -> str | None:
    record_id = _resource_id(response)
    if record_id is not None:
        return record_id
    headers = _value(response, "headers")
    if isinstance(headers, Mapping):
        candidate = headers.get("x-generation-id") or headers.get("X-Generation-Id")
        if isinstance(candidate, str) and candidate:
            return candidate[:256]
    return None


def _augment_openrouter_measurement(
    response: object,
    measurement: OperationMeasurement,
    *,
    provider: str | None = None,
) -> OperationMeasurement:
    resolved_provider = provider or _current_provider()
    if resolved_provider != "openrouter":
        return measurement
    usage = _value(response, "usage")
    cost, upstream, is_byok = _openrouter_costs(usage, "openrouter")
    dimensions = list(measurement.billing_dimensions)
    provider_name = _value(response, "provider") or _value(response, "provider_name")
    if isinstance(provider_name, str) and provider_name:
        dimensions.append(("upstream_provider", provider_name[:256]))
    if is_byok is not None:
        dimensions.append(("is_byok", "true" if is_byok else "false"))
    response_model = measurement.response_model
    if isinstance(response_model, str):
        response_model = _provider_model(response_model, "openrouter")
    return replace(
        measurement,
        provider_record_id=measurement.provider_record_id
        or _openrouter_record_id(response),
        provider_cost_usd=cost,
        provider_upstream_cost_usd=upstream,
        response_model=response_model,
        model_candidates=(response_model,) if response_model else (),
        billing_dimensions=tuple(dimensions),
    )


def _requested_model_or_default(kwargs: dict[str, Any], default: str) -> str:
    value = kwargs.get("model")
    return value if isinstance(value, str) and value else default


def _provider_session(
    *,
    task_type: str,
    service: str,
    operation: str,
    component: str,
    model: str | None,
    event_type: str = "external_cost",
) -> ProviderOperationSession | None:
    if _active_tracker is None:
        return None
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=task_type,
        provider=_current_provider(),
        service=service,
        operation=operation,
        component=component,
        model=_provider_model(model, _current_provider()) if model else model,
        event_type=event_type,
    )


def _provider_job_session(
    *,
    task_type: str,
    service: str,
    operation: str,
    model: str,
    event_type: ProviderJobEventType,
    component: str = "external",
    billing_dimensions: tuple[tuple[str, str], ...] = (),
) -> ProviderJobSession | None:
    if _active_tracker is None:
        return None
    return ProviderJobSession(
        tracker=_active_tracker,
        task_type=task_type,
        provider=_current_provider(),
        service=service,
        operation=operation,
        component=component,
        event_type=event_type,
        resource_type="model",
        resource_id=_provider_model(model, _current_provider()),
        billing_dimensions=billing_dimensions,
    )


def _resource_id(resource: object) -> str | None:
    value = _value(resource, "id")
    return value if isinstance(value, str) and value else None


def _resource_model(resource: object, fallback: str) -> str:
    value = _value(resource, "model")
    return value if isinstance(value, str) and value else fallback


def _job_error_identity(
    resource: object, *, namespace: str, status: ProviderJobStatus
) -> tuple[str | None, str | None]:
    error = _value(resource, "error")
    code = _value(error, "code")
    if code is None and namespace == "batch":
        errors = _value(resource, "errors")
        data = _value(errors, "data")
        if isinstance(data, Sequence) and data:
            code = _value(data[0], "code")
    if status == "failed":
        raw_status = _value(resource, "status")
        suffix = raw_status if isinstance(raw_status, str) else "failed"
        return f"openai.{namespace}.{suffix}", (
            str(code) if code is not None else None
        )
    return None, None


def _openai_candidates(model: str) -> tuple[str, ...]:
    return (f"openai/{model}", model)


_REALTIME_STATE_ATTRIBUTE = "_dexcost_realtime_meter_state"
_REALTIME_MODEL_ATTRIBUTE = "_dexcost_realtime_model"


def _realtime_model(value: object) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _realtime_state(connection: object, *, model: object = None) -> dict[str, Any]:
    state = getattr(connection, _REALTIME_STATE_ATTRIBUTE, None)
    if not isinstance(state, dict):
        state = {
            "model": _realtime_model(model),
            "pending": [],
            "active": {},
            "seen": set(),
        }
        setattr(connection, _REALTIME_STATE_ATTRIBUTE, state)
    elif model is not None and state.get("model") == "unknown":
        state["model"] = _realtime_model(model)
    return state


def _sync_realtime_connect_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    manager = wrapped(*args, **kwargs)
    setattr(manager, _REALTIME_MODEL_ATTRIBUTE, _realtime_model(kwargs.get("model")))
    return manager


def _async_realtime_connect_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    # AsyncRealtime.connect itself is synchronous: it returns an async context
    # manager whose __aenter__ performs the network operation.
    manager = wrapped(*args, **kwargs)
    setattr(manager, _REALTIME_MODEL_ATTRIBUTE, _realtime_model(kwargs.get("model")))
    return manager


def _sync_realtime_enter_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    connection = wrapped(*args, **kwargs)
    _realtime_state(
        connection,
        model=getattr(instance, _REALTIME_MODEL_ATTRIBUTE, None),
    )
    return connection


async def _async_realtime_enter_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    connection = await wrapped(*args, **kwargs)
    _realtime_state(
        connection,
        model=getattr(instance, _REALTIME_MODEL_ATTRIBUTE, None),
    )
    return connection


def _new_realtime_session(connection: object) -> ProviderOperationSession | None:
    state = _realtime_state(connection)
    return _provider_session(
        task_type="openai.realtime.response",
        service="realtime",
        operation="openai.realtime.response",
        component="llm",
        model=_realtime_model(state.get("model")),
        event_type="llm_call",
    )


def _sync_realtime_response_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    connection = getattr(instance, "_connection", None)
    session = _new_realtime_session(connection) if connection is not None else None
    try:
        result = wrapped(*args, **kwargs)
    except Exception as exc:
        if session is not None:
            session.fail(exc)
        raise
    if session is not None:
        session.release_context()
        _realtime_state(connection)["pending"].append(session)
    return result


async def _async_realtime_response_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    connection = getattr(instance, "_connection", None)
    session = _new_realtime_session(connection) if connection is not None else None
    try:
        result = await wrapped(*args, **kwargs)
    except Exception as exc:
        if session is not None:
            session.fail(exc)
        raise
    if session is not None:
        session.release_context()
        _realtime_state(connection)["pending"].append(session)
    return result


def _realtime_add_usage(
    pricing: dict[str, int],
    lines: list[ProviderUsageLine],
    metric: str,
    quantity: int | None,
) -> None:
    if quantity is None:
        return
    pricing[metric] = quantity
    lines.append(ProviderUsageLine(metric, quantity, "Tokens"))


def _realtime_usage_measurement(
    response: object,
    *,
    model: str,
) -> OperationMeasurement:
    usage = _value(response, "usage")
    response_id = _resource_id(response)
    input_tokens = _count(_value(usage, "input_tokens"))
    output_tokens = _count(_value(usage, "output_tokens"))
    input_details = _value(usage, "input_token_details")
    output_details = _value(usage, "output_token_details")
    cached_tokens = _count(_value(input_details, "cached_tokens"))
    cached_details = _value(input_details, "cached_tokens_details")

    pricing: dict[str, int] = {}
    lines: list[ProviderUsageLine] = []
    input_parts = {
        "text": _count(_value(input_details, "text_tokens")),
        "audio": _count(_value(input_details, "audio_tokens")),
        "image": _count(_value(input_details, "image_tokens")),
    }
    cached_parts = {
        "text": _count(_value(cached_details, "text_tokens")),
        "audio": _count(_value(cached_details, "audio_tokens")),
        "image": _count(_value(cached_details, "image_tokens")),
    }
    input_metrics = {
        "text": ("input_tokens", "cache_read_input_tokens"),
        "audio": ("input_audio_tokens", "cache_read_input_audio_tokens"),
        "image": ("input_image_tokens", "cache_read_input_image_tokens"),
    }

    has_cached_usage = cached_tokens is not None and cached_tokens > 0
    has_complete_cached_split = not has_cached_usage or all(
        cached_parts[name] is not None for name in input_parts if input_parts[name] is not None
    )
    classified_input = 0
    for name, total in input_parts.items():
        if total is None:
            continue
        classified_input += total
        if has_complete_cached_split:
            cached = cached_parts[name] or 0
            if cached <= total:
                _realtime_add_usage(pricing, lines, input_metrics[name][0], total - cached)
                _realtime_add_usage(pricing, lines, input_metrics[name][1], cached)
                continue
        # The provider reported a modality total but not enough information to
        # separate its discounted cached subset.  Retain the meter, unpriced.
        _realtime_add_usage(
            pricing,
            lines,
            f"realtime_input_{name}_tokens_gross",
            total,
        )

    if input_tokens is not None:
        remainder = max(0, input_tokens - classified_input)
        if remainder or not input_parts or all(value is None for value in input_parts.values()):
            _realtime_add_usage(
                pricing,
                lines,
                "realtime_unclassified_input_tokens",
                remainder if classified_input else input_tokens,
            )
    if has_cached_usage and not has_complete_cached_split:
        _realtime_add_usage(
            pricing,
            lines,
            "realtime_unclassified_cached_input_tokens",
            cached_tokens,
        )

    output_parts = {
        "text": _count(_value(output_details, "text_tokens")),
        "audio": _count(_value(output_details, "audio_tokens")),
    }
    output_metrics = {"text": "output_tokens", "audio": "output_audio_tokens"}
    classified_output = 0
    for name, total in output_parts.items():
        if total is not None:
            classified_output += total
            _realtime_add_usage(pricing, lines, output_metrics[name], total)
    if output_tokens is not None:
        remainder = max(0, output_tokens - classified_output)
        if remainder or all(value is None for value in output_parts.values()):
            _realtime_add_usage(
                pricing,
                lines,
                "realtime_unclassified_output_tokens",
                remainder if classified_output else output_tokens,
            )

    if input_tokens is not None and classified_input > input_tokens:
        _realtime_add_usage(pricing, lines, "realtime_input_usage_inconsistent", 1)
    if output_tokens is not None and classified_output > output_tokens:
        _realtime_add_usage(pricing, lines, "realtime_output_usage_inconsistent", 1)

    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        provider_record_id=response_id,
        response_model=model,
        model_candidates=_openai_candidates(model),
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
        task_cached_tokens=cached_tokens,
    )


def _realtime_terminal_status(response: object) -> OperationStatus:
    status = _value(response, "status")
    if status == "completed":
        return "succeeded"
    if status == "cancelled":
        return "cancelled"
    if status == "failed":
        return "failed"
    return "unknown"


def _observe_realtime_event(connection: object, event: object) -> None:
    if _active_tracker is None:
        return
    event_type = _value(event, "type")
    if event_type not in {"response.created", "response.done"}:
        return
    response = _value(event, "response")
    response_id = _resource_id(response)
    if response_id is None:
        return
    state = _realtime_state(connection)
    active = state["active"]
    pending = state["pending"]
    seen = state["seen"]
    if event_type == "response.created":
        if response_id in active or response_id in seen:
            return
        session = pending.pop(0) if pending else _new_realtime_session(connection)
        if session is not None:
            session.release_context()
            active[response_id] = session
        return
    if response_id in seen:
        return
    session = active.pop(response_id, None)
    if session is None:
        session = pending.pop(0) if pending else _new_realtime_session(connection)
    seen.add(response_id)
    if session is None:
        return
    session.release_context()
    model = _realtime_model(state.get("model"))
    measurement = _realtime_usage_measurement(response, model=model)
    status = _realtime_terminal_status(response)
    event_record = session.finish(measurement, status)
    tool_counts = _response_tool_counts(response)
    if tool_counts:
        _record_response_tool_events(response, session.task, 0)
        if session.auto_task and event_record is not None:
            _active_tracker._aggregate_costs(session.task)
            _active_tracker._storage.insert_task(session.task)


def _fail_realtime_sessions(connection: object, exc: BaseException) -> None:
    state = _realtime_state(connection)
    sessions = list(state["pending"]) + list(state["active"].values())
    state["pending"].clear()
    state["active"].clear()
    for session in sessions:
        session.fail(exc)


def _realtime_reconnect_configured(connection: object, exc: BaseException) -> bool:
    if getattr(connection, "_on_reconnecting", None) is None or getattr(
        connection, "_make_ws", None
    ) is None:
        return False
    try:
        module = import_module("openai.resources.realtime.realtime")
        recoverable = module.is_recoverable_close
        received = getattr(exc, "rcvd", None)
        code = getattr(received, "code", 1006)
        return bool(recoverable(code))
    except Exception:
        return False


def _cancel_realtime_sessions(connection: object) -> None:
    state = _realtime_state(connection)
    sessions = list(state["pending"]) + list(state["active"].values())
    state["pending"].clear()
    state["active"].clear()
    empty = OperationMeasurement(pricing_usage={}, usage_lines=())
    for session in sessions:
        session.cancel(empty)


def _sync_realtime_recv_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    try:
        event = wrapped(*args, **kwargs)
    except Exception as exc:
        if not _realtime_reconnect_configured(instance, exc):
            _fail_realtime_sessions(instance, exc)
        raise
    _observe_realtime_event(instance, event)
    return event


async def _async_realtime_recv_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    try:
        event = await wrapped(*args, **kwargs)
    except Exception as exc:
        if not _realtime_reconnect_configured(instance, exc):
            _fail_realtime_sessions(instance, exc)
        raise
    _observe_realtime_event(instance, event)
    return event


def _sync_realtime_reconnect_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    reconnected = wrapped(*args, **kwargs)
    if not reconnected and args and isinstance(args[0], BaseException):
        _fail_realtime_sessions(instance, args[0])
    return reconnected


async def _async_realtime_reconnect_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    reconnected = await wrapped(*args, **kwargs)
    if not reconnected and args and isinstance(args[0], BaseException):
        _fail_realtime_sessions(instance, args[0])
    return reconnected


def _sync_realtime_close_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    _cancel_realtime_sessions(instance)
    return wrapped(*args, **kwargs)


async def _async_realtime_close_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    _cancel_realtime_sessions(instance)
    return await wrapped(*args, **kwargs)


def _batch_status(resource: object, *, submission: bool = False) -> ProviderJobStatus:
    status = _value(resource, "status")
    if status == "completed":
        return "succeeded"
    if status in {"failed", "expired"}:
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "submitted" if submission else "running"


def _batch_dimensions(kwargs: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    dimensions: list[tuple[str, str]] = []
    endpoint = kwargs.get("endpoint")
    if isinstance(endpoint, str) and endpoint:
        dimensions.append(("batch_endpoint", endpoint))
    window = kwargs.get("completion_window")
    if isinstance(window, str) and window:
        dimensions.append(("batch_completion_window", window))
    return tuple(sorted(dimensions))


def _batch_measurement(
    resource: object, *, fallback_model: str
) -> OperationMeasurement | None:
    usage = _value(resource, "usage")
    pricing: dict[str, int] = {}
    lines: list[ProviderUsageLine] = []
    input_tokens = _count(_value(usage, "input_tokens"))
    input_details = _value(usage, "input_tokens_details")
    cached_tokens = _count(_value(input_details, "cached_tokens")) or 0
    if input_tokens is not None:
        uncached = max(0, input_tokens - cached_tokens)
        if uncached > 0:
            pricing["batch_input_tokens"] = uncached
            lines.append(
                ProviderUsageLine("batch_input_tokens", uncached, "Tokens")
            )
        if cached_tokens > 0:
            pricing["batch_cache_read_input_tokens"] = cached_tokens
            lines.append(
                ProviderUsageLine(
                    "batch_cache_read_input_tokens", cached_tokens, "Tokens"
                )
            )

    output_tokens = _count(_value(usage, "output_tokens"))
    output_details = _value(usage, "output_tokens_details")
    reasoning_tokens = _count(_value(output_details, "reasoning_tokens"))
    if output_tokens is not None and output_tokens > 0:
        # OpenAI output_tokens already includes reasoning tokens. Price the
        # total once and retain the reasoning subset as native evidence only.
        pricing["batch_output_tokens"] = output_tokens
        lines.append(
            ProviderUsageLine("batch_output_tokens", output_tokens, "Tokens")
        )
    if reasoning_tokens is not None and reasoning_tokens > 0:
        lines.append(
            ProviderUsageLine(
                "batch_reasoning_output_tokens", reasoning_tokens, "Tokens"
            )
        )

    counts = _value(resource, "request_counts")
    for metric, field in (
        ("batch_request_count", "total"),
        ("batch_successful_request_count", "completed"),
        ("batch_failed_request_count", "failed"),
    ):
        quantity = _count(_value(counts, field))
        if quantity is not None and quantity > 0:
            lines.append(ProviderUsageLine(metric, quantity, "Requests"))

    if not lines:
        return None
    model = _resource_model(resource, fallback_model)
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        provider_record_id=_resource_id(resource),
        response_model=model,
        model_candidates=_openai_candidates(model),
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
        task_cached_tokens=cached_tokens if input_tokens is not None else None,
    )


def _response_job_status(
    resource: object, *, submission: bool = False
) -> ProviderJobStatus:
    status = _value(resource, "status")
    if status == "completed":
        return "succeeded"
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status == "incomplete":
        return "unknown"
    return "submitted" if submission else "running"


def _response_job_dimensions(
    kwargs: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    dimensions: list[tuple[str, str]] = []
    for field, key in (
        ("service_tier", "service_tier"),
        ("prompt_cache_retention", "prompt_cache_retention"),
    ):
        value = kwargs.get(field)
        if isinstance(value, str) and value:
            dimensions.append((key, value))
    return tuple(sorted(dimensions))


def _response_job_measurement(
    resource: object, *, fallback_model: str
) -> OperationMeasurement | None:
    pricing: dict[str, int] = {}
    lines: list[ProviderUsageLine] = []
    input_total: int | None = None
    output_total: int | None = None
    cached_tokens: int | None = None
    usage = _value(resource, "usage")
    if usage is not None:
        try:
            normalized = normalize_openai_usage(usage)
        except OpenAIUsageError:
            normalized = None
        if normalized is not None:
            input_total = normalized.total_input_tokens
            output_total = normalized.total_output_tokens
            cached_tokens = normalized.cache_read_input_tokens
            cache_write = normalized.cache_write_input_tokens
            reasoning = normalized.reasoning_output_tokens
            uncached = max(0, input_total - cached_tokens - cache_write)
            visible_output = max(0, output_total - reasoning)
            for metric, quantity in (
                ("input_tokens", uncached),
                ("cache_read_input_tokens", cached_tokens),
                ("cache_write_input_tokens", cache_write),
                ("output_tokens", visible_output),
                ("reasoning_output_tokens", reasoning),
            ):
                if quantity > 0:
                    pricing[metric] = quantity
                    lines.append(ProviderUsageLine(metric, quantity, "Tokens"))

    for metric, quantity in sorted(_response_tool_counts(resource).items()):
        if quantity <= 0:
            continue
        pricing[metric] = quantity
        unit = "Images" if metric == "output_image_count" else "Calls"
        lines.append(ProviderUsageLine(metric, quantity, unit))

    if not lines:
        return None
    model = _resource_model(resource, fallback_model)
    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        provider_record_id=_resource_id(resource),
        response_model=model,
        model_candidates=_openai_candidates(model),
        task_input_tokens=input_total,
        task_output_tokens=output_total,
        task_cached_tokens=cached_tokens,
    )


def _fine_tuning_status(
    resource: object, *, submission: bool = False
) -> ProviderJobStatus:
    status = _value(resource, "status")
    if status == "succeeded":
        return "succeeded"
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "submitted" if submission else "running"


def _fine_tuning_dimensions(
    kwargs: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    dimensions: list[tuple[str, str]] = []
    method = _value(kwargs.get("method"), "type")
    if isinstance(method, str) and method:
        dimensions.append(("fine_tuning_method", method))
    hyperparameters = kwargs.get("hyperparameters")
    epochs = _count(_value(hyperparameters, "n_epochs"))
    if epochs is not None and epochs > 0:
        dimensions.append(("requested_epoch_count", str(epochs)))
    return tuple(sorted(dimensions))


def _fine_tuning_measurement(
    resource: object, *, fallback_model: str
) -> OperationMeasurement | None:
    trained_tokens = _count(_value(resource, "trained_tokens"))
    if trained_tokens is None or trained_tokens <= 0:
        return None
    model = _resource_model(resource, fallback_model)
    return OperationMeasurement(
        # The job object reports exact billable training tokens but no charge.
        # Training stays unpriced until the authoritative catalog supplies the
        # matching training SKU/rate.
        pricing_usage={},
        usage_lines=(
            ProviderUsageLine(
                "training_billable_tokens", trained_tokens, "Tokens"
            ),
        ),
        provider_record_id=_resource_id(resource),
        response_model=model,
        model_candidates=_openai_candidates(model),
    )


def _video_status(resource: object, *, submission: bool = False) -> ProviderJobStatus:
    status = _value(resource, "status")
    if status == "completed":
        return "succeeded"
    if status == "failed":
        return "failed"
    return "submitted" if submission else "running"


def _video_dimensions(kwargs: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    dimensions: list[tuple[str, str]] = []
    seconds = _decimal_quantity(kwargs.get("seconds"))
    if seconds is not None and seconds > 0:
        dimensions.append(("requested_video_seconds", str(seconds.normalize())))
    size = kwargs.get("size")
    if isinstance(size, str) and size:
        dimensions.append(("video_size", size.lower()))
    return tuple(sorted(dimensions))


def _video_candidates(model: str, size: object) -> tuple[str, ...]:
    candidates: list[str] = []
    if model.startswith("sora-2-pro") and size in {"1024x1792", "1792x1024"}:
        candidates.extend(
            (f"openai/{model}-high-res", f"{model}-high-res")
        )
    candidates.extend(_openai_candidates(model))
    return tuple(candidates)


def _video_measurement(
    resource: object, *, fallback_model: str
) -> OperationMeasurement | None:
    seconds = _decimal_quantity(_value(resource, "seconds"))
    if seconds is None or seconds <= 0:
        return None
    model = _resource_model(resource, fallback_model)
    size = _value(resource, "size")
    return OperationMeasurement(
        pricing_usage={
            "output_video_count": 1,
            "output_video_seconds": seconds,
        },
        usage_lines=(
            ProviderUsageLine("output_video_count", 1, "Videos"),
            ProviderUsageLine("output_video_seconds", seconds, "Seconds"),
        ),
        provider_record_id=_resource_id(resource),
        response_model=model,
        model_candidates=_video_candidates(model, size),
    )


def _terminal_status_with_usage(
    status: ProviderJobStatus, measurement: OperationMeasurement | None
) -> ProviderJobStatus:
    if status == "succeeded" and (
        measurement is None or not measurement.usage_lines
    ):
        return "unknown"
    return status


def _sync_job_submission_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    session: ProviderJobSession | None,
    submit: Any,
) -> Any:
    if session is None:
        return wrapped(*args, **kwargs)
    try:
        try:
            with suppress_network_event():
                resource = wrapped(*args, **kwargs)
        except Exception as exc:
            session.fail(exc)
            raise
        try:
            submit(session, resource)
        except Exception:
            _log.debug("dexcost: failed to persist OpenAI provider job", exc_info=True)
        return resource
    finally:
        session.release_context()


def _async_job_submission_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    session: ProviderJobSession | None,
    submit: Any,
) -> Any:
    async def invoke() -> Any:
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    resource = await wrapped(*args, **kwargs)
            except Exception as exc:
                session.fail(exc)
                raise
            try:
                submit(session, resource)
            except Exception:
                _log.debug(
                    "dexcost: failed to persist async OpenAI provider job",
                    exc_info=True,
                )
            return resource
        finally:
            session.release_context()

    return invoke()


def _submit_batch(session: ProviderJobSession, resource: object) -> None:
    record_id = _resource_id(resource)
    if record_id is None:
        return
    raw_status = _batch_status(resource, submission=True)
    measurement = (
        _batch_measurement(resource, fallback_model=session.resource_id)
        if raw_status not in {"submitted", "running"}
        else None
    )
    if measurement is not None:
        measurement = _augment_openrouter_measurement(resource, measurement)
    status = _terminal_status_with_usage(raw_status, measurement)
    error_type, error_code = _job_error_identity(
        resource, namespace="batch", status=status
    )
    session.submit(
        record_id,
        status=status,
        measurement=measurement,
        error_type=error_type,
        error_code=error_code,
    )


def _submit_response_job(session: ProviderJobSession, resource: object) -> None:
    record_id = _resource_id(resource)
    if record_id is None:
        return
    raw_status = _response_job_status(resource, submission=True)
    measurement = (
        _response_job_measurement(resource, fallback_model=session.resource_id)
        if raw_status not in {"submitted", "running"}
        else None
    )
    if measurement is not None:
        measurement = _augment_openrouter_measurement(resource, measurement)
    status = _terminal_status_with_usage(raw_status, measurement)
    error_type, error_code = _job_error_identity(
        resource, namespace="response", status=status
    )
    session.submit(
        record_id,
        status=status,
        measurement=measurement,
        error_type=error_type,
        error_code=error_code,
    )


def _submit_fine_tuning(session: ProviderJobSession, resource: object) -> None:
    record_id = _resource_id(resource)
    if record_id is None:
        return
    raw_status = _fine_tuning_status(resource, submission=True)
    measurement = (
        _fine_tuning_measurement(resource, fallback_model=session.resource_id)
        if raw_status not in {"submitted", "running"}
        else None
    )
    if measurement is not None:
        measurement = _augment_openrouter_measurement(resource, measurement)
    status = _terminal_status_with_usage(raw_status, measurement)
    error_type, error_code = _job_error_identity(
        resource, namespace="fine_tuning", status=status
    )
    session.submit(
        record_id,
        status=status,
        measurement=measurement,
        error_type=error_type,
        error_code=error_code,
    )


def _submit_video(session: ProviderJobSession, resource: object) -> None:
    record_id = _resource_id(resource)
    if record_id is None:
        return
    raw_status = _video_status(resource, submission=True)
    measurement = (
        _video_measurement(resource, fallback_model=session.resource_id)
        if raw_status not in {"submitted", "running"}
        else None
    )
    if measurement is not None:
        measurement = _augment_openrouter_measurement(resource, measurement)
    status = _terminal_status_with_usage(raw_status, measurement)
    error_type, error_code = _job_error_identity(
        resource, namespace="video", status=status
    )
    session.submit(
        record_id,
        status=status,
        measurement=measurement,
        error_type=error_type,
        error_code=error_code,
    )


def _reconcile_openai_job(
    resource: object, *, service: str, kind: str
) -> None:
    if _active_tracker is None:
        return
    record_id = _resource_id(resource)
    if record_id is None:
        return
    provider = _current_provider()
    previous = _active_tracker._storage.get_provider_job(provider, service, record_id)
    if previous is None:
        return
    if kind == "response":
        raw_status = _response_job_status(resource)
        measurement = (
            _response_job_measurement(
                resource, fallback_model=previous.resource_id
            )
            if raw_status not in {"submitted", "running"}
            else None
        )
    elif kind == "batch":
        raw_status = _batch_status(resource)
        measurement = (
            _batch_measurement(resource, fallback_model=previous.resource_id)
            if raw_status not in {"submitted", "running"}
            else None
        )
    elif kind == "fine_tuning":
        raw_status = _fine_tuning_status(resource)
        measurement = (
            _fine_tuning_measurement(
                resource, fallback_model=previous.resource_id
            )
            if raw_status not in {"submitted", "running"}
            else None
        )
    else:
        raw_status = _video_status(resource)
        measurement = (
            _video_measurement(resource, fallback_model=previous.resource_id)
            if raw_status not in {"submitted", "running"}
            else None
        )
    if measurement is not None:
        measurement = _augment_openrouter_measurement(resource, measurement)
    status = _terminal_status_with_usage(raw_status, measurement)
    error_type, error_code = _job_error_identity(
        resource, namespace=kind, status=status
    )
    try:
        reconcile_provider_job(
            tracker=_active_tracker,
            provider=provider,
            service=service,
            provider_record_id=record_id,
            status=status,
            measurement=measurement,
            error_type=error_type,
            error_code=error_code,
        )
    except Exception:
        _log.debug("dexcost: failed to reconcile OpenAI provider job", exc_info=True)


def _sync_batch_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    endpoint = kwargs.get("endpoint")
    resource = f"batch:{endpoint}" if isinstance(endpoint, str) else "batch:unknown"
    session = _provider_job_session(
        task_type="openai.batches.create",
        service="batches",
        operation="openai.batches.create",
        model=resource,
        event_type="llm_call",
        billing_dimensions=_batch_dimensions(kwargs),
    )
    return _sync_job_submission_call(
        wrapped, args, kwargs, session, _submit_batch
    )


def _sync_response_job_create(
    wrapped: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    model = _requested_model_or_default(kwargs, "unknown")
    session = _provider_job_session(
        task_type="openai.responses.create.background",
        service="responses",
        operation="openai.responses.create",
        model=model,
        event_type="llm_call",
        component="llm",
        billing_dimensions=_response_job_dimensions(kwargs),
    )
    return _sync_job_submission_call(
        wrapped, args, kwargs, session, _submit_response_job
    )


def _async_response_job_create(
    wrapped: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    model = _requested_model_or_default(kwargs, "unknown")
    session = _provider_job_session(
        task_type="openai.responses.create.background",
        service="responses",
        operation="openai.responses.create",
        model=model,
        event_type="llm_call",
        component="llm",
        billing_dimensions=_response_job_dimensions(kwargs),
    )
    return _async_job_submission_call(
        wrapped, args, kwargs, session, _submit_response_job
    )


def _response_job_id_from_call(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str | None:
    value = kwargs.get("response_id")
    if not isinstance(value, str) and args:
        value = args[0]
    return value if isinstance(value, str) and value else None


class _ResponseJobPollMeter:
    def __init__(self) -> None:
        self.response: Any | None = None

    def observe(self, event: Any) -> None:
        event_type = _value(event, "type")
        if event_type in {
            "response.completed",
            "response.failed",
            "response.cancelled",
            "response.incomplete",
        }:
            response = _value(event, "response")
            if response is not None:
                self.response = response

    def complete(self) -> None:
        if self.response is not None:
            _reconcile_openai_job(
                self.response, service="responses", kind="response"
            )


def _sync_response_job_reconcile_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    record_id = _response_job_id_from_call(args, kwargs)
    known = (
        _active_tracker is not None
        and record_id is not None
        and _active_tracker._storage.get_provider_job(
            "openai", "responses", record_id
        )
        is not None
    )
    with suppress_network_event():
        resource = wrapped(*args, **kwargs)
    if kwargs.get("stream") is True and known:
        meter = _ResponseJobPollMeter()
        return SyncProviderJobStream(
            resource,
            observe=meter.observe,
            complete=meter.complete,
        )
    _reconcile_openai_job(resource, service="responses", kind="response")
    return resource


def _async_response_job_reconcile_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    async def invoke() -> Any:
        record_id = _response_job_id_from_call(args, kwargs)
        known = (
            _active_tracker is not None
            and record_id is not None
            and _active_tracker._storage.get_provider_job(
                "openai", "responses", record_id
            )
            is not None
        )
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        if kwargs.get("stream") is True and known:
            meter = _ResponseJobPollMeter()
            return AsyncProviderJobStream(
                resource,
                observe=meter.observe,
                complete=meter.complete,
            )
        _reconcile_openai_job(resource, service="responses", kind="response")
        return resource

    return invoke()


def _async_batch_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    endpoint = kwargs.get("endpoint")
    resource = f"batch:{endpoint}" if isinstance(endpoint, str) else "batch:unknown"
    session = _provider_job_session(
        task_type="openai.batches.create",
        service="batches",
        operation="openai.batches.create",
        model=resource,
        event_type="llm_call",
        billing_dimensions=_batch_dimensions(kwargs),
    )
    return _async_job_submission_call(
        wrapped, args, kwargs, session, _submit_batch
    )


def _sync_batch_reconcile_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    with suppress_network_event():
        resource = wrapped(*args, **kwargs)
    _reconcile_openai_job(resource, service="batches", kind="batch")
    return resource


def _async_batch_reconcile_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        _reconcile_openai_job(resource, service="batches", kind="batch")
        return resource

    return invoke()


def _sync_fine_tuning_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    model = _requested_model_or_default(kwargs, "fine-tuning-unknown")
    session = _provider_job_session(
        task_type="openai.fine_tuning.jobs.create",
        service="fine_tuning",
        operation="openai.fine_tuning.jobs.create",
        model=model,
        event_type="external_cost",
        billing_dimensions=_fine_tuning_dimensions(kwargs),
    )
    return _sync_job_submission_call(
        wrapped, args, kwargs, session, _submit_fine_tuning
    )


def _async_fine_tuning_create_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    model = _requested_model_or_default(kwargs, "fine-tuning-unknown")
    session = _provider_job_session(
        task_type="openai.fine_tuning.jobs.create",
        service="fine_tuning",
        operation="openai.fine_tuning.jobs.create",
        model=model,
        event_type="external_cost",
        billing_dimensions=_fine_tuning_dimensions(kwargs),
    )
    return _async_job_submission_call(
        wrapped, args, kwargs, session, _submit_fine_tuning
    )


def _sync_fine_tuning_reconcile_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    with suppress_network_event():
        resource = wrapped(*args, **kwargs)
    _reconcile_openai_job(
        resource, service="fine_tuning", kind="fine_tuning"
    )
    return resource


def _async_fine_tuning_reconcile_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        _reconcile_openai_job(
            resource, service="fine_tuning", kind="fine_tuning"
        )
        return resource

    return invoke()


def _sync_video_submit_wrapper(method_name: str) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        model = _requested_model_or_default(kwargs, "sora-unknown")
        session = _provider_job_session(
            task_type=f"openai.videos.{method_name}",
            service="videos",
            operation=f"openai.videos.{method_name}",
            model=model,
            event_type="external_cost",
            billing_dimensions=_video_dimensions(kwargs),
        )
        return _sync_job_submission_call(
            wrapped, args, kwargs, session, _submit_video
        )

    return wrapper


def _async_video_submit_wrapper(method_name: str) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        model = _requested_model_or_default(kwargs, "sora-unknown")
        session = _provider_job_session(
            task_type=f"openai.videos.{method_name}",
            service="videos",
            operation=f"openai.videos.{method_name}",
            model=model,
            event_type="external_cost",
            billing_dimensions=_video_dimensions(kwargs),
        )
        return _async_job_submission_call(
            wrapped, args, kwargs, session, _submit_video
        )

    return wrapper


def _sync_video_reconcile_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    with suppress_network_event():
        resource = wrapped(*args, **kwargs)
    _reconcile_openai_job(resource, service="videos", kind="video")
    return resource


def _async_video_reconcile_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    async def invoke() -> Any:
        with suppress_network_event():
            resource = await wrapped(*args, **kwargs)
        _reconcile_openai_job(resource, service="videos", kind="video")
        return resource

    return invoke()


def _unknown_measurement(model: str | None = None) -> OperationMeasurement:
    return OperationMeasurement(
        pricing_usage={},
        usage_lines=(),
        response_model=model,
    )


def _sync_provider_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    task_type: str,
    service: str,
    operation: str,
    component: str,
    model: str | None,
    extract: Any,
    stream_factory: Any | None = None,
) -> Any:
    session = _provider_session(
        task_type=task_type,
        service=service,
        operation=operation,
        component=component,
        model=model,
    )
    if session is None:
        return wrapped(*args, **kwargs)
    try:
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except Exception as exc:
            session.fail(exc)
            raise
        if stream_factory is not None and kwargs.get("stream") is True:
            session.release_context()
            return stream_factory(response, session)
        try:
            measurement = extract(response, kwargs)
            session.succeed(_augment_openrouter_measurement(response, measurement))
        except Exception:
            _log.debug("dexcost: failed to extract OpenAI provider usage", exc_info=True)
            session.succeed(_unknown_measurement(model))
        return response
    finally:
        session.release_context()


def _async_provider_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    task_type: str,
    service: str,
    operation: str,
    component: str,
    model: str | None,
    extract: Any,
    stream_factory: Any | None = None,
) -> Any:
    async def invoke() -> Any:
        session = _provider_session(
            task_type=task_type,
            service=service,
            operation=operation,
            component=component,
            model=model,
        )
        if session is None:
            return await wrapped(*args, **kwargs)
        try:
            try:
                with suppress_network_event():
                    response = await wrapped(*args, **kwargs)
            except Exception as exc:
                session.fail(exc)
                raise
            if stream_factory is not None and kwargs.get("stream") is True:
                session.release_context()
                return stream_factory(response, session)
            try:
                measurement = extract(response, kwargs)
                session.succeed(_augment_openrouter_measurement(response, measurement))
            except Exception:
                _log.debug("dexcost: failed to extract OpenAI provider usage", exc_info=True)
                session.succeed(_unknown_measurement(model))
            return response
        finally:
            session.release_context()

    return invoke()


def _embedding_measurement(response: Any, kwargs: dict[str, Any]) -> OperationMeasurement:
    usage = _value(response, "usage")
    input_tokens = _count(_value(usage, "prompt_tokens"))
    if input_tokens is None:
        input_tokens = _count(_value(usage, "total_tokens"))
    lines = (
        ()
        if input_tokens is None
        else (ProviderUsageLine("input_tokens", input_tokens, "Tokens"),)
    )
    return OperationMeasurement(
        pricing_usage={} if input_tokens is None else {"input_tokens": input_tokens},
        usage_lines=lines,
        response_model=(
            _value(response, "model")
            if isinstance(_value(response, "model"), str)
            else requested_model(kwargs)
        ),
    )


def _sync_embeddings_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    model = requested_model(kwargs)
    return _sync_provider_call(
        wrapped,
        args,
        kwargs,
        task_type="openai.embeddings.create",
        service="embeddings",
        operation="openai.embeddings.create",
        component="external",
        model=model,
        extract=_embedding_measurement,
    )


def _async_embeddings_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    model = requested_model(kwargs)
    return _async_provider_call(
        wrapped,
        args,
        kwargs,
        task_type="openai.embeddings.create",
        service="embeddings",
        operation="openai.embeddings.create",
        component="external",
        model=model,
        extract=_embedding_measurement,
    )


def _image_model(kwargs: dict[str, Any], operation: str) -> str:
    defaults = {
        "generate": "dall-e-2",
        "edit": "gpt-image-1.5",
        "create_variation": "dall-e-2",
    }
    return _requested_model_or_default(kwargs, defaults[operation])


def _image_candidates(model: str, quality: object, size: object) -> tuple[str, ...]:
    if not model.startswith("dall-e-"):
        return ()
    resolved_quality = quality if isinstance(quality, str) else "standard"
    resolved_size = size if isinstance(size, str) else "1024x1024"
    size_key = resolved_size.replace("x", "-x-")
    return (
        f"{resolved_quality}/{size_key}/{model}",
        f"{size_key}/{model}",
    )


def _image_measurement_values(
    *,
    usage: Any,
    model: str,
    image_count: int,
    quality: object,
    size: object,
) -> OperationMeasurement:
    pricing_usage: dict[str, int] = {}
    lines: list[ProviderUsageLine] = []
    total_input = _count(_value(usage, "input_tokens"))
    input_details = _value(usage, "input_tokens_details")
    text_input = _count(_value(input_details, "text_tokens"))
    image_input = _count(_value(input_details, "image_tokens"))
    if text_input is not None:
        pricing_usage["input_tokens"] = text_input
        lines.append(ProviderUsageLine("input_tokens", text_input, "Tokens"))
    if image_input is not None:
        pricing_usage["input_image_tokens"] = image_input
        lines.append(ProviderUsageLine("input_image_tokens", image_input, "Tokens"))
    if total_input is not None:
        allocated = (text_input or 0) + (image_input or 0)
        if input_details is None:
            pricing_usage["input_tokens"] = total_input
            lines.append(ProviderUsageLine("input_tokens", total_input, "Tokens"))
        elif total_input > allocated:
            unallocated = total_input - allocated
            pricing_usage["unallocated_input_tokens"] = unallocated
            lines.append(
                ProviderUsageLine("unallocated_input_tokens", unallocated, "Tokens")
            )

    total_output = _count(_value(usage, "output_tokens"))
    output_details = _value(usage, "output_tokens_details")
    text_output = _count(_value(output_details, "text_tokens"))
    image_output = _count(_value(output_details, "image_tokens"))
    if text_output is not None:
        pricing_usage["output_tokens"] = text_output
        lines.append(ProviderUsageLine("output_tokens", text_output, "Tokens"))
    if image_output is not None:
        pricing_usage["output_image_tokens"] = image_output
        lines.append(ProviderUsageLine("output_image_tokens", image_output, "Tokens"))
    if total_output is not None:
        allocated = (text_output or 0) + (image_output or 0)
        if output_details is None:
            pricing_usage["output_image_tokens"] = total_output
            lines.append(ProviderUsageLine("output_image_tokens", total_output, "Tokens"))
        elif total_output > allocated:
            unallocated = total_output - allocated
            pricing_usage["unallocated_output_tokens"] = unallocated
            lines.append(
                ProviderUsageLine("unallocated_output_tokens", unallocated, "Tokens")
            )

    if image_count > 0:
        lines.append(ProviderUsageLine("image_count", image_count, "Images"))
    if usage is None and image_count > 0:
        pixel_match = (
            re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", size)
            if isinstance(size, str)
            else None
        )
        if model.startswith("dall-e-") and pixel_match is not None:
            pricing_usage["input_pixels"] = (
                int(pixel_match.group(1)) * int(pixel_match.group(2)) * image_count
            )
        else:
            pricing_usage["image_count"] = image_count
    return OperationMeasurement(
        pricing_usage=pricing_usage,
        usage_lines=tuple(lines),
        response_model=model,
        model_candidates=_image_candidates(model, quality, size),
    )


def _image_measurement(
    response: Any, kwargs: dict[str, Any], operation: str
) -> OperationMeasurement:
    model = _image_model(kwargs, operation)
    data = _value(response, "data")
    count = len(data) if isinstance(data, list) else 0
    if count == 0:
        count = _count(kwargs.get("n")) or 1
    return _image_measurement_values(
        usage=_value(response, "usage"),
        model=model,
        image_count=count,
        quality=_value(response, "quality") or kwargs.get("quality"),
        size=_value(response, "size") or kwargs.get("size"),
    )


class _ImageStreamMeter:
    def __init__(self, kwargs: dict[str, Any], operation: str) -> None:
        self.kwargs = kwargs
        self.operation = operation
        self.usage: Any = None
        self.count = 0
        self.quality: Any = kwargs.get("quality")
        self.size: Any = kwargs.get("size")
        self.final: Any = None
        self.provider = _current_provider()

    def observe(self, chunk: Any) -> None:
        if _value(chunk, "usage") is not None:
            self.usage = _value(chunk, "usage")
            self.final = chunk
        chunk_type = _value(chunk, "type")
        if isinstance(chunk_type, str) and chunk_type.endswith(".completed"):
            self.count += 1
        self.quality = _value(chunk, "quality") or self.quality
        self.size = _value(chunk, "size") or self.size

    def measurement(self) -> OperationMeasurement:
        measurement = _image_measurement_values(
            usage=self.usage,
            model=_image_model(self.kwargs, self.operation),
            image_count=self.count,
            quality=self.quality,
            size=self.size,
        )
        response = self.final or {
            "usage": self.usage,
            "model": measurement.response_model,
        }
        return _augment_openrouter_measurement(
            response,
            measurement,
            provider=self.provider,
        )


def _sync_image_stream(
    stream: Any, session: ProviderOperationSession, kwargs: dict[str, Any], operation: str
) -> SyncProviderStream:
    meter = _ImageStreamMeter(kwargs, operation)
    return SyncProviderStream(
        stream,
        session,
        observe=meter.observe,
        measurement=meter.measurement,
    )


def _async_image_stream(
    stream: Any, session: ProviderOperationSession, kwargs: dict[str, Any], operation: str
) -> AsyncProviderStream:
    meter = _ImageStreamMeter(kwargs, operation)
    return AsyncProviderStream(
        stream,
        session,
        observe=meter.observe,
        measurement=meter.measurement,
    )


def _sync_image_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    operation: str,
) -> Any:
    model = _image_model(kwargs, operation)
    return _sync_provider_call(
        wrapped,
        args,
        kwargs,
        task_type=f"openai.images.{operation}",
        service="images",
        operation=f"openai.images.{operation}",
        component="external",
        model=model,
        extract=lambda response, call_kwargs: _image_measurement(
            response, call_kwargs, operation
        ),
        stream_factory=(
            None
            if operation == "create_variation"
            else lambda stream, session: _sync_image_stream(
                stream, session, kwargs, operation
            )
        ),
    )


def _async_image_call(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    operation: str,
) -> Any:
    model = _image_model(kwargs, operation)
    return _async_provider_call(
        wrapped,
        args,
        kwargs,
        task_type=f"openai.images.{operation}",
        service="images",
        operation=f"openai.images.{operation}",
        component="external",
        model=model,
        extract=lambda response, call_kwargs: _image_measurement(
            response, call_kwargs, operation
        ),
        stream_factory=(
            None
            if operation == "create_variation"
            else lambda stream, session: _async_image_stream(
                stream, session, kwargs, operation
            )
        ),
    )


def _sync_image_generate_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _sync_image_call(wrapped, args, kwargs, "generate")


def _async_image_generate_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _async_image_call(wrapped, args, kwargs, "generate")


def _sync_image_edit_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _sync_image_call(wrapped, args, kwargs, "edit")


def _async_image_edit_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _async_image_call(wrapped, args, kwargs, "edit")


def _sync_image_variation_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _sync_image_call(wrapped, args, kwargs, "create_variation")


def _async_image_variation_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _async_image_call(wrapped, args, kwargs, "create_variation")


def _audio_measurement(response: Any, kwargs: dict[str, Any]) -> OperationMeasurement:
    usage = _value(response, "usage")
    pricing_usage: dict[str, Decimal | int] = {}
    lines: list[ProviderUsageLine] = []
    usage_type = _value(usage, "type")
    seconds = _decimal_quantity(_value(usage, "seconds"))
    if seconds is None:
        seconds = _decimal_quantity(_value(response, "duration"))
    if usage_type == "duration" or (usage is None and seconds is not None):
        if seconds is not None:
            pricing_usage["input_audio_seconds"] = seconds
            lines.append(ProviderUsageLine("audio_seconds", seconds, "Seconds"))
    elif usage is not None:
        total_input = _count(_value(usage, "input_tokens"))
        details = _value(usage, "input_token_details")
        audio_tokens = _count(_value(details, "audio_tokens"))
        text_tokens = _count(_value(details, "text_tokens"))
        if audio_tokens is not None:
            pricing_usage["input_audio_tokens"] = audio_tokens
            lines.append(ProviderUsageLine("input_audio_tokens", audio_tokens, "Tokens"))
        if text_tokens is not None:
            pricing_usage["input_tokens"] = text_tokens
            lines.append(ProviderUsageLine("input_tokens", text_tokens, "Tokens"))
        if total_input is not None:
            allocated = (audio_tokens or 0) + (text_tokens or 0)
            if details is None or total_input > allocated:
                unallocated = total_input if details is None else total_input - allocated
                pricing_usage["unallocated_input_tokens"] = unallocated
                lines.append(
                    ProviderUsageLine("unallocated_input_tokens", unallocated, "Tokens")
                )
        output_tokens = _count(_value(usage, "output_tokens"))
        if output_tokens is not None:
            pricing_usage["output_tokens"] = output_tokens
            lines.append(ProviderUsageLine("output_tokens", output_tokens, "Tokens"))
    return OperationMeasurement(
        pricing_usage=pricing_usage,
        usage_lines=tuple(lines),
        response_model=requested_model(kwargs),
    )


class _AudioStreamMeter:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.final: Any = None
        self.max_end = Decimal(0)
        self.provider = _current_provider()

    def observe(self, chunk: Any) -> None:
        if _value(chunk, "usage") is not None:
            self.final = chunk
        end = _decimal_quantity(_value(chunk, "end"))
        if end is not None:
            self.max_end = max(self.max_end, end)

    def measurement(self) -> OperationMeasurement:
        if self.final is not None:
            return _augment_openrouter_measurement(
                self.final,
                _audio_measurement(self.final, self.kwargs),
                provider=self.provider,
            )
        if self.max_end > 0:
            measurement = OperationMeasurement(
                pricing_usage={"input_audio_seconds": self.max_end},
                usage_lines=(ProviderUsageLine("audio_seconds", self.max_end, "Seconds"),),
                response_model=requested_model(self.kwargs),
            )
            return _augment_openrouter_measurement(
                {"model": measurement.response_model},
                measurement,
                provider=self.provider,
            )
        return _augment_openrouter_measurement(
            {"model": requested_model(self.kwargs)},
            _unknown_measurement(requested_model(self.kwargs)),
            provider=self.provider,
        )


def _sync_audio_stream(
    stream: Any, session: ProviderOperationSession, kwargs: dict[str, Any]
) -> SyncProviderStream:
    meter = _AudioStreamMeter(kwargs)
    return SyncProviderStream(
        stream,
        session,
        observe=meter.observe,
        measurement=meter.measurement,
    )


def _async_audio_stream(
    stream: Any, session: ProviderOperationSession, kwargs: dict[str, Any]
) -> AsyncProviderStream:
    meter = _AudioStreamMeter(kwargs)
    return AsyncProviderStream(
        stream,
        session,
        observe=meter.observe,
        measurement=meter.measurement,
    )


def _sync_audio_transcription_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _sync_provider_call(
        wrapped,
        args,
        kwargs,
        task_type="openai.audio.transcriptions.create",
        service="speech_to_text",
        operation="openai.audio.transcriptions.create",
        component="speech_to_text",
        model=_provider_model(requested_model(kwargs) or "unknown", _current_provider()),
        extract=_audio_measurement,
        stream_factory=lambda stream, session: _sync_audio_stream(stream, session, kwargs),
    )


def _async_audio_transcription_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _async_provider_call(
        wrapped,
        args,
        kwargs,
        task_type="openai.audio.transcriptions.create",
        service="speech_to_text",
        operation="openai.audio.transcriptions.create",
        component="speech_to_text",
        model=requested_model(kwargs),
        extract=_audio_measurement,
        stream_factory=lambda stream, session: _async_audio_stream(stream, session, kwargs),
    )


def _sync_audio_translation_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _sync_provider_call(
        wrapped,
        args,
        kwargs,
        task_type="openai.audio.translations.create",
        service="speech_to_text",
        operation="openai.audio.translations.create",
        component="speech_to_text",
        model=requested_model(kwargs),
        extract=_audio_measurement,
    )


def _async_audio_translation_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _async_provider_call(
        wrapped,
        args,
        kwargs,
        task_type="openai.audio.translations.create",
        service="speech_to_text",
        operation="openai.audio.translations.create",
        component="speech_to_text",
        model=requested_model(kwargs),
        extract=_audio_measurement,
    )


def _speech_measurement(response: Any, kwargs: dict[str, Any]) -> OperationMeasurement:
    input_text = kwargs.get("input")
    characters = len(input_text) if isinstance(input_text, str) else 0
    return OperationMeasurement(
        pricing_usage={} if characters == 0 else {"characters": characters},
        usage_lines=(
            ()
            if characters == 0
            else (ProviderUsageLine("characters", characters, "Characters"),)
        ),
        response_model=requested_model(kwargs),
    )


def _sync_audio_speech_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _sync_provider_call(
        wrapped,
        args,
        kwargs,
        task_type="openai.audio.speech.create",
        service="text_to_speech",
        operation="openai.audio.speech.create",
        component="text_to_speech",
        model=requested_model(kwargs),
        extract=_speech_measurement,
    )


def _async_audio_speech_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    return _async_provider_call(
        wrapped,
        args,
        kwargs,
        task_type="openai.audio.speech.create",
        service="text_to_speech",
        operation="openai.audio.speech.create",
        component="text_to_speech",
        model=requested_model(kwargs),
        extract=_speech_measurement,
    )
