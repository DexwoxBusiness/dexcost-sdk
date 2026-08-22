"""Durable delivery health, counters, and callback contracts."""

from __future__ import annotations

import urllib.error
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import dexcost
from dexcost.config import DexcostConfig
from dexcost.delivery import on_delivery_error, remove_delivery_error_callback
from dexcost.models.event import Event
from dexcost.models.outcome import OutcomeRevision, OutcomeValue
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.sync import SyncWorker


def _event(task_id: uuid.UUID | None = None) -> Event:
    return Event(
        event_id=uuid.uuid4(),
        task_id=task_id or uuid.uuid4(),
        event_type="llm_call",
        occurred_at=datetime.now(timezone.utc),
        provider="openai",
        model="gpt-5",
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0.01"),
        cost_confidence="computed",
        pricing_source="catalog",
    )


def _config() -> DexcostConfig:
    return DexcostConfig(api_key="dx_test_delivery")


def _success_response() -> MagicMock:
    response = MagicMock()
    response.status = 202
    response.read.return_value = b'{"accepted":1,"rejected":0}'
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_delivery_status_before_init_is_explicitly_local_only() -> None:
    dexcost.close()
    status = dexcost.delivery_status()
    assert not status.enabled
    assert status.worker_state == "local_only"
    assert status.pending_records == 0
    assert status.quarantined_records == 0
    assert status.healthy


def test_sqlite_delivery_counts_cover_every_durable_stream(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "delivery.db")
    task = Task(task_id=uuid.uuid4(), task_type="agent")
    event = _event(task.task_id)
    outcome = OutcomeRevision(
        task_id=task.task_id,
        name="resolved",
        state="achieved",
        value=OutcomeValue.from_input(1),
    )
    try:
        storage.insert_task(task)
        storage.insert_event(event)
        storage.insert_outcome(outcome)
        storage.mark_quarantined([str(event.event_id)])
        storage.mark_outcomes_quarantined(
            [(str(outcome.outcome_id), outcome.revision)]
        )

        counts = storage.delivery_counts()
        assert counts["pending_events"] == 0
        assert counts["quarantined_events"] == 1
        assert counts["pending_tasks"] == 1
        assert counts["pending_outcomes"] == 0
        assert counts["quarantined_outcomes"] == 1
        assert counts["pending_revenues"] == 0
        assert counts["quarantined_revenues"] == 0
        assert counts["oldest_pending_at"] == task.started_at.isoformat()
    finally:
        storage.close()


@patch("dexcost.sync.urllib.request.urlopen")
def test_success_status_joins_worker_counters_with_durable_depth(
    urlopen: MagicMock,
    tmp_path: Path,
) -> None:
    urlopen.return_value = _success_response()
    storage = SQLiteStorage(tmp_path / "delivery.db")
    event = _event()
    storage.insert_event(event)
    worker = SyncWorker(_config(), storage)
    try:
        assert worker._sync_batch()
        status = worker.status()
        assert status.enabled
        assert status.worker_state == "idle"
        assert status.pending_events == 0
        assert status.successful_batches == 1
        assert status.failed_batches == 0
        assert status.delivered_records == 1
        assert status.last_attempt_at is not None
        assert status.last_success_at is not None
        assert status.consecutive_failures == 0
        assert status.healthy
    finally:
        storage.close()


@patch("dexcost.sync.urllib.request.urlopen")
def test_auth_failure_is_visible_redacted_and_callback_safe(
    urlopen: MagicMock,
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "delivery.db")
    storage.insert_event(_event())
    worker = SyncWorker(_config(), storage)
    received = []

    def callback(event: object) -> None:
        received.append(event)

    on_delivery_error(callback)
    urlopen.side_effect = urllib.error.HTTPError(
        "https://api.dexcost.io/v1/ingest",
        401,
        "key dx_test_delivery rejected",
        Message(),
        None,
    )
    try:
        assert not worker._sync_batch()
        status = worker.status()
        assert status.worker_state == "auth_failed"
        assert status.pending_events == 1
        assert status.failed_batches == 1
        assert status.consecutive_failures == 1
        assert status.last_error_type == "HTTPError"
        assert status.last_error_message is not None
        assert "dx_test_delivery" not in status.last_error_message
        assert len(received) == 1
        event = received[0]
        assert event.operation == "authentication"
        assert not event.retryable

        worker.resume_after_auth()
        resumed = worker.status()
        assert resumed.worker_state == "idle"
        assert resumed.consecutive_failures == 0
    finally:
        remove_delivery_error_callback(callback)
        storage.close()


def test_callback_failure_never_breaks_delivery_health(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "delivery.db")
    worker = SyncWorker(_config(), storage)

    def broken_callback(_event: object) -> None:
        raise RuntimeError("callback bug")

    on_delivery_error(broken_callback)
    try:
        worker._record_error(
            RuntimeError("transport down"),
            operation="transport",
            retryable=True,
            state="backoff",
        )
        status = worker.status()
        assert status.worker_state == "backoff"
        assert status.failed_batches == 1
    finally:
        remove_delivery_error_callback(broken_callback)
        storage.close()
