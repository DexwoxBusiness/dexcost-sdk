"""Exact, immutable revenue capture and offline delivery tests."""

from __future__ import annotations

import json
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import dexcost
from dexcost.config import DexcostConfig
from dexcost.models.revenue import (
    RevenueAmount,
    RevenueRevision,
    RevenueSource,
)
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.sync import SyncWorker
from dexcost.tracker import CostTracker


def _recognized(
    *,
    revenue_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    outcome_id: uuid.UUID | None = None,
    revision: int = 1,
    value: str = "12.3400",
    currency: str = "USD",
    source: RevenueSource | None = None,
) -> RevenueRevision:
    return RevenueRevision(
        revenue_id=revenue_id or uuid.uuid4(),
        revision=revision,
        task_id=task_id or uuid.uuid4(),
        outcome_id=outcome_id,
        state="recognized",
        amount=RevenueAmount.from_input(value, currency),
        source=source or RevenueSource(record_id="invoice-42"),
        effective_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 8, 20, 12, 0, 1, tzinfo=timezone.utc),
    )


def _config() -> DexcostConfig:
    return DexcostConfig(api_key="dx_test_revenue")


def _success_response() -> MagicMock:
    response = MagicMock()
    response.status = 202
    response.read.return_value = b'{"accepted":1,"rejected":0}'
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_revenue_matches_exact_control_plane_wire_contract() -> None:
    revenue_id = uuid.UUID("4b2b84d8-972f-4ca2-8919-e2f21c41d17c")
    task_id = uuid.UUID("1d4fd238-0067-491e-85fb-c0075283a9df")
    outcome_id = uuid.UUID("0dd8a66a-b450-4978-9cf5-0eca443d0d66")
    revenue = _recognized(
        revenue_id=revenue_id,
        task_id=task_id,
        outcome_id=outcome_id,
    )

    assert revenue.to_dict() == {
        "schema_version": "1",
        "revenue_id": str(revenue_id),
        "task_id": str(task_id),
        "outcome_id": str(outcome_id),
        "effective_at": "2026-08-20T12:00:00.000000Z",
        "observed_at": "2026-08-20T12:00:01.000000Z",
        "lifecycle": {"state": "recognized", "revision": 1},
        "amount": {"value": "12.34", "currency": "USD"},
        "source": {"type": "sdk", "record_id": "invoice-42"},
    }
    assert RevenueRevision.from_dict(revenue.to_dict()).to_dict() == revenue.to_dict()


def test_revenue_rejects_lossy_money_and_invalid_lifecycle() -> None:
    with pytest.raises(TypeError, match="Decimal, integer, or decimal string"):
        RevenueAmount.from_input(1.1, "USD")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="three-letter uppercase"):
        RevenueAmount.from_input("1", "usd")
    with pytest.raises(ValueError, match="requires an amount"):
        RevenueRevision(task_id=uuid.uuid4(), state="recognized")
    with pytest.raises(ValueError, match="cannot assert an amount"):
        RevenueRevision(
            task_id=uuid.uuid4(),
            state="pending",
            amount=RevenueAmount.from_input("1", "USD"),
        )
    with pytest.raises(ValueError, match="supersede"):
        RevenueRevision(task_id=uuid.uuid4(), state="voided")


def test_storage_enforces_revenue_revision_stream(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "revenue.db")
    task_id = uuid.uuid4()
    revenue_id = uuid.uuid4()
    source = RevenueSource(record_id="invoice-42")
    first = RevenueRevision(
        revenue_id=revenue_id,
        task_id=task_id,
        state="pending",
        source=source,
        effective_at=datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 8, 20, 11, 0, 1, tzinfo=timezone.utc),
    )
    try:
        storage.insert_revenue(first)
        storage.insert_revenue(first)
        recognized = _recognized(
            revenue_id=revenue_id,
            task_id=task_id,
            revision=2,
            source=source,
        )
        storage.insert_revenue(recognized)

        assert storage.query_revenue_history(str(revenue_id)) == [first, recognized]
        assert storage.query_revenues_for_sync() == [first, recognized]

        with pytest.raises(ValueError, match="already exists with different contents"):
            storage.insert_revenue(
                RevenueRevision(
                    revenue_id=revenue_id,
                    revision=2,
                    task_id=task_id,
                    state="voided",
                    source=source,
                )
            )
        with pytest.raises(ValueError, match="expected revision 3"):
            storage.insert_revenue(
                _recognized(
                    revenue_id=revenue_id,
                    task_id=task_id,
                    revision=4,
                    source=source,
                )
            )
    finally:
        storage.close()


