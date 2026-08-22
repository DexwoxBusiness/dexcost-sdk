"""Python SDK business-outcome capture and durability tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import dexcost
from dexcost.models.outcome import OutcomeRevision, OutcomeValue
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def test_campaign_exported_matches_wire_contract() -> None:
    task_id = uuid.uuid4()
    outcome_id = uuid.UUID("4b2b84d8-972f-4ca2-8919-e2f21c41d17c")
    outcome = OutcomeRevision(
        outcome_id=outcome_id,
        task_id=task_id,
        name="campaign_exported",
        state="achieved",
        revision=1,
        effective_at=datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 8, 16, 12, 0, 1, tzinfo=timezone.utc),
        value=OutcomeValue.from_input(True),
    )

    assert outcome.to_dict() == {
        "schema_version": "1",
        "outcome_id": str(outcome_id),
        "task_id": str(task_id),
        "name": "campaign_exported",
        "effective_at": "2026-08-16T12:00:00.000000Z",
        "observed_at": "2026-08-16T12:00:01.000000Z",
        "lifecycle": {"state": "achieved", "revision": 1},
        "value": {"type": "boolean", "value": True},
    }


def test_outcome_rejects_placeholder_task_id_and_invalid_lifecycle() -> None:
    with pytest.raises(ValueError, match="task_id must be a valid UUID"):
        OutcomeRevision(  # type: ignore[arg-type]
            task_id="ROOT_CAMPAIGN_TASK_UUID",
            name="campaign_exported",
        )

    with pytest.raises(ValueError, match="pending outcomes cannot assert a value"):
        OutcomeRevision(
            task_id=uuid.uuid4(),
            name="campaign_exported",
            state="pending",
            value=OutcomeValue.from_input(True),
        )


def test_outcome_value_preserves_exact_types() -> None:
    assert OutcomeValue.from_input("approved").to_dict() == {
        "type": "string",
        "value": "approved",
    }
    assert OutcomeValue.from_input(3).to_dict() == {"type": "integer", "value": "3"}
    assert OutcomeValue.from_input(Decimal("1.2500")).to_dict() == {
        "type": "decimal",
        "value": "1.25",
    }


def test_storage_enforces_outcome_revision_stream(tmp_path: Path) -> None:
    storage = SQLiteStorage(db_path=tmp_path / "outcomes.db")
    task_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    first = OutcomeRevision(
        outcome_id=outcome_id,
        task_id=task_id,
        name="variant_approved",
        state="pending",
    )
    storage.insert_outcome(first)
    storage.insert_outcome(first)  # Identical delivery is idempotent.

    achieved = OutcomeRevision(
        outcome_id=outcome_id,
        revision=2,
        task_id=task_id,
        name="variant_approved",
        state="achieved",
        value=OutcomeValue.from_input(True),
    )
    storage.insert_outcome(achieved)
    assert [row.revision for row in storage.query_outcomes_for_sync()] == [1, 2]
    assert storage.query_outcome_history(str(outcome_id)) == [first, achieved]

    with pytest.raises(ValueError, match="already exists with different contents"):
        storage.insert_outcome(
            OutcomeRevision(
                outcome_id=outcome_id,
                revision=2,
                task_id=task_id,
                name="variant_approved",
                state="missed",
            )
        )

    with pytest.raises(ValueError, match="expected revision 3"):
        storage.insert_outcome(
            OutcomeRevision(
                outcome_id=outcome_id,
                revision=4,
                task_id=task_id,
                name="variant_approved",
                state="achieved",
            )
        )
    storage.close()


def test_cleanup_retains_outcome_ledger_for_later_corrections(tmp_path: Path) -> None:
    storage = SQLiteStorage(db_path=tmp_path / "retention.db")
    task_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    first = OutcomeRevision(
        outcome_id=outcome_id,
        task_id=task_id,
        name="campaign_exported",
        value=OutcomeValue.from_input(True),
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    storage.insert_outcome(first)
    storage.mark_outcomes_synced([(str(outcome_id), 1)])

    storage.purge_synced(retention_hours=1)
    storage.purge_old_pending(max_age_days=1)
    corrected = OutcomeRevision(
        outcome_id=outcome_id,
        revision=2,
        task_id=task_id,
        name="campaign_exported",
        state="missed",
        value=OutcomeValue.from_input(False),
    )
    storage.insert_outcome(corrected)

    assert storage.query_outcomes_for_sync() == [corrected]
    storage.close()


def test_tracked_task_records_outcome_without_inferring_from_success(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(db_path=tmp_path / "tracker.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    root_id = uuid.uuid4()

    with tracker.task(
        task_type="campaign.root",
        task_id=root_id,
        root_task_id=root_id,
        customer_id="dexcost-internal",
        project_id="dexcost-marketing-campaign",
    ) as task:
        outcome = task.record_outcome("campaign_exported", value=True)

    assert outcome.task_id == root_id
    assert outcome.state == "achieved"
    assert len(storage.query_outcomes_for_sync()) == 1

    with tracker.task(task_type="campaign.no_outcome"):
        pass
    assert len(storage.query_outcomes_for_sync()) == 1
    storage.close()


def test_singleton_record_outcome_uses_current_task(tmp_path: Path) -> None:
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "singleton.db"),
        auto_instrument=[],
        track_http=False,
        track_network=False,
    )
    try:
        root_id = uuid.uuid4()
        with dexcost.task(
            task_type="campaign.root",
            task_id=root_id,
            root_task_id=root_id,
        ):
            outcome = dexcost.record_outcome("campaign_exported", value=True)
        assert outcome.task_id == root_id
        assert outcome.to_dict()["lifecycle"] == {"state": "achieved", "revision": 1}
    finally:
        dexcost.close()
