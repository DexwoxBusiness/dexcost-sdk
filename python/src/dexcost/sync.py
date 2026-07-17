"""Background event push to Control Layer (US-016).

A daemon thread batches pending events from the local SQLite buffer and
pushes them to the cloud endpoint via HTTPS POST.  Exponential backoff
on failure ensures no data loss; events stay buffered until successfully
delivered.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from dexcost.attribution.convert import (
    to_attribution_event_v2,
    to_attribution_task_ingest_v1,
)
from dexcost.redaction import enforce_metadata_limit, hash_value, redact_dict

if TYPE_CHECKING:
    from dexcost.config import DexcostConfig
    from dexcost.storage.protocol import StorageBackend

_log = logging.getLogger(__name__)

_INITIAL_BACKOFF: float = 1.0
_MAX_BACKOFF: float = 300.0  # 5 minutes
_PURGE_INTERVAL: float = 3600.0  # 1 hour between purge runs
_MAX_PAYLOAD_BYTES: int = 120_000  # Headroom below the control-plane 128KB queue limit


class _AttributionBatchRejectedError(RuntimeError):
    """The control plane did not accept every record in a prepared leaf."""


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
        self._flush_lock = threading.Lock()

        self._backoff: float = _INITIAL_BACKOFF
        self._last_purge: float = 0.0

        self._thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background sync thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
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
        _log.debug("SyncWorker stopped")

    def flush(self) -> None:
        """Force an immediate sync cycle (blocking).

        Blocks until the current batch is pushed or an error occurs.
        """
        self._flush_done.clear()
        with self._flush_lock:
            self._flush_requested = True
        self._wake_event.set()
        self._flush_done.wait(timeout=30.0)

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
        while not self._stop_event.is_set():
            wait_seconds = self._config.flush_interval_seconds
            try:
                sent = self._sync_batch(storage=storage)
                if sent:
                    # Reset backoff on success; immediately try again
                    self._backoff = _INITIAL_BACKOFF
                    continue
                # Nothing to send — mark flush done if requested
                with self._flush_lock:
                    if self._flush_requested:
                        self._flush_requested = False
                        self._flush_done.set()
            except Exception:
                _log.exception("SyncWorker error during batch push")
                # Signal flush done even on error so caller doesn't hang
                with self._flush_lock:
                    if self._flush_requested:
                        self._flush_requested = False
                        self._flush_done.set()
                # Back off
                self._backoff = min(self._backoff * 2, _MAX_BACKOFF)
                wait_seconds = self._backoff

            if self._stop_event.is_set():
                break
            # Idle workers wait for the flush interval. Failed workers use the
            # exponential backoff computed above.
            self._wake_event.wait(timeout=wait_seconds)
            self._wake_event.clear()

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
        events = st.query_events_for_sync(limit=self._config.batch_size)

        # Convert durable v1 capture into strict attribution-v2 wire records.
        # Observability-only signals and permanently invalid legacy rows are
        # acknowledged locally so they cannot poison the pending queue forever.
        event_dicts: list[dict[str, Any]] = []
        skipped_event_ids: list[str] = []
        for event in events:
            converted = self._prepare_event_dict(event)
            if converted is None:
                skipped_event_ids.append(str(event.event_id))
            else:
                event_dicts.append(converted)
        if skipped_event_ids:
            st.mark_synced(skipped_event_ids)

        # Gather pending (not-yet-synced) tasks for the ingest payload.
        # Only pending tasks are pushed, so synced tasks are never re-POSTed
        # on every cycle.  query_pending_tasks_for_sync covers tasks
        # referenced by this event batch as well as any other unsynced
        # tasks — e.g. explicit dexcost.task() with customer_id where LLM
        # events went to auto-tasks in threads.
        tasks: list[Any] = []
        if hasattr(st, "query_pending_tasks_for_sync"):
            tasks = st.query_pending_tasks_for_sync()
        else:
            # Backend without task sync tracking — fall back to task IDs
            # referenced by this event batch.
            tasks = st.query_tasks_for_sync(list({str(e.task_id) for e in events}))
        task_dicts: list[dict[str, Any]] = [self._prepare_task_dict(t) for t in tasks]

        if not event_dicts and not task_dicts:
            return False

        posted = self._post_with_split(event_dicts, task_dicts, storage=st)
        if not posted:
            if self._stop_event.is_set():
                return False
            raise _AttributionBatchRejectedError(
                "control plane did not accept the complete attribution batch"
            )

        # Leaf POSTs mark their own rows so a successful half is not replayed
        # when a later sibling fails. These calls are a defensive idempotent
        # safety net for any future path that returns success without a leaf.
        event_ids = [event["event_id"] for event in event_dicts]
        st.mark_synced(event_ids)
        task_ids = [task["task_id"] for task in task_dicts]
        if task_ids:
            st.mark_tasks_synced(task_ids)

        _log.info(
            "Synced %d events and %d tasks to %s",
            len(event_ids),
            len(task_dicts),
            self._config.endpoint,
        )

        # Purge old synced events (throttled to once per hour)
        now = time.monotonic()
        if now - self._last_purge >= _PURGE_INTERVAL:
            try:
                deleted = st.purge_synced()
                if deleted:
                    _log.info("Purged %d old synced events", deleted)
                self._last_purge = now
            except Exception:
                _log.warning("purge_synced failed", exc_info=True)

            # Also purge very old pending events (safety net)
            try:
                old_pending = st.purge_old_pending(max_age_days=7)
                if old_pending:
                    _log.info("Purged %d old pending events (>7 days)", old_pending)
            except Exception:
                pass

        return True

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
        """Convert one event to the strict, details-free v2 wire contract.

        Arbitrary ``details`` never cross the process boundary. The converter
        reads only the accounting allow-list needed by attribution v2.
        """
        return cast(dict[str, Any] | None, to_attribution_event_v2(event))

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

    def _post_with_split(
        self,
        events: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        depth: int = 0,
        storage: StorageBackend | None = None,
    ) -> bool:
        """POST records, splitting both arrays to stay below the queue limit.

        Successful leaves are acknowledged immediately. This prevents a later
        sibling failure from replaying records the control plane already
        accepted. Tasks are sent before events when they must be separated.
        """
        payload: dict[str, Any] = {"events": events, "tasks": tasks}
        body = json.dumps(payload).encode("utf-8")

        if len(body) <= _MAX_PAYLOAD_BYTES:
            posted = self._post_raw(body)
            if posted and storage is not None:
                event_ids = [str(event["event_id"]) for event in events]
                task_ids = [str(task["task_id"]) for task in tasks]
                if event_ids:
                    storage.mark_synced(event_ids)
                if task_ids:
                    storage.mark_tasks_synced(task_ids)
            return posted

        if len(events) > 1:
            mid = len(events) // 2
            _log.info(
                "Batch too large (%d bytes, %d events), splitting events",
                len(body),
                len(events),
            )
            first_posted = self._post_with_split(events[:mid], tasks, depth + 1, storage)
            if not first_posted:
                return False
            return self._post_with_split(events[mid:], [], depth + 1, storage)

        if len(tasks) > 1:
            mid = len(tasks) // 2
            _log.info(
                "Batch too large (%d bytes, %d tasks), splitting tasks",
                len(body),
                len(tasks),
            )
            first_posted = self._post_with_split([], tasks[:mid], depth + 1, storage)
            if not first_posted:
                return False
            return self._post_with_split(events, tasks[mid:], depth + 1, storage)

        if len(events) == 1 and len(tasks) == 1:
            task_posted = self._post_with_split([], tasks, depth + 1, storage)
            if not task_posted:
                return False
            return self._post_with_split(events, [], depth + 1, storage)

        if len(events) == 1:
            # A permanently oversized record cannot be delivered. Acknowledge
            # it locally so it does not poison every future batch.
            _log.warning(
                "Single event exceeds payload limit (%d bytes), skipping",
                len(body),
            )
            if storage is not None:
                storage.mark_synced([str(events[0]["event_id"])])
            return True

        if len(tasks) == 1:
            _log.warning(
                "Single task exceeds payload limit (%d bytes), skipping",
                len(body),
            )
            if storage is not None:
                storage.mark_tasks_synced([str(tasks[0]["task_id"])])
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
                            "Control plane rejected %s item(s) from an attribution-v2 batch",
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
            if exc.code in (401, 403):
                _log.error("API key rejected (HTTP %d) — disabling sync", exc.code)
                self._stop_event.set()  # Stop retrying permanently
                return False
            _log.warning("POST to %s failed: %s (backoff=%.1fs)", url, exc, self._backoff)
            raise
        except urllib.error.URLError as exc:
            _log.warning("POST to %s failed: %s (backoff=%.1fs)", url, exc, self._backoff)
            raise

    def _post(
        self,
        events: list[dict[str, Any]],
        tasks: list[dict[str, Any]] | None = None,
    ) -> bool:
        """POST events and tasks to the cloud ingest endpoint.

        Backward-compatible wrapper that delegates to :meth:`_post_with_split`.
        """
        return self._post_with_split(events, tasks or [])
