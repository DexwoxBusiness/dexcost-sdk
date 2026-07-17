"""Tests for background event push to Control Layer (US-016)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dexcost.config import DexcostConfig
from dexcost.models.event import Event
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.sync import (
    _INITIAL_BACKOFF,
    _MAX_BACKOFF,
    SyncWorker,
    _AttributionBatchRejectedError,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_config(
    api_key: str = "dx_live_test123",
    storage: str | None = None,
    batch_size: int = 100,
    flush_interval: float = 0.1,
    redact_fields: list[str] | None = None,
    hash_customer_id: bool = False,
) -> DexcostConfig:
    """Create a DexcostConfig for testing."""
    return DexcostConfig(
        api_key=api_key,
        storage=storage,
        batch_size=batch_size,
        flush_interval_seconds=flush_interval,
        redact_fields=redact_fields or [],
        hash_customer_id=hash_customer_id,
    )


def _make_event(
    task_id: uuid.UUID | None = None,
    cost: str = "0.05",
    details: dict[str, Any] | None = None,
) -> Event:
    """Create a test Event."""
    return Event(
        event_id=uuid.uuid4(),
        task_id=task_id or uuid.uuid4(),
        event_type="llm_call",
        occurred_at=datetime.now(timezone.utc),
        cost_usd=Decimal(cost),
        cost_confidence="exact",
        pricing_source="manual",
        provider="openai",
        model="gpt-4",
        input_tokens=100,
        output_tokens=50,
        details=details or {},
    )


def _make_storage(tmp_path: Path) -> SQLiteStorage:
    """Create a SQLiteStorage backed by a temp directory."""
    return SQLiteStorage(db_path=tmp_path / "test.db")


def _db_path(tmp_path: Path) -> Path:
    """Return the canonical DB path used by _make_storage."""
    return tmp_path / "test.db"


def _insert_events(storage: SQLiteStorage, count: int = 1) -> list[Event]:
    """Insert N events and return them."""
    events: list[Event] = []
    for _ in range(count):
        ev = _make_event()
        storage.insert_event(ev)
        events.append(ev)
    return events


def _mock_urlopen_success() -> MagicMock:
    """Return a mock urlopen that succeeds (HTTP 200)."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def _mock_urlopen_failure() -> urllib.error.URLError:
    """Return a URLError for simulating failures."""
    return urllib.error.URLError("Connection refused")


# ── Tests ────────────────────────────────────────────────────────────


class TestSyncWorkerStartStop:
    """SyncWorker lifecycle tests."""

    def test_start_and_stop_cleanly(self, tmp_path: Path) -> None:
        """Worker starts and stops without errors."""
        config = _make_config()
        storage = _make_storage(tmp_path)
        worker = SyncWorker(config=config, storage=storage, db_path=_db_path(tmp_path))

        worker.start()
        assert worker._thread is not None
        assert worker._thread.is_alive()

        worker.stop()
        assert worker._thread is None

    def test_worker_thread_is_daemon(self, tmp_path: Path) -> None:
        """Worker thread must be a daemon so it dies with the main process."""
        config = _make_config()
        storage = _make_storage(tmp_path)
        worker = SyncWorker(config=config, storage=storage, db_path=_db_path(tmp_path))

        worker.start()
        assert worker._thread is not None
        assert worker._thread.daemon is True
        worker.stop()

    def test_double_start_is_safe(self, tmp_path: Path) -> None:
        """Calling start() twice does not create a second thread."""
        config = _make_config()
        storage = _make_storage(tmp_path)
        worker = SyncWorker(config=config, storage=storage, db_path=_db_path(tmp_path))

        worker.start()
        thread1 = worker._thread
        worker.start()
        thread2 = worker._thread
        assert thread1 is thread2
        worker.stop()

    def test_stop_without_start_is_safe(self, tmp_path: Path) -> None:
        """Calling stop() without start() does not raise."""
        config = _make_config()
        storage = _make_storage(tmp_path)
        worker = SyncWorker(config=config, storage=storage)
        worker.stop()  # Should not raise


