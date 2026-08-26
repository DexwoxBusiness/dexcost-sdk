"""Privacy-safe instrumentation for the official Ollama Python client.

The adapter covers the package-level singleton, ``Client`` and ``AsyncClient``
surfaces.  It records only provider usage quantities and bounded identifiers;
prompts, messages, generated text, images, embeddings, search results, fetched
pages, tool arguments, and error messages are never retained.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments._provider_metering import (
    AsyncProviderStream,
    OperationMeasurement,
    OperationStatus,
    ProviderOperationSession,
    ProviderUsageLine,
    SyncProviderStream,
)

_active_tracker: Any = None
_patched = False
_originals: dict[str, tuple[Any, str, Any]] = {}

_INFERENCE_OPERATIONS = frozenset({"chat", "generate"})
_EMBEDDING_OPERATIONS = frozenset({"embed", "embeddings"})
_WEB_OPERATIONS = frozenset({"web_search", "web_fetch"})
_ALL_OPERATIONS = (
    *sorted(_INFERENCE_OPERATIONS),
    *sorted(_EMBEDDING_OPERATIONS),
    *_WEB_OPERATIONS,
)


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _positive_line(metric: str, value: int | None, unit: str) -> ProviderUsageLine | None:
    if value is None or value <= 0:
        return None
    return ProviderUsageLine(metric, value, unit)


def _safe_model(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str | None:
    value = kwargs.get("model")
    if not isinstance(value, str) and args:
        value = args[0]
    return value if isinstance(value, str) and value else None


def _target_base_url(target: Any) -> str | None:
    client = getattr(target, "_client", None)
    base_url = getattr(client, "base_url", None)
    return str(base_url) if base_url is not None else None


def _is_cloud_call(target: Any, model: str | None) -> bool:
    base_url = _target_base_url(target)
    if base_url:
        try:
            hostname = (urlparse(base_url).hostname or "").lower()
        except ValueError:
            hostname = ""
        if hostname == "ollama.com" or hostname.endswith(".ollama.com"):
            return True
    normalized_model = (model or "").lower()
    return normalized_model.endswith("-cloud") or normalized_model.endswith(":cloud")


def _canonical_model(model: str | None, *, cloud: bool) -> str:
    name = model or "unknown"
    # Avoid the pricing engine's provider-prefix fallback for hosted calls: a
    # direct Ollama Cloud request must never inherit a similarly named local or
    # third-party model rate.
    return f"ollama-cloud:{name}" if cloud else f"ollama/{name}"


def _tool_call_count(response: Any) -> int | None:
    message = _value(response, "message")
    calls = _value(message, "tool_calls")
    if isinstance(calls, (list, tuple)):
        return len(calls)
    return None


class _OllamaUsageMeter:
    def __init__(self, operation: str, model: str, *, cloud: bool) -> None:
        self.operation = operation
        self.model = model
        self.cloud = cloud
        self._latest: Any = None
        self._done_seen = False
        self._done_reason: str | None = None

    def observe(self, response: Any) -> None:
        self._latest = response
        response_model = _value(response, "model")
        if isinstance(response_model, str) and response_model:
            self.model = _canonical_model(response_model, cloud=self.cloud)
        done = _value(response, "done")
        if done is True:
            self._done_seen = True
            reason = _value(response, "done_reason")
            self._done_reason = reason if isinstance(reason, str) else None

    def status(self, *, require_done: bool) -> OperationStatus:
        reason = (self._done_reason or "").lower()
        if reason in {"cancelled", "canceled", "aborted"}:
            return "cancelled"
        if reason in {"error", "failed", "failure"}:
            return "failed"
        if require_done and not self._done_seen:
            return "unknown"
        return "succeeded"

    def measurement(self) -> OperationMeasurement:
        response = self._latest
        input_tokens = _non_negative_int(_value(response, "prompt_eval_count"))
        output_tokens = _non_negative_int(_value(response, "eval_count"))
        total_duration = _non_negative_int(_value(response, "total_duration"))
        load_duration = _non_negative_int(_value(response, "load_duration"))
        prompt_duration = _non_negative_int(_value(response, "prompt_eval_duration"))
        eval_duration = _non_negative_int(_value(response, "eval_duration"))
        image_count = 1 if _value(response, "image") not in (None, "") else 0
        completed_steps = _non_negative_int(_value(response, "completed"))
        total_steps = _non_negative_int(_value(response, "total"))
        tool_calls = _tool_call_count(response)

        lines = tuple(
            line
            for line in (
                _positive_line("input_tokens", input_tokens, "Tokens"),
                _positive_line("output_tokens", output_tokens, "Tokens"),
                _positive_line("total_duration_ns", total_duration, "Nanoseconds"),
                _positive_line("load_duration_ns", load_duration, "Nanoseconds"),
                _positive_line("prompt_eval_duration_ns", prompt_duration, "Nanoseconds"),
                _positive_line("eval_duration_ns", eval_duration, "Nanoseconds"),
                _positive_line("output_image_count", image_count, "Images"),
                _positive_line("completed_generation_steps", completed_steps, "Steps"),
                _positive_line("total_generation_steps", total_steps, "Steps"),
                _positive_line("tool_call_count", tool_calls, "Calls"),
            )
            if line is not None
        )
        pricing_usage: dict[str, int] = {}
        if input_tokens is not None:
            pricing_usage["input_tokens"] = input_tokens
        if output_tokens is not None and self.operation in _INFERENCE_OPERATIONS:
            pricing_usage["output_tokens"] = output_tokens
        if image_count:
            pricing_usage["output_image_count"] = image_count
        return OperationMeasurement(
            pricing_usage=pricing_usage,
            usage_lines=lines,
            response_model=self.model,
            model_candidates=(self.model,),
            task_input_tokens=input_tokens,
            task_output_tokens=(
                output_tokens if self.operation in _INFERENCE_OPERATIONS else None
            ),
        )


def _web_measurement(operation: str, response: Any) -> OperationMeasurement:
    if operation == "web_search":
        results = _value(response, "results")
        result_count = len(results) if isinstance(results, (list, tuple)) else None
        lines = [
            ProviderUsageLine("query_count", 1, "Queries"),
            ProviderUsageLine("request_count", 1, "Requests"),
        ]
        if result_count is not None:
            lines.append(ProviderUsageLine("result_count", result_count, "Results"))
        return OperationMeasurement(
            pricing_usage={"query_count": 1},
            usage_lines=tuple(lines),
            response_model="ollama-web-search",
        )

    links = _value(response, "links")
    link_count = len(links) if isinstance(links, (list, tuple)) else None
    lines = [ProviderUsageLine("request_count", 1, "Requests")]
    if link_count is not None:
        lines.append(ProviderUsageLine("link_count", link_count, "Links"))
    return OperationMeasurement(
        pricing_usage={"request_count": 1},
        usage_lines=tuple(lines),
        response_model="ollama-web-fetch",
    )


def _new_session(operation: str, target: Any, model: str | None) -> ProviderOperationSession:
    cloud = _is_cloud_call(target, model)
    is_web = operation in _WEB_OPERATIONS
    canonical_model = (
        f"ollama-{operation.replace('_', '-')}" if is_web else _canonical_model(model, cloud=cloud)
    )
    return ProviderOperationSession(
        tracker=_active_tracker,
        task_type=f"ollama.{operation}",
        provider="ollama",
        service=("ollama_web" if is_web else "ollama_cloud" if cloud else "ollama_local"),
        operation=f"ollama.{operation}",
        component="external" if is_web else "llm",
        model=canonical_model,
        event_type="external_cost" if is_web else "llm_call",
    )


def _invoke_sync(
    original: Any,
    call_args: tuple[Any, ...],
    public_args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    operation: str,
    target: Any,
) -> Any:
    requested_model = _safe_model(public_args, kwargs)
    session = _new_session(operation, target, requested_model)
    try:
        result = original(*call_args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise

    if operation in _WEB_OPERATIONS:
        session.succeed(_web_measurement(operation, result))
        return result

    cloud = _is_cloud_call(target, requested_model)
    meter = _OllamaUsageMeter(
        operation,
        _canonical_model(requested_model, cloud=cloud),
        cloud=cloud,
    )
    if operation in _INFERENCE_OPERATIONS and kwargs.get("stream") is True:
        session.release_context()
        return SyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
            completion_status=lambda: meter.status(require_done=True),
        )

    meter.observe(result)
    session.finish(
        meter.measurement(),
        meter.status(require_done=operation in _INFERENCE_OPERATIONS),
    )
    return result


async def _invoke_async(
    original: Any,
    call_args: tuple[Any, ...],
    public_args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    operation: str,
    target: Any,
) -> Any:
    requested_model = _safe_model(public_args, kwargs)
    session = _new_session(operation, target, requested_model)
    try:
        result = await original(*call_args, **kwargs)
    except BaseException as exc:
        session.fail(exc)
        raise

    if operation in _WEB_OPERATIONS:
        session.succeed(_web_measurement(operation, result))
        return result

    cloud = _is_cloud_call(target, requested_model)
    meter = _OllamaUsageMeter(
        operation,
        _canonical_model(requested_model, cloud=cloud),
        cloud=cloud,
    )
    if operation in _INFERENCE_OPERATIONS and kwargs.get("stream") is True:
        session.release_context()
        return AsyncProviderStream(
            result,
            session,
            observe=meter.observe,
            measurement=meter.measurement,
            completion_status=lambda: meter.status(require_done=True),
        )

    meter.observe(result)
    session.finish(
        meter.measurement(),
        meter.status(require_done=operation in _INFERENCE_OPERATIONS),
    )
    return result


def _sync_method_wrapper(key: str, operation: str) -> Any:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        original = _originals[key][2]
        return _invoke_sync(
            original,
            (self, *args),
            args,
            kwargs,
            operation=operation,
            target=self,
        )

    return wrapper


def _async_method_wrapper(key: str, operation: str) -> Any:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        original = _originals[key][2]
        return await _invoke_async(
            original,
            (self, *args),
            args,
            kwargs,
            operation=operation,
            target=self,
        )

    return wrapper


def _module_wrapper(key: str, operation: str) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        original = _originals[key][2]
        target = getattr(original, "__self__", None)
        return _invoke_sync(
            original,
            args,
            args,
            kwargs,
            operation=operation,
            target=target,
        )

    return wrapper


def _patch(owner: Any, name: str, replacement: Any, key: str) -> None:
    original = getattr(owner, name, None)
    if not callable(original):
        return
    _originals[key] = (owner, name, original)
    setattr(
        owner,
        name,
        provider_capture_callable("ollama", replacement, original),
    )


def _restore_all() -> None:
    for owner, name, original in tuple(_originals.values()):
        with suppress(Exception):
            setattr(owner, name, original)
    _originals.clear()


def instrument_ollama(tracker: Any) -> None:
    """Instrument current official Ollama module/client operations."""
    global _active_tracker, _patched
    if _patched:
        raise RuntimeError("Ollama instrumentation is already active")
    try:
        import ollama
        from ollama import AsyncClient, Client
    except ImportError as exc:
        raise ImportError(
            "Ollama instrumentation requires the 'ollama' package; " "install dexcost[ollama]"
        ) from exc

    _active_tracker = tracker
    try:
        for operation in _ALL_OPERATIONS:
            sync_key = f"client.{operation}"
            _patch(Client, operation, _sync_method_wrapper(sync_key, operation), sync_key)

            async_key = f"async_client.{operation}"
            _patch(
                AsyncClient,
                operation,
                _async_method_wrapper(async_key, operation),
                async_key,
            )

            module_key = f"module.{operation}"
            _patch(
                ollama,
                operation,
                _module_wrapper(module_key, operation),
                module_key,
            )
    except Exception:
        _restore_all()
        _active_tracker = None
        raise
    _patched = True


def uninstrument_ollama() -> None:
    """Restore the exact module and classes captured during instrumentation."""
    global _active_tracker, _patched
    _restore_all()
    _active_tracker = None
    _patched = False


__all__ = ["instrument_ollama", "uninstrument_ollama"]
