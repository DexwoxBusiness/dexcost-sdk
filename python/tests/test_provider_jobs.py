"""Durable asynchronous provider-job lifecycle and reconciliation tests."""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dexcost.config import DexcostConfig
from dexcost.instruments._provider_metering import (
    OperationMeasurement,
    ProviderUsageLine,
    record_provider_operation,
)
from dexcost.models.provider_job import (
    ProviderJobRevision,
    ProviderJobUsageLine,
    provider_job_event_id,
)
from dexcost.models.task import Task
from dexcost.provider_jobs import (
    reconcile_provider_job,
    record_provider_job_submission,
)
from dexcost.storage.migrations import TARGET_SCHEMA_VERSION
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.sync import SyncWorker
from dexcost.tracker import CostTracker

_SUBMITTED = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)


class _Pricing:
    def get_metered_cost(self, *args: object, **kwargs: object) -> SimpleNamespace:
        usage = kwargs.get("usage")
        if usage is None and len(args) > 1:
            usage = args[1]
        token_count = Decimal(str(dict(usage or {}).get("output_tokens", 0)))
        return SimpleNamespace(
            cost_usd=token_count / Decimal("10"),
            cost_confidence="computed",
            pricing_source="service_catalog",
            pricing_version="provider-jobs-test-v1",
            resolved_model="gemini-3-pro",
            lines=(),
            unpriced_dimensions=(),
        )


def _task() -> Task:
    return Task(
        task_id=uuid.uuid4(),
        task_type="google.background.interaction",
        started_at=_SUBMITTED,
    )


def _measurement(output_tokens: int = 20) -> OperationMeasurement:
    return OperationMeasurement(
        pricing_usage={"output_tokens": output_tokens},
        usage_lines=(ProviderUsageLine("output_tokens", output_tokens, "Tokens"),),
        response_model="gemini-3-pro",
        task_output_tokens=output_tokens,
    )


def _submission(task: Task, revision: int = 1) -> ProviderJobRevision:
    record_id = "interaction-job-123"
    return ProviderJobRevision(
        event_id=provider_job_event_id("google", "interactions", record_id),
        revision=revision,
        task_id=task.task_id,
        provider="google",
        service="interactions",
        provider_record_id=record_id,
        operation="google.genai.interactions.create",
        component="external",
        event_type="llm_call",
        resource_type="model",
        resource_id="gemini-3-pro",
        status="submitted",
        submitted_at=_SUBMITTED,
        observed_at=_SUBMITTED,
        owns_task=True,
    )


def _success_response() -> MagicMock:
    response = MagicMock()
    response.status = 202
    response.read.return_value = b'{"accepted":3,"rejected":0}'
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_pending_and_terminal_snapshots_are_strict_v3_observations() -> None:
    task = _task()
    pending = _submission(task)
    wire = pending.to_attribution_observation(environment="production")
    assert wire["lifecycle"] == {"state": "pending", "revision": 1}
    assert wire["operation"]["status"] == "in_progress"
    assert wire["usage"] == []
    assert "cost_evidence" not in wire
    assert wire["usage_period"] == {"start_at": "2026-08-21T10:00:00.000000Z"}

    # Exercise the durable parser instead of relying on dataclass construction
    # accepting a wire-shaped dictionary.
    final = ProviderJobRevision.from_dict(
        {
            **pending.to_dict(),
            "revision": 2,
            "status": "succeeded",
            "observed_at": "2026-08-21T10:01:00.000000Z",
            "usage": [
                ProviderJobUsageLine("output_tokens", 20, "Tokens").to_dict()
            ],
            "cost_amount": "2",
            "cost_source": "sdk_catalog",
            "cost_confidence": "computed",
            "pricing_version": "provider-jobs-test-v1",
            "task_output_tokens": 20,
        }
    )
    final_wire = final.to_attribution_observation(environment="production")
    assert final_wire["lifecycle"] == {"state": "final", "revision": 2}
    assert final_wire["operation"]["status"] == "succeeded"
    assert final_wire["usage"][0]["quantity"] == "20"
    assert final_wire["cost_evidence"]["amount"] == "2"
    assert final_wire["usage_period"]["end_at"] == "2026-08-21T10:01:00.000000Z"


