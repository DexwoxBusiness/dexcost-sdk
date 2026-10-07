"""Single-region PAYG command evidence. Only reconciled server invoices supply money."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from dexcost.context import get_current_task
from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments.database import database_resource_id
from dexcost.instruments.vector_database import _now
from dexcost.models.event import Event

_COMMANDS = frozenset({"get", "set", "mget", "delete", "exists", "incr"})


def _host(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname


def upstash_redis_resource_id(
    billing_account_id: str, region: str, database_id: str, endpoint_host: str
) -> str:
    """Account/SHA256(JSON([provider,region,database,host,payg_single_region]))."""
    database_resource_id(billing_account_id, database_id)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", region) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,62}\.upstash\.io", endpoint_host
    ):
        raise ValueError("An explicit region and exact hosted Upstash endpoint are required")
    identity = json.dumps(
        ["upstash_redis", region, database_id, endpoint_host, "payg_single_region"],
        separators=(",", ":"),
    )
    return database_resource_id(billing_account_id, hashlib.sha256(identity.encode()).hexdigest())


class _Proxy:
    def __init__(self, target: Any, overrides: dict[str, Any]) -> None:
        self._target, self._overrides = target, overrides

    def __getattr__(self, name: str) -> Any:
        return self._overrides[name] if name in self._overrides else getattr(self._target, name)


class _UpstashFacade:
    def __init__(
        self, client: Any, tracker: Any, account: str, region: str, database: str, host: str
    ) -> None:
        self._resource = upstash_redis_resource_id(account, region, database, host)
        self._client, self._tracker, self._region, self._host = client, tracker, region, host
        self._active = True

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        if name not in _COMMANDS or not callable(native):
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task, start = get_current_task(), _now()
            eligible = False
            with suppress(Exception):
                http = self._client._http
                eligible = (
                    self._active
                    and task is not None
                    and _host(self._client._url) == self._host
                    and type(http._retries) is int
                    and http._retries == 0
                    and http._client.follow_redirects is False
                    and native.__func__.__module__.startswith("upstash_redis.commands")
                    and self._client.execute.__func__.__module__
                    in ("upstash_redis.client", "upstash_redis.asyncio.client")
                    and http.execute.__func__.__module__ == "upstash_redis.http"
                )
            if not eligible:
                return native(*args, **kwargs)
            assert task is not None
            observed = {"attempts": 0, "valid": False}
            original_post = http._client.post
            expected = "DEL" if name == "delete" else name.upper()

            def post(*a: Any, **kw: Any) -> Any:
                observed["attempts"] += 1
                response = original_post(*a, **kw)

                def inspect_response(value: Any) -> Any:
                    with suppress(Exception):
                        command = kw.get("json")
                        url = a[0] if a else kw.get("url")
                        body = value.json()
                        observed["valid"] = (
                            _host(str(url)) == self._host
                            and _host(str(value.url)) == self._host
                            and value.status_code == 200
                            and not value.history
                            and isinstance(command, list)
                            and len(command) >= 2
                            and command[0] == expected
                            and isinstance(body, dict)
                            and "result" in body
                            and body.get("error") is None
                        )
                    return value

                if inspect.isawaitable(response):

                    async def await_response() -> Any:
                        return inspect_response(await response)

                    return await_response()
                return inspect_response(response)

            http_proxy = _Proxy(http, {"_client": _Proxy(http._client, {"post": post})})
            http_proxy._overrides["execute"] = http.execute.__func__.__get__(http_proxy)
            receiver = _Proxy(self._client, {"_http": http_proxy})
            receiver._overrides["execute"] = self._client.execute.__func__.__get__(receiver)

            def capture(value: Any) -> Any:
                with suppress(Exception):
                    if self._active and observed["attempts"] == 1 and observed["valid"]:
                        end = max(_now(end=True), start + timedelta(milliseconds=1))
                        milliseconds = (end - start) // timedelta(milliseconds=1)
                        self._tracker.storage.insert_event(
                            Event(
                                task_id=task.task_id,
                                occurred_at=end,
                                provider="upstash_redis",
                                service_name="redis",
                                event_type="external_cost",
                                cost_confidence="unknown",
                                latency_ms=milliseconds,
                                details={
                                    "region": self._region,
                                    "attribution_component": "storage",
                                    "attribution_resource_type": "endpoint",
                                    "attribution_resource_id": self._resource,
                                    "attribution_operation_name": "redis.command",
                                    "attribution_operation_status": "succeeded",
                                    "attribution_usage_duration_seconds": str(
                                        Decimal(milliseconds) / 1000
                                    ),
                                    "attribution_usage_lines": [
                                        {
                                            "metric": "upstash_redis.payg_single_region_commands",
                                            "quantity": "1",
                                            "unit": "Commands",
                                        }
                                    ],
                                    "redis_capture_basis": (
                                        "single_acknowledged_command_not_invoice"
                                    ),
                                },
                            )
                        )
                return value

            result = native.__func__.__get__(receiver)(*args, **kwargs)
            if inspect.isawaitable(result):

                async def await_result() -> Any:
                    return capture(await result)

                return await_result()
            return capture(result)

        return provider_capture_callable("upstash_redis", invoke, native)


def instrument_upstash_redis(
    client: Any,
    tracker: Any,
    *,
    billing_account_id: str,
    region: str,
    database_id: str,
    endpoint_host: str,
    billing_plan: str,
    topology: str,
) -> Any:
    """Return a native facade for explicit PAYG/single_region binding (rest_retries=0).

    Binding facts are caller attestations, not account discovery. Raw clients,
    pipelines, scripts, operational/free commands and unknown routes stay unpriced.
    Nothing changes the provider client's retry, redirect or execution behavior.
    """
    if billing_plan != "pay_as_you_go" or topology != "single_region":
        raise ValueError("Only explicit pay_as_you_go and single_region bindings are admitted")
    return _UpstashFacade(client, tracker, billing_account_id, region, database_id, endpoint_host)


def uninstrument_upstash_redis(client: Any) -> None:
    """Stop only this facade; never close the underlying database client."""
    if isinstance(client, _UpstashFacade):
        client._active = False
