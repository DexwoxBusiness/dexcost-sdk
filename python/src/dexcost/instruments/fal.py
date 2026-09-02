"""First-class metering and durable queue reconciliation for fal-client."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from decimal import Decimal
from importlib import import_module
from typing import Any

from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)
from dexcost.models.provider_job import ProviderJobStatus
from dexcost.provider_jobs import ProviderJobSession, reconcile_provider_job

_log = logging.getLogger(__name__)
_active_tracker: Any | None = None
_patched = False
_originals: dict[str, tuple[Any, str, Any]] = {}
_inside_fal: ContextVar[bool] = ContextVar("dexcost_inside_fal", default=False)


def _value(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _line(metric: str, quantity: Any, unit: str) -> ProviderUsageLine | None:
    parsed = _decimal(quantity)
    return ProviderUsageLine(metric, parsed, unit) if parsed is not None and parsed > 0 else None


def _application(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    value = kwargs.get("application")
    if not isinstance(value, str) and args:
        value = args[0]
    return value if isinstance(value, str) and value else "unknown"


def _arguments(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
    value = kwargs.get("arguments")
    if not isinstance(value, Mapping) and len(args) > 1:
        value = args[1]
    return value if isinstance(value, Mapping) else {}


def _request_id(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str | None:
    value = kwargs.get("request_id")
    if not isinstance(value, str) and len(args) > 1:
        value = args[1]
    return value if isinstance(value, str) and value else None


def _model(application: str, path: Any = "") -> str:
    name = application.strip("/") or "unknown"
    if isinstance(path, str) and path.strip("/"):
        name = f"{name}/{path.strip('/')}"
    if name.startswith("fal_ai/"):
        return name
    if not name.startswith("fal-ai/") and name != "unknown":
        name = f"fal-ai/{name}"
    return f"fal_ai/{name}"


def _media_kind(application: str, result: Any = None) -> str | None:
    images = _value(result, "images")
    if (
        isinstance(images, Sequence) and not isinstance(images, (str, bytes, bytearray))
    ) or _value(result, "image") is not None:
        return "image"
    if _value(result, "video") is not None or _value(result, "video_url") is not None:
        return "video"
    if _value(result, "audio") is not None or _value(result, "audio_url") is not None:
        return "audio"
    name = application.lower()
    if any(token in name for token in ("video", "kling", "veo", "luma")):
        return "video"
    if any(token in name for token in ("audio", "music", "speech", "tts", "whisper")):
        return "audio"
    if any(
        token in name
        for token in ("image", "flux", "stable-diffusion", "ideogram", "recraft", "imagen")
    ):
        return "image"
    return None


def _request_dimensions(
    application: str, arguments: Mapping[str, Any], path: Any = ""
) -> tuple[tuple[str, str], ...]:
    dimensions: list[tuple[str, str]] = []
    kind = _media_kind(application)
    if kind is not None:
        dimensions.append(("media_type", kind))
    if isinstance(path, str) and path.strip("/"):
        dimensions.append(("endpoint_path", path.strip("/")[:256]))
    for name in ("duration", "resolution", "aspect_ratio", "quality", "num_images"):
        value = arguments.get(name)
        if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
            dimensions.append((name, str(value)[:256]))
    return tuple(dimensions[:24])


def _provider_cost(result: Any) -> Decimal | None:
    usage = _value(result, "usage")
    metrics = _value(result, "metrics")
    for value in (
        _value(usage, "total_cost"),
        _value(usage, "cost"),
        _value(metrics, "cost"),
        _value(result, "cost"),
    ):
        parsed = _decimal(value)
        if parsed is not None:
            return parsed
    return None


def _measurement(
    result: Any,
    model: str,
    application: str,
    arguments: Mapping[str, Any] | None = None,
) -> OperationMeasurement:
    request = arguments or {}
    lines: list[ProviderUsageLine] = []
    dimensions = list(_request_dimensions(application, request))
    kind = _media_kind(application, result)
    if kind is not None and not any(key == "media_type" for key, _ in dimensions):
        dimensions.append(("media_type", kind))

    images = _value(result, "images")
    image = _value(result, "image")
    image_items: Sequence[Any] = (
        images
        if isinstance(images, Sequence) and not isinstance(images, (str, bytes, bytearray))
        else (() if image is None else (image,))
    )
    if image_items:
        count = len(image_items)
        lines.append(ProviderUsageLine("output_image_count", count, "Images"))
        first = image_items[0]
        for name in ("width", "height"):
            value = _integer(_value(first, name))
            if value is not None:
                dimensions.append((f"output_{name}", str(value)))

    video = _value(result, "video")
    video_seconds = _decimal(_value(video, "duration"))
    if video_seconds is None:
        video_seconds = _decimal(_value(result, "duration")) if kind == "video" else None
    if video_seconds is not None and video_seconds > 0:
        lines.append(ProviderUsageLine("output_video_seconds", video_seconds, "Seconds"))

    audio = _value(result, "audio")
    audio_seconds = _decimal(_value(audio, "duration"))
    if audio_seconds is None:
        audio_seconds = _decimal(_value(result, "duration")) if kind == "audio" else None
    if audio_seconds is not None and audio_seconds > 0:
        lines.append(ProviderUsageLine("output_audio_seconds", audio_seconds, "Seconds"))

    usage = _value(result, "usage")
    input_tokens = _integer(_value(usage, "prompt_tokens") or _value(usage, "input_tokens"))
    output_tokens = _integer(_value(usage, "completion_tokens") or _value(usage, "output_tokens"))
    cached_tokens = _integer(_value(usage, "cached_tokens")) or 0
    ordinary_input_tokens = (
        input_tokens - cached_tokens
        if input_tokens is not None and cached_tokens <= input_tokens
        else input_tokens
    )
    for metric, quantity in (
        ("input_tokens", ordinary_input_tokens),
        ("cache_read_input_tokens", cached_tokens),
        ("output_tokens", output_tokens),
    ):
        if quantity is not None:
            item = _line(metric, quantity, "Tokens")
            if item is not None:
                lines.append(item)
    if not lines:
        lines.append(ProviderUsageLine("request_count", 1, "Requests"))
    record_id = _value(result, "request_id")
    return OperationMeasurement(
        # fal pricing is endpoint- and account-specific.  In particular, image
        # endpoints may bill per image, per megapixel, or GPU second, while the
        # provider's billing-events API applies the final account discount.
        # Preserve every observed meter for server reconciliation without
        # allowing the bundled legacy cost map to become a second money source.
        pricing_usage={},
        usage_lines=tuple(lines),
        provider_record_id=(record_id[:256] if isinstance(record_id, str) and record_id else None),
        provider_cost_usd=_provider_cost(result),
        response_model=model,
        model_candidates=(model,),
        billing_dimensions=tuple(dict(dimensions[:24]).items()),
        task_input_tokens=input_tokens,
        task_output_tokens=output_tokens,
        task_cached_tokens=cached_tokens,
    )


def _operation_session(method: str, model: str) -> ProviderOperationSession:
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=f"fal_ai.{method}",
        provider="fal_ai",
        service="inference",
        operation=f"fal_ai.{method}",
        component="external",
        model=model,
        event_type="external_cost",
    )


class _FalStreamMeter:
    def __init__(self, model: str, application: str, arguments: Mapping[str, Any]) -> None:
        self.model = model
        self.application = application
        self.arguments = arguments
        self.latest: Any = None

    def observe(self, item: Any) -> None:
        self.latest = item

    def measurement(self) -> OperationMeasurement:
        return _measurement(self.latest, self.model, self.application, self.arguments)


def _sync_operation(
    call: Callable[[], Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
) -> Any:
    if _inside_fal.get():
        return call()
    application = _application(args, kwargs)
    arguments = _arguments(args, kwargs)
    model = _model(application, kwargs.get("path", ""))
    session = _operation_session(method, model)
    token = _inside_fal.set(True)
    try:
        result = call()
    except BaseException as exc:
        session.fail(exc)
        raise
    finally:
        _inside_fal.reset(token)
    if method == "stream" and hasattr(result, "__next__"):
        meter = _FalStreamMeter(model, application, arguments)
        session.release_context()
        return SyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
        )
    session.succeed(_measurement(result, model, application, arguments))
    return result


async def _async_operation(
    call: Callable[[], Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
) -> Any:
    if _inside_fal.get():
        return await call()
    application = _application(args, kwargs)
    arguments = _arguments(args, kwargs)
    model = _model(application, kwargs.get("path", ""))
    session = _operation_session(method, model)
    token = _inside_fal.set(True)
    try:
        result = await call()
    except BaseException as exc:
        session.fail(exc)
        raise
    finally:
        _inside_fal.reset(token)
    if method == "stream" and hasattr(result, "__anext__"):
        meter = _FalStreamMeter(model, application, arguments)
        session.release_context()
        return AsyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
        )
    session.succeed(_measurement(result, model, application, arguments))
    return result


def _async_stream_operation(
    call: Callable[[], Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Wrap fal's async-generator method without changing it into a coroutine."""
    if _inside_fal.get():
        return call()
    application = _application(args, kwargs)
    arguments = _arguments(args, kwargs)
    model = _model(application, kwargs.get("path", ""))
    session = _operation_session("stream", model)
    token = _inside_fal.set(True)
    try:
        result = call()
    except BaseException as exc:
        session.fail(exc)
        raise
    finally:
        _inside_fal.reset(token)
    meter = _FalStreamMeter(model, application, arguments)
    session.release_context()
    return AsyncProviderStream(
        result,
        session,
        observe=meter.observe,
        measurement=meter.measurement,
    )


