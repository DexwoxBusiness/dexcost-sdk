"""Opt-in native database observations. No prices, payloads or connection strings.

MongoDB captures wire commands through PyMongo's public monitoring API (sync
and async). Redis captures logical commands through redis-py's execute_command
and executed pipelines; its internal network retries do not create extra rows.
Resource IDs must be explicit cloud IDs, not inferred from a hostname.
"""

from __future__ import annotations

import functools
import inspect
import re
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any
from weakref import WeakValueDictionary

from dexcost.context import get_current_task
from dexcost.models.event import Event

_ID = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_listeners: WeakValueDictionary[tuple[int, str], Any] = WeakValueDictionary()
_listeners_lock = threading.Lock()
_READ = frozenset(
    [
        "find",
        "count",
        "distinct",
        "getmore",
        "get",
        "mget",
        "hget",
        "hmget",
        "hgetall",
        "exists",
        "scan",
        "hscan",
        "sscan",
        "zscan",
        "smembers",
        "lrange",
        "zrange",
        "ft.search",
        "ft.aggregate",
        "json.get",
    ]
)
_WRITE = frozenset(
    [
        "insert",
        "update",
        "delete",
        "findandmodify",
        "bulkwrite",
        "set",
        "mset",
        "hset",
        "del",
        "unlink",
        "incr",
        "incrby",
        "decr",
        "decrby",
        "lpush",
        "rpush",
        "sadd",
        "zadd",
        "expire",
        "json.set",
    ]
)


def database_resource_id(billing_account_id: str, resource_id: str) -> str:
    """Same opaque account/resource identity used by server invoice mappings."""
    if not _ID.fullmatch(billing_account_id) or not _ID.fullmatch(resource_id):
        raise ValueError("Use cloud account/resource IDs (1-100 letters, digits, '.', '_' or '-')")
    return f"{billing_account_id}/{resource_id}"


def _category(command: Any) -> str:
    # Never persist arbitrary command names, arguments, exception text or replies.
    name = command.lower() if isinstance(command, str) else ""
    return "read" if name in _READ else "write" if name in _WRITE else "other"


class _Recorder:
    def __init__(self, tracker: Any, service: str, account: str, resource: str):
        self.tracker = tracker
        self.service = service
        self.resource = database_resource_id(account, resource)
        self.active = True

    def start(self) -> tuple[Any, datetime, float]:
        return get_current_task(), datetime.now(timezone.utc), time.monotonic()

    def finish(
        self, start: tuple[Any, datetime, float], category: str, quantity: int, failed: bool
    ) -> None:
        task, occurred_at, clock = start
        if task is None or not self.active or quantity < 1:
            return
        # Telemetry must not change a database result or log sensitive errors.
        with suppress(Exception):
            self.tracker.storage.insert_event(
                Event(
                    task_id=task.task_id,
                    occurred_at=occurred_at,
                    event_type="external_cost",
                    cost_confidence="unknown",
                    provider=self.service,
                    service_name="database",
                    latency_ms=max(0, int((time.monotonic() - clock) * 1000)),
                    details={
                        "attribution_component": "storage",
                        "attribution_resource_type": "endpoint",
                        "attribution_resource_id": self.resource,
                        "attribution_operation_name": f"database.{category}",
                        "attribution_operation_status": "failed" if failed else "succeeded",
                        "attribution_usage_lines": [
                            {
                                "metric": f"{self.service}.commands",
                                "unit": "Commands",
                                "quantity": str(quantity),
                            }
                        ],
                        "database_capture_version": "1",
                        "database_capture_basis": "observed_commands_not_billed_usage",
                    },
                )
            )