def test_storage_enforces_revision_identity_and_terminal_monotonicity(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "jobs.db")
    task = _task()
    first = _submission(task)
    try:
        assert storage.get_schema_version() == TARGET_SCHEMA_VERSION == 14
        storage.insert_provider_job_revision(first)
        storage.insert_provider_job_revision(first)
        assert storage.query_provider_job_history(str(first.event_id)) == [first]

        with pytest.raises(ValueError, match="expected revision 2"):
            storage.insert_provider_job_revision(_submission(task, revision=3))

        final = ProviderJobRevision.from_dict(
            {
                **first.to_dict(),
                "revision": 2,
                "status": "failed",
                "observed_at": "2026-08-21T10:01:00.000000Z",
                "error_type": "provider.failed",
            }
        )
        storage.insert_provider_job_revision(final)
        with pytest.raises(ValueError, match="cannot return to pending"):
            storage.insert_provider_job_revision(
                ProviderJobRevision.from_dict(
                    {
                        **first.to_dict(),
                        "revision": 3,
                        "status": "running",
                        "observed_at": "2026-08-21T10:02:00.000000Z",
                    }
                )
            )
    finally:
        storage.close()


def test_reconciliation_deduplicates_polls_and_replaces_task_rollup(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "reconcile.db")
    tracker = CostTracker(storage=storage, pricing=_Pricing(), auto_instrument=[])
    task = _task()
    storage.insert_task(task)
    try:
        submitted = record_provider_job_submission(
            tracker=tracker,
            task=task,
            owns_task=True,
            provider="google",
            service="interactions",
            provider_record_id="interaction-job-123",
            operation="google.genai.interactions.create",
            component="external",
            event_type="llm_call",
            resource_type="model",
            resource_id="gemini-3-pro",
            submitted_at=_SUBMITTED,
            observed_at=_SUBMITTED,
        )
        running = reconcile_provider_job(
            tracker=tracker,
            provider="google",
            service="interactions",
            provider_record_id="interaction-job-123",
            status="running",
            observed_at=_SUBMITTED + timedelta(seconds=10),
        )
        replay = reconcile_provider_job(
            tracker=tracker,
            provider="google",
            service="interactions",
            provider_record_id="interaction-job-123",
            status="running",
            observed_at=_SUBMITTED + timedelta(seconds=20),
        )
        assert (submitted.revision, running.revision, replay.revision) == (1, 2, 2)

        succeeded = reconcile_provider_job(
            tracker=tracker,
            provider="google",
            service="interactions",
            provider_record_id="interaction-job-123",
            status="succeeded",
            measurement=_measurement(20),
            observed_at=_SUBMITTED + timedelta(minutes=1),
        )
        stored_task = storage.get_task(str(task.task_id))
        assert succeeded.revision == 3
        assert stored_task is not None
        assert stored_task.status == "success"
        assert stored_task.total_cost_usd == Decimal("2")
        assert stored_task.total_output_tokens == 20

        corrected = reconcile_provider_job(
            tracker=tracker,
            provider="google",
            service="interactions",
            provider_record_id="interaction-job-123",
            status="succeeded",
            measurement=_measurement(30),
            observed_at=_SUBMITTED + timedelta(minutes=2),
        )
        stored_task = storage.get_task(str(task.task_id))
        assert corrected.revision == 4
        assert stored_task is not None
        assert stored_task.total_cost_usd == Decimal("3")
        assert stored_task.total_output_tokens == 30
        assert len(storage.query_provider_job_history(str(submitted.event_id))) == 4
    finally:
        storage.close()


def test_provider_reported_cost_overrides_catalog_for_calls_and_jobs(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "provider-cost.db")
    tracker = CostTracker(storage=storage, pricing=_Pricing(), auto_instrument=[])
    task = _task()
    storage.insert_task(task)
    measurement = OperationMeasurement(
        pricing_usage={"output_tokens": 20},
        usage_lines=(ProviderUsageLine("output_tokens", 20, "Tokens"),),
        provider_cost_usd="0.125",
        response_model="openrouter/openai/gpt-5",
        task_output_tokens=20,
    )
    try:
        event = record_provider_operation(
            tracker=tracker,
            task=task,
            provider="openrouter",
            service="openrouter",
            operation="openrouter.chat.send",
            component="llm",
            event_type="llm_call",
            model="openrouter/openai/gpt-5",
            measurement=measurement,
            latency_ms=1,
        )
        assert event.cost_usd == Decimal("0.125")
        assert event.cost_confidence == "exact"
        assert event.pricing_source == "provider_response"
        assert event.pricing_version is None
        assert event.details["provider_reported_cost_usd"] == "0.125"

        submitted = record_provider_job_submission(
            tracker=tracker,
            task=task,
            owns_task=False,
            provider="openrouter",
            service="video_generation",
            provider_record_id="video-job-1",
            operation="openrouter.video_generation.generate",
            component="external",
            event_type="external_cost",
            resource_type="model",
            resource_id="openrouter/google/veo-3.1",
        )
        final = reconcile_provider_job(
            tracker=tracker,
            provider="openrouter",
            service="video_generation",
            provider_record_id=submitted.provider_record_id,
            status="succeeded",
            measurement=measurement,
        )
        assert final.cost_amount == Decimal("0.125")
        assert final.cost_source == "provider_reported"
        assert final.cost_confidence == "exact"
        assert final.pricing_version is None
    finally:
        storage.close()


