"""Privacy-safe auto-instrumentation for the official Cohere Python SDK.

Both the legacy ``Client``/``AsyncClient`` API and the current
``ClientV2``/``AsyncClientV2`` API are supported.  Chat, chat streaming,
embedding, and reranking calls made inside an active
:class:`~dexcost.tracker.CostTracker` task are recorded without retaining
prompts, documents, generated content, or embeddings.

V1 usage is extracted from ``response.meta.billed_units``; V2 chat usage is
extracted from ``response.usage.billed_units``.  V2 stream usage is carried by
the terminal ``message-end`` event's delta.

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
from collections.abc import Iterator, Mapping
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from inspect import getattr_static
from typing import Any, Literal

import wrapt

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.capabilities import apply_event_capability, get_capability
from dexcost.context import (
    _current_task,
    get_current_task,
    set_current_task,
    suppress_network_event,
)
from dexcost.idempotency import (
    IdempotencyKey,
    apply_event_idempotency,
    capture_idempotency_key,
)
from dexcost.instruments._capture import provider_capture_callable, provider_capture_wrapper
from dexcost.instruments._errors import (
    error_details,
    finalize_failed_auto_task,
    record_call_failure,
    requested_model,
)
from dexcost.instruments._provider_metering import (
    OperationMeasurement,
    ProviderOperationSession,
    ProviderUsageLine,
)
from dexcost.models.capability import CapabilityIdentity
from dexcost.models.event import Event

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_active_tracker: Any | None = None  # CostTracker (lazy to avoid circular import)
_patched: bool = False
_originals: dict[str, Any] = {}

_MeteredKind = Literal["embed", "rerank"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def instrument_cohere(tracker: Any) -> None:
    """Monkey-patch the Cohere SDK to capture billable calls automatically.

    Patches V1 and V2 sync/async chat, chat-stream, embed, and rerank methods
    when the installed SDK exposes them.  V2 base classes are patched so both
    ``ClientV2`` and the V2 view exposed by a combined client are covered.

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
        provider_capture_wrapper("cohere", _sync_chat_wrapper),
    )
    wrapt.wrap_function_wrapper(
        "cohere",
        "AsyncClient.chat",
        provider_capture_wrapper("cohere", _async_chat_wrapper),
    )

    # Streaming path — Cohere exposes ``chat_stream`` as a separate method
    # that returns an iterator of streaming events.  Patch it when present.
    if hasattr(cohere.Client, "chat_stream"):
        _originals["sync_chat_stream"] = cohere.Client.chat_stream
        if isinstance(getattr_static(cohere.Client, "chat_stream"), staticmethod):
            wrapt.wrap_function_wrapper(
                "cohere",
                "Client.chat_stream",
                provider_capture_wrapper("cohere", _sync_chat_stream_wrapper),
            )
        else:
            cohere.Client.chat_stream = _direct_stream_callable(  # type: ignore[method-assign]
                _originals["sync_chat_stream"],
                async_call=False,
            )
    if hasattr(cohere.AsyncClient, "chat_stream"):
        _originals["async_chat_stream"] = cohere.AsyncClient.chat_stream
        if isinstance(getattr_static(cohere.AsyncClient, "chat_stream"), staticmethod):
            wrapt.wrap_function_wrapper(
                "cohere",
                "AsyncClient.chat_stream",
                provider_capture_wrapper("cohere", _async_chat_stream_wrapper),
            )
        else:
            cohere.AsyncClient.chat_stream = _direct_stream_callable(  # type: ignore[method-assign]
                _originals["async_chat_stream"],
                async_call=True,
            )

    metered_methods: tuple[tuple[str, _MeteredKind], ...] = (
        ("embed", "embed"),
        ("rerank", "rerank"),
    )
    for legacy_owner, owner_path, async_call in (
        (cohere.Client, "Client", False),
        (cohere.AsyncClient, "AsyncClient", True),
    ):
        for method, legacy_kind in metered_methods:
            if not hasattr(legacy_owner, method):
                continue
            key = f"{'async' if async_call else 'sync'}_{legacy_kind}"
            _originals[key] = getattr(legacy_owner, method)
            wrapt.wrap_function_wrapper(
                "cohere",
                f"{owner_path}.{method}",
                provider_capture_wrapper(
                    "cohere",
                    _async_metered_wrapper(legacy_kind)
                    if async_call
                    else _sync_metered_wrapper(legacy_kind),
                ),
            )

    # Current Cohere releases implement ClientV2 on generated V2 base
    # classes.  Patch those bases rather than only the exported subclasses so
    # inherited entry points and combined-client V2 views cannot bypass us.
    cohere_v2_client: Any = None
    try:
        from cohere.v2 import client as loaded_v2_client
    except ImportError:
        pass
    else:
        cohere_v2_client = loaded_v2_client

    if cohere_v2_client is not None:
        for owner_name, async_call in (("V2Client", False), ("AsyncV2Client", True)):
            v2_owner = getattr(cohere_v2_client, owner_name, None)
            if v2_owner is None:
                continue
            for method in ("chat", "chat_stream", "embed", "rerank"):
                if not hasattr(v2_owner, method):
                    continue
                key = f"v2_{'async' if async_call else 'sync'}_{method}"
                _originals[key] = getattr(v2_owner, method)
                if method == "chat_stream":
                    setattr(
                        v2_owner,
                        method,
                        _direct_stream_callable(_originals[key], async_call=async_call),
                    )
                    continue
                if method == "chat":
                    adapter = _async_chat_wrapper if async_call else _sync_chat_wrapper
                else:
                    metered_kind: _MeteredKind = "embed" if method == "embed" else "rerank"
                    adapter = (
                        _async_metered_wrapper(metered_kind)
                        if async_call
                        else _sync_metered_wrapper(metered_kind)
                    )
                wrapt.wrap_function_wrapper(
                    "cohere.v2.client",
                    f"{owner_name}.{method}",
                    provider_capture_wrapper("cohere", adapter),
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
            cohere.Client.chat = _originals["sync_chat"]  # type: ignore[method-assign]
        if "async_chat" in _originals:
            cohere.AsyncClient.chat = _originals["async_chat"]  # type: ignore[method-assign]
        if "sync_chat_stream" in _originals:
            cohere.Client.chat_stream = _originals["sync_chat_stream"]  # type: ignore[method-assign]
        if "async_chat_stream" in _originals:
            cohere.AsyncClient.chat_stream = _originals["async_chat_stream"]  # type: ignore[method-assign]
        for legacy_owner, async_call in (
            (cohere.Client, False),
            (cohere.AsyncClient, True),
        ):
            for method in ("embed", "rerank"):
                key = f"{'async' if async_call else 'sync'}_{method}"
                if key in _originals:
                    setattr(legacy_owner, method, _originals[key])

        cohere_v2_client: Any = None
        try:
            from cohere.v2 import client as loaded_v2_client
        except ImportError:
            pass
        else:
            cohere_v2_client = loaded_v2_client
        if cohere_v2_client is not None:
            for owner_name, async_call in (("V2Client", False), ("AsyncV2Client", True)):
                v2_owner = getattr(cohere_v2_client, owner_name, None)
                if v2_owner is None:
                    continue
                for method in ("chat", "chat_stream", "embed", "rerank"):
                    key = f"v2_{'async' if async_call else 'sync'}_{method}"
                    if key in _originals:
                        setattr(v2_owner, method, _originals[key])
    except ImportError:
        pass

    _originals.clear()
    _active_tracker = None
    _patched = False


# ---------------------------------------------------------------------------
# Wrapper functions
# ---------------------------------------------------------------------------


def _record_call_failure(
    exc: BaseException,
    start_time: float,
    kwargs: dict[str, Any],
    auto_task_obj: Any = None,
    task: Any = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event | None:
    """Record a raised Cohere call as a failed operation. Never raises."""
    try:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
    except Exception:  # pragma: no cover - defensive
        latency_ms = None
    event = record_call_failure(
        tracker=_active_tracker,
        exc=exc,
        provider="cohere",
        model=requested_model(kwargs),
        latency_ms=latency_ms,
        service_name="chat",
        task=task,
        details={
            "attribution_component": "llm",
            "attribution_operation_name": "cohere.chat",
            "attribution_operation_status": "failed",
            "attribution_resource_type": "model",
            "attribution_resource_id": requested_model(kwargs) or "command-r-plus",
            "attribution_usage_lines": [
                {"metric": "request_count", "quantity": "1", "unit": "Requests"}
            ],
            "provider_usage_privacy": "quantities_only",
        },
        capability=capability,
        idempotency_key=idempotency_key,
    )
    finalize_failed_auto_task(_active_tracker, auto_task_obj, event)
    return event


def _sync_chat_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Client.chat``."""
    task = get_current_task()
    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("cohere.chat")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()

        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(
                exc,
                start_time,
                kwargs,
                auto_task_obj,
                task or auto_task_obj,
                capability,
                idempotency_key,
            )
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(
                response,
                latency_ms,
                kwargs,
                task or auto_task_obj,
                capability,
                idempotency_key,
            )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

        if auto and auto_task_obj is not None and event is not None:
            try:
                outcome = (
                    "success"
                    if event.details.get("attribution_operation_status") == "succeeded"
                    else "failed"
                )
                finalize_auto_task(auto_task_obj, event, status=outcome)
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


def _async_chat_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for async ``AsyncClient.chat``."""
    task = get_current_task()
    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("cohere.chat")

    start_time = time.perf_counter()
    return _async_chat_handler(
        wrapped,
        args,
        kwargs,
        start_time,
        auto_task_obj,
        auto_token,
        task,
        capability,
        idempotency_key,
    )


async def _async_chat_handler(
    wrapped: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    start_time: float,
    auto_task_obj: Any = None,
    auto_token: Any = None,
    task: Any = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Any:
    """Await the async chat call and record the response."""
    if auto_task_obj is not None and auto_token is None:
        auto_token = set_current_task(auto_task_obj)
    try:
        try:
            with suppress_network_event():
                response = await wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(
                exc,
                start_time,
                kwargs,
                auto_task_obj,
                task or auto_task_obj,
                capability,
                idempotency_key,
            )
            raise
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        event: Any = None
        try:
            event = _record_from_response(
                response,
                latency_ms,
                kwargs,
                task or auto_task_obj,
                capability,
                idempotency_key,
            )
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

        if auto_task_obj is not None and event is not None:
            try:
                outcome = (
                    "success"
                    if event.details.get("attribution_operation_status") == "succeeded"
                    else "failed"
                )
                finalize_auto_task(auto_task_obj, event, status=outcome)
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(auto_task_obj)
            except Exception:
                _log.debug("dexcost: failed to finalize auto-task", exc_info=True)

        return response
    finally:
        if auto_token is not None:
            _current_task.reset(auto_token)


# ---------------------------------------------------------------------------
# Embedding and rerank wrappers
# ---------------------------------------------------------------------------


def _value(owner: Any, name: str, default: Any = None) -> Any:
    if isinstance(owner, Mapping):
        return owner.get(name, default)
    attributes = getattr(owner, "__dict__", None)
    if isinstance(attributes, dict):
        # Looking in __dict__ first avoids MagicMock fabricating arbitrary
        # response fields and also matches Cohere's generated Pydantic models.
        return attributes.get(name, default)
    return getattr(owner, name, default)


def _non_negative_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _token_count(value: Any) -> int | None:
    parsed = _non_negative_decimal(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _billed_units(response: Any) -> Any | None:
    usage = _value(response, "usage")
    billed = _value(usage, "billed_units") if usage is not None else None
    if billed is not None:
        return billed
    meta = _value(response, "meta")
    return _value(meta, "billed_units") if meta is not None else None


def _metered_model(kind: _MeteredKind, kwargs: Mapping[str, Any]) -> str:
    raw = kwargs.get("model")
    if not isinstance(raw, str) or not raw:
        return "unknown"
    if kind == "embed" and raw.startswith("cohere/"):
        return raw.removeprefix("cohere/")
    return raw


def _metered_measurement(
    kind: _MeteredKind,
    response: Any,
    model: str,
) -> OperationMeasurement:
    billed = _billed_units(response)
    input_tokens = _token_count(_value(billed, "input_tokens"))
    image_tokens = _token_count(_value(billed, "image_tokens"))
    output_tokens = _token_count(_value(billed, "output_tokens"))
    search_units = _non_negative_decimal(_value(billed, "search_units"))
    classifications = _non_negative_decimal(_value(billed, "classifications"))

    pricing: dict[str, Decimal | int | str] = {}
    lines: list[ProviderUsageLine] = []
    if input_tokens is not None:
        if kind != "embed":
            pricing["input_tokens"] = input_tokens
        lines.append(ProviderUsageLine("input_tokens", input_tokens, "Tokens"))
    if kind == "embed" and image_tokens is not None:
        lines.append(ProviderUsageLine("input_image_tokens", image_tokens, "Tokens"))
    if output_tokens is not None:
        pricing["output_tokens"] = output_tokens
        lines.append(ProviderUsageLine("output_tokens", output_tokens, "Tokens"))
    if search_units is not None:
        # Cohere calls this provider-native meter ``search_units``; the pricing
        # catalog's corresponding rate is expressed per query.
        pricing["query_count"] = search_units
        lines.append(ProviderUsageLine("search_units", search_units, "SearchUnits"))
    if classifications is not None:
        lines.append(ProviderUsageLine("classifications", classifications, "Classifications"))

    return OperationMeasurement(
        pricing_usage=pricing,
        usage_lines=tuple(lines),
        provider_record_id=_provider_record_id(response),
        response_model=model,
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
        provider_service="embed" if kind == "embed" else None,
    )


def _metered_session(kind: _MeteredKind, model: str) -> ProviderOperationSession:
    service = "embeddings" if kind == "embed" else "rerank"
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=f"cohere.{kind}",
        provider="cohere",
        service=service,
        operation=f"cohere.{kind}",
        component="external" if kind == "embed" else "llm",
        model=model,
        event_type="external_cost" if kind == "embed" else "llm_call",
    )


def _sync_metered_wrapper(kind: _MeteredKind) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        model = _metered_model(kind, kwargs)
        session = _metered_session(kind, model)
        try:
            with suppress_network_event():
                response = wrapped(*args, **kwargs)
        except BaseException as exc:
            session.fail(exc)
            raise
        session.succeed(_metered_measurement(kind, response, model))
        return response

    return wrapper


def _async_metered_wrapper(kind: _MeteredKind) -> Any:
    def wrapper(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        async def invoke() -> Any:
            # Build the session after the coroutine is awaited so an automatic
            # task's ContextVar cannot leak between concurrently created calls.
            model = _metered_model(kind, kwargs)
            session = _metered_session(kind, model)
            try:
                with suppress_network_event():
                    response = await wrapped(*args, **kwargs)
            except BaseException as exc:
                session.fail(exc)
                raise
            session.succeed(_metered_measurement(kind, response, model))
            return response

        return invoke()

    return wrapper


# ---------------------------------------------------------------------------
# Streaming wrappers
# ---------------------------------------------------------------------------


def _direct_stream_callable(original: Any, *, async_call: bool) -> Any:
    """Wrap an SDK generator method without preserving its generator flag.

    ``wrapt`` intentionally mirrors generator-function behavior, which would
    return the raw Cohere generator and skip our stream lifecycle object.  A
    regular descriptor wrapper ensures the adapter runs at invocation time.
    """
    adapter = _async_chat_stream_wrapper if async_call else _sync_chat_stream_wrapper

    def replacement(instance: Any, *args: Any, **kwargs: Any) -> Any:
        def bound_original(*call_args: Any, **call_kwargs: Any) -> Any:
            return original(instance, *call_args, **call_kwargs)

        return adapter(bound_original, instance, args, kwargs)

    return provider_capture_callable("cohere", replacement, original)


def _sync_chat_stream_wrapper(
    wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    """wrapt wrapper for sync ``Client.chat_stream``.

    ``chat_stream`` returns an iterator of streaming events; the wrapper
    accumulates token usage from the terminal ``stream-end`` event and
    records an ``llm_call`` event once the stream is fully consumed.
    """
    task = get_current_task()
    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("cohere.chat")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()
        model = kwargs.get("model") or "command-r-plus"
        try:
            with suppress_network_event():
                raw_stream = wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(
                exc,
                start_time,
                kwargs,
                auto_task_obj,
                task or auto_task_obj,
                capability,
                idempotency_key,
            )
            raise
        return _SyncStreamWrapper(
            raw_stream,
            start_time,
            str(model),
            task=task or auto_task_obj,
            auto_task_obj=auto_task_obj,
            capability=capability,
            idempotency_key=idempotency_key,
        )
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
    capability = get_capability()
    idempotency_key = capture_idempotency_key()
    auto = task is None
    auto_task_obj = None
    auto_token = None

    if auto:
        auto_task_obj = create_auto_task("cohere.chat")
        auto_token = set_current_task(auto_task_obj)

    try:
        start_time = time.perf_counter()
        model = kwargs.get("model") or "command-r-plus"
        try:
            with suppress_network_event():
                raw_stream = wrapped(*args, **kwargs)
        except Exception as exc:
            _record_call_failure(
                exc,
                start_time,
                kwargs,
                auto_task_obj,
                task or auto_task_obj,
                capability,
                idempotency_key,
            )
            raise
        return _AsyncStreamWrapper(
            raw_stream,
            start_time,
            str(model),
            task=task or auto_task_obj,
            auto_task_obj=auto_task_obj,
            capability=capability,
            idempotency_key=idempotency_key,
        )
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
    # V2 puts terminal usage on ``message-end.delta.usage`` and does not
    # include a nested response object.  V1 puts it on the terminal response.
    delta = _value(event, "delta")
    usage = _value(delta, "usage") if delta is not None else None
    billed = _value(usage, "billed_units") if usage is not None else None
    if billed is not None:
        return billed
    response = _value(event, "response")
    return _billed_units(response) if response is not None else None


def _extract_stream_response(event: Any) -> Any | None:
    event_type = getattr(event, "event_type", None) or getattr(event, "type", None)
    if event_type not in ("stream-end", "message-end"):
        return None
    return _value(event, "response") or event


def _response_status(response: Any) -> str:
    delta = _value(response, "delta")
    reason = _value(delta, "finish_reason") if delta is not None else None
    if reason is None:
        reason = _value(response, "finish_reason")
    if isinstance(reason, str) and reason.upper() in {"ERROR", "TIMEOUT"}:
        return "failed"
    return "succeeded"


def _is_stream_tool_call_start(event: Any) -> bool:
    event_type = _value(event, "event_type") or _value(event, "type")
    return isinstance(event_type, str) and event_type == "tool-call-start"


class _SyncStreamWrapper(Iterator[Any]):
    """Wraps a sync Cohere chat stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        model: str,
        task: Any = None,
        auto_task_obj: Any = None,
        capability: CapabilityIdentity | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model = model
        self._billed_units: Any | None = None
        self._terminal_response: Any | None = None
        self._tool_calls = 0
        self._finalized: bool = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._capability = capability
        self._idempotency_key = idempotency_key

    def __iter__(self) -> _SyncStreamWrapper:
        return self

    def __next__(self) -> Any:
        try:
            # Cohere implements chat_stream as a lazy generator; the HTTP call
            # happens on first iteration, after the method wrapper returned.
            with suppress_network_event():
                event = next(self._stream)
            usage = _extract_stream_usage(event)
            if usage is not None:
                self._billed_units = usage
            response = _extract_stream_response(event)
            if response is not None:
                self._terminal_response = response
            if _is_stream_tool_call_start(event):
                self._tool_calls += 1
            return event
        except StopIteration:
            self._finalize(_response_status(self._terminal_response))
            raise
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _record_failure(self, exc: BaseException) -> None:
        """Persist a provider error raised while the stream was being consumed.

        Preserve a billed-usage envelope already observed before the failure.
        When Cohere has not emitted one yet, the failed event remains visibly
        unpriced instead of fabricating token counts.
        """
        self._finalize("failed", exc)

    def _finalize(self, status: str, error: BaseException | None = None) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                self._model,
                self._billed_units,
                latency_ms,
                task=self._task,
                status=status,
                error=error,
                response=self._terminal_response,
                tool_calls=self._tool_calls,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            if self._auto_task_obj is not None and event is not None:
                finalize_auto_task(
                    self._auto_task_obj,
                    event,
                    status="success" if status == "succeeded" else "failed",
                )
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(self._auto_task_obj)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def __del__(self) -> None:
        """Account for an abandoned stream exactly once during collection."""
        with suppress(Exception):
            self._finalize("cancelled")

    def close(self) -> None:
        try:
            if hasattr(self._stream, "close"):
                self._stream.close()
        except Exception as exc:
            self._record_failure(exc)
            raise
        self._finalize("cancelled")

    def __enter__(self) -> _SyncStreamWrapper:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        try:
            result = self._stream.__exit__(*args) if hasattr(self._stream, "__exit__") else None
        except Exception as exit_exc:
            self._record_failure(exit_exc)
            raise
        if not self._finalized:
            self._finalize("cancelled")
        return result


class _AsyncStreamWrapper:
    """Wraps an async Cohere chat stream to capture usage on completion."""

    def __init__(
        self,
        stream: Any,
        start_time: float,
        model: str,
        task: Any = None,
        auto_task_obj: Any = None,
        capability: CapabilityIdentity | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> None:
        self._stream = stream
        self._start_time = start_time
        self._model = model
        self._billed_units: Any | None = None
        self._terminal_response: Any | None = None
        self._tool_calls = 0
        self._finalized: bool = False
        self._task = task
        self._auto_task_obj = auto_task_obj
        self._capability = capability
        self._idempotency_key = idempotency_key

    def __aiter__(self) -> _AsyncStreamWrapper:
        return self

    async def __anext__(self) -> Any:
        try:
            # AsyncClient chat_stream is an async generator and is equally
            # lazy, so keep lower-level network attribution suppressed here.
            with suppress_network_event():
                event = await self._stream.__anext__()
            usage = _extract_stream_usage(event)
            if usage is not None:
                self._billed_units = usage
            response = _extract_stream_response(event)
            if response is not None:
                self._terminal_response = response
            if _is_stream_tool_call_start(event):
                self._tool_calls += 1
            return event
        except StopAsyncIteration:
            self._finalize(_response_status(self._terminal_response))
            raise
        except Exception as exc:
            self._record_failure(exc)
            raise

    def _record_failure(self, exc: BaseException) -> None:
        """Persist a provider error raised while the stream was being consumed.

        Preserve a billed-usage envelope already observed before the failure.
        When Cohere has not emitted one yet, the failed event remains visibly
        unpriced instead of fabricating token counts.
        """
        self._finalize("failed", exc)

    def _finalize(self, status: str, error: BaseException | None = None) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            latency_ms = int((time.perf_counter() - self._start_time) * 1000)
            event = _record_from_stream_usage(
                self._model,
                self._billed_units,
                latency_ms,
                task=self._task,
                status=status,
                error=error,
                response=self._terminal_response,
                tool_calls=self._tool_calls,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            if self._auto_task_obj is not None and event is not None:
                finalize_auto_task(
                    self._auto_task_obj,
                    event,
                    status="success" if status == "succeeded" else "failed",
                )
                if _active_tracker is not None:
                    _active_tracker._storage.insert_task(self._auto_task_obj)
        except Exception:
            _log.debug("dexcost: failed to record event", exc_info=True)

    def __del__(self) -> None:
        """Account for an abandoned async stream exactly once during collection."""
        with suppress(Exception):
            self._finalize("cancelled")

    async def aclose(self) -> None:
        try:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()
        except Exception as exc:
            self._record_failure(exc)
            raise
        self._finalize("cancelled")

    async def __aenter__(self) -> _AsyncStreamWrapper:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        try:
            result = (
                await self._stream.__aexit__(*args) if hasattr(self._stream, "__aexit__") else None
            )
        except Exception as exit_exc:
            self._record_failure(exit_exc)
            raise
        if not self._finalized:
            self._finalize("cancelled")
        return result


# ---------------------------------------------------------------------------
# Event recording helpers
# ---------------------------------------------------------------------------


def _provider_record_id(response: Any) -> str | None:
    for name in ("response_id", "generation_id", "id"):
        value = _value(response, name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:256]
    return None


def _tool_call_count(response: Any) -> int:
    for owner in (response, getattr(response, "message", None)):
        calls = getattr(owner, "tool_calls", None)
        if isinstance(calls, list):
            return len(calls)
    return 0


def _usage_lines(input_tokens: int, output_tokens: int, tool_calls: int) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    for metric, quantity, unit in (
        ("input_tokens", input_tokens, "Tokens"),
        ("output_tokens", output_tokens, "Tokens"),
        ("tool_call_count", tool_calls, "Calls"),
    ):
        if quantity > 0:
            lines.append({"metric": metric, "quantity": str(quantity), "unit": unit})
    return lines or [{"metric": "request_count", "quantity": "1", "unit": "Requests"}]


def _record_from_stream_usage(
    model: str,
    billed_units: Any | None,
    latency_ms: int,
    *,
    task: Any,
    status: str,
    error: BaseException | None = None,
    response: Any = None,
    tool_calls: int = 0,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event | None:
    """Record an event from accumulated Cohere stream usage data."""
    tracker = _active_tracker
    if tracker is None:
        return None

    if task is None:
        return None

    parsed_input = _token_count(_value(billed_units, "input_tokens"))
    parsed_output = _token_count(_value(billed_units, "output_tokens"))
    input_tokens = parsed_input or 0
    output_tokens = parsed_output or 0
    has_usage = parsed_input is not None or parsed_output is not None

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        operation_status=status,
        error=error,
        provider_record_id=_provider_record_id(response),
        tool_calls=max(tool_calls, _tool_call_count(response)),
        capability=capability,
        idempotency_key=idempotency_key,
    )


def _record_from_response(
    response: Any,
    latency_ms: int,
    kwargs: dict[str, Any],
    task: Any,
    capability: CapabilityIdentity | None,
    idempotency_key: IdempotencyKey | None,
) -> Event | None:
    """Extract fields from a Cohere chat response and record an event."""
    tracker = _active_tracker
    if tracker is None:
        return None

    if task is None:
        return None

    model = kwargs.get("model") or "command-r-plus"
    if not isinstance(model, str):
        model = str(model)

    billed_units = _billed_units(response)
    parsed_input = _token_count(_value(billed_units, "input_tokens"))
    parsed_output = _token_count(_value(billed_units, "output_tokens"))
    input_tokens = parsed_input or 0
    output_tokens = parsed_output or 0
    has_usage = parsed_input is not None or parsed_output is not None

    return _insert_llm_event(
        tracker=tracker,
        task_id=task.task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        has_usage=has_usage,
        operation_status=_response_status(response),
        provider_record_id=_provider_record_id(response),
        tool_calls=_tool_call_count(response),
        capability=capability,
        idempotency_key=idempotency_key,
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
    operation_status: str = "succeeded",
    error: BaseException | None = None,
    provider_record_id: str | None = None,
    tool_calls: int = 0,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event:
    """Create and persist an llm_call Event."""
    if has_usage:
        cost_result = tracker._pricing.get_cost(model, input_tokens, output_tokens)
        cost_usd = cost_result.cost_usd
        cost_confidence = cost_result.cost_confidence
        pricing_source = cost_result.pricing_source
        pricing_version = cost_result.pricing_version
    else:
        cost_usd = Decimal("0")
        cost_confidence = "estimated"
        pricing_source = "unknown"
        pricing_version = None

    details: dict[str, Any] = {
        "attribution_component": "llm",
        "attribution_operation_name": "cohere.chat",
        "attribution_operation_status": operation_status,
        "attribution_resource_type": "model",
        "attribution_resource_id": model,
        "attribution_usage_lines": _usage_lines(input_tokens, output_tokens, tool_calls),
        "provider_usage_privacy": "quantities_only",
    }
    if provider_record_id is not None:
        details["provider_record_id"] = provider_record_id
    if error is not None:
        details.update(error_details(error))

    event = Event(
        task_id=task_id,
        event_type="llm_call",
        cost_usd=cost_usd,
        cost_confidence=cost_confidence,
        pricing_source=pricing_source,
        pricing_version=pricing_version,
        service_name="chat",
        provider="cohere",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    apply_event_capability(event, capability)
    apply_event_idempotency(event, idempotency_key)
    tracker._storage.insert_event(event)
    return event
