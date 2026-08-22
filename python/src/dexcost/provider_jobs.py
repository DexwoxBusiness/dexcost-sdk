"""Durable lifecycle reconciliation for asynchronous provider work."""

from __future__ import annotations

import contextvars
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.capabilities import get_capability
from dexcost.context import _reset_current_task, get_current_task, set_current_task
from dexcost.idempotency import get_idempotency_key
from dexcost.instruments._errors import error_details
from dexcost.instruments._provider_metering import (
    OperationMeasurement,
    record_provider_operation,
)
from dexcost.models.capability import CapabilityIdentity
from dexcost.models.provider_job import (
    ProviderJobCostConfidence,
    ProviderJobCostSource,
    ProviderJobEventType,
    ProviderJobRevision,
    ProviderJobStatus,
    ProviderJobUsageLine,
    provider_job_event_id,
)
from dexcost.models.task import Task

_log = logging.getLogger(__name__)
_NON_CANONICAL = re.compile(r"[^a-z0-9._-]")
_LEADING_JUNK = re.compile(r"^[^a-z0-9]+")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_identity(
    error: BaseException | None,
    *,
    error_type: str | None = None,
    error_code: str | None = None,
) -> tuple[str | None, str | None]:
    details = error_details(error) if error is not None else {}
    raw_type = error_type or cast(str | None, details.get("error_type"))
    raw_code = error_code or cast(str | None, details.get("error_code"))
    if raw_type is None:
        return None, None
    normalized = _NON_CANONICAL.sub("_", raw_type.strip().lower())
    normalized = _LEADING_JUNK.sub("", normalized)[:127]
    if not normalized:
        return None, None
    return normalized, raw_code[:64] if raw_code else None


def _cost_evidence(pricing: Any) -> tuple[
    Decimal | None,
    ProviderJobCostSource | None,
    ProviderJobCostConfidence | None,
    str | None,
]:
    amount = Decimal(str(pricing.cost_usd))
    if amount <= 0:
        return None, None, None, None
    source = pricing.pricing_source
    confidence = pricing.cost_confidence
    if source == "provider_response":
        return (
            amount,
            "provider_reported",
            "exact" if confidence == "exact" else "estimated",
            None,
        )
    if source == "rate_registry":
        mapped_source: ProviderJobCostSource = "sdk_rate_registry"
    elif source in {"service_catalog", "litellm", "tokencost"} or bool(
        source
        and source.startswith(("compute_catalog:", "gpu_catalog:", "egress_catalog:"))
    ):
        mapped_source = "sdk_catalog"
    else:
        return None, None, None, None
    if not pricing.pricing_version:
        return None, None, None, None
    mapped_confidence: ProviderJobCostConfidence = (
        "computed" if confidence == "exact" else cast(ProviderJobCostConfidence, confidence)
    )
    return amount, mapped_source, mapped_confidence, pricing.pricing_version


def _positive_usage(measurement: OperationMeasurement) -> tuple[ProviderJobUsageLine, ...]:
    return tuple(
        ProviderJobUsageLine(line.metric, line.quantity, line.unit)
        for line in measurement.usage_lines
        if Decimal(str(line.quantity)) > 0
    )


def _measurement_fields(
    tracker: Any,
    resource_id: str,
    measurement: OperationMeasurement | None,
) -> dict[str, Any]:
    if measurement is None:
        return {
            "usage": (),
            "cost_amount": None,
            "cost_source": None,
            "cost_confidence": None,
            "pricing_version": None,
            "task_input_tokens": None,
            "task_output_tokens": None,
            "task_cached_tokens": None,
        }
    resolved_model = measurement.response_model or resource_id
    candidates = tuple(
        candidate
        for candidate in measurement.model_candidates
        if isinstance(candidate, str) and candidate
    )
    if measurement.provider_cost_usd is not None:
        provider_amount = Decimal(str(measurement.provider_cost_usd))
        if provider_amount > 0:
            amount: Decimal | None = provider_amount
            source: ProviderJobCostSource | None = "provider_reported"
            confidence: ProviderJobCostConfidence | None = "exact"
        else:
            amount = None
            source = None
            confidence = None
        version = None
    else:
        pricing = tracker._pricing.get_metered_cost(
            resolved_model,
            measurement.pricing_usage,
            model_candidates=candidates,
        )
        amount, source, confidence, version = _cost_evidence(pricing)
    return {
        "usage": _positive_usage(measurement),
        "cost_amount": amount,
        "cost_source": source,
        "cost_confidence": confidence,
        "pricing_version": version,
        "task_input_tokens": measurement.task_input_tokens,
        "task_output_tokens": measurement.task_output_tokens,
        "task_cached_tokens": measurement.task_cached_tokens,
    }


