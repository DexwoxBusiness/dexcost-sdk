"""Opt-in hosted OCR page evidence; only reconciled server invoices own money.

No document bytes, text, source locations, headers or credentials are retained.
Document AI uses a local observation ID, never a fabricated provider request ID.
"""

from __future__ import annotations

import inspect
import re
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from dexcost.context import get_current_task
from dexcost.instruments._capture import provider_capture_callable
from dexcost.instruments.database import database_resource_id
from dexcost.models.event import Event
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobUsageLine,
    provider_job_event_id,
)

_transport: ContextVar[dict[str, Any] | None] = ContextVar("dexcost_ocr_transport", default=None)
_MIME = {"application/pdf", "image/png", "image/jpeg", "image/tiff"}


def _now() -> datetime:
    value = datetime.now(timezone.utc)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _empty(value: Any) -> bool:
    # Raw protobuf (FieldMask) is truthy even when empty; proto-plus differs.
    if callable(getattr(value, "ListFields", None)):
        return not value.ListFields()
    return value is None or not bool(value)


def _before_send(request: Any, **kwargs: Any) -> None:
    evidence = _transport.get()
    if evidence is None:
        return
    evidence["attempts"] += 1
    with suppress(Exception):
        url = urlsplit(request.url)
        evidence["valid"] = (
            url.scheme == "https"
            and url.hostname == evidence["host"]
            and url.port in (None, 443)
            and url.path in ("", "/")
            and not (url.username or url.password or url.query or url.fragment)
            and request.method == "POST"
        )


