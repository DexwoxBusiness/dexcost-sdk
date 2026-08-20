"""Shared helpers that turn a raised provider call into a failed operation.

Auto-instrumentation wraps provider SDK calls. When the wrapped call raises,
the call still happened — it just failed — so it must be recorded as an event
whose attribution-v3 ``operation.status`` is ``"failed"`` and which carries an
``operation.error`` identity.

Two hard rules govern everything in this module:

1. The user's exception is never swallowed, replaced, or altered. Callers
   record and then bare-``raise`` so the original traceback survives.
2. A failure inside *our* recording is caught and logged, never raised. Every
   public entry point here is total: it returns ``None`` instead of raising.

The failure marker is threaded through ``Event.details`` using the *existing*
mechanism that :func:`dexcost.attribution.v3_convert._operation_status` already
understands (``details["error_type"]``), so the converter needs no
instrument-specific special cases.
"""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from typing import Any

from dexcost.models.event import Event

_log = logging.getLogger(__name__)

# Server contract: operation.error.type must match ^[a-z0-9][a-z0-9._-]{0,127}$
_NON_CANONICAL = re.compile(r"[^a-z0-9._-]")
_LEADING_JUNK = re.compile(r"^[^a-z0-9]+")
MAX_ERROR_TYPE_LENGTH = 127
MAX_ERROR_CODE_LENGTH = 64
UNKNOWN_ERROR_TYPE = "unknown_error"

# Attributes that commonly carry a provider error code, in preference order.
_CODE_ATTRIBUTES: tuple[str, ...] = ("code", "status_code", "error_code", "http_status")


def canonical_error_type(value: str | None) -> str:
    """Return *value* as a canonical ``operation.error.type``.

    Lower-cased; every character outside ``[a-z0-9._-]`` becomes ``"_"``;
    leading characters that cannot start the token are dropped; truncated to
    :data:`MAX_ERROR_TYPE_LENGTH`. Never returns an invalid token — an
    unusable input degrades to :data:`UNKNOWN_ERROR_TYPE`.
    """
    normalized = _NON_CANONICAL.sub("_", (value or "").strip().lower())
    normalized = _LEADING_JUNK.sub("", normalized)[:MAX_ERROR_TYPE_LENGTH]
    return normalized or UNKNOWN_ERROR_TYPE


def error_type_of(exc: BaseException) -> str:
    """Return the canonical error type for an exception instance."""
    try:
        name = type(exc).__name__
    except Exception:  # pragma: no cover - defensive; type() cannot normally fail
        return UNKNOWN_ERROR_TYPE
    return canonical_error_type(name)