def _finalize_attached_task(tracker: Any, job: ProviderJobRevision) -> None:
    if not job.terminal:
        return
    task = tracker._storage.get_task(str(job.task_id))
    if task is None:
        return
    tracker._aggregate_costs(task)
    if job.owns_task:
        task.status = "success" if job.status == "succeeded" else "failed"
        task.ended_at = job.observed_at
        task.failure_count = 0 if job.status == "succeeded" else 1
    tracker._storage.update_task(task)


def record_provider_job_submission(
    *,
    tracker: Any,
    task: Task,
    owns_task: bool,
    provider: str,
    service: str,
    provider_record_id: str,
    operation: str,
    component: str,
    event_type: ProviderJobEventType,
    resource_type: str,
    resource_id: str,
    submitted_at: datetime | None = None,
    observed_at: datetime | None = None,
    status: ProviderJobStatus = "submitted",
    measurement: OperationMeasurement | None = None,
    latency_ms: int | None = None,
    error: BaseException | None = None,
    error_type: str | None = None,
    error_code: str | None = None,
    capability: CapabilityIdentity | None = None,
    billing_dimensions: tuple[tuple[str, str], ...] = (),
) -> ProviderJobRevision:
    """Persist revision one for provider work accepted beyond the request."""
    existing = cast(
        ProviderJobRevision | None,
        tracker._storage.get_provider_job(provider, service, provider_record_id),
    )
    started = submitted_at or _now()
    observed = observed_at or _now()
    resolved_error_type, resolved_error_code = _error_identity(
        error, error_type=error_type, error_code=error_code
    )
    fields = _measurement_fields(tracker, resource_id, measurement)
    candidate = ProviderJobRevision(
        event_id=provider_job_event_id(provider, service, provider_record_id),
        revision=1,
        task_id=task.task_id,
        provider=provider,
        service=service,
        provider_record_id=provider_record_id,
        operation=operation,
        component=component,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        status=status,
        submitted_at=started,
        observed_at=observed,
        owns_task=owns_task,
        billing_dimensions=billing_dimensions,
        latency_ms=latency_ms,
        error_type=resolved_error_type,
        error_code=resolved_error_code,
        capability=capability,
        **fields,
    )
    if existing is not None:
        if (
            existing.revision == 1
            and existing.economic_snapshot() == candidate.economic_snapshot()
        ):
            return existing
        raise ValueError("provider job identity is already attached to different work")
    tracker._storage.insert_provider_job_revision(candidate)
    _finalize_attached_task(tracker, candidate)
    return candidate


def reconcile_provider_job(
    *,
    tracker: Any,
    provider: str,
    service: str,
    provider_record_id: str,
    status: ProviderJobStatus,
    measurement: OperationMeasurement | None = None,
    observed_at: datetime | None = None,
    latency_ms: int | None = None,
    error: BaseException | None = None,
    error_type: str | None = None,
    error_code: str | None = None,
) -> ProviderJobRevision:
    """Append a changed job snapshot, deduplicating replayed polls atomically."""
    for _attempt in range(5):
        previous = cast(
            ProviderJobRevision | None,
            tracker._storage.get_provider_job(
                provider, service, provider_record_id
            ),
        )
        if previous is None:
            raise LookupError(
                f"unknown provider job {provider}/{service}/{provider_record_id}"
            )
        resolved_error_type, resolved_error_code = _error_identity(
            error, error_type=error_type, error_code=error_code
        )
        fields = _measurement_fields(tracker, previous.resource_id, measurement)
        candidate = ProviderJobRevision(
            event_id=previous.event_id,
            revision=previous.revision + 1,
            task_id=previous.task_id,
            provider=previous.provider,
            service=previous.service,
            provider_record_id=previous.provider_record_id,
            operation=previous.operation,
            component=previous.component,
            event_type=previous.event_type,
            resource_type=previous.resource_type,
            resource_id=previous.resource_id,
            status=status,
            submitted_at=previous.submitted_at,
            observed_at=observed_at or _now(),
            owns_task=previous.owns_task,
            billing_dimensions=previous.billing_dimensions,
            latency_ms=latency_ms,
            error_type=resolved_error_type,
            error_code=resolved_error_code,
            capability=previous.capability,
            **fields,
        )
        if previous.economic_snapshot() == candidate.economic_snapshot():
            return previous
        try:
            tracker._storage.insert_provider_job_revision(candidate)
        except ValueError as exc:
            text = str(exc)
            if "expected revision" in text or "already exists with different" in text:
                continue
            raise
        _finalize_attached_task(tracker, candidate)
        return candidate
    raise RuntimeError("provider job changed concurrently too many times; retry reconciliation")


