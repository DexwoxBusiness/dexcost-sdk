"""Opt-in S3-compatible request evidence; money comes only from reconciled bills.

No object keys, content, URLs, byte-month estimates, or SDK prices are persisted.
AWS uses an owner-pays bucket identity; R2 uses the whole account billing endpoint.
"""

from __future__ import annotations

import inspect
import re
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from dexcost.context import get_current_task
from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments.database import database_resource_id
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobUsageLine,
    provider_job_event_id,
)

_OPERATIONS = {
    "get_object": "get_requests",
    "put_object": "put_requests",
    "list_objects_v2": "list_requests",
}
_transport: ContextVar[dict[str, Any] | None] = ContextVar(
    "dexcost_storage_transport", default=None
)


def _before_send(request: Any, **kwargs: Any) -> None:
    evidence = _transport.get()
    if evidence is None:
        return
    evidence["attempts"] += 1
    with suppress(Exception):
        url = urlsplit(request.url)
        hosts, bucket = evidence["hosts"], evidence["bucket"]
        # Only bucket routing is inspected; object paths/query/headers never persist.
        routed = (
            url.hostname in hosts
            and (url.path == f"/{bucket}" or url.path.startswith(f"/{bucket}/"))
        ) or any(url.hostname == f"{bucket}.{host}" for host in hosts)
        evidence["valid"] = (
            url.scheme == "https"
            and url.port in (None, 443)
            and not (url.username or url.password)
            and routed
            and request.method == evidence["method"]
        )


def _now() -> datetime:
    value = datetime.now(timezone.utc)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def _bucket(value: str) -> bool:
    return (
        bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value))
        and ".." not in value
        and not value.endswith(("--x-s3", "-s3alias", ".mrap"))
    )