def mongodb_command_listener(tracker: Any, *, billing_account_id: str, resource_id: str) -> Any:
    """Pass the returned listener once to MongoClient/AsyncMongoClient event_listeners.

    Each wire command (including retry attempts and cursor getMore) counts once.
    Bulk operations count commands, not documents. Call listener.close() to stop.
    """
    from pymongo.monitoring import CommandListener

    recorder = _Recorder(tracker, "mongodb_atlas", billing_account_id, resource_id)
    cache_key = (id(tracker), recorder.resource)

    class Listener(CommandListener):
        def __init__(self) -> None:
            self.active = True
            self.pending: OrderedDict[Any, Any] = OrderedDict()
            self.lock = threading.Lock()

        def started(self, event: Any) -> None:
            if not recorder.active:
                return
            start = recorder.start()
            if start[0] is None:
                return
            key = (event.connection_id, event.request_id)
            with self.lock:
                self.pending[key] = (start, _category(event.command_name))
                if len(self.pending) > 1024:
                    self.pending.popitem(last=False)

        def _finish(self, event: Any, failed: bool) -> None:
            with self.lock:
                saved = self.pending.pop((event.connection_id, event.request_id), None)
            if saved is not None:
                recorder.finish(saved[0], saved[1], 1, failed)

        def succeeded(self, event: Any) -> None:
            reply = getattr(event, "reply", {})
            partial = isinstance(reply, dict) and bool(
                reply.get("writeErrors") or reply.get("writeConcernError")
            )
            self._finish(event, partial)

        def failed(self, event: Any) -> None:
            self._finish(event, True)

        def close(self) -> None:
            self.active = False
            recorder.active = False
            with self.lock:
                self.pending.clear()

    with _listeners_lock:
        existing = _listeners.get(cache_key)
        if existing is not None and existing.active:
            return existing
        listener = Listener()
        _listeners[cache_key] = listener
        return listener


def instrument_redis_client(
    client: Any, tracker: Any, *, billing_account_id: str, resource_id: str
) -> Any:
    """Instrument one redis-py Redis/asyncio.Redis instance; return an undo callback.

    Cluster, PubSub and custom subclasses are not covered. Pipeline commands
    are counted only when execute() runs; errors count attempted commands and
    are not evidence that writes committed. Register each client only once.
    """
    if getattr(client, "_dexcost_database_instrumented", False):
        raise ValueError("Redis client is already instrumented")
    recorder = _Recorder(tracker, "redis_cloud", billing_account_id, resource_id)
    original = client.execute_command
    original_pipeline = client.pipeline

    def wrap_call(fn: Any, category: Any, quantity: Any) -> Any:
        @functools.wraps(fn)
        def call(*args: Any, **kwargs: Any) -> Any:
            start = recorder.start()
            name, count = category(args), quantity()
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                recorder.finish(start, name, count, True)
                raise
            if inspect.isawaitable(result):

                async def awaited() -> Any:
                    try:
                        value = await result
                    except BaseException:
                        recorder.finish(start, name, count, True)
                        raise
                    recorder.finish(start, name, count, _partial_failure(value))
                    return value

                return awaited()
            recorder.finish(start, name, count, _partial_failure(result))
            return result

        return call

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        pipe = original_pipeline(*args, **kwargs)
        # Queued execute_command calls must not count until the pipeline executes.
        pipe.execute = wrap_call(pipe.execute, lambda _: "batch", lambda: len(pipe.command_stack))
        # WATCH-mode commands are sent immediately, outside the queued batch.
        if hasattr(pipe, "immediate_execute_command"):
            pipe.immediate_execute_command = wrap_call(
                pipe.immediate_execute_command,
                lambda a: _category(a[0] if a else None),
                lambda: 1,
            )
        return pipe

    client.execute_command = wrap_call(
        original, lambda a: _category(a[0] if a else None), lambda: 1
    )
    client.pipeline = pipeline
    client._dexcost_database_instrumented = True
    closed = False

    def undo() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        recorder.active = False
        client.execute_command = original
        client.pipeline = original_pipeline
        client._dexcost_database_instrumented = False
        client._dexcost_database_undo = None

    client._dexcost_database_undo = undo
    return undo


def uninstrument_redis_client(client: Any) -> None:
    """Remove this client's instrumentation (idempotent, clients stay open)."""
    undo = getattr(client, "_dexcost_database_undo", None)
    if callable(undo):
        undo()


def _partial_failure(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, BaseException) for item in value)