def test_storage_rejects_identity_transition_and_currency_mutation(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "invariants.db")
    revenue_id = uuid.uuid4()
    task_id = uuid.uuid4()
    source = RevenueSource(record_id="invoice-42")
    first = _recognized(revenue_id=revenue_id, task_id=task_id, source=source)
    storage.insert_revenue(first)
    try:
        with pytest.raises(ValueError, match="cannot change task_id"):
            storage.insert_revenue(
                _recognized(
                    revenue_id=revenue_id,
                    task_id=uuid.uuid4(),
                    revision=2,
                    source=source,
                )
            )
        with pytest.raises(ValueError, match="recognized -> provisional"):
            storage.insert_revenue(
                RevenueRevision(
                    revenue_id=revenue_id,
                    revision=2,
                    task_id=task_id,
                    state="provisional",
                    amount=RevenueAmount.from_input("12", "USD"),
                    source=source,
                )
            )
        with pytest.raises(ValueError, match="currency cannot change"):
            storage.insert_revenue(
                _recognized(
                    revenue_id=revenue_id,
                    task_id=task_id,
                    revision=2,
                    currency="EUR",
                    source=source,
                )
            )
    finally:
        storage.close()


def test_cleanup_retains_revenue_ledger_for_later_void(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "retention.db")
    revenue = _recognized()
    storage.insert_revenue(revenue)
    storage.mark_revenues_synced([(str(revenue.revenue_id), 1)])
    try:
        storage.purge_synced(retention_hours=0)
        storage.purge_old_pending(max_age_days=0)
        voided = RevenueRevision(
            revenue_id=revenue.revenue_id,
            revision=2,
            task_id=revenue.task_id,
            state="voided",
            source=revenue.source,
        )
        storage.insert_revenue(voided)
        assert storage.query_revenue_history(str(revenue.revenue_id)) == [revenue, voided]
    finally:
        storage.close()


def test_tracked_task_records_explicit_revenue_and_history(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "tracker.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    task_id = uuid.uuid4()
    try:
        with tracker.task(task_type="campaign.root", task_id=task_id) as task:
            revenue = task.record_revenue("24.50", source_record_id="invoice-99")

        assert revenue.task_id == task_id
        assert tracker.get_revenue_history(revenue.revenue_id) == [revenue]
    finally:
        storage.close()


def test_singleton_record_revenue_uses_current_task(tmp_path: Path) -> None:
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "singleton.db"),
        auto_instrument=[],
        track_http=False,
        track_network=False,
    )
    try:
        task_id = uuid.uuid4()
        with dexcost.task(task_type="campaign.root", task_id=task_id):
            revenue = dexcost.record_revenue(25, source_record_id="invoice-100")
        assert revenue.task_id == task_id
        assert dexcost.get_revenue_history(revenue.revenue_id) == [revenue]
    finally:
        dexcost.close()


@patch("dexcost.sync.urllib.request.urlopen")
def test_revenue_only_batch_is_uploaded_and_acknowledged(
    urlopen: MagicMock, tmp_path: Path
) -> None:
    urlopen.return_value = _success_response()
    storage = SQLiteStorage(tmp_path / "sync.db")
    revenue = _recognized()
    storage.insert_revenue(revenue)
    worker = SyncWorker(_config(), storage)
    try:
        assert worker._sync_batch()
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        assert payload["revenue_revisions"] == [revenue.to_dict()]
        assert storage.query_revenues_for_sync() == []
    finally:
        storage.close()


@patch("dexcost.sync.urllib.request.urlopen")
def test_transport_failure_keeps_revenue_pending(
    urlopen: MagicMock, tmp_path: Path
) -> None:
    urlopen.side_effect = urllib.error.URLError("offline")
    storage = SQLiteStorage(tmp_path / "retry.db")
    revenue = _recognized()
    storage.insert_revenue(revenue)
    worker = SyncWorker(_config(), storage)
    try:
        with pytest.raises(urllib.error.URLError):
            worker._sync_batch()
        assert storage.query_revenues_for_sync() == [revenue]
    finally:
        storage.close()


def test_oversized_revenue_is_quarantined(tmp_path: Path) -> None:
    storage = MagicMock()
    worker_storage = SQLiteStorage(tmp_path / "oversized.db")
    worker = SyncWorker(_config(), worker_storage)
    revenue_id = str(uuid.uuid4())
    revenue = _recognized(revenue_id=uuid.UUID(revenue_id)).to_dict()
    revenue["source"]["record_id"] = "x" * 130_000  # type: ignore[index]
    try:
        with patch.object(worker, "_post_raw") as post_raw:
            assert worker._post_with_split(
                [], [], storage=storage, revenue_revisions=[revenue]
            )
        post_raw.assert_not_called()
        storage.mark_revenues_quarantined.assert_called_once_with([(revenue_id, 1)])
    finally:
        worker_storage.close()