class ProviderJobSession:
    """Capture a provider submission call and detach its durable future lifecycle."""

    def __init__(
        self,
        *,
        tracker: Any,
        task_type: str,
        provider: str,
        service: str,
        operation: str,
        component: str,
        event_type: ProviderJobEventType,
        resource_type: str,
        resource_id: str,
        billing_dimensions: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.tracker = tracker
        self.provider = provider
        self.service = service
        self.operation = operation
        self.component = component
        self.event_type = event_type
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.billing_dimensions = billing_dimensions
        self.started_at = _now()
        self._started_clock = time.perf_counter()
        current_task = get_current_task()
        self.owns_task = current_task is None
        self.task: Task = current_task or create_auto_task(task_type)
        self._token: contextvars.Token[Task | None] | None = (
            set_current_task(self.task) if self.owns_task else None
        )
        self._released = False
        self._finished = False
        self._capability = get_capability()
        self._idempotency_key = get_idempotency_key()

    def release_context(self) -> None:
        if self._released:
            return
        self._released = True
        if self._token is not None:
            _reset_current_task(self._token)
            self._token = None

    def submit(
        self,
        provider_record_id: str,
        *,
        status: ProviderJobStatus = "submitted",
        measurement: OperationMeasurement | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
    ) -> ProviderJobRevision:
        if self._finished:
            raise RuntimeError("provider job submission session is already complete")
        self._finished = True
        if self.owns_task:
            self.tracker._storage.insert_task(self.task)
        try:
            return record_provider_job_submission(
                tracker=self.tracker,
                task=self.task,
                owns_task=self.owns_task,
                provider=self.provider,
                service=self.service,
                provider_record_id=provider_record_id,
                operation=self.operation,
                component=self.component,
                event_type=self.event_type,
                resource_type=self.resource_type,
                resource_id=self.resource_id,
                submitted_at=self.started_at,
                status=status,
                measurement=measurement,
                latency_ms=max(0, int((time.perf_counter() - self._started_clock) * 1000)),
                error_type=error_type,
                error_code=error_code,
                capability=self._capability,
                billing_dimensions=self.billing_dimensions,
            )
        finally:
            self.release_context()

    def fail(self, error: BaseException) -> None:
        """Record a submission failure that never acquired a provider job ID."""
        if self._finished:
            return
        self._finished = True
        self.release_context()
        try:
            event = record_provider_operation(
                tracker=self.tracker,
                task=self.task,
                provider=self.provider,
                service=self.service,
                operation=self.operation,
                component=self.component,
                event_type=self.event_type,
                model=self.resource_id,
                measurement=OperationMeasurement(pricing_usage={}, usage_lines=()),
                latency_ms=max(
                    0, int((time.perf_counter() - self._started_clock) * 1000)
                ),
                status="failed",
                error=error,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            if self.owns_task:
                finalize_auto_task(self.task, event, status="failed")
                self.tracker._storage.insert_task(self.task)
        except Exception:
            _log.debug("dexcost: failed to record provider job submission", exc_info=True)


class SyncProviderJobStream(Iterator[Any]):
    """Observe a job retrieval stream without changing state on client close."""

    def __init__(
        self,
        stream: Any,
        *,
        observe: Callable[[Any], None],
        complete: Callable[[], None],
    ) -> None:
        self._stream = stream
        self._observe = observe
        self._complete = complete
        self._completed = False

    def __iter__(self) -> SyncProviderJobStream:
        return self

    def __next__(self) -> Any:
        try:
            item = next(self._stream)
        except StopIteration:
            if not self._completed:
                self._completed = True
                self._complete()
            raise
        self._observe(item)
        return item

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def close(self) -> Any:
        # Closing the local response does not cancel provider-side work.
        return self._stream.close() if hasattr(self._stream, "close") else None

    def __enter__(self) -> SyncProviderJobStream:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Any:
        if hasattr(self._stream, "__exit__"):
            return self._stream.__exit__(exc_type, exc, tb)
        return None


class AsyncProviderJobStream(AsyncIterator[Any]):
    """Async equivalent of :class:`SyncProviderJobStream`."""

    def __init__(
        self,
        stream: Any,
        *,
        observe: Callable[[Any], None],
        complete: Callable[[], None],
    ) -> None:
        self._stream = stream
        self._observe = observe
        self._complete = complete
        self._completed = False

    def __aiter__(self) -> AsyncProviderJobStream:
        return self

    async def __anext__(self) -> Any:
        try:
            item = await self._stream.__anext__()
        except StopAsyncIteration:
            if not self._completed:
                self._completed = True
                self._complete()
            raise
        self._observe(item)
        return item

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    async def aclose(self) -> Any:
        # Closing the local response does not cancel provider-side work.
        return await self._stream.aclose() if hasattr(self._stream, "aclose") else None

    async def __aenter__(self) -> AsyncProviderJobStream:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Any:
        if hasattr(self._stream, "__aexit__"):
            return await self._stream.__aexit__(exc_type, exc, tb)
        return None


__all__ = [
    "AsyncProviderJobStream",
    "ProviderJobSession",
    "SyncProviderJobStream",
    "reconcile_provider_job",
    "record_provider_job_submission",
]