class _Textract:
    def __init__(self, client: Any, tracker: Any, account: str, usage_account: str, region: str):
        self._client, self._tracker, self._region = client, tracker, region
        self._resource = database_resource_id(
            account, f"{usage_account}.{region}.detect_document_text"
        )
        self._active = True
        with suppress(Exception):
            client.meta.events.register(
                "before-send.textract", _before_send, unique_id="dexcost-ocr-route"
            )

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        if name != "detect_document_text" or not callable(native):
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task, started = get_current_task(), _now()
            meta = getattr(self._client, "meta", None)
            eligible = (
                self._active
                and task is not None
                and _get(_get(meta, "service_model"), "service_name") == "textract"
                and _get(meta, "region_name") == self._region
            )
            evidence = {
                "attempts": 0,
                "valid": False,
                "host": f"textract.{self._region}.amazonaws.com",
            }

            def capture(result: Any) -> Any:
                with suppress(Exception):
                    metadata = _get(result, "ResponseMetadata", {})
                    pages = _get(_get(result, "DocumentMetadata"), "Pages")
                    request_id = _get(metadata, "RequestId")
                    if (
                        not eligible
                        or not self._active
                        or evidence["attempts"] != 1
                        or not evidence["valid"]
                        or type(pages) is not int
                        or not 0 < pages <= 9007199254740991
                        or _get(metadata, "HTTPStatusCode") != 200
                        or type(_get(metadata, "RetryAttempts")) is not int
                        or _get(metadata, "RetryAttempts") != 0
                        or not isinstance(request_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", request_id)
                        or _get(result, "Error") is not None
                    ):
                        return result
                    record = f"{self._resource}/{request_id}"
                    assert task is not None
                    if (
                        self._tracker.storage.get_provider_job("amazon_textract", "ocr", record)
                        is None
                    ):
                        self._tracker.storage.insert_provider_job_revision(
                            ProviderJobRevision(
                                event_id=provider_job_event_id("amazon_textract", "ocr", record),
                                revision=1,
                                task_id=task.task_id,
                                provider="amazon_textract",
                                service="ocr",
                                provider_record_id=record,
                                operation="ocr.detect_document_text",
                                component="external",
                                event_type="external_cost",
                                resource_type="endpoint",
                                resource_id=self._resource,
                                status="succeeded",
                                submitted_at=started,
                                observed_at=_now(),
                                usage=(
                                    ProviderJobUsageLine(
                                        "amazon_textract.detect_document_text_pages",
                                        pages,
                                        "Pages",
                                    ),
                                ),
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

        guarded = provider_capture_callable("amazon_textract", invoke, native)
        return lambda *args, **kwargs: (
            guarded(*args, **kwargs) if self._active else native(*args, **kwargs)
        )


def instrument_textract(
    client: Any, tracker: Any, *, billing_account_id: str, usage_account_id: str, region: str
) -> Any:
    """Bind boto3/aiobotocore Textract payer + credential-owner account explicitly.

    Only successful single-attempt DetectDocumentText on the standard regional
    HTTPS route is captured. No account discovery, hidden calls or money formula.
    """
    if (
        not re.fullmatch(r"[0-9]{12}", billing_account_id)
        or not re.fullmatch(r"[0-9]{12}", usage_account_id)
        or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region)
    ):
        raise ValueError("Textract requires payer account, credential-owner account and region")
    return _Textract(client, tracker, billing_account_id, usage_account_id, region)


class _DocumentAI:
    def __init__(
        self,
        client: Any,
        tracker: Any,
        account: str,
        processor: str,
        project: str,
        location: str,
        identifier: str,
    ):
        self._client, self._tracker, self._processor = client, tracker, processor
        self._host, self._active = f"{location}-documentai.googleapis.com", True
        # Billing population is the processor, not one mutable processor version.
        self._resource = database_resource_id(
            account, f"{project}.{location}.{identifier}.enterprise_ocr"
        )

    def __getattr__(self, name: str) -> Any:
        native = getattr(self._client, name)
        if name != "process_document" or not callable(native):
            return native

        def invoke(*args: Any, **kwargs: Any) -> Any:
            task, started = get_current_task(), _now()
            request = kwargs.get("request", args[0] if len(args) == 1 else None)
            eligible = False
            with suppress(Exception):
                host = self._client.transport._host
                source = _get(request, "raw_document") or _get(request, "gcs_document")
                eligible = (
                    self._active
                    and task is not None
                    and len(args) <= 1
                    and request is not None
                    and kwargs.get("retry", "default") is None
                    and not kwargs.get("metadata")
                    and host in (self._host, self._host + ":443")
                    and _get(request, "name") == self._processor
                    and _empty(_get(request, "process_options"))
                    and _empty(_get(request, "field_mask"))
                    and _empty(_get(request, "inline_document"))
                    and _get(source, "mime_type") in _MIME
                )

            def capture(result: Any) -> Any:
                with suppress(Exception):
                    document = _get(result, "document")
                    pages = _get(document, "pages")
                    if (
                        not eligible
                        or not self._active
                        or not pages
                        or not _empty(_get(document, "shard_info"))
                        or _get(_get(document, "error"), "code", 0) != 0
                        or any(
                            type(_get(page, "page_number")) is not int
                            or _get(page, "page_number") != i + 1
                            for i, page in enumerate(pages)
                        )
                    ):
                        return result
                    ended = _now()
                    if ended < started:
                        return result
                    assert task is not None
                    self._tracker.storage.insert_event(
                        Event(
                            task_id=task.task_id,
                            occurred_at=ended,
                            event_type="external_cost",
                            cost_confidence="unknown",
                            provider="google_document_ai",
                            service_name="ocr",
                            details={
                                "attribution_component": "external",
                                "attribution_resource_type": "endpoint",
                                "attribution_resource_id": self._resource,
                                "attribution_operation_name": "ocr.process_document",
                                "attribution_operation_status": "succeeded",
                                "attribution_usage_duration_seconds": (
                                    ended - started
                                ).total_seconds(),
                                "attribution_usage_lines": [
                                    {
                                        "metric": "google_document_ai.enterprise_ocr_pages",
                                        "quantity": str(len(pages)),
                                        "unit": "Pages",
                                    }
                                ],
                                "ocr_capture_basis": "provider_returned_pages_local_call_identity",
                            },
                        )
                    )
                return result

            result = native(*args, **kwargs)
            if inspect.isawaitable(result):

                async def awaited() -> Any:
                    return capture(await result)

                return awaited()
            return capture(result)

        guarded = provider_capture_callable("google_document_ai", invoke, native)
        return lambda *args, **kwargs: (
            guarded(*args, **kwargs) if self._active else native(*args, **kwargs)
        )


def instrument_document_ai(
    client: Any,
    tracker: Any,
    *,
    billing_account_id: str,
    processor_version: str,
    processor_type: str,
) -> Any:
    """Bind a verified OCR_PROCESSOR version to its invoice account/processor.

    Caller attests the processor type; no GetProcessor polling. Calls must pass
    retry=None, full PDF/image response, no page selection/options or metadata
    overrides. Sync/async official v1 clients are supported; batches are not.
    A returned page is allocation evidence, not a claim of per-call billed cash.
    """
    match = re.fullmatch(
        r"projects/([a-z0-9-]{1,30})/locations/([a-z0-9-]{2,30})/processors/([a-zA-Z0-9_-]{1,32})/processorVersions/([A-Za-z0-9._-]{1,100})",
        processor_version,
    )
    if processor_type != "OCR_PROCESSOR" or match is None:
        raise ValueError("Document AI requires an explicit verified OCR_PROCESSOR version")
    return _DocumentAI(client, tracker, billing_account_id, processor_version, *match.groups()[:3])


def uninstrument_ocr(client: Any) -> None:
    if isinstance(client, (_Textract, _DocumentAI)):
        client._active = False


def uninstrument_textract(client: Any) -> None:
    """Disable capture on the facade returned by instrument_textract."""
    uninstrument_ocr(client)


def uninstrument_document_ai(client: Any) -> None:
    """Disable capture on the facade returned by instrument_document_ai."""
    uninstrument_ocr(client)
