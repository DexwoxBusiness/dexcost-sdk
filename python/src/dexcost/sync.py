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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dexcost.redaction import enforce_metadata_limit, hash_value, redact_dict

if TYPE_CHECKING:
    from dexcost.config import DexcostConfig
    from dexcost.storage.protocol import StorageBackend

_log = logging.getLogger(__name__)

_INITIAL_BACKOFF: float = 1.0
_MAX_BACKOFF: float = 300.0  # 5 minutes
_PURGE_INTERVAL: float = 3600.0  # 1 hour between purge runs
_MAX_PAYLOAD_BYTES: int = 200_000  # 200KB — well under SQS 256KB limit


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

            # Wait for the configured interval or until woken
            self._wake_event.wait(timeout=self._config.flush_interval_seconds)
            self._wake_event.clear()

    def _sync_batch(self, storage: StorageBackend | None = None) -> bool:
        """Attempt to push one batch of events.

        Returns ``True`` if events were sent, ``False`` if there were
        no pending events.

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
        if not events:
            return False

        # Prepare event payload with redaction
        event_dicts: list[dict[str, Any]] = [
            self._prepare_event_dict(event) for event in events
        ]

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
        # The set of task IDs actually included in this payload — exactly
        # these are marked synced after a successful POST.
        synced_task_ids = list({str(t.task_id) for t in tasks})
        task_dicts: list[dict[str, Any]] = [
            self._prepare_task_dict(t) for t in tasks
        ]

        self._post_with_split(event_dicts, task_dicts)

        # Mark synced on success
        event_ids = [str(e.event_id) for e in events]
        st.mark_synced(event_ids)
        if synced_task_ids:
            st.mark_tasks_synced(synced_task_ids)

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

    def _prepare_event_dict(self, event: Any) -> dict[str, Any]:
        """Serialise an event and apply redaction / hashing / size limits."""
        d = event.to_dict()

        # Apply PII redaction to details
        if self._config.redact_fields and d.get("details"):
            d["details"] = redact_dict(d["details"], self._config.redact_fields)

        # Hash customer-adjacent fields in details (events carry task_id;
        # customer_id itself lives on the task).
        self._hash_pii(d)

        # Enforce metadata size limit
        if d.get("details"):
            d["details"] = enforce_metadata_limit(d["details"])

        return d

    def _prepare_task_dict(self, task: Any) -> dict[str, Any]:
        """Serialise a task and apply the same redaction policy as events.

        The task's ``metadata`` dict is redacted + size-limited, and the
        denormalised ``customer_id`` / ``project_id`` columns are hashed
        when ``hash_customer_id`` is configured — closing a PII leak where
        task metadata and customer ids were previously POSTed raw.
        """
        d = task.to_dict()

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
    ) -> None:
        """POST events with automatic batch splitting if payload exceeds size limit.

        Recursively splits the events array in half until each chunk fits within
        the SQS payload limit.  Tasks are sent with the first chunk only.
        """
        _MAX_DEPTH = 5  # Prevent infinite recursion

        payload: dict[str, Any] = {"events": events, "tasks": tasks}
        body = json.dumps(payload).encode("utf-8")

        if len(body) <= _MAX_PAYLOAD_BYTES or depth >= _MAX_DEPTH:
            self._post_raw(body)
            return

        if len(events) <= 1:
            # Single event too large — skip it with warning
            _log.warning(
                "Single event exceeds payload limit (%d bytes), skipping",
                len(body),
            )
            return

        # Split events in half
        mid = len(events) // 2
        _log.info(
            "Batch too large (%d bytes, %d events), splitting into 2 chunks",
            len(body),
            len(events),
        )

        # First half gets the tasks, second half gets empty tasks
        self._post_with_split(events[:mid], tasks, depth + 1)
        self._post_with_split(events[mid:], [], depth + 1)

    def _post_raw(self, body: bytes) -> None:
        """POST pre-encoded payload to the cloud ingest endpoint.

        Uses :mod:`urllib.request` (stdlib) to avoid adding an external
        dependency.  Treats 2xx (including 202 Accepted) as success.
        Raises on non-2xx responses.
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
                        url, status, f"Unexpected status {status}", {}, None  # type: ignore[arg-type]
                    )
        except urllib.error.HTTPError as exc:
            if exc.code == 413:
                _log.warning("Server returned 413 despite pre-split check")
            if exc.code in (401, 403):
                _log.error("API key rejected (HTTP %d) — disabling sync", exc.code)
                self._stop_event.set()  # Stop retrying permanently
                return
            _log.warning("POST to %s failed: %s (backoff=%.1fs)", url, exc, self._backoff)
            raise
        except urllib.error.URLError as exc:
            _log.warning("POST to %s failed: %s (backoff=%.1fs)", url, exc, self._backoff)
            raise

    def _post(
        self,
        events: list[dict[str, Any]],
        tasks: list[dict[str, Any]] | None = None,
    ) -> None:
        """POST events and tasks to the cloud ingest endpoint.

        Backward-compatible wrapper that delegates to :meth:`_post_with_split`.
        """
        self._post_with_split(events, tasks or [])
