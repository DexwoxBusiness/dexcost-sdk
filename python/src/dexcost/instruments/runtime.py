"""Opt-in runtime allocation evidence, never provider-billed seconds or money.

Capture client-observed work on an explicitly mapped resource. Idle capacity,
background work, disconnected clients and missing terminal callbacks are not
invented. The server clips closed intervals to each invoice allocation window.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from dexcost.context import get_current_task
from dexcost.instruments.database import database_resource_id
from dexcost.models.event import Event

_active: contextvars.ContextVar[frozenset[tuple[str, str, str]]] = contextvars.ContextVar(
    "dexcost_runtime_spans", default=frozenset()
)


def _configuration(
    service: str, account: str, resource: str, cpu: int | None, memory: int | None
) -> str:
    if service not in {"modal_compute", "e2b_sandbox"}:
        raise ValueError("Supported runtimes: modal_compute, e2b_sandbox")
    for value in (cpu, memory):
        if value is not None and (type(value) is not int or not 1 <= value <= 1_048_576):
            raise ValueError("Resource configuration must be a positive integer")
    return database_resource_id(account, resource)


def wrap_runtime_handler(
    fn: Any,
    tracker: Any,
    *,
    service_key: str,
    billing_account_id: str,
    resource_id: str,
    vcpu_count: int | None = None,
    memory_mib: int | None = None,
) -> Any:
    """Wrap sync/async work; elapsed time is an optional invoice allocation weight.

    For Modal pass its app/object ID, not an inferred hostname. For E2B use the
    sandbox ID. Use instead of, not around, the legacy monetary GPU wrapper.
    Nested wrappers for the same resource/task are suppressed. Distinct parallel
    calls remain distinct weights, not claims of exclusive physical CPU time.
    """
    resource = _configuration(service_key, billing_account_id, resource_id, vcpu_count, memory_mib)

    def start() -> Any:
        task = get_current_task()
        if task is None:
            return None
        key = (service_key, resource, str(task.task_id))
        if key in _active.get():
            return None
        return task, key, datetime.now(timezone.utc), time.monotonic_ns()

    def finish(saved: Any, failed: bool) -> None:
        if saved is None:
            return
        task, _key, started, clock = saved
        with suppress(Exception):
            milliseconds = (time.monotonic_ns() - clock) // 1_000_000
            if not 0 < milliseconds <= 86_400_000:
                return
            seconds = f"{milliseconds // 1000}.{milliseconds % 1000:03d}"
            dimensions = []
            for key, value in (
                ("runtime.vcpu_count", vcpu_count),
                ("runtime.memory_mib", memory_mib),
            ):
                if value is not None:
                    dimensions.append(
                        {"key": key, "value": {"type": "integer", "value": str(value)}}
                    )
            tracker.storage.insert_event(
                Event(
                    task_id=task.task_id,
                    occurred_at=started + timedelta(milliseconds=milliseconds),
                    event_type="external_cost",
                    cost_confidence="unknown",
                    provider=service_key,
                    service_name="runtime",
                    latency_ms=milliseconds,
                    details={
                        "attribution_component": "compute",
                        "attribution_resource_type": "instance",
                        "attribution_resource_id": resource,
                        "attribution_operation_name": "runtime.work",
                        "attribution_operation_status": "failed" if failed else "succeeded",
                        "attribution_usage_duration_seconds": seconds,
                        "attribution_usage_lines": [
                            {
                                "metric": "runtime.task_seconds",
                                "unit": "Seconds",
                                "quantity": seconds,
                            }
                        ],
                        "attribution_dimensions": dimensions,
                        "runtime_capture_basis": "observed_task_wall_time_not_billed_runtime",
                    },
                )
            )

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_call(*args: Any, **kwargs: Any) -> Any:
            saved = start()
            token = _active.set(_active.get() | ({saved[1]} if saved else set()))
            failed = True
            result: Any = None
            try:
                result = await fn(*args, **kwargs)
                failed = False
                return result
            finally:
                _active.reset(token)
                if failed or not isinstance(result, (Iterator, AsyncIterator)):
                    finish(saved, failed)

        return async_call

    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> Any:
        saved = start()
        token = _active.set(_active.get() | ({saved[1]} if saved else set()))
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            finish(saved, True)
            raise
        finally:
            _active.reset(token)
        if inspect.isawaitable(result):

            async def awaited() -> Any:
                nested = _active.set(_active.get() | ({saved[1]} if saved else set()))
                failed = True
                value: Any = None
                try:
                    value = await result
                    failed = False
                    return value
                finally:
                    _active.reset(nested)
                    if failed or not isinstance(value, (Iterator, AsyncIterator)):
                        finish(saved, failed)

            return awaited()
        # Iterator creation is not completed stream consumption.
        if not isinstance(result, (Iterator, AsyncIterator)):
            finish(saved, False)
        return result

    return call


def instrument_e2b_sandbox(
    sandbox: Any,
    tracker: Any,
    *,
    billing_account_id: str,
    vcpu_count: int | None = None,
    memory_mib: int | None = None,
) -> Any:
    """Return an opt-in facade for commands.run and run_code, sync or async.

    Other methods delegate to the original sandbox. No API is called at setup;
    get_info.end_at is a timeout, never billed usage. Background commands are
    deliberately not timed because their return is not execution completion.
    """
    resource_id = sandbox.sandbox_id
    _configuration("e2b_sandbox", billing_account_id, resource_id, vcpu_count, memory_mib)
    if isinstance(sandbox, _SandboxFacade):
        raise ValueError("Sandbox is already instrumented")
    return _SandboxFacade(
        sandbox, tracker, billing_account_id, resource_id, vcpu_count, memory_mib
    )


def uninstrument_e2b_sandbox(sandbox: Any) -> None:
    """Stop a returned facade's capture, idempotently; never kill the sandbox."""
    if isinstance(sandbox, _SandboxFacade):
        sandbox.close()


class _SandboxFacade:
    def __init__(
        self,
        sandbox: Any,
        tracker: Any,
        account: str,
        resource: str,
        cpu: int | None,
        memory: int | None,
    ):
        self._sandbox = sandbox
        self._tracker = tracker
        self._config: dict[str, Any] = dict(
            service_key="e2b_sandbox",
            billing_account_id=account,
            resource_id=resource,
            vcpu_count=cpu,
            memory_mib=memory,
        )
        self._closed = False

    def close(self) -> None:
        """Stop capture only; never terminate a provider resource."""
        self._closed = True

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._sandbox, name)
        if name == "commands":
            return _CommandsFacade(value, self)
        if name == "run_code":
            return self._wrap(value)
        return value

    def _wrap(self, fn: Any) -> Any:
        wrapped = wrap_runtime_handler(fn, self._tracker, **self._config)

        def call(*args: Any, **kwargs: Any) -> Any:
            if self._closed or kwargs.get("background"):
                return fn(*args, **kwargs)
            return wrapped(*args, **kwargs)

        return call


class _CommandsFacade:
    def __init__(self, commands: Any, owner: _SandboxFacade):
        self._commands, self._owner = commands, owner

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._commands, name)
        return self._owner._wrap(value) if name == "run" else value
