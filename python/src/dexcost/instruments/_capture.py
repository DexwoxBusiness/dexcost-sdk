"""Context-local ownership for one logical provider operation.

Gateway SDKs such as LiteLLM invoke provider SDKs internally. If both layers
are instrumented, the outer public call owns attribution and nested adapters
must pass through without recording a second event. Context variables preserve
that ownership across threads copied by Dexcost and across awaited coroutines.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from inspect import isawaitable
from typing import Any

_capture_owner: ContextVar[str | None] = ContextVar("dexcost_provider_capture_owner", default=None)


def current_provider_capture_owner() -> str | None:
    """Return the outermost adapter recording the current provider operation."""
    return _capture_owner.get()


@contextmanager
def provider_capture_scope(owner: str) -> Generator[bool, None, None]:
    """Claim one logical provider operation for the outermost adapter.

    The yielded boolean is ``False`` for a nested adapter. Callers must still
    invoke the provider SDK in that case, but must bypass their own recording.
    """
    if _capture_owner.get() is not None:
        yield False
        return
    token = _capture_owner.set(owner)
    try:
        yield True
    finally:
        _capture_owner.reset(token)


def provider_capture_wrapper(owner: str, wrapper: Any) -> Any:
    """Wrap a wrapt-compatible adapter with outermost capture ownership."""

    def capture(
        wrapped: Any,
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        with provider_capture_scope(owner) as claimed:
            result = (
                wrapper(wrapped, instance, args, kwargs) if claimed else wrapped(*args, **kwargs)
            )
        if not isawaitable(result):
            return result

        async def await_result() -> Any:
            if not claimed:
                return await result
            with provider_capture_scope(owner):
                return await result

        return await_result()

    return capture


def provider_capture_callable(owner: str, replacement: Any, original: Any) -> Any:
    """Apply outermost ownership to a directly monkey-patched callable."""

    def capture(*args: Any, **kwargs: Any) -> Any:
        with provider_capture_scope(owner) as claimed:
            result = replacement(*args, **kwargs) if claimed else original(*args, **kwargs)
        if not isawaitable(result):
            return result

        async def await_result() -> Any:
            if not claimed:
                return await result
            with provider_capture_scope(owner):
                return await result

        return await_result()

    return capture


__all__ = [
    "current_provider_capture_owner",
    "provider_capture_callable",
    "provider_capture_scope",
    "provider_capture_wrapper",
]
