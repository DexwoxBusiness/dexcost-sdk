"""Background event push to Control Layer (US-016).

A daemon thread batches pending events from the local SQLite buffer and
pushes them to the cloud endpoint via HTTPS POST. Exponential backoff keeps
retryable failures pending, while locally unrepresentable records are retained
in a separate quarantine for diagnosis and bounded cleanup.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from dexcost._user_agent import sdk_user_agent
from dexcost.attribution.convert import (
    to_attribution_task_ingest_v1,
    to_business_identity_revision_v1,
)
from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.delivery import (
    DeliveryErrorEvent,
    DeliveryErrorOperation,
    DeliveryStatus,
    DeliveryWorkerState,
    _count,
    _emit_delivery_error,
    _oldest,
    _queue_counts,
    _utcnow,
)
from dexcost.redaction import enforce_metadata_limit, hash_value, redact_dict
from dexcost.session import get_session_manager
from dexcost.storage.protocol import StorageBackend

if TYPE_CHECKING:
    from dexcost.config import DexcostConfig

_log = logging.getLogger(__name__)

_INITIAL_BACKOFF: float = 1.0
_MAX_BACKOFF: float = 300.0  # 5 minutes
_PURGE_INTERVAL: float = 3600.0  # 1 hour between purge runs
_MAX_PAYLOAD_BYTES: int = 120_000  # Headroom below the control-plane 128KB queue limit
_MAX_CONVERSION_SCAN: int = 1000
_CONVERSION_SCAN_MULTIPLIER: int = 10
_CONVERSION_WARNING_INTERVAL: float = 3600.0
_AUTO_SESSION_IDLE_SECONDS: float = 30.0


class _AttributionBatchRejectedError(RuntimeError):
    """The control plane did not accept every record in a prepared leaf."""


class _AttributionConversionError(RuntimeError):
    """One or more durable events cannot be represented by attribution v3."""

    def __init__(self, event_ids: list[str]) -> None:
        self.event_ids = tuple(sorted(event_ids))
        preview = ", ".join(event_ids[:3])
        super().__init__(
            f"{len(event_ids)} event(s) were quarantined because they cannot be "
            f"represented by attribution v3 (event IDs: {preview})"
        )


def _optional_storage_method(storage: StorageBackend, name: str) -> Any | None:
    """Return an implemented optional capability, excluding Protocol stubs.

    A concrete class may explicitly inherit ``StorageBackend`` while only
    implementing an older version of the protocol. Python then exposes later
    Protocol methods as bound, callable ellipsis stubs that return ``None``.
    Treat only a real override (or an instance-supplied callable) as support.
    """
    method = getattr(storage, name, None)
    if not callable(method):
        return None
    protocol_stub = getattr(StorageBackend, name, None)
    if getattr(method, "__func__", None) is protocol_stub:
        return None
    return method


def _mark_event_snapshots_synced(
    storage: StorageBackend,
    event_ids: list[str],
    sync_versions: dict[str, int] | None,
) -> None:
    """Acknowledge posted event snapshots without consuming newer mutations."""
    marker = _optional_storage_method(storage, "mark_event_deliveries_synced")
    if sync_versions is not None and marker is not None:
        versioned = [
            (event_id, sync_versions[event_id])
            for event_id in event_ids
            if event_id in sync_versions
        ]
        marker(versioned)
        return
    storage.mark_synced(event_ids)


def _mark_event_snapshots_quarantined(
    storage: StorageBackend,
    event_ids: list[str],
    sync_versions: dict[str, int] | None,
) -> None:
    """Quarantine converter failures without hiding a corrected revision."""
    marker = _optional_storage_method(storage, "mark_event_deliveries_quarantined")
    if sync_versions is not None and marker is not None:
        versioned = [
            (event_id, sync_versions[event_id])
            for event_id in event_ids
            if event_id in sync_versions
        ]
        marker(versioned)
        return
    storage.mark_quarantined(event_ids)


def _mark_task_snapshots_synced(
    storage: StorageBackend,
    task_ids: list[str],
    sync_versions: dict[str, int] | None,
) -> None:
    """Acknowledge posted task snapshots without consuming newer rollups."""
    marker = _optional_storage_method(storage, "mark_task_deliveries_synced")
    if sync_versions is not None and marker is not None:
        versioned = [
            (task_id, sync_versions[task_id])
            for task_id in task_ids
            if task_id in sync_versions
        ]
        marker(versioned)
        return
    storage.mark_tasks_synced(task_ids)


def _mark_task_snapshots_quarantined(
    storage: StorageBackend,
    task_ids: list[str],
    sync_versions: dict[str, int] | None,
) -> None:
    """Quarantine oversized task snapshots without hiding a newer rollup."""
    marker = _optional_storage_method(storage, "mark_task_deliveries_quarantined")
    if sync_versions is not None and marker is not None:
        versioned = [
            (task_id, sync_versions[task_id])
            for task_id in task_ids
            if task_id in sync_versions
        ]
        marker(versioned)
        return
    marker = _optional_storage_method(storage, "mark_tasks_quarantined")
    if marker is not None:
        marker(task_ids)
        return

    # StorageBackend implementations from before task quarantine was added
    # cannot retain a terminal-failure marker.  Advance the legacy snapshot
    # through its guaranteed acknowledgement method so one oversized task
    # cannot remain pending and drive the worker into a tight retry loop.
    storage.mark_tasks_synced(task_ids)


class SyncWorker:
    """Background worker that pushes events to the Control Layer.

    Events and their related tasks are read from the local storage buffer,
    redacted according to the SDK configuration, then POSTed as a JSON
    object to ``{config.endpoint}/v1/ingest``.  On success (HTTP 202)
    the events are marked as synced.  On failure the worker backs off exponentially (starting
    at 1 s, doubling up to 300 s).

    The worker runs as a daemon thread so it is automatically terminated
    when the main process exits.

    Parameters
    ----------
    config:
        SDK configuration (carries endpoint, API key, batch size, etc.).
    storage:
        A :class:`StorageBackend` used for **direct** (same-thread) calls
        such as :meth:`_sync_batch` invoked from the calling thread.
    db_path:
        Optional path to the SQLite database.  When provided the worker
        opens its **own** connection inside the background thread, which
        is required because SQLite connections cannot be shared across
        threads.  If omitted the caller-supplied *storage* is used
        directly (safe only when :meth:`_sync_batch` is called from the
        same thread that created *storage*).
    """

    def __init__(
        self,
        config: DexcostConfig,
        storage: StorageBackend,
        db_path: str | Path | None = None,
    ) -> None:
        self._config = config
        self._storage = storage
        self._db_path = db_path

        # Threading primitives
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._flush_done = threading.Event()
        self._flush_requested = False
        self._finalize_sessions_requested = False
        self._flush_lock = threading.Lock()

        self._backoff: float = _INITIAL_BACKOFF
        self._last_purge: float = 0.0
        self._last_conversion_warning_at: float = 0.0
        self._last_conversion_warning_key: tuple[str, ...] = ()
        self._quarantine_recovery_attempted = False
        self._quarantine_recovery_active = False

        self._thread: threading.Thread | None = None

        # Delivery observability is process-local; queue depths below are
        # always read from the durable storage snapshot on demand.
        self._health_lock = threading.Lock()
        self._worker_state: DeliveryWorkerState = "stopped"
        self._last_attempt_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_error_type: str | None = None
        self._last_error_message: str | None = None
        self._consecutive_failures = 0
        self._successful_batches = 0
        self._failed_batches = 0
        self._delivered_records = 0
        self._active_post_stats: dict[str, int] | None = None

    # ── Public API ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background sync thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._set_worker_state("idle")
        self._thread = threading.Thread(target=self._run, daemon=True, name="dexcost-sync")
        self._thread.start()
        _log.debug("SyncWorker started")

    def stop(self) -> None:
        """Signal the worker to stop and wait for it to finish."""
        self._stop_event.set()
        self._wake_event.set()  # unblock any wait
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        self._set_worker_state("stopped")
        _log.debug("SyncWorker stopped")

    def flush(self) -> None:
        """Force an immediate sync cycle (blocking).

        Blocks until the current batch is pushed or an error occurs.
        """
        self._flush_done.clear()
        with self._flush_lock:
            self._flush_requested = True
            # An explicit flush (including close()) is a session boundary.
            # The worker consumes this request using its thread-local storage
            # connection before it acknowledges the flush.
            self._finalize_sessions_requested = True
        self._wake_event.set()
        self._flush_done.wait(timeout=30.0)

    def resume_after_auth(self) -> None:
        """Clear an authentication stop and reset delivery backoff."""
        self._stop_event.clear()
        self._backoff = _INITIAL_BACKOFF
        with self._health_lock:
            self._consecutive_failures = 0
            self._worker_state = "idle"

    def status(self, storage: StorageBackend | None = None) -> DeliveryStatus:
        """Return worker health joined with current durable queue depths."""
        counts = _queue_counts(storage if storage is not None else self._storage)
        with self._health_lock:
            return DeliveryStatus(
                enabled=True,
                worker_state=self._worker_state,
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
                last_attempt_at=self._last_attempt_at,
                last_success_at=self._last_success_at,
                last_error_at=self._last_error_at,
                last_error_type=self._last_error_type,
                last_error_message=self._last_error_message,
                consecutive_failures=self._consecutive_failures,
                successful_batches=self._successful_batches,
                failed_batches=self._failed_batches,
                delivered_records=self._delivered_records,
                backoff_seconds=(self._backoff if self._worker_state == "backoff" else 0),
            )

    def _set_worker_state(self, state: DeliveryWorkerState) -> None:
        with self._health_lock:
            self._worker_state = state

    def _record_attempt(self) -> None:
        with self._health_lock:
            self._last_attempt_at = _utcnow()
            self._worker_state = "syncing"

    def _record_delivery_progress_locked(self, delivered_records: int) -> None:
        """Record accepted leaves while ``_health_lock`` is held."""
        if delivered_records > 0:
            self._last_success_at = _utcnow()
            self._successful_batches += 1
            self._delivered_records += delivered_records

    def _record_delivery_progress(self, delivered_records: int) -> None:
        """Preserve accepted-leaf counters without masking a sibling failure."""
        with self._health_lock:
            self._record_delivery_progress_locked(delivered_records)

    def _record_success(self, delivered_records: int) -> None:
        with self._health_lock:
            self._consecutive_failures = 0
            self._record_delivery_progress_locked(delivered_records)
            self._worker_state = "idle"

    def _record_error(
        self,
        exc: BaseException,
        *,
        operation: DeliveryErrorOperation,
        retryable: bool,
        state: DeliveryWorkerState,
    ) -> None:
        message = str(exc)
        api_key = self._config.api_key
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        message = message[:1024]
        occurred_at = _utcnow()
        with self._health_lock:
            self._last_error_at = occurred_at
            self._last_error_type = type(exc).__name__
            self._last_error_message = message
            self._consecutive_failures += 1
            self._failed_batches += 1
            self._worker_state = state
            consecutive_failures = self._consecutive_failures
        _emit_delivery_error(
            DeliveryErrorEvent(
                occurred_at=occurred_at,
                operation=operation,
                error_type=type(exc).__name__,
                message=message,
                retryable=retryable,
                consecutive_failures=consecutive_failures,
            )
        )

    # ── Internal ──────────────────────────────────────────────────────

    def _open_thread_storage(self) -> StorageBackend:
        """Open a thread-local storage connection.

        If *db_path* was provided at construction, a fresh
        :class:`SQLiteStorage` is created (safe for this thread).
        Otherwise the caller-supplied storage is returned as-is.
        """
        if self._db_path is not None:
            from dexcost.storage.sqlite import SQLiteStorage

            return SQLiteStorage(db_path=self._db_path)
        return self._storage

    def _run(self) -> None:
        """Main loop for the background thread."""
        storage = self._open_thread_storage()
        owns_storage = storage is not self._storage
        try:
            while not self._stop_event.is_set():
                wait_seconds = self._config.flush_interval_seconds
                try:
                    sent = self._sync_batch(storage=storage)
                    if sent:
                        # Reset backoff on success; immediately try again
                        self._backoff = _INITIAL_BACKOFF
                        continue
                    if self._worker_state != "auth_failed":
                        self._set_worker_state("idle")
                    # Nothing to send — mark flush done if requested
                    with self._flush_lock:
                        # A flush request can arrive while a batch is already in
                        # progress. Do another immediate cycle so its forced
                        # session finalization is not acknowledged prematurely.
                        if self._finalize_sessions_requested:
                            continue
                        if self._flush_requested:
                            self._flush_requested = False
                            self._flush_done.set()
                except _AttributionConversionError as exc:
                    # A conversion failure is local data state, not a transport
                    # outage. Quarantined rows leave the normal pending scan; if
                    # storage could not persist quarantine, suppress log floods.
                    self._warn_conversion_failure(exc)
                    self._backoff = _INITIAL_BACKOFF
                    self._record_error(
                        exc,
                        operation="conversion",
                        retryable=False,
                        state="idle",
                    )
                    with self._flush_lock:
                        if self._flush_requested:
                            self._flush_requested = False
                            self._flush_done.set()
                except Exception as exc:
                    _log.exception("SyncWorker error during batch push")
                    # Signal flush done even on error so caller doesn't hang
                    with self._flush_lock:
                        if self._flush_requested:
                            self._flush_requested = False
                            self._flush_done.set()
                    # Back off
                    self._backoff = min(self._backoff * 2, _MAX_BACKOFF)
                    wait_seconds = self._backoff
                    self._record_error(
                        exc,
                        operation="transport",
                        retryable=True,
                        state="backoff",
                    )

                if self._stop_event.is_set():
                    break
                # Idle workers wait for the flush interval. Failed workers use the
                # exponential backoff computed above.
                self._wake_event.wait(timeout=wait_seconds)
                self._wake_event.clear()
        finally:
            if owns_storage:
                storage.close()
            if self._worker_state != "auth_failed":
                self._set_worker_state("stopped")

    def _sync_batch(self, storage: StorageBackend | None = None) -> bool:
        """Attempt to push one batch of events.

        Returns ``True`` if records were sent and ``False`` if there were no
        pending records or sync was permanently disabled by authentication.

        Raises on HTTP/network errors so the caller can back off.

        Parameters
        ----------
        storage:
            Override storage backend.  Used by :meth:`_run` to pass the
            thread-local connection.  When ``None`` the instance-level
            ``_storage`` is used (suitable for same-thread calls).
        """
        st = storage if storage is not None else self._storage
        with self._flush_lock:
            force_finalize_sessions = self._finalize_sessions_requested
            self._finalize_sessions_requested = False
        get_session_manager().finalize_idle_sessions(
            idle_seconds=(0.0 if force_finalize_sessions else _AUTO_SESSION_IDLE_SECONDS),
            storage=st,
        )
        self._recover_quarantined_events(st)
        self._purge_if_due(st)
        batch_size = max(1, self._config.batch_size)

        # Quarantine failed conversion pages before reading the next page, so
        # malformed legacy rows cannot block newer valid attribution data.
        event_dicts: list[dict[str, Any]] = []
        event_sync_versions: dict[str, int] | None = None
        failed_event_ids: list[str] = []
        seen_event_ids: set[str] = set()
        scan_limit = max(
            batch_size,
            min(_MAX_CONVERSION_SCAN, batch_size * _CONVERSION_SCAN_MULTIPLIER),
        )
        scanned = 0

        while len(event_dicts) < batch_size and scanned < scan_limit:
            page_limit = min(batch_size - len(event_dicts), scan_limit - scanned)
            query_event_deliveries = _optional_storage_method(
                st, "query_event_deliveries_for_sync"
            )
            if query_event_deliveries is not None:
                if event_sync_versions is None:
                    event_sync_versions = {}
                event_deliveries = query_event_deliveries(limit=page_limit)
                events = [event for event, _version in event_deliveries]
                event_sync_versions.update(
                    (str(event.event_id), version) for event, version in event_deliveries
                )
            else:
                events = st.query_events_for_sync(limit=page_limit)
            if not events:
                break

            page_failed_event_ids: list[str] = []
            newly_scanned = 0
            for event in events:
                event_id = str(event.event_id)
                if event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)
                newly_scanned += 1
                scanned += 1
                converted = self._prepare_event_dict(event)
                if converted is None:
                    page_failed_event_ids.append(event_id)
                else:
                    event_dicts.append(converted)

            if page_failed_event_ids:
                _mark_event_snapshots_quarantined(st, page_failed_event_ids, event_sync_versions)
                failed_event_ids.extend(page_failed_event_ids)

            # If quarantine could not advance storage, the next query returns
            # the same rows. Stop this cycle rather than spinning forever.
            if newly_scanned == 0 or len(events) < page_limit:
                break

        # Gather pending (not-yet-synced) tasks for the ingest payload.
        # Only pending tasks are pushed, so synced tasks are never re-POSTed
        # on every cycle.  query_pending_tasks_for_sync covers tasks
        # referenced by this event batch as well as any other unsynced
        # tasks — e.g. explicit dexcost.task() with customer_id where LLM
        # events went to auto-tasks in threads.
        tasks: list[Any] = []
        task_sync_versions: dict[str, int] | None = None
        query_task_deliveries = _optional_storage_method(st, "query_task_deliveries_for_sync")
        if query_task_deliveries is not None:
            task_sync_versions = {}
            task_deliveries = query_task_deliveries()
            tasks = [task for task, _version in task_deliveries]
            task_sync_versions.update(
                (str(task.task_id), version) for task, version in task_deliveries
            )
        elif (
            query_pending_tasks := _optional_storage_method(
                st, "query_pending_tasks_for_sync"
            )
        ) is not None:
            tasks = query_pending_tasks()
        else:
            # Backend without task sync tracking — fall back to task IDs
            # referenced by this event batch.
            tasks = st.query_tasks_for_sync(list({str(event["task_id"]) for event in event_dicts}))
        task_dicts: list[dict[str, Any]] = [self._prepare_task_dict(t) for t in tasks]
        business_identities = [
            identity
            for task in tasks
            if (identity := self._prepare_business_identity(task)) is not None
        ]
        # Outcome persistence was added after the public StorageBackend
        # protocol had already been adopted by custom backends. Keep outcome
        # sync capability-based so upgrading dexcost cannot block their
        # existing event/task delivery when no outcomes are recorded.
        query_outcomes = _optional_storage_method(st, "query_outcomes_for_sync")
        outcomes = query_outcomes(limit=batch_size) if query_outcomes is not None else []
        outcome_dicts = [outcome.to_dict() for outcome in outcomes]
        query_revenues = _optional_storage_method(st, "query_revenues_for_sync")
        revenues = query_revenues(limit=batch_size) if query_revenues is not None else []
        revenue_dicts = [revenue.to_dict() for revenue in revenues]

        # Provider jobs already are strict attribution-v3 revision streams.
        # Keep this capability-based for existing custom storage backends.
        query_provider_jobs = _optional_storage_method(st, "query_provider_jobs_for_sync")
        provider_jobs = (
            query_provider_jobs(limit=max(1, batch_size - len(event_dicts)))
            if query_provider_jobs is not None and len(event_dicts) < batch_size
            else []
        )
        provider_job_dicts = [
            cast(
                dict[str, Any],
                job.to_attribution_observation(environment=self._config.environment),
            )
            for job in provider_jobs
        ]
        provider_job_revisions = {
            (str(job.event_id), job.revision) for job in provider_jobs
        }
        event_dicts.extend(provider_job_dicts)

        if not event_dicts and not task_dicts and not outcome_dicts and not revenue_dicts:
            self._finish_quarantine_recovery_if_drained(st)
            self._raise_conversion_failure(failed_event_ids)
            return False

        self._record_attempt()
        post_stats = {"delivered_records": 0, "quarantined_records": 0}
        self._active_post_stats = post_stats
        try:
            posted = self._post_with_split(
                event_dicts,
                task_dicts,
                business_identities,
                storage=st,
                outcomes=outcome_dicts,
                revenue_revisions=revenue_dicts,
                event_sync_versions=event_sync_versions,
                task_sync_versions=task_sync_versions,
                provider_job_revisions=provider_job_revisions,
            )
        except Exception:
            self._record_delivery_progress(post_stats["delivered_records"])
            raise
        finally:
            self._active_post_stats = None
        if not posted:
            self._record_delivery_progress(post_stats["delivered_records"])
            if self._stop_event.is_set():
                return False
            raise _AttributionBatchRejectedError(
                "control plane did not accept the complete attribution batch"
            )

        _log.info(
            "Delivered %d records and quarantined %d undeliverable records to %s",
            post_stats["delivered_records"],
            post_stats["quarantined_records"],
            self._config.endpoint,
        )

        self._record_success(post_stats["delivered_records"])

        self._finish_quarantine_recovery_if_drained(st)
        self._raise_conversion_failure(failed_event_ids)
        return True

    def _recover_quarantined_events(self, storage: StorageBackend) -> None:
        """Requeue retained converter failures once per worker lifetime.

        Converter defects are fixed by SDK upgrades. Replaying the durable
        quarantine after startup lets corrected rows enter the ordinary,
        idempotent delivery path without requiring users to edit SQLite files.
        """
        if self._quarantine_recovery_attempted:
            return
        self._quarantine_recovery_attempted = True
        restored = 0
        requeue = _optional_storage_method(storage, "requeue_quarantined_events")
        if requeue is not None:
            try:
                restored = int(requeue())
            except Exception:
                _log.warning("requeue_quarantined_events failed", exc_info=True)
                return
        else:
            # Compatibility path for third-party storage backends released
            # before the atomic requeue operation was added. Only rows that
            # the current converter can represent are requeued.
            query = _optional_storage_method(storage, "query_quarantined_events")
            if query is None:
                return
            for event in query(limit=_MAX_CONVERSION_SCAN):
                if self._prepare_event_dict(event) is None:
                    continue
                storage.update_event(event)
                restored += 1
        if restored:
            self._quarantine_recovery_active = True
            _log.info(
                "Requeued %d retained attribution event(s) after converter upgrade",
                restored,
            )

    def _finish_quarantine_recovery_if_drained(self, storage: StorageBackend) -> None:
        if not self._quarantine_recovery_active:
            return
        try:
            if not storage.query_events_for_sync(limit=1):
                self._quarantine_recovery_active = False
        except Exception:
            _log.debug("Could not inspect quarantine recovery progress", exc_info=True)

    def _purge_if_due(self, storage: StorageBackend) -> None:
        """Clean acknowledged events without deleting unsent attribution."""
        now = time.monotonic()
        if now - self._last_purge < _PURGE_INTERVAL:
            return
        self._last_purge = now
        try:
            deleted = storage.purge_synced()
            if deleted:
                _log.info("Purged %d old synced events", deleted)
        except Exception:
            _log.warning("purge_synced failed", exc_info=True)

    def _warn_conversion_failure(self, exc: _AttributionConversionError) -> None:
        """Throttle duplicate background warnings for the same event set."""
        now = time.monotonic()
        if (
            exc.event_ids != self._last_conversion_warning_key
            or now - self._last_conversion_warning_at >= _CONVERSION_WARNING_INTERVAL
        ):
            _log.warning("%s", exc)
            self._last_conversion_warning_key = exc.event_ids
            self._last_conversion_warning_at = now

    @staticmethod
    def _raise_conversion_failure(event_ids: list[str]) -> None:
        if not event_ids:
            return
        raise _AttributionConversionError(event_ids)

    def _hash_pii(self, d: dict[str, Any]) -> None:
        """Hash ``customer_id`` / ``project_id`` keys in-place, if configured.

        Operates on the top-level dict (event/task payload) and, when
        present, its nested ``details`` / ``metadata`` sub-dict.
        """
        if not self._config.hash_customer_id:
            return
        for container in (d, d.get("details"), d.get("metadata")):
            if not isinstance(container, dict):
                continue
            for key in ("customer_id", "project_id"):
                val = container.get(key)
                if isinstance(val, str):
                    container[key] = hash_value(val)

    def _prepare_event_dict(self, event: Any) -> dict[str, Any] | None:
        """Convert one event to the strict, details-free v3 wire contract.

        Arbitrary ``details`` never cross the process boundary. The converter
        reads only the accounting allow-list needed by attribution v3.
        """
        sanitized = event
        if self._config.redact_fields and isinstance(event.details, dict):
            # Do not mutate durable capture: a later config may permit fields
            # that this push redacts. Conversion must only see the sanitized
            # allow-list so request/call/resource identifiers cannot bypass
            # configured field-level redaction.
            sanitized = copy.copy(event)
            sanitized.details = redact_dict(event.details, self._config.redact_fields)
            dimensions = sanitized.details.get("attribution_dimensions")
            if isinstance(dimensions, list):
                # Billing dimensions promote values from an array, so redact
                # by the logical dimension key before v3 conversion.
                sanitized.details["attribution_dimensions"] = [
                    candidate
                    for candidate in dimensions
                    if not (
                        isinstance(candidate, dict)
                        and candidate.get("key") in self._config.redact_fields
                    )
                ]
        return cast(
            dict[str, Any] | None,
            to_attribution_observation_v3(sanitized, environment=self._config.environment),
        )

    def _prepare_task_dict(self, task: Any) -> dict[str, Any]:
        """Serialise a task and apply the same redaction policy as events.

        The task's ``metadata`` dict is redacted + size-limited, and the
        denormalised ``customer_id`` / ``project_id`` columns are hashed
        when ``hash_customer_id`` is configured — closing a PII leak where
        task metadata and customer ids were previously POSTed raw.
        """
        d = cast(dict[str, Any], to_attribution_task_ingest_v1(task))

        # Redact configured PII fields from task metadata.
        if self._config.redact_fields and d.get("metadata"):
            d["metadata"] = redact_dict(d["metadata"], self._config.redact_fields)

        # Hash customer_id / project_id — both the top-level task columns
        # and any copies nested inside metadata.
        self._hash_pii(d)

        # Enforce the metadata size limit (same 10KB cap as event details).
        if d.get("metadata"):
            d["metadata"] = enforce_metadata_limit(d["metadata"])

        return d

    def _prepare_business_identity(self, task: Any) -> dict[str, Any] | None:
        """Serialize an opted-in immutable business-identity snapshot."""
        # Running tasks may be pushed more than once. Publish the immutable
        # revision only with the final task update so a later privacy-config
        # change cannot rewrite revision 1 after an early running snapshot.
        if task.ended_at is None:
            return None
        identity = cast(
            dict[str, Any] | None,
            to_business_identity_revision_v1(task),
        )
        if identity is None:
            return None
        assignment = cast(dict[str, Any], identity["assignment"])
        if self._config.redact_fields:
            for key in tuple(assignment):
                if key in self._config.redact_fields:
                    assignment.pop(key, None)
            # The wire contract forbids a variant without its experiment.
            if "experiment_id" not in assignment:
                assignment.pop("variant", None)
            redact_fields = set(self._config.redact_fields)
            if redact_fields.intersection({"agent", "agent_id", "id"}):
                identity.pop("agent", None)
            elif redact_fields.intersection({"agent_version", "version"}):
                agent = identity.get("agent")
                if isinstance(agent, dict):
                    agent.pop("version", None)
            if redact_fields.intersection({"workflow", "workflow_id", "id"}):
                identity.pop("workflow", None)
            elif redact_fields.intersection({"workflow_session_id", "session_id"}):
                workflow = identity.get("workflow")
                if isinstance(workflow, dict):
                    workflow.pop("session_id", None)
        self._hash_pii(assignment)
        return identity

    def _post_with_split(
        self,
        events: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        business_identities: list[dict[str, Any]] | None = None,
        depth: int = 0,
        storage: StorageBackend | None = None,
        outcomes: list[dict[str, Any]] | None = None,
        revenue_revisions: list[dict[str, Any]] | None = None,
        event_sync_versions: dict[str, int] | None = None,
        task_sync_versions: dict[str, int] | None = None,
        provider_job_revisions: set[tuple[str, int]] | None = None,
    ) -> bool:
        """POST records, splitting every durable stream below the queue limit.

        Successful leaves are acknowledged immediately. This prevents a later
        sibling failure from replaying records the control plane already
        accepted. Tasks are sent before events when they must be separated.
        """
        identities = business_identities or []
        outcome_revisions = outcomes or []
        revenues = revenue_revisions or []
        event_versions = event_sync_versions
        task_versions = task_sync_versions
        provider_job_keys = provider_job_revisions or set()
        payload: dict[str, Any] = {
            "events": events,
            "tasks": tasks,
            "business_identities": identities,
            "outcomes": outcome_revisions,
            "revenue_revisions": revenues,
        }
        body = json.dumps(payload).encode("utf-8")

        if len(body) <= _MAX_PAYLOAD_BYTES:
            posted = self._post_raw(body)
            if posted and storage is not None:
                event_ids: list[str] = []
                provider_job_ids: list[tuple[str, int]] = []
                for event in events:
                    event_id = str(event["event_id"])
                    lifecycle = event.get("lifecycle")
                    revision = (
                        lifecycle.get("revision") if isinstance(lifecycle, dict) else None
                    )
                    candidate = (
                        (event_id, revision) if isinstance(revision, int) else None
                    )
                    if candidate is not None and candidate in provider_job_keys:
                        provider_job_ids.append(candidate)
                    else:
                        event_ids.append(event_id)
                task_ids = [str(task["task_id"]) for task in tasks]
                outcome_ids = [
                    (
                        str(outcome["outcome_id"]),
                        int(cast(dict[str, Any], outcome["lifecycle"])["revision"]),
                    )
                    for outcome in outcome_revisions
                ]
                revenue_ids = [
                    (
                        str(revenue["revenue_id"]),
                        int(cast(dict[str, Any], revenue["lifecycle"])["revision"]),
                    )
                    for revenue in revenues
                ]
                if event_ids:
                    _mark_event_snapshots_synced(storage, event_ids, event_versions)
                if provider_job_ids:
                    mark_provider_jobs = _optional_storage_method(
                        storage, "mark_provider_jobs_synced"
                    )
                    if mark_provider_jobs is not None:
                        mark_provider_jobs(provider_job_ids)
                if task_ids:
                    _mark_task_snapshots_synced(storage, task_ids, task_versions)
                if outcome_ids:
                    storage.mark_outcomes_synced(outcome_ids)
                mark_revenues = _optional_storage_method(storage, "mark_revenues_synced")
                if revenue_ids and mark_revenues is not None:
                    mark_revenues(revenue_ids)
            if posted and self._active_post_stats is not None:
                self._active_post_stats["delivered_records"] += (
                    len(events)
                    + len(tasks)
                    + len(identities)
                    + len(outcome_revisions)
                    + len(revenues)
                )
            return posted

        if len(events) > 1:
            mid = len(events) // 2
            _log.info(
                "Batch too large (%d bytes, %d events), splitting events",
                len(body),
                len(events),
            )
            first_posted = self._post_with_split(
                events[:mid],
                tasks,
                identities,
                depth + 1,
                storage,
                outcome_revisions,
                revenues,
                event_versions,
                task_versions,
                provider_job_keys,
            )
            if not first_posted:
                return False
            return self._post_with_split(
                events[mid:],
                [],
                [],
                depth + 1,
                storage,
                [],
                [],
                event_versions,
                task_versions,
                provider_job_keys,
            )

        if len(tasks) > 1:
            mid = len(tasks) // 2
            first_task_ids = {str(task["task_id"]) for task in tasks[:mid]}
            first_identities = [
                identity for identity in identities if str(identity["task_id"]) in first_task_ids
            ]
            second_identities = [
                identity
                for identity in identities
                if str(identity["task_id"]) not in first_task_ids
            ]
            _log.info(
                "Batch too large (%d bytes, %d tasks), splitting tasks",
                len(body),
                len(tasks),
            )
            first_posted = self._post_with_split(
                [],
                tasks[:mid],
                first_identities,
                depth + 1,
                storage,
                [],
                [],
                event_versions,
                task_versions,
                provider_job_keys,
            )
            if not first_posted:
                return False
            return self._post_with_split(
                events,
                tasks[mid:],
                second_identities,
                depth + 1,
                storage,
                outcome_revisions,
                revenues,
                event_versions,
                task_versions,
                provider_job_keys,
            )

        if len(outcome_revisions) > 1:
            mid = len(outcome_revisions) // 2
            _log.info(
                "Batch too large (%d bytes, %d outcomes), splitting outcomes",
                len(body),
                len(outcome_revisions),
            )
            first_posted = self._post_with_split(
                events,
                tasks,
                identities,
                depth + 1,
                storage,
                outcome_revisions[:mid],
                [],
                event_versions,
                task_versions,
                provider_job_keys,
            )
            if not first_posted:
                return False
            return self._post_with_split(
                [],
                [],
                [],
                depth + 1,
                storage,
                outcome_revisions[mid:],
                revenues,
                event_versions,
                task_versions,
                provider_job_keys,
            )

        if len(revenues) > 1:
            mid = len(revenues) // 2
            _log.info(
                "Batch too large (%d bytes, %d revenue revisions), splitting revenue",
                len(body),
                len(revenues),
            )
            first_posted = self._post_with_split(
                events,
                tasks,
                identities,
                depth + 1,
                storage,
                outcome_revisions,
                revenues[:mid],
                event_versions,
                task_versions,
                provider_job_keys,
            )
            if not first_posted:
                return False
            return self._post_with_split(
                [],
                [],
                [],
                depth + 1,
                storage,
                [],
                revenues[mid:],
                event_versions,
                task_versions,
                provider_job_keys,
            )

        durable_count = len(events) + len(tasks) + len(outcome_revisions) + len(revenues)
        if durable_count > 1 and tasks:
            task_posted = self._post_with_split(
                [],
                tasks,
                identities,
                depth + 1,
                storage,
                [],
                [],
                event_versions,
                task_versions,
                provider_job_keys,
            )
            if not task_posted:
                return False
            return self._post_with_split(
                events,
                [],
                [],
                depth + 1,
                storage,
                outcome_revisions,
                revenues,
                event_versions,
                task_versions,
                provider_job_keys,
            )

        if durable_count > 1 and outcome_revisions:
            outcome_posted = self._post_with_split(
                [],
                [],
                [],
                depth + 1,
                storage,
                outcome_revisions,
                [],
                event_versions,
                task_versions,
                provider_job_keys,
            )
            if not outcome_posted:
                return False
            return self._post_with_split(
                events,
                [],
                [],
                depth + 1,
                storage,
                [],
                revenues,
                event_versions,
                task_versions,
                provider_job_keys,
            )

        if durable_count > 1 and revenues:
            revenue_posted = self._post_with_split(
                [],
                [],
                [],
                depth + 1,
                storage,
                [],
                revenues,
                event_versions,
                task_versions,
                provider_job_keys,
            )
            if not revenue_posted:
                return False
            return self._post_with_split(
                events,
                [],
                [],
                depth + 1,
                storage,
                [],
                [],
                event_versions,
                task_versions,
                provider_job_keys,
            )

        if len(events) == 1:
            _log.warning(
                "Single event exceeds payload limit (%d bytes), quarantining",
                len(body),
            )
            if storage is not None:
                event_id = str(events[0]["event_id"])
                lifecycle = events[0].get("lifecycle")
                revision = lifecycle.get("revision") if isinstance(lifecycle, dict) else None
                provider_job_key = (
                    (event_id, revision) if isinstance(revision, int) else None
                )
                if provider_job_key is not None and provider_job_key in provider_job_keys:
                    marker = _optional_storage_method(storage, "mark_provider_jobs_quarantined")
                    if marker is not None:
                        marker([provider_job_key])
                else:
                    _mark_event_snapshots_quarantined(
                        storage, [event_id], event_versions
                    )
            if self._active_post_stats is not None:
                self._active_post_stats["quarantined_records"] += 1
            return True

        if len(tasks) == 1:
            _log.warning(
                "Single task exceeds payload limit (%d bytes), quarantining",
                len(body),
            )
            if storage is not None:
                _mark_task_snapshots_quarantined(
                    storage, [str(tasks[0]["task_id"])], task_versions
                )
            if self._active_post_stats is not None:
                self._active_post_stats["quarantined_records"] += 1
            return True

        if len(outcome_revisions) == 1:
            outcome = outcome_revisions[0]
            revision = (
                str(outcome["outcome_id"]),
                int(cast(dict[str, Any], outcome["lifecycle"])["revision"]),
            )
            _log.warning(
                "Single outcome exceeds payload limit (%d bytes), quarantining",
                len(body),
            )
            if storage is not None:
                storage.mark_outcomes_quarantined([revision])
            if self._active_post_stats is not None:
                self._active_post_stats["quarantined_records"] += 1
            return True

        if len(revenues) == 1:
            revenue = revenues[0]
            revision = (
                str(revenue["revenue_id"]),
                int(cast(dict[str, Any], revenue["lifecycle"])["revision"]),
            )
            _log.warning(
                "Single revenue revision exceeds payload limit (%d bytes), quarantining",
                len(body),
            )
            if storage is not None:
                mark_quarantined = _optional_storage_method(
                    storage, "mark_revenues_quarantined"
                )
                if mark_quarantined is not None:
                    mark_quarantined([revision])
            if self._active_post_stats is not None:
                self._active_post_stats["quarantined_records"] += 1
        return True

    def _post_raw(self, body: bytes) -> bool:
        """POST pre-encoded payload to the cloud ingest endpoint.

        Uses :mod:`urllib.request` (stdlib) to avoid adding an external
        dependency. Returns ``True`` only when the whole leaf was accepted.
        Network and retryable HTTP failures still raise so the worker backs off.
        """
        url = f"{self._config.endpoint}/v1/ingest"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._config.api_key}",
                "User-Agent": sdk_user_agent(),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status: int = resp.status
                if status >= 300:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        f"Unexpected status {status}",
                        Message(),
                        None,
                    )
                try:
                    result = json.loads(resp.read())
                    rejected = result.get("rejected", 0)
                    if isinstance(rejected, (int, float)) and rejected > 0:
                        _log.warning(
                            "Control plane rejected %s item(s) from an attribution-v3 batch",
                            rejected,
                        )
                        return False
                except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
                    # Some compatible/private endpoints return an empty body.
                    pass
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 413:
                _log.warning("Server returned 413 despite pre-split check")
                return False
            if exc.code == 401:
                _log.error("API key rejected (HTTP 401) — disabling sync")
                self._record_error(
                    exc,
                    operation="authentication",
                    retryable=False,
                    state="auth_failed",
                )
                self._stop_event.set()  # Stop retrying permanently
                return False
            if exc.code == 403:
                # The ingestion contract uses 401 for invalid, revoked, and
                # blocklisted keys. A 403 may be emitted by an edge/WAF policy
                # and can recover without a credential change, so preserve the
                # batch and let the worker retry with normal backoff.
                _log.warning(
                    "POST to %s was forbidden (HTTP 403); preserving the batch for retry",
                    url,
                )
                raise
            _log.warning("POST to %s failed: %s (backoff=%.1fs)", url, exc, self._backoff)
            raise
        except urllib.error.URLError as exc:
            _log.warning("POST to %s failed: %s (backoff=%.1fs)", url, exc, self._backoff)
            raise

    def _post(
        self,
        events: list[dict[str, Any]],
        tasks: list[dict[str, Any]] | None = None,
        business_identities: list[dict[str, Any]] | None = None,
        outcomes: list[dict[str, Any]] | None = None,
        revenue_revisions: list[dict[str, Any]] | None = None,
    ) -> bool:
        """POST events and tasks to the cloud ingest endpoint.

        Backward-compatible wrapper that delegates to :meth:`_post_with_split`.
        """
        return self._post_with_split(
            events,
            tasks or [],
            business_identities or [],
            outcomes=outcomes or [],
            revenue_revisions=revenue_revisions or [],
        )
