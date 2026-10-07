"""Hosted vector response meters; reconciled server invoices alone supply money."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

from dexcost.context import get_current_task
from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments.database import database_resource_id
from dexcost.instruments.vector_database import _field, _now
from dexcost.models.event import Event

Provider = Literal["qdrant_cloud", "milvus_zilliz"]


def _endpoint(value: Any, ports: tuple[int | None, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if (
        parsed.scheme != "https"
        or parsed.port not in ports
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname


def cloud_vector_resource_id(
    provider: Provider, billing_account_id: str, region: str, cluster_host: str
) -> str:
    """Account/SHA256(JSON([provider,region,host])), shared with TypeScript."""
    database_resource_id(billing_account_id, "validation")
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", region)
        or _endpoint(cluster_host, (None,)) != cluster_host
        or len(cluster_host) > 253
    ):
        raise ValueError("An explicit region and lowercase hosted cluster hostname are required")
    pattern = (
        r"[a-z0-9][a-z0-9.-]*\.cloud\.qdrant\.io"
        if provider == "qdrant_cloud"
        else rf"[a-z0-9-]+\.serverless\.{re.escape(region)}\.vectordb\.zillizcloud\.com"
    )
    if provider not in ("qdrant_cloud", "milvus_zilliz") or not re.fullmatch(
        pattern, cluster_host
    ):
        raise ValueError("Only a bound hosted Qdrant or Zilliz serverless endpoint is supported")
    identity = json.dumps([provider, region, cluster_host], separators=(",", ":"))
    return database_resource_id(billing_account_id, hashlib.sha256(identity.encode()).hexdigest())


def _quantity(value: Any) -> str | None:
    if type(value) is int and 0 <= value <= 9007199254740991:
        return str(value)
    if (
        isinstance(value, str)
        and re.fullmatch(r"0|[1-9][0-9]*", value)
        and int(value) <= 9007199254740991
    ):
        return value
    return None


class _Proxy:
    """Call-local delegation, not a clone with a provider destructor or shared mutation."""

    def __init__(self, target: Any, overrides: dict[str, Any]) -> None:
        self._target, self._overrides = target, overrides

    def __getattr__(self, name: str) -> Any:
        return self._overrides[name] if name in self._overrides else getattr(self._target, name)


class _CloudVectorFacade:
    def __init__(
        self, client: Any, tracker: Any, provider: Provider, account: str, region: str, host: str
    ) -> None:
        self._resource = cloud_vector_resource_id(provider, account, region, host)
        self._client, self._tracker, self._provider = client, tracker, provider
        self._region, self._host, self._active = region, host, True

    def _emit(self, task: Any, started: Any, value: str) -> None:
        if value == "0":
            return  # Usage lines must be positive; zero usage does not assert zero money.
        ended = max(_now(end=True), started + timedelta(milliseconds=1))
        milliseconds = (ended - started) // timedelta(milliseconds=1)
        qdrant = self._provider == "qdrant_cloud"
        self._tracker.storage.insert_event(
            Event(
                task_id=task.task_id,
                occurred_at=ended,
                provider=self._provider,
                service_name="vector_database",
                event_type="external_cost",
                cost_confidence="unknown",
                latency_ms=milliseconds,
                details={
                    "region": self._region,
                    "attribution_component": "compute" if qdrant else "storage",
                    "attribution_resource_type": "endpoint",
                    "attribution_resource_id": self._resource,
                    "attribution_operation_name": "vector_database.search",
                    "attribution_operation_status": "succeeded",
                    "attribution_usage_duration_seconds": str(Decimal(milliseconds) / 1000),
                    "attribution_usage_lines": [
                        {
                            "metric": "qdrant.hardware.cpu" if qdrant else "zilliz.read_vcu",
                            "quantity": value,
                            "unit": "Units" if qdrant else "VCU",
                        }
                    ],
                    "vector_capture_basis": "provider_response_meter",
                },
            )
        )

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        if name != (
            "query_points" if self._provider == "qdrant_cloud" else "search"
        ) or not callable(native):
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task, start = get_current_task(), _now()
            eligible, cpu, method = False, None, native
            with suppress(Exception):
                if self._active and task is not None:
                    if self._provider == "qdrant_cloud":
                        remote = self._client._client
                        query_value = kwargs.get("query", args[1] if len(args) > 1 else None)
                        eligible = (
                            not remote._prefer_grpc
                            and _endpoint(remote.rest_uri, (None, 443, 6333)) == self._host
                            and not self._client.cloud_inference
                            and kwargs.get("prefetch") is None
                            and (
                                query_value is None
                                or type(query_value) in (str, int)
                                or (
                                    isinstance(query_value, list)
                                    and all(type(value) in (int, float) for value in query_value)
                                )
                            )
                        )
                        if eligible:
                            api = remote.http.search_api
                            original = api.query_points

                            def envelope(value: Any) -> Any:
                                nonlocal cpu
                                with suppress(Exception):
                                    if (
                                        _field(value, "status") == "ok"
                                        and _field(value, "result") is not None
                                    ):
                                        cpu = _quantity(
                                            _field(
                                                _field(_field(value, "usage"), "hardware"), "cpu"
                                            )
                                        )
                                return value

                            def query(*a: Any, **kw: Any) -> Any:
                                response = original(*a, **kw)
                                if inspect.isawaitable(response):

                                    async def awaited_envelope() -> Any:
                                        return envelope(await response)

                                    return awaited_envelope()
                                return envelope(response)

                            api_proxy = _Proxy(
                                remote.http, {"search_api": _Proxy(api, {"query_points": query})}
                            )
                            remote_proxy = _Proxy(remote, {"http": api_proxy, "rest": api_proxy})
                            remote_proxy._overrides["query_points"] = (
                                remote.query_points.__func__.__get__(remote_proxy)
                            )
                            client_proxy = _Proxy(self._client, {"_client": remote_proxy})
                            method = native.__func__.__get__(client_proxy)
                    else:
                        config = self._client._config
                        eligible = (
                            _endpoint(config.uri, (None, 443, 19530)) == self._host
                            and not kwargs.get("cluster_id")
                            and not kwargs.get("_async")
                            and not kwargs.get("metadata")
                            and not getattr(self._client, "_cluster_id", None)
                        )
            if not eligible:
                return native(*args, **kwargs)

            def capture(response: Any) -> Any:
                with suppress(Exception):
                    value = (
                        cpu
                        if self._provider == "qdrant_cloud"
                        else _quantity(_field(_field(response, "extra"), "cost"))
                    )
                    if self._active and value is not None:
                        self._emit(task, start, value)
                return response

            result = method(*args, **kwargs)
            if inspect.isawaitable(result):

                async def awaited() -> Any:
                    return capture(await result)

                return awaited()
            return capture(result)

        return provider_capture_callable(self._provider, invoke, native)


def instrument_qdrant(
    client: Any, tracker: Any, *, billing_account_id: str, region: str, cluster_host: str
) -> Any:
    """Wrap QdrantClient/AsyncQdrantClient query_points using REST. gRPC/raw APIs are excluded.

    The generated response envelope is observed before the native SDK strips usage;
    the original client is never changed and no extra requests are issued.
    """
    return _CloudVectorFacade(
        client, tracker, "qdrant_cloud", billing_account_id, region, cluster_host
    )


def instrument_zilliz(
    client: Any, tracker: Any, *, billing_account_id: str, region: str, cluster_host: str
) -> Any:
    """Wrap native MilvusClient/AsyncMilvusClient search on a bound serverless cluster.

    SearchResult.extra.cost is reported read vCU, not money. Missing counters remain
    unpriced. Dedicated, on-demand, writes, iterators and raw calls are excluded.
    """
    return _CloudVectorFacade(
        client, tracker, "milvus_zilliz", billing_account_id, region, cluster_host
    )


def uninstrument_qdrant(client: Any) -> None:
    if isinstance(client, _CloudVectorFacade):
        client._active = False


def uninstrument_zilliz(client: Any) -> None:
    if isinstance(client, _CloudVectorFacade):
        client._active = False
