"""Observable delivery health for DexCost's durable store-and-forward queue."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

_LOG = logging.getLogger(__name__)

DeliveryWorkerState = Literal[
    "local_only",
    "idle",
    "syncing",
    "backoff",
    "auth_failed",
    "stopped",
]
DeliveryErrorOperation = Literal["transport", "authentication", "conversion"]


@dataclass(frozen=True)
class DeliveryErrorEvent:
    """One sanitized delivery failure notification."""

    occurred_at: datetime
    operation: DeliveryErrorOperation
    error_type: str
    message: str
    retryable: bool
    consecutive_failures: int


@dataclass(frozen=True)
class DeliveryStatus:
    """Point-in-time delivery state plus durable queue depths."""

    enabled: bool
    worker_state: DeliveryWorkerState
    pending_events: int
    quarantined_events: int
    pending_tasks: int
    quarantined_tasks: int
    pending_outcomes: int
    quarantined_outcomes: int
    pending_revenues: int
    quarantined_revenues: int
    pending_provider_jobs: int
    quarantined_provider_jobs: int
    oldest_pending_at: datetime | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_type: str | None
    last_error_message: str | None
    consecutive_failures: int
    successful_batches: int
    failed_batches: int
    delivered_records: int
    backoff_seconds: float

    @property
    def pending_records(self) -> int:
        return (
            self.pending_events
            + self.pending_tasks
            + self.pending_outcomes
            + self.pending_revenues
            + self.pending_provider_jobs
        )

    @property
    def quarantined_records(self) -> int:
        return (
            self.quarantined_events
            + self.quarantined_tasks
            + self.quarantined_outcomes
            + self.quarantined_revenues
            + self.quarantined_provider_jobs
        )

    @property
    def healthy(self) -> bool:
        return (
            self.worker_state not in {"auth_failed", "backoff"}
            and self.quarantined_records == 0
        )


DeliveryErrorCallback = Callable[[DeliveryErrorEvent], None]

_CALLBACK_LOCK = threading.Lock()
_ERROR_CALLBACKS: list[DeliveryErrorCallback] = []


def on_delivery_error(callback: DeliveryErrorCallback) -> DeliveryErrorCallback:
    """Register a process-wide failure callback and return it for decorator use."""
    if not callable(callback):
        raise TypeError("delivery error callback must be callable")
    with _CALLBACK_LOCK:
        if callback not in _ERROR_CALLBACKS:
            _ERROR_CALLBACKS.append(callback)
    return callback


def remove_delivery_error_callback(callback: DeliveryErrorCallback) -> bool:
    """Remove a callback, returning whether it had been registered."""
    with _CALLBACK_LOCK:
        try:
            _ERROR_CALLBACKS.remove(callback)
        except ValueError:
            return False
    return True


def _emit_delivery_error(event: DeliveryErrorEvent) -> None:
    with _CALLBACK_LOCK:
        callbacks = tuple(_ERROR_CALLBACKS)
    for callback in callbacks:
        try:
            callback(event)
        except Exception:
            _LOG.warning("delivery error callback failed; ignoring", exc_info=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _queue_counts(storage: Any) -> dict[str, Any]:
    method = getattr(storage, "delivery_counts", None)
    if not callable(method):
        return {}
    try:
        result = method()
    except Exception:
        _LOG.warning("could not read durable delivery queue counts", exc_info=True)
        return {}
    return result if isinstance(result, dict) else {}


def _count(counts: dict[str, Any], key: str) -> int:
    value = counts.get(key, 0)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _oldest(counts: dict[str, Any]) -> datetime | None:
    value = counts.get("oldest_pending_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def local_delivery_status(storage: Any | None = None) -> DeliveryStatus:
    """Build status when cloud delivery is intentionally disabled."""
    counts = {} if storage is None else _queue_counts(storage)
    return DeliveryStatus(
        enabled=False,
        worker_state="local_only",
        pending_events=_count(counts, "pending_events"),
        quarantined_events=_count(counts, "quarantined_events"),
        pending_tasks=_count(counts, "pending_tasks"),
        quarantined_tasks=_count(counts, "quarantined_tasks"),
        pending_outcomes=_count(counts, "pending_outcomes"),
        quarantined_outcomes=_count(counts, "quarantined_outcomes"),
        pending_revenues=_count(counts, "pending_revenues"),
        quarantined_revenues=_count(counts, "quarantined_revenues"),
        pending_provider_jobs=_count(counts, "pending_provider_jobs"),
        quarantined_provider_jobs=_count(counts, "quarantined_provider_jobs"),
        oldest_pending_at=_oldest(counts),
        last_attempt_at=None,
        last_success_at=None,
        last_error_at=None,
        last_error_type=None,
        last_error_message=None,
        consecutive_failures=0,
        successful_batches=0,
        failed_batches=0,
        delivered_records=0,
        backoff_seconds=0,
    )


__all__ = [
    "DeliveryErrorCallback",
    "DeliveryErrorEvent",
    "DeliveryErrorOperation",
    "DeliveryStatus",
    "DeliveryWorkerState",
    "on_delivery_error",
    "remove_delivery_error_callback",
]
