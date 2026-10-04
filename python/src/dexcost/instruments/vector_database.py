"""Opt-in native vector usage. Reconciled invoice pools alone supply money.

Bindings are explicit billing-account ownership assertions, not account discovery.
Only successful query/fetch (Pinecone) and query/write (turbopuffer) replies are
observed. No vector values, IDs, filters, documents, credentials or prices persist.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from dexcost.context import get_current_task
from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments.database import database_resource_id
from dexcost.models.event import Event

Provider = Literal["pinecone", "turbopuffer"]


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _host(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = urlsplit(value if "://" in value else f"https://{value}")
    if (
        url.scheme != "https"
        or url.port not in (None, 443)
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        return None
    return url.hostname


def _namespace(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ValueError("A namespace must be an explicit string of at most 128 characters")
    return value or "__default__"


def vector_database_resource_id(
    provider: Provider, billing_account_id: str, region: str, endpoint_host: str, namespace: str
) -> str:
    """Shared invoice resource: account/SHA256(JSON([provider,region,host,namespace])).

    The digest includes every identity boundary without exposing namespace names.
    Supply this exact ID as both invoice resource.id and resource scope.id.
    """
    database_resource_id(billing_account_id, "validation")
    if provider not in ("pinecone", "turbopuffer") or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,63}", region
    ):
        raise ValueError("An explicit supported provider and region are required")
    host = _host(endpoint_host)
    if host != endpoint_host or not host or len(host) > 253:
        raise ValueError("Use a lowercase provider hostname, not a URL")
    if provider == "pinecone":
        if not re.fullmatch(r"[a-z0-9.-]+\.svc(?:\.[a-z0-9-]+)?\.pinecone\.io", host):
            raise ValueError("An explicit hosted Pinecone index endpoint is required")
    elif host != f"{region}.turbopuffer.com" or namespace == "":
        raise ValueError("turbopuffer requires its regional endpoint and a named namespace")
    _namespace(namespace)
    identity = json.dumps(
        [provider, region, host, namespace], ensure_ascii=False, separators=(",", ":")
    )
    return database_resource_id(billing_account_id, hashlib.sha256(identity.encode()).hexdigest())


def _now(*, end: bool = False) -> datetime:
    value = datetime.now(timezone.utc)
    remainder = value.microsecond % 1000
    return (
        value
        - timedelta(microseconds=remainder)
        + (timedelta(milliseconds=1) if end and remainder else timedelta())
    )


def _count(value: Any) -> int:
    # Match JS safe integers; never round imprecise JSON numbers into money weights.
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise ValueError("Provider usage must be a nonnegative safe integer")
    return value


def _read_units(value: Any) -> Decimal:
    # RU are fractional (including 0.25). Preserve the provider number, never ceil.
    if type(value) not in (int, float, Decimal):
        raise ValueError("Pinecone read units must be a finite nonnegative number")
    quantity = Decimal(str(value))
    if (
        not quantity.is_finite()
        or quantity < 0
        or quantity > 9007199254740991
        or cast(int, quantity.as_tuple().exponent) < -12
    ):
        raise ValueError("Pinecone read units must fit the exact 12-decimal domain")
    return quantity


def _usage(provider: Provider, operation: str, response: Any) -> tuple[dict[str, str], ...]:
    if provider == "pinecone":
        usage = _field(response, "usage")
        snake, camel = _field(usage, "read_units"), _field(usage, "readUnits")
        if snake is not None and camel is not None and snake != camel:
            raise ValueError("Conflicting Pinecone read-unit fields")
        values: list[tuple[str, int | Decimal, str]] = [
            (
                "pinecone.read_units_rounded",
                _read_units(snake if snake is not None else camel),
                "ReadUnits",
            )
        ]
    else:
        billing = _field(response, "billing")
        query = billing if operation == "query" else _field(billing, "query")
        values = []
        if operation == "write":
            values.append(
                (
                    "turbopuffer.logical_bytes_written",
                    _count(_field(billing, "billable_logical_bytes_written")),
                    "Bytes",
                )
            )
        if operation == "query" or query is not None:
            values.extend(
                (
                    f"turbopuffer.logical_bytes_{suffix}",
                    _count(_field(query, f"billable_logical_bytes_{suffix}")),
                    "Bytes",
                )
                for suffix in ("queried", "returned")
            )
    return tuple(
        {"metric": metric, "quantity": str(count), "unit": unit}
        for metric, count, unit in values
        if count
    )


class _VectorFacade:
    def __init__(
        self,
        client: Any,
        tracker: Any,
        provider: Provider,
        account: str,
        region: str,
        endpoint: str,
        namespace: str,
    ) -> None:
        self._resource = vector_database_resource_id(
            provider, account, region, endpoint, namespace
        )
        self._client, self._tracker, self._provider = client, tracker, provider
        self._region, self._endpoint, self._namespace = region, endpoint, _namespace(namespace)
        self._active = True

    def _eligible(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
        if (
            args
            or any(
                kwargs.get(key) is not None
                for key in ("extra_body", "extra_query", "extra_headers")
            )
            or kwargs.get("async_req")
        ):
            return False
        if self._provider == "pinecone":
            return (
                _host(getattr(self._client, "host", None)) == self._endpoint
                and _namespace(kwargs.get("namespace", "")) == self._namespace
            )
        client = getattr(self._client, "_client", None)
        return (
            _host(str(getattr(client, "base_url", ""))) == self._endpoint
            and _namespace(kwargs.get("namespace") or getattr(client, "default_namespace", None))
            == self._namespace
        )

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        operations = {"query", "fetch"} if self._provider == "pinecone" else {"query", "write"}
        if name not in operations or not callable(native):
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task, started = get_current_task(), _now()
            eligible = False
            with suppress(Exception):
                eligible = self._active and task is not None and self._eligible(args, kwargs)
            captured = False

            def capture(response: Any) -> Any:
                nonlocal captured
                if captured or not eligible or not self._active:
                    return response
                captured = True
                with suppress(Exception):
                    # Native query/fetch always returns the selected namespace.
                    if (
                        self._provider == "pinecone"
                        and _namespace(_field(response, "namespace")) != self._namespace
                    ):
                        return response
                    usage = _usage(self._provider, name, response)
                    if not usage:
                        return response
                    assert task is not None
                    ended = max(_now(end=True), started + timedelta(milliseconds=1))
                    milliseconds = (ended - started) // timedelta(milliseconds=1)
                    for component in ("storage", "network"):
                        meters = tuple(
                            line
                            for line in usage
                            if (line["metric"].endswith("bytes_returned"))
                            == (component == "network")
                        )
                        if not meters:
                            continue
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
                                    "attribution_component": component,
                                    "attribution_resource_type": "endpoint",
                                    "attribution_resource_id": self._resource,
                                    "attribution_operation_name": f"vector_database.{name}",
                                    "attribution_operation_status": "succeeded",
                                    "attribution_usage_duration_seconds": str(
                                        Decimal(milliseconds) / 1000
                                    ),
                                    "attribution_usage_lines": list(meters),
                                    "vector_capture_basis": "provider_response_meter",
                                },
                            )
                        )
                return response

            result = native(*args, **kwargs)
            if inspect.isawaitable(result):

                async def awaited() -> Any:
                    return capture(await result)

                return awaited()
            return capture(result)

        return provider_capture_callable(self._provider, invoke, native)


def instrument_pinecone(
    client: Any,
    tracker: Any,
    *,
    billing_account_id: str,
    region: str,
    index_host: str,
    namespace: str = "",
) -> Any:
    """Wrap a hosted native Index/AsyncIndex or synchronous GrpcIndex, query/fetch only.

    Bind the exact account, region, index and namespace from your billing inventory.
    Calls must select the same namespace; future/thread-pool and fanout helpers,
    integrated inference, document search, DRN capacity and write usage are excluded.
    """
    return _VectorFacade(
        client, tracker, "pinecone", billing_account_id, region, index_host, namespace
    )


def instrument_turbopuffer(
    client: Any, tracker: Any, *, billing_account_id: str, region: str, namespace: str
) -> Any:
    """Wrap a native client.namespace(name) query/write resource (sync or async).

    Capture provider BILLABLE bytes only, without reapplying floors or discounts.
    Raw/streaming helpers, multi-query and other operations remain native/unwrapped.
    Retries' unreported usage stays in the provider invoice's unallocated residual.
    """
    return _VectorFacade(
        client,
        tracker,
        "turbopuffer",
        billing_account_id,
        region,
        f"{region}.turbopuffer.com",
        namespace,
    )


def uninstrument_vector_database(client: Any) -> None:
    if isinstance(client, _VectorFacade):
        client._active = False


def uninstrument_pinecone(client: Any) -> None:
    """Disable capture on the facade returned by instrument_pinecone."""
    uninstrument_vector_database(client)


def uninstrument_turbopuffer(client: Any) -> None:
    """Disable capture on the facade returned by instrument_turbopuffer."""
    uninstrument_vector_database(client)
