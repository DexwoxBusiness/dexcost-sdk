"""Opt-in You.com base Search evidence, never query/result content or SDK prices."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from contextlib import suppress
from copy import copy
from datetime import datetime, timezone
from types import MethodType
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from dexcost.context import get_current_task
from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments.database import database_resource_id
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobUsageLine,
    provider_job_event_id,
)

_HOSTS = {"api.you.com", "ydc-index.io"}
_BASE = {
    "query",
    "count",
    "freshness",
    "offset",
    "country",
    "language",
    "safesearch",
    "knowledge",
    "include_domains",
    "exclude_domains",
    "boost_domains",
    "crawl_timeout",
}
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.I
)


def _now() -> datetime:
    value = datetime.now(timezone.utc)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def _endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("An exact direct You.com HTTPS endpoint is required")
    return f"https://{parsed.hostname}"


def _base_request(request: Any, endpoint: str) -> bool:
    url = urlsplit(str(request.url))
    if (
        _endpoint(f"{url.scheme}://{url.netloc}") != endpoint
        or url.path != "/v1/search"
        or url.fragment
        or request.headers.get("host", url.hostname) not in (url.hostname, f"{url.hostname}:443")
        or not request.headers.get("x-api-key")
        or any(
            key.lower() in {"authorization", "payment-signature", "x-payment", "x-forwarded-host"}
            for key in request.headers
        )
    ):
        return False
    if request.method == "POST":
        if (
            url.query
            or request.headers.get("content-type", "").split(";")[0] != "application/json"
        ):
            return False
        pairs = json.loads(request.content, object_pairs_hook=list)
        if not isinstance(pairs, list) or any(not isinstance(pair, tuple) for pair in pairs):
            return False
    elif request.method == "GET":
        pairs = parse_qsl(url.query, keep_blank_values=True)
    else:
        return False
    keys = [key for key, _ in pairs]
    values = dict(pairs)
    if len(keys) != len(set(keys)) or not set(keys) <= _BASE:
        return False  # Unknown/add-on parameters are not silently treated as base Search.
    count = values.get("count", 10)
    if request.method == "GET" and isinstance(count, str) and count.isdigit():
        count = int(count)
    return (
        isinstance(values.get("query"), str)
        and bool(values["query"])
        and type(count) is int
        and 1 <= count <= 100
        and values.get("knowledge") in (None, "core")
    )


class _Transport:
    def __init__(self, native: Any, observe: Any) -> None:
        self._native, self._observe = native, observe

    def __getattr__(self, name: str) -> Any:
        return getattr(self._native, name)

    def send(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        result = self._native.send(request, *args, **kwargs)
        if inspect.isawaitable(result):

            async def awaited() -> Any:
                response = await result
                self._observe(request, response)
                return response

            return awaited()
        self._observe(request, result)
        return result


class _CallProxy:
    """A call-local SDK receiver: retain native methods, hooks and transports."""

    def __init__(self, native: Any, configuration: Any) -> None:
        self._native, self.sdk_configuration = native, configuration

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._native, name)
        if inspect.ismethod(value) and value.__self__ is self._native:
            return MethodType(value.__func__, self)
        return value


class _SearchCall:
    def __init__(self, native: Any, invoke: Any) -> None:
        self._native, self._invoke = native, invoke

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._native, name)  # Deprecated/raw interfaces remain native, uncaptured.


class _YouSearch:
    def __init__(self, client: Any, tracker: Any, account: str, endpoint: str, tier: str):
        database_resource_id(account, "validation")
        if tier not in {"paid", "free", "unknown"}:
            raise ValueError("Explicit paid, free or unknown public billing tier is required")
        self._client, self._tracker, self._account = client, tracker, account
        self._endpoint, self._tier, self._active = _endpoint(endpoint), tier, True

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        if name not in {"search", "search_async"} or not callable(native):
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task, started = get_current_task(), _now()
            if not self._active or task is None:
                return native(*args, **kwargs)
            overrides = kwargs.get("http_headers")
            if overrides and any(
                str(key).lower()
                in {
                    "authorization",
                    "x-api-key",
                    "host",
                    "x-forwarded-host",
                    "payment-signature",
                    "x-payment",
                }
                for key in overrides
            ):
                return native(*args, **kwargs)
            identifiers: list[str] = []

            def observe(request: Any, response: Any) -> None:
                with suppress(Exception):
                    if (
                        response.status_code != 200
                        or response.history
                        or str(response.url) != str(request.url)
                        or not _base_request(request, self._endpoint)
                    ):
                        return
                    data = response.json()
                    identifier = data.get("metadata", {}).get("search_uuid")
                    if (
                        isinstance(data.get("results"), dict)
                        and isinstance(identifier, str)
                        and _UUID.fullmatch(identifier)
                    ):
                        identifiers.append(identifier.lower())

            # Do not modify the original SDK or its HTTP clients; parallel calls have
            # independent receivers. The original SearchShim's public API is retained.
            try:
                configuration = copy(self._client.sdk_configuration)
                configuration.client = _Transport(configuration.client, observe)
                configuration.async_client = _Transport(configuration.async_client, observe)
                receiver = _CallProxy(self._client, configuration)
                if name == "search":
                    if not hasattr(native, "_you"):
                        return native(*args, **kwargs)
                    method = copy(native)
                    method._you = receiver
                else:
                    method = getattr(receiver, name)
            except (AttributeError, TypeError):
                return native(*args, **kwargs)

            def finish(result: Any) -> Any:
                with suppress(Exception):
                    if self._active:
                        for identifier in set(identifiers):
                            record = hashlib.sha256(
                                json.dumps(
                                    [self._account, identifier], separators=(",", ":")
                                ).encode()
                            ).hexdigest()
                            if (
                                self._tracker.storage.get_provider_job("you_com", "search", record)
                                is None
                            ):
                                self._tracker.storage.insert_provider_job_revision(
                                    ProviderJobRevision(
                                        event_id=provider_job_event_id(
                                            "you_com", "search", record
                                        ),
                                        revision=1,
                                        task_id=task.task_id,
                                        provider="you_com",
                                        service="search",
                                        provider_record_id=record,
                                        operation="search.base",
                                        component="external",
                                        event_type="external_cost",
                                        resource_type="sku",
                                        resource_id="base",
                                        status="succeeded",
                                        submitted_at=started,
                                        observed_at=_now(),
                                        billing_dimensions=(
                                            ("you_search_billing_lane", "public_payg_base"),
                                        )
                                        if self._tier == "paid"
                                        else (),
                                        usage=(
                                            ProviderJobUsageLine(
                                                "service.request_count", 1, "Requests"
                                            ),
                                        ),
                                    )
                                )
                return result

            result = method(*args, **kwargs)
            if inspect.isawaitable(result):

                async def awaited() -> Any:
                    return finish(await result)

                return awaited()
            return finish(result)

        wrapped = provider_capture_callable("you_com", invoke, native)
        return _SearchCall(native, wrapped) if name == "search" else wrapped


def instrument_you_search(
    client: Any,
    tracker: Any,
    *,
    billing_account_id: str,
    endpoint: str,
    billing_tier: Literal["paid", "free", "unknown"],
) -> Any:
    """Wrap You 3.5 search/search_async; caller attests this client's account/tier.

    Paid means public-PAYG gross eligibility, not credits, discounts or actual cash.
    Use a new facade after changing credentials/account. No credentials are retained.
    Only direct successful base Search is captured; legacy shims/add-ons are excluded.
    """
    if isinstance(client, _YouSearch):
        if (
            client._tracker is not tracker
            or client._account != billing_account_id
            or client._endpoint != _endpoint(endpoint)
            or client._tier != billing_tier
        ):
            raise ValueError(
                "An existing You Search facade cannot be rebound; use the native client"
            )
        return client
    return _YouSearch(client, tracker, billing_account_id, endpoint, billing_tier)


def uninstrument_you_search(client: Any) -> None:
    """Stop this facade's capture without closing the provider client."""
    if isinstance(client, _YouSearch):
        client._active = False
