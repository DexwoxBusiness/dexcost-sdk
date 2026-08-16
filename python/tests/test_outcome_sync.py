"""Outcome revisions use the same durable mixed-ingest transport as costs."""

from __future__ import annotations

import json
import urllib.error
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dexcost.config import DexcostConfig
from dexcost.models.event import Event
from dexcost.models.outcome import OutcomeRevision, OutcomeValue
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.sync import SyncWorker


def _config() -> DexcostConfig:
    return DexcostConfig(
        api_key="dx_live_test123",
        batch_size=100,
        flush_interval_seconds=1,
    )


def _success_response() -> MagicMock:
    response = MagicMock()
    response.status = 202
    response.read.return_value = b'{"accepted":1,"rejected":0}'
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class _LegacyStorageBackend:
    """Pre-outcome custom backend used to verify upgrade compatibility."""

    def __init__(self, event: Event) -> None:
        self.pending_events = [event]

    def query_events_for_sync(self, limit: int = 1000) -> list[Event]:
        return self.pending_events[:limit]

    def mark_quarantined(self, event_ids: list[str]) -> None:
        self.pending_events = [
            event for event in self.pending_events if str(event.event_id) not in event_ids
        ]

    def query_tasks_for_sync(self, task_ids: list[str]) -> list[object]:
        return []

    def mark_synced(self, event_ids: list[str]) -> None:
        self.pending_events = [
            event for event in self.pending_events if str(event.event_id) not in event_ids
        ]

    def mark_tasks_synced(self, task_ids: list[str]) -> None:
        return None

    def purge_synced(self, retention_hours: int = 48) -> int:
        return 0

    def purge_old_pending(self, max_age_days: int = 7) -> int:
        return 0


@patch("dexcost.sync.urllib.request.urlopen")
def test_outcome_only_batch_is_uploaded_and_acknowledged(
    mock_urlopen: MagicMock,
    tmp_path: Path,
) -> None:
    mock_urlopen.return_value = _success_response()
    storage = SQLiteStorage(db_path=tmp_path / "sync.db")
    outcome = OutcomeRevision(
        task_id=uuid.uuid4(),
        name="campaign_exported",
        value=OutcomeValue.from_input(True),
    )
    storage.insert_outcome(outcome)
    worker = SyncWorker(config=_config(), storage=storage)

    assert worker._sync_batch() is True
    request = mock_urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert payload["events"] == []
    assert payload["tasks"] == []
    assert payload["business_identities"] == []
    assert payload["outcomes"] == [outcome.to_dict()]
    assert storage.query_outcomes_for_sync() == []
    storage.close()


@patch("dexcost.sync.urllib.request.urlopen")
def test_legacy_backend_without_outcome_methods_still_uploads_events(
    mock_urlopen: MagicMock,
) -> None:
    mock_urlopen.return_value = _success_response()
    event = Event(
        task_id=uuid.uuid4(),
        event_type="llm_call",
        occurred_at=datetime.now(timezone.utc),
        cost_usd=Decimal("0.001"),
        cost_confidence="exact",
        pricing_source="manual",
        provider="openai",
        model="gpt-4o",
        input_tokens=10,
        output_tokens=5,
    )
    storage = _LegacyStorageBackend(event)
    worker = SyncWorker(config=_config(), storage=storage)  # type: ignore[arg-type]

    assert worker._sync_batch() is True
    request = mock_urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert [item["event_id"] for item in payload["events"]] == [str(event.event_id)]
    assert payload["outcomes"] == []
    assert storage.pending_events == []


@patch("dexcost.sync.urllib.request.urlopen")
def test_transport_failure_keeps_outcome_pending(
    mock_urlopen: MagicMock,
    tmp_path: Path,
) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("offline")
    storage = SQLiteStorage(db_path=tmp_path / "retry.db")
    outcome = OutcomeRevision(task_id=uuid.uuid4(), name="render_completed")
    storage.insert_outcome(outcome)
    worker = SyncWorker(config=_config(), storage=storage)

    with pytest.raises(urllib.error.URLError):
        worker._sync_batch()
    assert storage.query_outcomes_for_sync() == [outcome]
    storage.close()


def test_oversized_outcome_is_quarantined_not_claimed_as_delivered(tmp_path: Path) -> None:
    storage = MagicMock()
    worker = SyncWorker(
        config=_config(),
        storage=SQLiteStorage(db_path=tmp_path / "oversized.db"),
    )
    outcome_id = str(uuid.uuid4())
    outcome = {
        "schema_version": "1",
        "outcome_id": outcome_id,
        "task_id": str(uuid.uuid4()),
        "name": "campaign_exported",
        "effective_at": "2026-08-16T12:00:00.000000Z",
        "observed_at": "2026-08-16T12:00:01.000000Z",
        "lifecycle": {"state": "achieved", "revision": 1},
        "value": {"type": "string", "value": "x" * 130_000},
    }

    with patch.object(worker, "_post_raw") as post_raw:
        assert worker._post_with_split([], [], storage=storage, outcomes=[outcome]) is True

    post_raw.assert_not_called()
    storage.mark_outcomes_quarantined.assert_called_once_with([(outcome_id, 1)])