class _ObjectStorage:
    def __init__(
        self,
        client: Any,
        tracker: Any,
        provider: str,
        account: str,
        bucket: str,
        region: str,
        owner: str | None,
    ) -> None:
        self._client, self._tracker = client, tracker
        self._provider, self._account, self._bucket = provider, account, bucket
        self._region, self._owner, self._active = region, owner, True
        self._resource = database_resource_id(
            account, f"{owner}.{region}.{bucket}" if provider == "aws_s3" else "r2"
        )
        # One transparent observer per native client. It is a no-op outside the
        # facade's context and never performs, modifies or cancels a request.
        with suppress(Exception):
            client.meta.events.register(
                "before-send.s3", _before_send, unique_id="dexcost-object-storage-route"
            )

    def _route(self) -> bool:
        meta = getattr(self._client, "meta", None)
        if (
            getattr(getattr(meta, "service_model", None), "service_name", None) != "s3"
            or getattr(meta, "region_name", None) != self._region
        ):
            return False
        raw = getattr(meta, "endpoint_url", None)
        if not isinstance(raw, str):
            return False
        url = urlsplit(raw)
        allowed = (
            {f"s3.{self._region}.amazonaws.com"}
            if self._provider == "aws_s3"
            else {f"{self._account}.r2.cloudflarestorage.com"}
        )
        if self._provider == "aws_s3" and self._region == "us-east-1":
            allowed.add("s3.amazonaws.com")
        return (
            url.scheme == "https"
            and url.hostname in allowed
            and url.port in (None, 443)
            and not (url.username or url.password or url.query or url.fragment)
            and url.path in ("", "/")
        )

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        if name not in _OPERATIONS or not callable(native):
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task, started = get_current_task(), _now()
            eligible = (
                self._active
                and task is not None
                and not args
                and kwargs.get("Bucket") == self._bucket
            )
            hosts = (
                [f"s3.{self._region}.amazonaws.com"]
                if self._provider == "aws_s3"
                else [f"{self._account}.r2.cloudflarestorage.com"]
            )
            if self._provider == "aws_s3" and self._region == "us-east-1":
                hosts.append("s3.amazonaws.com")
            evidence = {
                "attempts": 0,
                "valid": False,
                "hosts": hosts,
                "bucket": self._bucket,
                "method": "PUT" if name == "put_object" else "GET",
            }
            # Unknown endpoint metadata never establishes ownership.
            if eligible:
                try:
                    eligible = self._route()
                except Exception:
                    eligible = False
            eligible = (
                eligible
                and "RequestPayer" not in kwargs
                and (
                    "ExpectedBucketOwner" not in kwargs
                    or kwargs["ExpectedBucketOwner"] == self._owner
                )
            )

            def capture(result: Any) -> Any:
                with suppress(Exception):
                    if (
                        not eligible
                        or evidence["attempts"] != 1
                        or not evidence["valid"]
                        or not self._active
                        or not isinstance(result, dict)
                        or "RequestCharged" in result
                        or "Error" in result
                    ):
                        return result
                    metadata = result.get("ResponseMetadata", {})
                    request_id, status, retries = (
                        metadata.get("RequestId"),
                        metadata.get("HTTPStatusCode"),
                        metadata.get("RetryAttempts"),
                    )
                    if (
                        not isinstance(request_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", request_id)
                        or type(status) is not int
                        or not 200 <= status < 300
                        or type(retries) is not int
                        or retries != 0
                    ):
                        return result
                    class_value = (
                        result.get(
                            "StorageClass", "STANDARD" if self._provider == "aws_s3" else None
                        )
                        if name == "get_object"
                        else kwargs.get(
                            "StorageClass", "STANDARD" if self._provider == "aws_s3" else None
                        )
                    )
                    if self._provider == "aws_s3":
                        # AWS documents omitted GET StorageClass and default PUT as Standard.
                        if name != "list_objects_v2" and class_value != "STANDARD":
                            return result
                        metric = f"aws_s3.{_OPERATIONS[name]}"
                    else:
                        # R2 bucket defaults are mutable; omission is not proof of Standard.
                        if name == "list_objects_v2" or class_value not in (
                            "STANDARD",
                            "STANDARD_IA",
                        ):
                            return result
                        tier = "standard" if class_value == "STANDARD" else "ia"
                        metric = (
                            f"r2_cloudflare.{tier}_class_{'a' if name == 'put_object' else 'b'}"
                        )
                    record = f"{self._resource}/{request_id}"
                    if (
                        self._tracker.storage.get_provider_job(
                            self._provider, "object_storage", record
                        )
                        is not None
                    ):
                        return result
                    assert task is not None
                    self._tracker.storage.insert_provider_job_revision(
                        ProviderJobRevision(
                            event_id=provider_job_event_id(
                                self._provider, "object_storage", record
                            ),
                            revision=1,
                            task_id=task.task_id,
                            provider=self._provider,
                            service="object_storage",
                            provider_record_id=record,
                            operation=f"object_storage.{name}",
                            component="storage",
                            event_type="external_cost",
                            resource_type="endpoint",
                            resource_id=self._resource,
                            status="succeeded",
                            submitted_at=started,
                            observed_at=_now(),
                            usage=(ProviderJobUsageLine(metric, 1, "Requests"),),
                        )
                    )
                return result

            token = _transport.set(evidence)
            try:
                result = native(*args, **kwargs)
            finally:
                _transport.reset(token)
            if inspect.isawaitable(result):

                async def awaited() -> Any:
                    token = _transport.set(evidence)
                    try:
                        return capture(await result)
                    finally:
                        _transport.reset(token)

                return awaited()
            return capture(result)

        guarded = provider_capture_callable(self._provider, invoke, native)
        return lambda *args, **kwargs: (
            guarded(*args, **kwargs) if self._active else native(*args, **kwargs)
        )


def instrument_object_storage(
    client: Any,
    tracker: Any,
    *,
    provider: Literal["aws_s3", "r2_cloudflare"],
    billing_account_id: str,
    bucket: str,
    region: str,
    bucket_owner_account_id: str | None = None,
    owner_pays: bool = False,
) -> Any:
    """Return a boto3/aiobotocore facade with a transparent before-send observer.

    AWS requires explicit owner-pays attestation and payer/owner account mapping.
    R2 requires region='auto'; every bucket shares account/r2 invoice identity.
    Only successful single-attempt GET/PUT (and AWS LIST) become request counters.
    """
    if not _bucket(bucket):
        raise ValueError("An explicit general-purpose bucket name is required")
    if provider == "aws_s3":
        if (
            not re.fullmatch(r"[0-9]{12}", billing_account_id)
            or not isinstance(bucket_owner_account_id, str)
            or not re.fullmatch(r"[0-9]{12}", bucket_owner_account_id)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region)
            or owner_pays is not True
        ):
            raise ValueError("AWS requires payer, bucket-owner, region and owner_pays=True")
    elif provider == "r2_cloudflare":
        if (
            not re.fullmatch(r"[a-f0-9]{32}", billing_account_id)
            or region != "auto"
            or bucket_owner_account_id is not None
        ):
            raise ValueError("R2 requires its account ID, region auto and no AWS owner mapping")
    else:
        raise ValueError("Unsupported object-storage provider")
    return _ObjectStorage(
        client, tracker, provider, billing_account_id, bucket, region, bucket_owner_account_id
    )


def uninstrument_object_storage(client: Any) -> None:
    if isinstance(client, _ObjectStorage):
        client._active = False