def _coerce_code(value: object) -> str | None:
    """Coerce a candidate provider error code to a bounded string."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int)):
        return None
    try:
        text = str(value).strip()
    except Exception:  # pragma: no cover - defensive
        return None
    if not text:
        return None
    return text[:MAX_ERROR_CODE_LENGTH]


def error_code_of(exc: BaseException) -> str | None:
    """Return the provider error code carried by *exc*, when it has one.

    Looks at the conventional exception attributes (``code``,
    ``status_code``, ...) and at the botocore-style
    ``exc.response["Error"]["Code"]`` envelope. Returns ``None`` when no
    usable code is present, so the optional wire field is simply omitted.
    """
    try:
        for attribute in _CODE_ATTRIBUTES:
            code = _coerce_code(getattr(exc, attribute, None))
            if code is not None:
                return code
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error")
            if isinstance(error, dict):
                code = _coerce_code(error.get("Code"))
                if code is not None:
                    return code
            code = _coerce_code(response.get("status_code"))
            if code is not None:
                return code
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = _coerce_code(error.get("code") or error.get("type"))
                if code is not None:
                    return code
    except Exception:  # pragma: no cover - defensive; attribute access can raise
        _log.debug("dexcost: failed to extract provider error code", exc_info=True)
    return None


def error_details(exc: BaseException) -> dict[str, Any]:
    """Return the ``Event.details`` fragment that marks a failed operation."""
    details: dict[str, Any] = {"error_type": error_type_of(exc)}
    code = error_code_of(exc)
    if code is not None:
        details["error_code"] = code
    return details


def record_call_failure(
    *,
    tracker: Any,
    exc: BaseException,
    provider: str | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    event_type: str = "llm_call",
    service_name: str | None = None,
    details: dict[str, Any] | None = None,
    task: Any = None,
) -> Event | None:
    """Persist a failed-operation event for a provider call that raised.

    Usage and cost are unknown on a failure, so nothing is fabricated: cost is
    zero with ``cost_confidence="unknown"`` and the token counters are left
    ``None``.

    This function never raises. It returns the persisted :class:`Event`, or
    ``None`` when there is nothing to record (no tracker, no active task) or
    when recording itself failed.
    """
    try:
        if tracker is None:
            return None
        resolved_task = task
        if resolved_task is None:
            from dexcost.context import get_current_task

            resolved_task = get_current_task()
        if resolved_task is None:
            return None

        event_details: dict[str, Any] = dict(details or {})
        event_details.update(error_details(exc))
        if latency_ms is not None and latency_ms >= 0:
            event_details.setdefault("latency_ms", latency_ms)

        event = Event(
            task_id=resolved_task.task_id,
            event_type=event_type,
            cost_usd=Decimal("0"),
            cost_confidence="unknown",
            pricing_source="unknown",
            pricing_version=None,
            service_name=service_name,
            provider=provider,
            model=model,
            latency_ms=latency_ms if latency_ms is not None and latency_ms >= 0 else None,
            details=event_details,
        )
        tracker._storage.insert_event(event)
        return event
    except Exception:
        _log.debug("dexcost: failed to record call failure", exc_info=True)
        return None


def record_stream_failure(
    *,
    tracker: Any,
    exc: BaseException,
    start_time: float | None = None,
    provider: str | None = None,
    model: str | None = None,
    task: Any = None,
    auto_task_obj: Any = None,
    event_type: str = "llm_call",
    service_name: str | None = None,
    details: dict[str, Any] | None = None,
) -> Event | None:
    """Record a provider error raised *while a stream was being consumed*.

    Streaming calls have two distinct failure points. The provider SDK can
    raise when the stream is created, which the call-site ``try`` around
    ``wrapped(...)`` already covers; or it can return a stream successfully and
    raise later, from ``next()`` / ``__anext__()``, as the response is pulled
    over the wire. The second is the more common failure for long generations,
    and without this path the call is silently lost: no failed event is
    persisted and an auto-task started for the call never closes.

    By consumption time the wrapper's contextvar token has already been reset,
    so the owning task is passed in explicitly instead of being read from the
    ambient context — reading it there would attribute the failure to whatever
    task happens to be current, or to nothing at all.

    Latency is measured from stream creation, so it covers the time actually
    spent streaming before the error. Never raises.
    """
    latency_ms: int | None = None
    try:
        if start_time is not None:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
    except Exception:  # pragma: no cover - defensive
        latency_ms = None
    event = record_call_failure(
        tracker=tracker,
        exc=exc,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        event_type=event_type,
        service_name=service_name,
        details=details,
        task=task if task is not None else auto_task_obj,
    )
    finalize_failed_auto_task(tracker, auto_task_obj, event)
    return event


def finalize_failed_auto_task(tracker: Any, auto_task_obj: Any, event: Event | None) -> None:
    """Close an auto-task that ended in a failed provider call.

    Mirrors the success path so the failure event is not orphaned. Never
    raises.
    """
    if auto_task_obj is None or event is None:
        return
    try:
        from dexcost.auto_task import finalize_auto_task

        finalize_auto_task(auto_task_obj, event, status="failed")
        if tracker is not None:
            tracker._storage.insert_task(auto_task_obj)
    except Exception:
        _log.debug("dexcost: failed to finalize failed auto-task", exc_info=True)


def requested_model(kwargs: dict[str, Any], *keys: str) -> str | None:
    """Return the model requested by a provider call, when it is a plain string.

    Failure events carry the *requested* model because no response exists to
    read it from. Anything that is not a usable string is dropped rather than
    guessed at.
    """
    for key in keys or ("model",):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