def test_two_storage_connections_reconcile_same_terminal_poll_once(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "cross-process.db"
    first_storage = SQLiteStorage(db_path)
    second_storage = SQLiteStorage(db_path)
    first_tracker = CostTracker(
        storage=first_storage, pricing=_Pricing(), auto_instrument=[]
    )
    second_tracker = CostTracker(
        storage=second_storage, pricing=_Pricing(), auto_instrument=[]
    )
    task = _task()
    first_storage.insert_task(task)
    submitted = record_provider_job_submission(
        tracker=first_tracker,
        task=task,
        owns_task=False,
        provider="google",
        service="interactions",
        provider_record_id="cross-process-job-1",
        operation="google.genai.interactions.create",
        component="external",
        event_type="llm_call",
        resource_type="model",
        resource_id="gemini-3-pro",
        submitted_at=_SUBMITTED,
        observed_at=_SUBMITTED,
    )
    barrier = threading.Barrier(2)

    def finish(tracker: CostTracker) -> int:
        barrier.wait()
        return reconcile_provider_job(
            tracker=tracker,
            provider="google",
            service="interactions",
            provider_record_id="cross-process-job-1",
            status="succeeded",
            measurement=_measurement(),
            observed_at=_SUBMITTED + timedelta(minutes=1),
        ).revision

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            revisions = list(
                executor.map(finish, (first_tracker, second_tracker))
            )
        assert revisions == [2, 2]
        history = first_storage.query_provider_job_history(str(submitted.event_id))
        assert [revision.revision for revision in history] == [1, 2]
    finally:
        second_storage.close()
        first_storage.close()


@patch("dexcost.sync.urllib.request.urlopen")
def test_job_revisions_share_mixed_ingest_and_ack_individually(
    urlopen: MagicMock,
    tmp_path: Path,
) -> None:
    urlopen.return_value = _success_response()
    storage = SQLiteStorage(tmp_path / "sync.db")
    tracker = CostTracker(storage=storage, pricing=_Pricing(), auto_instrument=[])
    task = _task()
    storage.insert_task(task)
    submission = record_provider_job_submission(
        tracker=tracker,
        task=task,
        owns_task=True,
        provider="google",
        service="interactions",
        provider_record_id="interaction-job-123",
        operation="google.genai.interactions.create",
        component="external",
        event_type="llm_call",
        resource_type="model",
        resource_id="gemini-3-pro",
        submitted_at=_SUBMITTED,
        observed_at=_SUBMITTED,
    )
    reconcile_provider_job(
        tracker=tracker,
        provider="google",
        service="interactions",
        provider_record_id="interaction-job-123",
        status="succeeded",
        measurement=_measurement(),
        observed_at=_SUBMITTED + timedelta(minutes=1),
    )
    worker = SyncWorker(
        DexcostConfig(api_key="dx_live_test123", environment="production"),
        storage,
    )
    try:
        assert worker._sync_batch()
        payload = json.loads(urlopen.call_args.args[0].data)
        jobs = [
            event
            for event in payload["events"]
            if event["event_id"] == str(submission.event_id)
        ]
        assert [job["lifecycle"]["revision"] for job in jobs] == [1, 2]
        assert jobs[0]["lifecycle"]["state"] == "pending"
        assert jobs[1]["lifecycle"]["state"] == "final"
        assert storage.query_provider_jobs_for_sync() == []
        counts = storage.delivery_counts()
        assert counts["pending_provider_jobs"] == 0
        assert counts["quarantined_provider_jobs"] == 0
    finally:
        storage.close()
