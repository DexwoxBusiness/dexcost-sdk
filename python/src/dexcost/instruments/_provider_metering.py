"""Shared, privacy-safe lifecycle metering for provider SDK integrations.

The provider adapters deliberately contain only API-shape extraction.  This
module owns the invariants that must remain identical across every provider:
auto-task lifecycle, failure/cancellation capture, exact catalog arithmetic,
capability/idempotency propagation, and stream completion semantics.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from dexcost.auto_task import create_auto_task, finalize_auto_task
from dexcost.capabilities import apply_event_capability, get_capability
from dexcost.context import _reset_current_task, get_current_task, set_current_task
from dexcost.idempotency import (
    IdempotencyKey,
    apply_event_idempotency,
    capture_idempotency_key,
)
from dexcost.instruments._errors import error_details
from dexcost.models._serde import canonical_decimal
from dexcost.models.capability import CapabilityIdentity
from dexcost.models.event import Event

_log = logging.getLogger(__name__)
_CANONICAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._{}/*^+-]{0,63}$")

OperationStatus = Literal["succeeded", "failed", "cancelled", "unknown"]


def _quantity(name: str, value: Decimal | int | str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must be an integer, Decimal, or decimal string")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a plain decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed


@dataclass(frozen=True)
class ProviderUsageLine:
    """One native provider meter destined for attribution v3."""

    metric: str
    quantity: Decimal | int | str
    unit: str

    def __post_init__(self) -> None:
        if _CANONICAL_NAME.fullmatch(self.metric) is None:
            raise ValueError(f"invalid provider usage metric {self.metric!r}")
        if _UNIT.fullmatch(self.unit) is None:
            raise ValueError(f"invalid provider usage unit {self.unit!r}")
        object.__setattr__(self, "quantity", _quantity(self.metric, self.quantity))


@dataclass(frozen=True)
class OperationMeasurement:
    """Usage extracted from one completed provider operation."""

    pricing_usage: Mapping[str, Decimal | int | str]
    usage_lines: tuple[ProviderUsageLine, ...]
    provider_record_id: str | None = None
    provider_retry_count: int | None = None
    provider_cost_usd: Decimal | int | str | None = None
    provider_upstream_cost_usd: Decimal | int | str | None = None
    computed_cost_usd: Decimal | int | str | None = None
    computed_cost_source: Literal["sdk_catalog", "sdk_rate_registry", "litellm"] | None = None
    computed_cost_confidence: Literal["computed", "estimated", "unknown"] | None = None
    computed_pricing_version: str | None = None
    response_model: str | None = None
    model_candidates: tuple[str, ...] = ()
    billing_dimensions: tuple[tuple[str, str], ...] = ()
    task_input_tokens: int | None = None
    task_output_tokens: int | None = None
    task_cached_tokens: int | None = None
    provider_service: str | None = None

    def __post_init__(self) -> None:
        if (
            self.provider_service is not None
            and _CANONICAL_NAME.fullmatch(self.provider_service) is None
        ):
            raise ValueError(f"invalid provider service {self.provider_service!r}")
        for cost_field in (
            "provider_cost_usd",
            "provider_upstream_cost_usd",
            "computed_cost_usd",
        ):
            cost_value = getattr(self, cost_field)
            if cost_value is not None:
                object.__setattr__(
                    self,
                    cost_field,
                    _quantity(cost_field, cost_value),
                )
        computed_fields = (
            self.computed_cost_source,
            self.computed_cost_confidence,
            self.computed_pricing_version,
        )
        if self.computed_cost_usd is None:
            if any(value is not None for value in computed_fields):
                raise ValueError("computed cost evidence requires computed_cost_usd")
        elif (
            self.computed_cost_source is None
            or self.computed_cost_confidence is None
            or not isinstance(self.computed_pricing_version, str)
            or not self.computed_pricing_version
        ):
            raise ValueError(
                "computed cost evidence requires source, confidence, and pricing version"
            )
        if self.provider_cost_usd is not None and self.computed_cost_usd is not None:
            raise ValueError("provider and SDK-computed cost evidence are mutually exclusive")
        if len(self.billing_dimensions) > 24:
            raise ValueError("provider billing dimensions cannot exceed 24 entries")
        dimension_keys: set[str] = set()
        for key, value in self.billing_dimensions:
            if _CANONICAL_NAME.fullmatch(key) is None:
                raise ValueError(f"invalid provider billing dimension {key!r}")
            if not isinstance(value, str) or not 1 <= len(value) <= 256:
                raise ValueError("provider billing dimension values must contain 1-256 characters")
            if key in dimension_keys:
                raise ValueError(f"duplicate provider billing dimension {key!r}")
            dimension_keys.add(key)
        for field_name in (
            "task_input_tokens",
            "task_output_tokens",
            "task_cached_tokens",
            "provider_retry_count",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer or None")


def _safe_latency_ms(start_time: float) -> int | None:
    try:
        return max(0, int((time.perf_counter() - start_time) * 1000))
    except Exception:  # pragma: no cover - defensive clock failure
        return None


def _usage_details(lines: Sequence[ProviderUsageLine]) -> list[dict[str, str]]:
    positive = [line for line in lines if _quantity(line.metric, line.quantity) > 0]
    if not positive:
        positive = [ProviderUsageLine("request_count", 1, "Requests")]
    identities: set[tuple[str, str]] = set()
    encoded: list[dict[str, str]] = []
    for line in positive:
        identity = (line.metric, line.unit)
        if identity in identities:
            raise ValueError(f"duplicate provider usage line {identity!r}")
        identities.add(identity)
        encoded.append(
            {
                "metric": line.metric,
                "quantity": canonical_decimal(_quantity(line.metric, line.quantity)),
                "unit": line.unit,
            }
        )
    return encoded


def _pricing_breakdown(result: Any) -> list[dict[str, str]]:
    return [
        {
            "dimension": line.dimension,
            "quantity": canonical_decimal(line.quantity),
            "rate_field": line.rate_field,
            "rate_usd": canonical_decimal(line.rate_usd),
            "cost_usd": canonical_decimal(line.cost_usd),
        }
        for line in result.lines
    ]


def record_provider_operation(
    *,
    tracker: Any,
    task: Any,
    provider: str,
    service: str,
    operation: str,
    component: str,
    event_type: str,
    model: str | None,
    measurement: OperationMeasurement,
    latency_ms: int | None,
    status: OperationStatus = "succeeded",
    error: BaseException | None = None,
    capability: CapabilityIdentity | None = None,
    idempotency_key: IdempotencyKey | None = None,
) -> Event:
    """Persist one provider operation without retaining prompts or outputs."""
    resolved_model = measurement.response_model or model or "unknown"
    candidates = tuple(
        candidate
        for candidate in measurement.model_candidates
        if isinstance(candidate, str) and candidate
    )
    pricing = tracker._pricing.get_metered_cost(
        resolved_model,
        measurement.pricing_usage,
        model_candidates=candidates,
    )
    provider_cost = (
        _quantity("provider_cost_usd", measurement.provider_cost_usd)
        if measurement.provider_cost_usd is not None
        else None
    )
    computed_cost = (
        _quantity("computed_cost_usd", measurement.computed_cost_usd)
        if measurement.computed_cost_usd is not None
        else None
    )
    details: dict[str, Any] = {
        "attribution_component": component,
        "attribution_operation_name": operation,
        "attribution_operation_status": status,
        "attribution_resource_type": "model",
        "attribution_resource_id": resolved_model,
        "attribution_usage_lines": _usage_details(measurement.usage_lines),
        "provider_usage_privacy": "quantities_only",
    }
    if measurement.provider_service is not None:
        details["attribution_provider_service"] = measurement.provider_service
    if measurement.billing_dimensions:
        details["attribution_dimensions"] = [
            {"key": key, "value": {"type": "string", "value": value}}
            for key, value in measurement.billing_dimensions
        ]
    if pricing.resolved_model is not None:
        details["pricing_resolved_model"] = pricing.resolved_model
    breakdown = _pricing_breakdown(pricing)
    if breakdown:
        details["pricing_breakdown"] = breakdown
    if pricing.unpriced_dimensions:
        details["pricing_unpriced_dimensions"] = list(pricing.unpriced_dimensions)
    if provider_cost is not None:
        details["provider_reported_cost_usd"] = canonical_decimal(provider_cost)
    if computed_cost is not None:
        details["sdk_computed_cost_usd"] = canonical_decimal(computed_cost)
        if measurement.computed_cost_source == "litellm":
            details["gateway_calculated_cost_usd"] = canonical_decimal(computed_cost)
    if measurement.provider_upstream_cost_usd is not None:
        upstream_cost = _quantity(
            "provider_upstream_cost_usd", measurement.provider_upstream_cost_usd
        )
        details["provider_upstream_cost_usd"] = canonical_decimal(upstream_cost)
    if measurement.provider_record_id:
        details["provider_record_id"] = measurement.provider_record_id[:256]
    if measurement.provider_retry_count is not None:
        details["provider_retry_count"] = measurement.provider_retry_count
        details["provider_attempt_count"] = measurement.provider_retry_count + 1
    if error is not None:
        details.update(error_details(error))

    event = Event(
        task_id=task.task_id,
        event_type=event_type,
        cost_usd=(
            provider_cost
            if provider_cost is not None
            else computed_cost if computed_cost is not None else pricing.cost_usd
        ),
        cost_confidence=(
            "exact"
            if provider_cost is not None
            else measurement.computed_cost_confidence or "unknown"
            if computed_cost is not None
            else pricing.cost_confidence
        ),
        pricing_source=(
            "provider_response"
            if provider_cost is not None
            else (
                "service_catalog"
                if measurement.computed_cost_source == "sdk_catalog"
                else (
                    "litellm"
                    if measurement.computed_cost_source == "litellm"
                    else "rate_registry"
                )
            )
            if computed_cost is not None
            else pricing.pricing_source
        ),
        pricing_version=(
            None
            if provider_cost is not None
            else measurement.computed_pricing_version
            if computed_cost is not None
            else pricing.pricing_version
        ),
        service_name=service,
        provider=provider,
        model=resolved_model,
        input_tokens=measurement.task_input_tokens,
        output_tokens=measurement.task_output_tokens,
        cached_tokens=measurement.task_cached_tokens,
        latency_ms=latency_ms,
        details=details,
    )
    apply_event_capability(event, capability)
    apply_event_idempotency(event, idempotency_key)
    tracker._storage.insert_event(event)
    return event


class ProviderOperationSession:
    """Own one provider call from invocation through stream completion."""

    def __init__(
        self,
        *,
        tracker: Any,
        task_type: str,
        provider: str,
        service: str,
        operation: str,
        component: str,
        model: str | None,
        event_type: str = "external_cost",
    ) -> None:
        self.tracker = tracker
        self.provider = provider
        self.service = service
        self.operation = operation
        self.component = component
        self.event_type = event_type
        self.model = model
        self.start_time = time.perf_counter()
        current_task = get_current_task()
        self.auto_task = current_task is None
        self.task = current_task or create_auto_task(task_type)
        self._task_token = set_current_task(self.task) if self.auto_task else None
        self._context_released = False
        self._finalized = False
        self._capability = get_capability()
        self._idempotency_key = capture_idempotency_key()

    def release_context(self) -> None:
        if self._context_released:
            return
        self._context_released = True
        if self._task_token is not None:
            _reset_current_task(self._task_token)
            self._task_token = None

    def _record(
        self,
        measurement: OperationMeasurement,
        status: OperationStatus,
        error: BaseException | None = None,
    ) -> Event | None:
        if self._finalized:
            return None
        self._finalized = True
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
                model=self.model,
                measurement=measurement,
                latency_ms=_safe_latency_ms(self.start_time),
                status=status,
                error=error,
                capability=self._capability,
                idempotency_key=self._idempotency_key,
            )
            if self.auto_task:
                finalize_auto_task(
                    self.task,
                    event,
                    status="success" if status == "succeeded" else "failed",
                )
                self.tracker._storage.insert_task(self.task)
            return event
        except Exception:
            _log.debug("dexcost: failed to record provider operation", exc_info=True)
            return None

    def succeed(self, measurement: OperationMeasurement) -> Event | None:
        return self._record(measurement, "succeeded")

    def finish(
        self,
        measurement: OperationMeasurement,
        status: OperationStatus,
    ) -> Event | None:
        """Finalize a provider result whose terminal status is response data."""
        return self._record(measurement, status)

    def fail(
        self,
        error: BaseException,
        measurement: OperationMeasurement | None = None,
    ) -> Event | None:
        return self._record(
            measurement or OperationMeasurement(pricing_usage={}, usage_lines=()),
            "failed",
            error,
        )

    def cancel(self, measurement: OperationMeasurement | None = None) -> Event | None:
        return self._record(
            measurement or OperationMeasurement(pricing_usage={}, usage_lines=()),
            "cancelled",
        )

    @property
    def finalized(self) -> bool:
        return self._finalized


class SyncProviderStream(Iterator[Any]):
    """Preserve a provider sync stream while finalizing usage exactly once."""

    def __init__(
        self,
        stream: Any,
        session: ProviderOperationSession,
        *,
        observe: Callable[[Any], None],
        measurement: Callable[[], OperationMeasurement],
        completion_status: Callable[[], OperationStatus] | None = None,
    ) -> None:
        self._stream = stream
        self._session = session
        self._observe = observe
        self._measurement = measurement
        self._completion_status = completion_status or (lambda: "succeeded")

    def __iter__(self) -> SyncProviderStream:
        return self

    def __next__(self) -> Any:
        try:
            item = next(self._stream)
        except StopIteration:
            self._finish_terminal()
            raise
        except BaseException as exc:
            self._fail_with_partial_measurement(exc)
            raise
        self._observe_safely(item)
        return item

    def _observe_safely(self, item: Any) -> None:
        try:
            self._observe(item)
        except Exception:
            # Usage extraction is telemetry-only. A new or malformed provider
            # chunk must never replace a valid provider item with an SDK error.
            _log.debug("dexcost: failed to observe provider stream item", exc_info=True)

    def _measurement_safely(self, phase: str) -> OperationMeasurement:
        try:
            return self._measurement()
        except Exception:
            _log.debug(
                "dexcost: failed to extract %s provider stream usage",
                phase,
                exc_info=True,
            )
            return OperationMeasurement(pricing_usage={}, usage_lines=())

    def _finish_terminal(self) -> None:
        measurement = self._measurement_safely("terminal")
        try:
            status = self._completion_status()
            if status not in {"succeeded", "failed", "cancelled", "unknown"}:
                raise ValueError(f"invalid provider stream completion status {status!r}")
        except Exception:
            _log.debug(
                "dexcost: failed to extract provider stream completion status",
                exc_info=True,
            )
            status = "unknown"
        self._session.finish(measurement, status)

    def _fail_with_partial_measurement(self, exc: BaseException) -> None:
        try:
            measurement = self._measurement()
        except Exception:
            _log.debug("dexcost: failed to extract partial stream usage", exc_info=True)
            measurement = None
        self._session.fail(exc, measurement)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def close(self) -> Any:
        try:
            result = self._stream.close() if hasattr(self._stream, "close") else None
        except BaseException as exc:
            self._fail_with_partial_measurement(exc)
            raise
        self._session.cancel(self._measurement_safely("cancelled"))
        return result

    def __enter__(self) -> SyncProviderStream:
        try:
            if hasattr(self._stream, "__enter__"):
                self._stream.__enter__()
        except BaseException as exc:
            self._fail_with_partial_measurement(exc)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Any:
        try:
            result = (
                self._stream.__exit__(exc_type, exc, tb)
                if hasattr(self._stream, "__exit__")
                else None
            )
        except BaseException as exit_exc:
            self._fail_with_partial_measurement(exit_exc)
            raise
        self._session.cancel(self._measurement_safely("cancelled"))
        return result


class AsyncProviderStream(AsyncIterator[Any]):
    """Preserve a provider async stream while finalizing usage exactly once."""

    def __init__(
        self,
        stream: Any,
        session: ProviderOperationSession,
        *,
        observe: Callable[[Any], None],
        measurement: Callable[[], OperationMeasurement],
        completion_status: Callable[[], OperationStatus] | None = None,
    ) -> None:
        self._stream = stream
        self._session = session
        self._observe = observe
        self._measurement = measurement
        self._completion_status = completion_status or (lambda: "succeeded")

    def __aiter__(self) -> AsyncProviderStream:
        return self

    async def __anext__(self) -> Any:
        try:
            item = await self._stream.__anext__()
        except StopAsyncIteration:
            self._finish_terminal()
            raise
        except BaseException as exc:
            self._fail_with_partial_measurement(exc)
            raise
        self._observe_safely(item)
        return item

    def _observe_safely(self, item: Any) -> None:
        try:
            self._observe(item)
        except Exception:
            # Usage extraction is telemetry-only. A new or malformed provider
            # chunk must never replace a valid provider item with an SDK error.
            _log.debug("dexcost: failed to observe provider stream item", exc_info=True)

    def _measurement_safely(self, phase: str) -> OperationMeasurement:
        try:
            return self._measurement()
        except Exception:
            _log.debug(
                "dexcost: failed to extract %s provider stream usage",
                phase,
                exc_info=True,
            )
            return OperationMeasurement(pricing_usage={}, usage_lines=())

    def _finish_terminal(self) -> None:
        measurement = self._measurement_safely("terminal")
        try:
            status = self._completion_status()
            if status not in {"succeeded", "failed", "cancelled", "unknown"}:
                raise ValueError(f"invalid provider stream completion status {status!r}")
        except Exception:
            _log.debug(
                "dexcost: failed to extract provider stream completion status",
                exc_info=True,
            )
            status = "unknown"
        self._session.finish(measurement, status)

    def _fail_with_partial_measurement(self, exc: BaseException) -> None:
        try:
            measurement = self._measurement()
        except Exception:
            _log.debug("dexcost: failed to extract partial stream usage", exc_info=True)
            measurement = None
        self._session.fail(exc, measurement)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    async def aclose(self) -> Any:
        try:
            result = await self._stream.aclose() if hasattr(self._stream, "aclose") else None
        except BaseException as exc:
            self._fail_with_partial_measurement(exc)
            raise
        self._session.cancel(self._measurement_safely("cancelled"))
        return result

    async def __aenter__(self) -> AsyncProviderStream:
        try:
            if hasattr(self._stream, "__aenter__"):
                await self._stream.__aenter__()
        except BaseException as exc:
            self._fail_with_partial_measurement(exc)
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Any:
        try:
            result = (
                await self._stream.__aexit__(exc_type, exc, tb)
                if hasattr(self._stream, "__aexit__")
                else None
            )
        except BaseException as exit_exc:
            self._fail_with_partial_measurement(exit_exc)
            raise
        self._session.cancel(self._measurement_safely("cancelled"))
        return result


__all__ = [
    "AsyncProviderStream",
    "OperationMeasurement",
    "OperationStatus",
    "ProviderOperationSession",
    "ProviderUsageLine",
    "SyncProviderStream",
    "record_provider_operation",
]
