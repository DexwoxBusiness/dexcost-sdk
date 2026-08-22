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
from collections.abc import Iterator, Mapping
from contextlib import suppress
from decimal import Decimal
from typing import Any

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.capabilities import apply_event_capability, get_capability
from dexcost.context import (
    _current_task,
    get_current_task,
    set_current_task,
    suppress_network_event,
)
from dexcost.idempotency import apply_event_idempotency, get_idempotency_key
from dexcost.instruments._errors import (
    finalize_failed_auto_task,
    record_call_failure,
    record_stream_failure,
    requested_model,
)
from dexcost.instruments.openai_usage import OpenAIUsageError, normalize_openai_usage
from dexcost.models._serde import canonical_decimal
from dexcost.models.event import Event

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}
_patched_owner: Any | None = None


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
    global _active_tracker, _patched, _patched_owner

    # Verify litellm is importable
    try:
        import litellm as _mod  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "The 'litellm' package is required for LiteLLM auto-instrumentation. "
            "Install it with: pip install litellm"
        ) from exc

    import litellm

    if _patched:
        if _patched_owner is litellm:
            raise RuntimeError(
                "LiteLLM instrumentation is already active. "
                "Call uninstrument_litellm() before re-instrumenting."
            )
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None

    _active_tracker = tracker
    _patched_owner = litellm

    # Store originals for uninstrument

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
    global _active_tracker, _patched, _patched_owner

    if not _patched:
        return

    try:
        import litellm
    except ImportError:
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None
        return

    if _patched_owner is not litellm:
        _originals.clear()
        _active_tracker = None
        _patched = False
        _patched_owner = None
        return

    if "completion" in _originals:
        litellm.completion = _originals["completion"]
    if "acompletion" in _originals:
        litellm.acompletion = _originals["acompletion"]

    _originals.clear()
    _active_tracker = None
    _patched = False
    _patched_owner = None


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _record_call_failure(
    exc: BaseException,
    start_time: float,
    kwargs: dict[str, Any],
    auto_task_obj: Any = None,
) -> Event | None:
    """Record a raised LiteLLM call as a failed operation. Never raises.

    No response exists, so the provider is resolved from the requested model
    string prefix alone (``"openai/gpt-4o"`` -> ``"openai"``).
    """
    try:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
    except Exception:  # pragma: no cover - defensive
        latency_ms = None
    model = requested_model(kwargs)
    try:
        provider = _resolve_provider(request_model=model)
    except Exception:  # pragma: no cover - defensive
        provider = "unknown"
    model = _canonical_model(model, provider, model)
    event = record_call_failure(
        tracker=_active_tracker,
        exc=exc,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        service_name="litellm",
        details={
            "attribution_component": "llm",
            "attribution_operation_name": "litellm.completion",
            "attribution_operation_status": "failed",
            "attribution_resource_type": "model",
            "attribution_resource_id": model or "unknown",
            "provider_usage_privacy": "quantities_only",
        },
        capability=get_capability(),
        idempotency_key=get_idempotency_key(),
    )
    finalize_failed_auto_task(_active_tracker, auto_task_obj, event)
    return event


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
            try:
                with suppress_network_event():
                    raw_stream = wrapped(*args, **kwargs)
            except Exception as exc:
                _record_call_failure(exc, start_time, kwargs, auto_task_obj)
                raise
            return _SyncStreamWrapper(
                raw_stream,
                start_time,
                kwargs.get("model"),
                task,
                auto_task_obj,
            )

        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(exc, start_time, kwargs, auto_task_obj)
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(response, latency_ms, request_model=kwargs.get("model"))
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
        return _async_stream_handler(
            wrapped, args, kwargs, start_time, auto_task_obj, auto_token, task
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
    """Await the async acompletion call and record the response."""
    try:
        try:
            with suppress_network_event():
                response = await wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(exc, start_time, kwargs, auto_task_obj)
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(response, latency_ms, request_model=kwargs.get("model"))
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
    task: Any = None,
) -> Any:
    """Wrap async streaming to capture usage from the final chunk."""
    try:
        try:
            with suppress_network_event():
                raw_stream = await wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(exc, start_time, kwargs, auto_task_obj)
            raise
        return _AsyncStreamWrapper(
            raw_stream,
            start_time,
            kwargs.get("model"),
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
    """Wraps a sync LiteLLM stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        request_model: Any = None,
        task: Any = None,
        auto_task_obj: Any = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._request_model = request_model
        self._model: str | None = None
        self._usage: Any | None = None
        self._hidden_params: dict[str, Any] | None = None
        self._finalized: bool = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._capability = get_capability()
        self._idempotency_key = get_idempotency_key()

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
        provider = _resolve_provider(
            hidden_params=self._hidden_params,
            request_model=self._request_model,
        )
        model = _canonical_model(self._model, provider, self._request_model)
        record_stream_failure(
            tracker=_active_tracker,
            exc=exc,
            start_time=self._start_time,
            provider=provider,
            model=model,
            task=self._task,
            auto_task_obj=self._auto_task_obj,
            service_name="litellm",
            details={
                "attribution_component": "llm",
                "attribution_operation_name": "litellm.completion",
                "attribution_operation_status": "failed",
                "attribution_resource_type": "model",
                "attribution_resource_id": model,
                "provider_usage_privacy": "quantities_only",
            },
            capability=self._capability,
            idempotency_key=self._idempotency_key,
        )

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
            event = _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                request_model=self._request_model,
                latency_ms=latency_ms,
                task=self._task or self._auto_task_obj,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            self._finalize_auto_task(event, "success")
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def _cancel(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                request_model=self._request_model,
                latency_ms=latency_ms,
                task=self._task or self._auto_task_obj,
                operation_status="cancelled",
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            self._finalize_auto_task(event, "failed")
        except Exception:
            _log.debug("dexcost: failed to record stream cancellation", exc_info=True)

    def _finalize_auto_task(self, event: Event | None, status: str) -> None:
        if self._auto_task_obj is None or event is None:
            return
        finalize_auto_task(self._auto_task_obj, event, status=status)
        if _active_tracker is not None:
            _active_tracker._storage.insert_task(self._auto_task_obj)

    # Forward close/context-manager to the underlying stream
    def close(self) -> None:
        try:
            if hasattr(self._stream, "close"):
                self._stream.close()
        except BaseException as exc:
            self._record_failure(exc)
            raise
        self._cancel()

    def __enter__(self) -> _SyncStreamWrapper:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._cancel()
        if hasattr(self._stream, "__exit__"):
            self._stream.__exit__(*args)


class _AsyncStreamWrapper:
    """Wraps an async LiteLLM stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        request_model: Any = None,
        task: Any = None,
        auto_task_obj: Any = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._request_model = request_model
        self._model: str | None = None
        self._usage: Any | None = None
        self._hidden_params: dict[str, Any] | None = None
        self._finalized: bool = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._capability = get_capability()
        self._idempotency_key = get_idempotency_key()

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
        provider = _resolve_provider(
            hidden_params=self._hidden_params,
            request_model=self._request_model,
        )
        model = _canonical_model(self._model, provider, self._request_model)
        record_stream_failure(
            tracker=_active_tracker,
            exc=exc,
            start_time=self._start_time,
            provider=provider,
            model=model,
            task=self._task,
            auto_task_obj=self._auto_task_obj,
            service_name="litellm",
            details={
                "attribution_component": "llm",
                "attribution_operation_name": "litellm.completion",
                "attribution_operation_status": "failed",
                "attribution_resource_type": "model",
                "attribution_resource_id": model,
                "provider_usage_privacy": "quantities_only",
            },
            capability=self._capability,
            idempotency_key=self._idempotency_key,
        )

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
            event = _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                request_model=self._request_model,
                latency_ms=latency_ms,
                task=self._task or self._auto_task_obj,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            self._finalize_auto_task(event, "success")
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def _cancel(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                model=self._model,
                usage=self._usage,
                hidden_params=self._hidden_params,
                request_model=self._request_model,
                latency_ms=latency_ms,
                task=self._task or self._auto_task_obj,
                operation_status="cancelled",
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            self._finalize_auto_task(event, "failed")
        except Exception:
            _log.debug("dexcost: failed to record stream cancellation", exc_info=True)

    def _finalize_auto_task(self, event: Event | None, status: str) -> None:
        if self._auto_task_obj is None or event is None:
            return
        finalize_auto_task(self._auto_task_obj, event, status=status)
        if _active_tracker is not None:
            _active_tracker._storage.insert_task(self._auto_task_obj)

    async def aclose(self) -> None:
        try:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()
        except BaseException as exc:
            self._record_failure(exc)
            raise
        self._cancel()

    async def __aenter__(self) -> _AsyncStreamWrapper:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._cancel()
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
    hp = _response_hidden_params(response, hidden_params)

    if hp and isinstance(hp, dict):
        provider = hp.get("custom_llm_provider")
        if provider and isinstance(provider, str):
            return _canonical_provider(provider)

    # Try extracting from model string prefix (e.g. "openai/gpt-4")
    model_str: str | None = None
    if response is not None:
        raw = _field(response, "model")
        if raw is not None:
            model_str = str(raw)
    if not model_str and request_model is not None:
        model_str = str(request_model)

    if model_str and "/" in model_str:
        prefix = model_str.split("/", 1)[0]
        if prefix:
            return _canonical_provider(prefix)

    return "unknown"


def _field(value: Any, key: str) -> Any:
    """Read one SDK field without materialising dynamic mock attributes."""
    if isinstance(value, Mapping):
        return value.get(key)
    if value is None:
        return None
    model_extra = getattr(value, "model_extra", None)
    if isinstance(model_extra, Mapping) and key in model_extra:
        return model_extra.get(key)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        if key in attributes:
            return attributes.get(key)
        if isinstance(getattr(type(value), "model_fields", None), Mapping):
            return getattr(value, key, None)
        return None
    return getattr(value, key, None)


def _response_hidden_params(
    response: Any,
    explicit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(explicit, dict):
        return explicit
    candidate = _field(response, "_hidden_params")
    return candidate if isinstance(candidate, dict) else {}


def _canonical_provider(provider: str) -> str:
    normalized = provider.strip().lower().replace("-", "_")
    aliases = {
        "open_router": "openrouter",
        "openrouter_ai": "openrouter",
        "azure": "azure_openai",
        "azure_text": "azure_openai",
        "azure_openai": "azure_openai",
        "google_ai_studio": "google",
        "gemini": "google",
        "palm": "google",
        "vertex": "google",
        "vertex_ai": "google",
        "aws_bedrock": "bedrock",
        "bedrock_converse": "bedrock",
        "hugging_face": "huggingface",
        "huggingface_hub": "huggingface",
        "together_ai": "together",
        "fal": "fal_ai",
        "perplexity_ai": "perplexity",
    }
    return aliases.get(normalized, normalized) or "unknown"


def _canonical_model(model: Any, provider: str, request_model: Any = None) -> str:
    selected = model if isinstance(model, str) and model.strip() else request_model
    name = selected.strip() if isinstance(selected, str) and selected.strip() else "unknown"
    request_name = request_model.strip() if isinstance(request_model, str) else ""
    prefix = {
        "openrouter": "openrouter",
        "azure_openai": "azure",
        "azure_ai": "azure_ai",
        "bedrock": "bedrock",
        "fal_ai": "fal_ai",
        "groq": "groq",
        "huggingface": "huggingface",
        "mistral": "mistral",
        "ollama": "ollama",
        "perplexity": "perplexity",
        "together": "together_ai",
    }.get(provider)
    if provider == "google":
        prefix = "vertex_ai" if request_name.startswith("vertex_ai/") else "gemini"
    if prefix is not None and not name.startswith(f"{prefix}/"):
        return f"{prefix}/{name}"
    return name


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _non_negative_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _provider_response_cost(
    provider: str,
    usage: Any,
    hidden_params: dict[str, Any],
) -> Decimal | None:
    """Return provider-authoritative USD cost, never a LiteLLM estimate."""
    if provider == "perplexity":
        return _non_negative_decimal(_field(_field(usage, "cost"), "total_cost"))
    if provider != "openrouter":
        return None
    direct = _non_negative_decimal(_field(usage, "cost"))
    if direct is not None:
        return direct
    headers = hidden_params.get("additional_headers")
    if isinstance(headers, Mapping):
        return _non_negative_decimal(headers.get("llm_provider-x-litellm-response-cost"))
    return None


def _provider_upstream_cost(usage: Any) -> Decimal | None:
    cost_details = _field(usage, "cost_details")
    return _non_negative_decimal(
        _field(cost_details, "upstream_inference_cost") or _field(cost_details, "upstream_cost")
    )


def _try_hidden_litellm_cost(hidden_params: dict[str, Any]) -> Decimal | None:
    return _non_negative_decimal(hidden_params.get("response_cost"))


def _usage_measurement(usage: Any) -> tuple[dict[str, int], list[dict[str, str]], int, int, int]:
    """Normalize LiteLLM usage into mutually exclusive billing buckets."""
    if usage is None:
        return {}, [], 0, 0, 0

    try:
        normalized = normalize_openai_usage(usage)
    except OpenAIUsageError:
        normalized = None

    pricing: dict[str, int] = {}
    input_total = (
        _non_negative_int(_field(usage, "prompt_tokens") or _field(usage, "input_tokens")) or 0
    )
    output_total = (
        _non_negative_int(_field(usage, "completion_tokens") or _field(usage, "output_tokens"))
        or 0
    )
    cached_total = 0
    if normalized is not None:
        input_total = normalized.total_input_tokens
        output_total = normalized.total_output_tokens
        cached_total = normalized.cache_read_input_tokens
        pricing.update(
            {
                "input_tokens": normalized.input_tokens,
                "cache_read_input_tokens": normalized.cache_read_input_tokens,
                "cache_write_input_tokens": normalized.cache_write_input_tokens,
                "output_tokens": normalized.output_tokens,
                "reasoning_output_tokens": normalized.reasoning_output_tokens,
            }
        )
    else:
        pricing["input_tokens"] = input_total
        pricing["output_tokens"] = output_total
        for metric, field_name in (
            ("cache_read_input_tokens", "cache_read_input_tokens"),
            ("cache_write_input_tokens", "cache_creation_input_tokens"),
        ):
            quantity = _non_negative_int(_field(usage, field_name)) or 0
            if quantity:
                pricing[metric] = quantity
                if metric == "cache_read_input_tokens":
                    cached_total = quantity
        output_details = _field(usage, "completion_tokens_details") or _field(
            usage, "output_tokens_details"
        )
        reasoning = _non_negative_int(_field(output_details, "reasoning_tokens")) or 0
        if reasoning:
            pricing["reasoning_output_tokens"] = reasoning

    server_tools = _field(usage, "server_tool_use") or _field(usage, "server_tool_use_details")
    for metric, field_name, _unit in (
        ("server_tool_calls_requested", "tool_calls_requested", "Calls"),
        ("server_tool_calls_executed", "tool_calls_executed", "Calls"),
        ("web_search_requests", "web_search_requests", "Requests"),
    ):
        quantity = _non_negative_int(_field(server_tools, field_name)) or 0
        if quantity:
            pricing[metric] = quantity

    units = {
        "server_tool_calls_requested": "Calls",
        "server_tool_calls_executed": "Calls",
        "web_search_requests": "Requests",
    }
    lines = [
        {
            "metric": metric,
            "quantity": str(quantity),
            "unit": units.get(metric, "Tokens"),
        }
        for metric, quantity in pricing.items()
        if quantity > 0
    ]
    return pricing, lines, input_total, output_total, cached_total


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
        parsed = _non_negative_decimal(cost)
        if parsed is not None and parsed > 0:
            return parsed
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _record_from_response(
    response: Any, latency_ms: int, *, request_model: Any = None
) -> Event | None:
    """Extract fields from a LiteLLM ModelResponse and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    task = get_current_task()
    if task is None:
        return None

    response_model = _field(response, "model")
    usage = _field(response, "usage")
    hidden_params = _response_hidden_params(response)
    provider = _resolve_provider(response)
    model = _canonical_model(response_model, provider, request_model)

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        provider=provider,
        latency_ms=latency_ms,
        usage=usage,
        hidden_params=hidden_params,
        response=response,
        capability=get_capability(),
        idempotency_key=get_idempotency_key(),
    )


def _record_from_stream_usage(
    *,
    model: str | None,
    usage: Any | None,
    hidden_params: dict[str, Any] | None,
    request_model: Any,
    latency_ms: int,
    task: Any = None,
    operation_status: str = "succeeded",
    capability: Any = None,
    idempotency_key: str | None = None,
) -> Event | None:
    """Record an event from accumulated stream data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    resolved_task = task or get_current_task()
    if resolved_task is None:
        return None

    provider = _resolve_provider(hidden_params=hidden_params, request_model=request_model)
    resolved_model = _canonical_model(model, provider, request_model)

    return _insert_llm_event(
        tracker=tracker,
        task_id=resolved_task.task_id,
        model=resolved_model,
        provider=provider,
        latency_ms=latency_ms,
        usage=usage,
        hidden_params=hidden_params or {},
        response=None,
        operation_status=operation_status,
        capability=capability,
        idempotency_key=idempotency_key,
    )


def _insert_llm_event(
    *,
    tracker: Any,
    task_id: Any,
    model: str,
    provider: str,
    latency_ms: int,
    usage: Any,
    hidden_params: dict[str, Any],
    response: Any | None,
    operation_status: str = "succeeded",
    capability: Any = None,
    idempotency_key: str | None = None,
) -> Event:
    """Create and persist an llm_call Event.

    Tries LiteLLM's own ``completion_cost`` first; falls back to
    the dexcost pricing engine.
    """
    pricing_usage, usage_lines, input_tokens, output_tokens, cached_tokens = _usage_measurement(
        usage
    )
    provider_cost = _provider_response_cost(provider, usage, hidden_params)
    upstream_cost = _provider_upstream_cost(usage)
    litellm_cost = (
        _try_litellm_cost(response)
        if response is not None
        else _try_hidden_litellm_cost(hidden_params)
    )
    cost_result = tracker._pricing.get_metered_cost(
        model,
        pricing_usage,
        model_candidates=(model,),
    )
    if provider_cost is not None:
        cost_usd = provider_cost
        cost_confidence = "exact"
        pricing_source = "provider_response"
        pricing_version: str | None = None
    elif litellm_cost is not None:
        cost_usd = litellm_cost
        cost_confidence = "computed"
        pricing_source = "litellm"
        pricing_version = None
    elif usage is not None:
        cost_usd = cost_result.cost_usd
        cost_confidence = cost_result.cost_confidence
        pricing_source = cost_result.pricing_source
        pricing_version = cost_result.pricing_version
    else:
        cost_usd = Decimal("0")
        cost_confidence = "unknown"
        pricing_source = "unknown"
        pricing_version = None

    details: dict[str, Any] = {
        "attribution_component": "llm",
        "attribution_operation_name": "litellm.completion",
        "attribution_operation_status": operation_status,
        "attribution_resource_type": "model",
        "attribution_resource_id": model,
        "attribution_usage_lines": usage_lines
        or [{"metric": "request_count", "quantity": "1", "unit": "Requests"}],
        "attribution_dimensions": [
            {"key": "gateway", "value": {"type": "string", "value": "litellm"}}
        ],
        "provider_usage_privacy": "quantities_only",
    }
    if provider_cost is not None:
        details["provider_reported_cost_usd"] = canonical_decimal(provider_cost)
    if upstream_cost is not None:
        details["provider_upstream_cost_usd"] = canonical_decimal(upstream_cost)
    if pricing_source == "litellm":
        details["gateway_calculated_cost_usd"] = canonical_decimal(cost_usd)
    reasoning_tokens = pricing_usage.get("reasoning_output_tokens", 0)
    cache_write_tokens = pricing_usage.get("cache_write_input_tokens", 0)
    if reasoning_tokens:
        details["reasoning_output_tokens"] = reasoning_tokens
    if cache_write_tokens:
        details["cache_creation_input_tokens"] = cache_write_tokens
    if cost_result.resolved_model is not None:
        details["pricing_resolved_model"] = cost_result.resolved_model
    if cost_result.unpriced_dimensions:
        details["pricing_unpriced_dimensions"] = list(cost_result.unpriced_dimensions)

    event = Event(
        task_id=task_id,
        event_type="llm_call",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        service_name="litellm",
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    apply_event_capability(event, capability)
    apply_event_idempotency(event, idempotency_key)
    tracker._storage.insert_event(event)
    return event