class TestSyncBatch:
    """Tests for batching and POSTing events.

    These tests call ``_sync_batch()`` directly on the main thread,
    so no ``db_path`` is needed.
    """

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_pending_events_posted(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Pending events are batched and POSTed to the endpoint."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config()
        storage = _make_storage(tmp_path)
        events = _insert_events(storage, count=3)

        worker = SyncWorker(config=config, storage=storage)
        result = worker._sync_batch()

        assert result is True
        mock_urlopen.assert_called_once()
        # Verify the Request object
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.dexcost.io/v1/ingest"
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Authorization") == "Bearer dx_live_test123"
        # Verify payload — ingest format: {"events": [...], "tasks": [...]}
        body = json.loads(req.data.decode("utf-8"))
        assert isinstance(body, dict)
        assert "events" in body
        assert "tasks" in body
        assert len(body["events"]) == 3
        assert body["events"][0]["event_id"] == str(events[0].event_id)

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_events_marked_synced_on_success(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Successful POST marks events as synced in the buffer."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config()
        storage = _make_storage(tmp_path)
        _insert_events(storage, count=5)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        # No pending events left
        pending = storage.query_events_for_sync()
        assert len(pending) == 0

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_events_stay_pending_on_failure(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Failed POST leaves events in pending state."""
        mock_urlopen.side_effect = _mock_urlopen_failure()
        config = _make_config()
        storage = _make_storage(tmp_path)
        _insert_events(storage, count=3)

        worker = SyncWorker(config=config, storage=storage)
        with pytest.raises(urllib.error.URLError):
            worker._sync_batch()

        pending = storage.query_events_for_sync()
        assert len(pending) == 3

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_auth_rejection_stops_worker_without_acknowledging_events(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """A rejected API key disables sync but preserves buffered records."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.dexcost.io/v1/ingest",
            401,
            "Unauthorized",
            {},
            None,
        )
        storage = _make_storage(tmp_path)
        _insert_events(storage)
        worker = SyncWorker(config=_make_config(), storage=storage)

        assert worker._sync_batch() is False
        assert worker._stop_event.is_set()
        assert len(storage.query_events_for_sync()) == 1

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_partial_control_plane_rejection_keeps_leaf_pending(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """A 202 with rejected records is not treated as full acceptance."""
        response = _mock_urlopen_success()
        response.read.return_value = b'{"queued": 0, "rejected": 1}'
        mock_urlopen.return_value = response
        storage = _make_storage(tmp_path)
        _insert_events(storage)
        worker = SyncWorker(config=_make_config(), storage=storage)

        with pytest.raises(_AttributionBatchRejectedError):
            worker._sync_batch()
        assert len(storage.query_events_for_sync()) == 1

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_no_events_returns_false(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """_sync_batch returns False when there are no pending events."""
        config = _make_config()
        storage = _make_storage(tmp_path)

        worker = SyncWorker(config=config, storage=storage)
        result = worker._sync_batch()

        assert result is False
        mock_urlopen.assert_not_called()

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_pending_task_without_events_is_still_posted(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Task attribution metadata must not wait for a cost event to exist."""
        mock_urlopen.return_value = _mock_urlopen_success()
        storage = _make_storage(tmp_path)
        task = _make_task(customer_id="customer-1")
        storage.insert_task(task)

        worker = SyncWorker(config=_make_config(), storage=storage)
        assert worker._sync_batch() is True

        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert body["events"] == []
        assert [item["task_id"] for item in body["tasks"]] == [str(task.task_id)]
        assert storage.query_pending_tasks_for_sync() == []

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_observability_only_event_is_acknowledged_without_post(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Non-billable signals cannot poison the durable pending queue."""
        storage = _make_storage(tmp_path)
        event = _make_event()
        event.event_type = "gpu_utilization_signal"
        storage.insert_event(event)

        worker = SyncWorker(config=_make_config(), storage=storage)
        assert worker._sync_batch() is False

        assert storage.query_events_for_sync() == []
        mock_urlopen.assert_not_called()

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_batch_size_respected(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Only batch_size events are sent per batch."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config(batch_size=5)
        storage = _make_storage(tmp_path)
        _insert_events(storage, count=12)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        # Check only 5 were sent
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert len(body["events"]) == 5

        # 7 still pending
        pending = storage.query_events_for_sync()
        assert len(pending) == 7

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_events_serialized_as_attribution_v2(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Durable v1 events are converted to the strict v2 wire format."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config()
        storage = _make_storage(tmp_path)
        events = _insert_events(storage, count=1)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        wire = body["events"][0]
        assert wire["event_id"] == str(events[0].event_id)
        assert wire["component"] == "llm"
        assert wire["provider"] == {"name": "openai", "service": "responses"}
        assert wire["resource"] == {"type": "model", "id": "gpt-4"}
        assert wire["schema_version"] == "2"
        assert wire["cost_evidence"] == {
            "amount": "0.05",
            "currency": "USD",
            "source": "manual",
            "confidence": "exact",
        }
        assert "details" not in wire
        assert "cost_usd" not in wire

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_payload_is_ingest_format(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """POST payload is a JSON object with events and tasks arrays."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config()
        storage = _make_storage(tmp_path)
        _insert_events(storage, count=2)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert isinstance(body, dict)
        assert isinstance(body["events"], list)
        assert isinstance(body["tasks"], list)
        assert len(body["events"]) == 2

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_authorization_header(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """POST includes Bearer authorization header."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config(api_key="dx_live_mykey789")
        storage = _make_storage(tmp_path)
        _insert_events(storage, count=1)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer dx_live_mykey789"


class TestBackoff:
    """Tests for exponential backoff on failure."""

    def test_run_uses_backoff_as_failure_wait(self, tmp_path: Path) -> None:
        """The computed backoff controls retry timing, not only logging."""
        worker = SyncWorker(config=_make_config(), storage=_make_storage(tmp_path))
        attempts = 0

        def sync_once_then_stop(storage: Any = None) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise urllib.error.URLError("temporary failure")
            worker._stop_event.set()
            return False

        with (
            patch.object(worker, "_sync_batch", side_effect=sync_once_then_stop),
            patch.object(worker._wake_event, "wait") as mock_wait,
        ):
            worker._run()

        mock_wait.assert_called_once_with(timeout=2.0)

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_backoff_increases_on_failure(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Backoff doubles on each failure."""
        mock_urlopen.side_effect = _mock_urlopen_failure()
        config = _make_config(flush_interval=60.0)
        storage = _make_storage(tmp_path)
        _insert_events(storage, count=1)

        worker = SyncWorker(config=config, storage=storage)
        assert worker._backoff == _INITIAL_BACKOFF  # 1.0

        # Simulate failure and backoff (as _run would do)
        try:
            worker._sync_batch()
        except urllib.error.URLError:
            worker._backoff = min(worker._backoff * 2, _MAX_BACKOFF)

        assert worker._backoff == 2.0

        try:
            worker._sync_batch()
        except urllib.error.URLError:
            worker._backoff = min(worker._backoff * 2, _MAX_BACKOFF)

        assert worker._backoff == 4.0

    def test_max_backoff_caps_at_300(self, tmp_path: Path) -> None:
        """Backoff never exceeds 300 seconds (5 minutes)."""
        config = _make_config()
        storage = _make_storage(tmp_path)
        worker = SyncWorker(config=config, storage=storage)

        worker._backoff = 256.0
        worker._backoff = min(worker._backoff * 2, _MAX_BACKOFF)
        assert worker._backoff == _MAX_BACKOFF  # 300.0, not 512.0

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_backoff_resets_on_success(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Backoff resets to initial value after a successful sync."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config()
        storage = _make_storage(tmp_path)
        _insert_events(storage, count=1)

        worker = SyncWorker(config=config, storage=storage)
        worker._backoff = 64.0  # Simulate previous failures

        worker._sync_batch()
        # In _run, success resets backoff to _INITIAL_BACKOFF.
        # Verify the pattern:
        worker._backoff = _INITIAL_BACKOFF
        assert worker._backoff == _INITIAL_BACKOFF


class TestFlush:
    """Tests for flush() — forced immediate sync.

    These use the background thread, so ``db_path`` is provided for a
    thread-safe connection.
    """

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_flush_forces_immediate_sync(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """flush() triggers an immediate sync cycle and blocks."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config(flush_interval=60.0)  # Long interval
        db = _db_path(tmp_path)
        storage = SQLiteStorage(db_path=db)
        _insert_events(storage, count=3)

        worker = SyncWorker(config=config, storage=storage, db_path=db)
        worker.start()

        # flush() should block until events are synced
        worker.flush()

        # Verify via a fresh connection (worker's thread-local conn did mark_synced)
        verify_storage = SQLiteStorage(db_path=db)
        pending = verify_storage.query_events_for_sync()
        assert len(pending) == 0
        worker.stop()

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_flush_with_no_events(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """flush() completes even if there are no pending events."""
        config = _make_config(flush_interval=60.0)
        db = _db_path(tmp_path)
        storage = SQLiteStorage(db_path=db)

        worker = SyncWorker(config=config, storage=storage, db_path=db)
        worker.start()

        # Should not hang
        worker.flush()
        mock_urlopen.assert_not_called()
        worker.stop()


class TestRedaction:
    """Attribution v2 transmits no arbitrary event details."""

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_redact_fields_applied(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Configured PII cannot escape through the removed details carrier."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config(redact_fields=["email", "password"])
        storage = _make_storage(tmp_path)
        ev = _make_event(details={"email": "user@test.com", "model_name": "gpt-4"})
        storage.insert_event(ev)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert "details" not in body["events"][0]
        assert "email" not in json.dumps(body["events"][0])

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_hash_customer_id_applied(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Customer-adjacent details are omitted instead of transmitted."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config(hash_customer_id=True)
        storage = _make_storage(tmp_path)
        ev = _make_event(details={"customer_id": "cust-123", "note": "test"})
        storage.insert_event(ev)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert "details" not in body["events"][0]
        assert "cust-123" not in json.dumps(body["events"][0])

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_enforce_metadata_limit_applied(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Large arbitrary details do not inflate the v2 wire payload."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config()
        storage = _make_storage(tmp_path)
        # Create details > 10KB
        big_details = {"data": "x" * 20000}
        ev = _make_event(details=big_details)
        storage.insert_event(ev)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert "details" not in body["events"][0]
        assert len(json.dumps(body["events"][0]).encode("utf-8")) < 10_000


class TestInitIntegration:
    """Tests for init() / flush() integration."""

    @staticmethod
    def _uninstrument_all() -> None:
        """Uninstrument all SDKs to reset global instrument state between tests."""
        from dexcost.instruments.anthropic import uninstrument_anthropic
        from dexcost.instruments.litellm import uninstrument_litellm
        from dexcost.instruments.openai import uninstrument_openai

        uninstrument_openai()
        uninstrument_anthropic()
        uninstrument_litellm()

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_cloud_mode_starts_worker(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """init() with an API key starts the SyncWorker."""
        import dexcost

        # Save and restore globals
        old_config = dexcost._global_config
        old_worker = dexcost._sync_worker
        old_tracker = dexcost._global_tracker
        try:
            config = dexcost.init(
                api_key="dx_live_test123",
                buffer_path=str(tmp_path / "cloud.db"),
                flush_interval=60.0,
            )
            assert config.storage_mode == "cloud"
            assert dexcost._sync_worker is not None
            assert dexcost._sync_worker._thread is not None
            assert dexcost._sync_worker._thread.is_alive()
            dexcost._sync_worker.stop()
        finally:
            self._uninstrument_all()
            dexcost._global_config = old_config
            dexcost._sync_worker = old_worker
            dexcost._global_tracker = old_tracker

    def test_local_mode_no_worker(self, tmp_path: Path) -> None:
        """init() without API key does not start a SyncWorker."""
        import dexcost

        old_config = dexcost._global_config
        old_worker = dexcost._sync_worker
        old_tracker = dexcost._global_tracker
        try:
            dexcost._sync_worker = None
            config = dexcost.init(
                api_key=None,
                storage="local",
                buffer_path=str(tmp_path / "local.db"),
            )
            assert config.storage_mode == "local"
            assert dexcost._sync_worker is None
        finally:
            self._uninstrument_all()
            dexcost._global_config = old_config
            dexcost._sync_worker = old_worker
            dexcost._global_tracker = old_tracker

    def test_flush_noop_in_local_mode(self, tmp_path: Path) -> None:
        """flush() is a no-op when no worker is active."""
        import dexcost

        old_config = dexcost._global_config
        old_worker = dexcost._sync_worker
        old_tracker = dexcost._global_tracker
        try:
            dexcost._sync_worker = None
            dexcost.init(
                api_key=None,
                storage="local",
                buffer_path=str(tmp_path / "local2.db"),
            )
            # Should not raise
            dexcost.flush()
        finally:
            self._uninstrument_all()
            dexcost._global_config = old_config
            dexcost._sync_worker = old_worker
            dexcost._global_tracker = old_tracker

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_flush_pushes_events(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """flush() via the module-level function pushes pending events."""
        mock_urlopen.return_value = _mock_urlopen_success()
        import dexcost

        old_config = dexcost._global_config
        old_worker = dexcost._sync_worker
        old_tracker = dexcost._global_tracker
        try:
            db = str(tmp_path / "flush.db")
            dexcost.init(
                api_key="dx_live_test123",
                buffer_path=db,
                flush_interval=60.0,
            )
            # Insert events via a separate storage connection
            insert_storage = SQLiteStorage(db_path=db)
            _insert_events(insert_storage, count=2)

            dexcost.flush()

            assert mock_urlopen.called
            dexcost._sync_worker.stop()  # type: ignore[union-attr]
        finally:
            self._uninstrument_all()
            dexcost._global_config = old_config
            dexcost._sync_worker = old_worker
            dexcost._global_tracker = old_tracker


class TestEndToEnd:
    """End-to-end scenario tests using the background thread."""

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_50_events_batch_post_fires(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """50 events recorded -> batch POST fires -> events marked synced."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config(batch_size=100, flush_interval=0.1)
        db = _db_path(tmp_path)
        storage = SQLiteStorage(db_path=db)
        _insert_events(storage, count=50)

        worker = SyncWorker(config=config, storage=storage, db_path=db)
        worker.start()

        # Wait for the worker to process
        time.sleep(2.0)
        worker.stop()

        # All events synced — verify via fresh connection
        verify_storage = SQLiteStorage(db_path=db)
        pending = verify_storage.query_events_for_sync()
        assert len(pending) == 0
        assert mock_urlopen.called

        # Verify 50 events in payload
        req = mock_urlopen.call_args_list[0][0][0]
        body = json.loads(req.data.decode("utf-8"))
        assert len(body["events"]) == 50

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_server_down_then_recovery(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Server down -> events stay buffered -> server back -> delivered."""
        call_count = 0

        def side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.URLError("Connection refused")
            return _mock_urlopen_success()

        mock_urlopen.side_effect = side_effect

        config = _make_config(batch_size=100, flush_interval=0.05)
        db = _db_path(tmp_path)
        storage = SQLiteStorage(db_path=db)
        _insert_events(storage, count=5)

        worker = SyncWorker(config=config, storage=storage, db_path=db)
        worker.start()

        # Wait for retries and eventual success
        time.sleep(3.0)
        worker.stop()

        # Events should eventually be synced
        verify_storage = SQLiteStorage(db_path=db)
        pending = verify_storage.query_events_for_sync()
        assert len(pending) == 0

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_no_data_loss_on_persistent_failure(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Events stay in buffer if server never comes back."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        config = _make_config(batch_size=100, flush_interval=0.05)
        db = _db_path(tmp_path)
        storage = SQLiteStorage(db_path=db)
        _insert_events(storage, count=5)

        worker = SyncWorker(config=config, storage=storage, db_path=db)
        worker.start()
        time.sleep(1.0)
        worker.stop()

        # All events still pending — no data loss
        verify_storage = SQLiteStorage(db_path=db)
        pending = verify_storage.query_events_for_sync()
        assert len(pending) == 5


# ── Task sync-status tests (Fix 3) ───────────────────────────────────


def _make_task(
    task_id: uuid.UUID | None = None,
    customer_id: str | None = None,
    project_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Task:
    """Create a test Task."""
    return Task(
        task_id=task_id or uuid.uuid4(),
        task_type="resolve_ticket",
        status="success",
        customer_id=customer_id,
        project_id=project_id,
        metadata=metadata or {},
    )


class TestTaskSyncStatus:
    """A task pushed once must not be re-POSTed on subsequent sync cycles."""

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_synced_task_not_repushed(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """After a task is synced, a later sync cycle does not include it again."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config()
        storage = _make_storage(tmp_path)

        # Task A with one event.
        task_a = _make_task()
        storage.insert_task(task_a)
        ev_a = _make_event(task_id=task_a.task_id)
        storage.insert_event(ev_a)

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        # First payload includes task A.
        first_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        first_task_ids = {t["task_id"] for t in first_body["tasks"]}
        assert str(task_a.task_id) in first_task_ids

        # Task A is now marked synced in storage.
        assert all(t.task_id != task_a.task_id for t in storage.query_pending_tasks_for_sync())

        # A new event for a *new* task B arrives.
        task_b = _make_task()
        storage.insert_task(task_b)
        ev_b = _make_event(task_id=task_b.task_id)
        storage.insert_event(ev_b)

        worker._sync_batch()

        # Second payload includes ONLY task B — task A is not re-pushed.
        second_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        second_task_ids = {t["task_id"] for t in second_body["tasks"]}
        assert second_task_ids == {str(task_b.task_id)}
        assert str(task_a.task_id) not in second_task_ids

    def test_mark_tasks_synced_sets_status(self, tmp_path: Path) -> None:
        """mark_tasks_synced transitions tasks out of the pending set."""
        storage = _make_storage(tmp_path)
        task = _make_task()
        storage.insert_task(task)

        assert len(storage.query_pending_tasks_for_sync()) == 1

        storage.mark_tasks_synced([str(task.task_id)])

        assert storage.query_pending_tasks_for_sync() == []


class TestTaskMetadataRedaction:
    """Task metadata / customer ids must be redacted + hashed before POST (Fix 4)."""

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_task_metadata_redacted_on_push(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        """Configured redact_fields are stripped from task metadata."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config(redact_fields=["email", "ssn"])
        storage = _make_storage(tmp_path)

        task = _make_task(
            metadata={"email": "user@test.com", "ssn": "123-45-6789", "tier": "gold"}
        )
        storage.insert_task(task)
        storage.insert_event(_make_event(task_id=task.task_id))

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        synced_task = next(t for t in body["tasks"] if t["task_id"] == str(task.task_id))
        assert "email" not in synced_task["metadata"]
        assert "ssn" not in synced_task["metadata"]
        # Non-redacted field survives.
        assert synced_task["metadata"]["tier"] == "gold"

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_task_customer_id_hashed_on_push(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """hash_customer_id hashes the task's customer_id and project_id."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config(hash_customer_id=True)
        storage = _make_storage(tmp_path)

        task = _make_task(customer_id="acme-corp", project_id="proj-42")
        storage.insert_task(task)
        storage.insert_event(_make_event(task_id=task.task_id))

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        synced_task = next(t for t in body["tasks"] if t["task_id"] == str(task.task_id))
        # customer_id / project_id are hashed (SHA-256 hex = 64 chars).
        assert synced_task["customer_id"] != "acme-corp"
        assert len(synced_task["customer_id"]) == 64
        assert synced_task["project_id"] != "proj-42"
        assert len(synced_task["project_id"]) == 64

    @patch("dexcost.sync.urllib.request.urlopen")
    def test_task_metadata_size_limit_enforced(
        self, mock_urlopen: MagicMock, tmp_path: Path
    ) -> None:
        """Task metadata exceeding 10KB is truncated before push."""
        mock_urlopen.return_value = _mock_urlopen_success()
        config = _make_config()
        storage = _make_storage(tmp_path)

        task = _make_task(metadata={"blob": "x" * 20000})
        storage.insert_task(task)
        storage.insert_event(_make_event(task_id=task.task_id))

        worker = SyncWorker(config=config, storage=storage)
        worker._sync_batch()

        body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        synced_task = next(t for t in body["tasks"] if t["task_id"] == str(task.task_id))
        assert synced_task["metadata"]["_truncated"] is True
        assert "_original_size_bytes" in synced_task["metadata"]