def _sync_submit(call: Callable[[], Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if _inside_fal.get():
        return call()
    application = _application(args, kwargs)
    arguments = _arguments(args, kwargs)
    model = _model(application, kwargs.get("path", ""))
    job = ProviderJobSession(
        tracker=_active_tracker,
        task_type="fal_ai.submit",
        provider="fal_ai",
        service="queue",
        operation="fal_ai.submit",
        component="external",
        event_type="external_cost",
        resource_type="model",
        resource_id=model,
        billing_dimensions=_request_dimensions(application, arguments, kwargs.get("path", "")),
    )
    token = _inside_fal.set(True)
    try:
        result = call()
    except BaseException as exc:
        job.fail(exc)
        raise
    finally:
        _inside_fal.reset(token)
    record_id = _value(result, "request_id")
    if not isinstance(record_id, str) or not record_id:
        job.fail(ValueError("fal submit response omitted its request_id"))
        return result
    job.submit(record_id)
    return result


async def _async_submit(
    call: Callable[[], Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Any:
    if _inside_fal.get():
        return await call()
    application = _application(args, kwargs)
    arguments = _arguments(args, kwargs)
    model = _model(application, kwargs.get("path", ""))
    job = ProviderJobSession(
        tracker=_active_tracker,
        task_type="fal_ai.submit",
        provider="fal_ai",
        service="queue",
        operation="fal_ai.submit",
        component="external",
        event_type="external_cost",
        resource_type="model",
        resource_id=model,
        billing_dimensions=_request_dimensions(application, arguments, kwargs.get("path", "")),
    )
    token = _inside_fal.set(True)
    try:
        result = await call()
    except BaseException as exc:
        job.fail(exc)
        raise
    finally:
        _inside_fal.reset(token)
    record_id = _value(result, "request_id")
    if not isinstance(record_id, str) or not record_id:
        job.fail(ValueError("fal submit response omitted its request_id"))
        return result
    job.submit(record_id)
    return result


def _status(result: Any) -> ProviderJobStatus:
    name = type(result).__name__
    if name == "Queued":
        return "submitted"
    if name == "InProgress":
        return "running"
    if name == "Completed":
        return "failed" if _value(result, "error") else "succeeded"
    return "running"


def _reconcile(
    request_id: str,
    *,
    status: ProviderJobStatus,
    result: Any = None,
) -> None:
    tracker = _active_tracker
    if tracker is None:
        return
    previous = tracker._storage.get_provider_job("fal_ai", "queue", request_id)
    if previous is None:
        return
    measurement = None
    if result is not None and status == "succeeded":
        application = previous.resource_id.removeprefix("fal_ai/")
        measurement = _measurement(result, previous.resource_id, application)
    reconcile_provider_job(
        tracker=tracker,
        provider="fal_ai",
        service="queue",
        provider_record_id=request_id,
        status=status,
        measurement=measurement,
        error_type=(
            str(_value(result, "error_type"))
            if result is not None and _value(result, "error_type")
            else None
        ),
    )


def _sync_reconcile_call(
    call: Callable[[], Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
    *,
    handle_id: str | None = None,
) -> Any:
    if _inside_fal.get():
        return call()
    token = _inside_fal.set(True)
    try:
        result = call()
    finally:
        _inside_fal.reset(token)
    request_id = handle_id or _request_id(args, kwargs)
    if request_id is None:
        return result
    with suppress(Exception):
        if method == "status":
            _reconcile(request_id, status=_status(result), result=result)
        elif method in {"result", "get"}:
            _reconcile(request_id, status="succeeded", result=result)
        # A successful cancel is only CANCELLATION_REQUESTED. fal documents
        # that in-progress work may still complete, so it is not terminalized.
    return result


async def _async_reconcile_call(
    call: Callable[[], Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    method: str,
    *,
    handle_id: str | None = None,
) -> Any:
    if _inside_fal.get():
        return await call()
    token = _inside_fal.set(True)
    try:
        result = await call()
    finally:
        _inside_fal.reset(token)
    request_id = handle_id or _request_id(args, kwargs)
    if request_id is None:
        return result
    with suppress(Exception):
        if method == "status":
            _reconcile(request_id, status=_status(result), result=result)
        elif method in {"result", "get"}:
            _reconcile(request_id, status="succeeded", result=result)
    return result


def _patch(owner: Any, name: str, replacement: Any, key: str) -> None:
    original = getattr(owner, name, None)
    if callable(original):
        _originals[key] = (owner, name, original)
        setattr(
            owner,
            name,
            provider_capture_callable("fal", replacement, original),
        )


def _class_sync_wrapper(key: str, method: str) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        def call() -> Any:
            return _originals[key][2](self, *args, **kwargs)

        if method == "submit":
            return _sync_submit(call, args, kwargs)
        if method in {"status", "result", "cancel"}:
            return _sync_reconcile_call(call, args, kwargs, method)
        return _sync_operation(call, args, kwargs, method)

    return wrapper


def _class_async_wrapper(key: str, method: str) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        def call() -> Any:
            return _originals[key][2](self, *args, **kwargs)

        if method == "submit":
            return await _async_submit(call, args, kwargs)
        if method in {"status", "result", "cancel"}:
            return await _async_reconcile_call(call, args, kwargs, method)
        return await _async_operation(call, args, kwargs, method)

    return wrapper


def _class_async_stream_wrapper(key: str) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        def call() -> Any:
            return _originals[key][2](self, *args, **kwargs)

        return _async_stream_operation(call, args, kwargs)

    return wrapper


def _module_sync_wrapper(key: str, method: str) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        def call() -> Any:
            return _originals[key][2](*args, **kwargs)

        if method == "submit":
            return _sync_submit(call, args, kwargs)
        if method in {"status", "result", "cancel"}:
            return _sync_reconcile_call(call, args, kwargs, method)
        return _sync_operation(call, args, kwargs, method)

    return wrapper


def _module_async_wrapper(key: str, method: str) -> Any:
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        def call() -> Any:
            return _originals[key][2](*args, **kwargs)

        if method == "submit":
            return await _async_submit(call, args, kwargs)
        if method in {"status", "result", "cancel"}:
            return await _async_reconcile_call(call, args, kwargs, method)
        return await _async_operation(call, args, kwargs, method)

    return wrapper


def _module_async_stream_wrapper(key: str) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        def call() -> Any:
            return _originals[key][2](*args, **kwargs)

        return _async_stream_operation(call, args, kwargs)

    return wrapper


def _handle_sync_wrapper(key: str, method: str) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return _sync_reconcile_call(
            lambda: _originals[key][2](self, *args, **kwargs),
            args,
            kwargs,
            method,
            handle_id=_value(self, "request_id"),
        )

    return wrapper


def _handle_async_wrapper(key: str, method: str) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await _async_reconcile_call(
            lambda: _originals[key][2](self, *args, **kwargs),
            args,
            kwargs,
            method,
            handle_id=_value(self, "request_id"),
        )

    return wrapper


def _restore_all() -> None:
    for owner, name, original in tuple(_originals.values()):
        with suppress(Exception):
            setattr(owner, name, original)
    _originals.clear()


def instrument_fal(tracker: Any) -> None:
    """Instrument fal sync/async clients, module helpers, and queue handles."""
    global _active_tracker, _patched
    if _patched:
        raise RuntimeError("fal instrumentation is already active")
    try:
        module = import_module("fal_client")
    except ImportError as exc:
        raise ImportError(
            "fal instrumentation requires 'fal-client'; install dexcost[fal]"
        ) from exc
    _active_tracker = tracker
    try:
        for class_name, is_async in (("SyncClient", False), ("AsyncClient", True)):
            owner = getattr(module, class_name)
            for method in ("run", "subscribe", "stream", "submit", "status", "result", "cancel"):
                key = f"fal_client:{class_name}:{method}"
                replacement = (
                    _class_async_stream_wrapper(key)
                    if is_async and method == "stream"
                    else (
                        _class_async_wrapper(key, method)
                        if is_async
                        else _class_sync_wrapper(key, method)
                    )
                )
                _patch(owner, method, replacement, key)
        for name, method, is_async in (
            ("run", "run", False),
            ("subscribe", "subscribe", False),
            ("stream", "stream", False),
            ("submit", "submit", False),
            ("status", "status", False),
            ("result", "result", False),
            ("cancel", "cancel", False),
            ("run_async", "run", True),
            ("subscribe_async", "subscribe", True),
            ("stream_async", "stream", True),
            ("submit_async", "submit", True),
            ("status_async", "status", True),
            ("result_async", "result", True),
            ("cancel_async", "cancel", True),
        ):
            key = f"fal_client:module:{name}"
            replacement = (
                _module_async_stream_wrapper(key)
                if is_async and method == "stream"
                else (
                    _module_async_wrapper(key, method)
                    if is_async
                    else _module_sync_wrapper(key, method)
                )
            )
            _patch(module, name, replacement, key)
        for class_name, is_async in (
            ("SyncRequestHandle", False),
            ("AsyncRequestHandle", True),
        ):
            owner = getattr(module, class_name)
            for method in ("status", "get", "cancel"):
                key = f"fal_client:{class_name}:{method}"
                replacement = (
                    _handle_async_wrapper(key, method)
                    if is_async
                    else _handle_sync_wrapper(key, method)
                )
                _patch(owner, method, replacement, key)
    except Exception:
        _restore_all()
        _active_tracker = None
        raise
    _patched = True


def uninstrument_fal() -> None:
    """Restore exact fal-client functions and classes captured at patch time."""
    global _active_tracker, _patched
    if not _patched:
        return
    _restore_all()
    _active_tracker = None
    _patched = False


__all__ = ["instrument_fal", "uninstrument_fal"]
