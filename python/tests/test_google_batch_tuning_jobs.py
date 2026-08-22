"""Real google-genai batch and tuning lifecycle compatibility gates."""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dexcost.instruments.gemini import instrument_gemini, uninstrument_gemini
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

genai = pytest.importorskip("google.genai")
from google.genai import batches, tunings, types  # noqa: E402


@pytest.fixture()
def storage(tmp_path: Path) -> Generator[SQLiteStorage, None, None]:
    value = SQLiteStorage(tmp_path / "google-batch-tuning.db")
    yield value
    value.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _restore_google() -> Generator[None, None, None]:
    yield
    uninstrument_gemini()


def _content_response(secret: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        model_version="models/gemini-3.1-pro-preview",
        response_id=f"response-{secret}",
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=f"private-{secret}")]
                )
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100,
            cached_content_token_count=20,
            candidates_token_count=12,
            thoughts_token_count=5,
            total_token_count=117,
        ),
    )


def _completed_batch(name: str) -> types.BatchJob:
    return types.BatchJob(
        name=name,
        model="models/gemini-3.1-pro-preview",
        state="JOB_STATE_SUCCEEDED",
        completion_stats=types.CompletionStats(
            successful_count=1,
            failed_count=1,
            incomplete_count=2,
        ),
        dest=types.BatchJobDestination(
            inlined_responses=[
                types.InlinedResponse(
                    response=_content_response("batch-response-payload")
                )
            ]
        ),
    )


def _completed_tuning(name: str) -> types.TuningJob:
    return types.TuningJob(
        name=name,
        base_model="models/gemini-2.5-flash",
        state="JOB_STATE_SUCCEEDED",
        tuning_data_stats=types.TuningDataStats(
            supervised_tuning_data_stats=types.SupervisedTuningDataStats(
                total_billable_token_count=1250,
                total_billable_character_count=4200,
                tuning_dataset_example_count=40,
                tuning_step_count=10,
            )
        ),
        tuning_job_metadata=types.TuningJobMetadata(
            completed_step_count=8,
            completed_epoch_count=2,
        ),
    )


def test_sync_batch_lifecycle_native_usage_pricing_cancel_and_privacy(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    def create(self: Any, **kwargs: Any) -> types.BatchJob:
        suffix = "cancel" if isinstance(kwargs.get("src"), str) else "success"
        return types.BatchJob(
            name=f"batches/{suffix}",
            model="models/gemini-3.1-pro-preview",
            state="JOB_STATE_QUEUED",
        )

    def get(self: Any, **kwargs: Any) -> types.BatchJob:
        name = kwargs["name"]
        if name == "batches/cancel":
            return types.BatchJob(
                name=name,
                model="models/gemini-3.1-pro-preview",
                state="JOB_STATE_CANCELLED",
                error=types.JobError(code=1, message="private-cancel-error"),
            )
        return _completed_batch(name)

    def cancel(self: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(batches.Batches, "create", create)
    monkeypatch.setattr(batches.Batches, "get", get)
    monkeypatch.setattr(batches.Batches, "cancel", cancel)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-batch-sync") as task:
            submitted = client.batches.create(
                model="gemini-3.1-pro-preview",
                src=[
                    {
                        "contents": [
                            {"parts": [{"text": "private-batch-request"}]}
                        ]
                    }
                ],
            )
            pending = storage.get_provider_job("google", "gemini", submitted.name)
            assert pending is not None
            assert (pending.status, pending.revision, pending.usage) == (
                "submitted",
                1,
                (),
            )
            completed = client.batches.get(name=submitted.name)
            replay = client.batches.get(name=submitted.name)

            cancelling = client.batches.create(
                model="gemini-3.1-pro-preview",
                src="gs://private-bucket/private-requests.jsonl",
            )
            assert client.batches.cancel(name=cancelling.name) is None
            before_poll = storage.get_provider_job(
                "google", "gemini", cancelling.name
            )
            assert before_poll is not None and before_poll.status == "submitted"
            cancelled = client.batches.get(name=cancelling.name)
    finally:
        client.close()

    assert completed.state == "JOB_STATE_SUCCEEDED"
    assert replay.state == "JOB_STATE_SUCCEEDED"
    assert cancelled.state == "JOB_STATE_CANCELLED"
    job = storage.get_provider_job("google", "gemini", "batches/success")
    assert job is not None
    assert (job.status, job.revision) == ("succeeded", 2)
    usage = {line.metric: line.quantity for line in job.usage}
    assert usage == {
        "batch_cache_read_input_tokens": Decimal(20),
        "batch_failed_request_count": Decimal(1),
        "batch_incomplete_request_count": Decimal(2),
        "batch_input_tokens": Decimal(80),
        "batch_output_tokens": Decimal(12),
        "batch_reasoning_output_tokens": Decimal(5),
        "batch_successful_request_count": Decimal(1),
    }
    assert job.task_input_tokens == 100
    assert job.task_output_tokens == 17
    assert job.task_cached_tokens == 20
    assert job.cost_amount == Decimal("0.000182")
    assert job.cost_source == "sdk_catalog"
    assert dict(job.billing_dimensions) == {"batch_source": "inline"}
    assert len(storage.query_provider_job_history(str(job.event_id))) == 2

    cancelled_job = storage.get_provider_job(
        "google", "gemini", "batches/cancel"
    )
    assert cancelled_job is not None
    assert (cancelled_job.status, cancelled_job.revision) == ("cancelled", 2)
    assert dict(cancelled_job.billing_dimensions) == {"batch_source": "gcs"}
    durable = str(job.to_dict()) + str(cancelled_job.to_dict())
    assert "private-batch" not in durable
    assert "private-bucket" not in durable
    assert "private-cancel" not in durable
    assert "batch-response-payload" not in durable
    stored_task = storage.get_task(str(task.task_id))
    assert stored_task is not None and stored_task.status == "success"


def test_sync_tuning_lifecycle_observed_training_usage_and_privacy(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    def tune(self: Any, **kwargs: Any) -> types.TuningJob:
        return types.TuningJob(
            name="tuningJobs/sync",
            base_model=kwargs["base_model"],
            state="JOB_STATE_RUNNING",
        )

    def get(self: Any, **kwargs: Any) -> types.TuningJob:
        return _completed_tuning(kwargs["name"])

    def cancel(self: Any, **kwargs: Any) -> types.CancelTuningJobResponse:
        return types.CancelTuningJobResponse()

    monkeypatch.setattr(tunings.Tunings, "tune", tune)
    monkeypatch.setattr(tunings.Tunings, "get", get)
    monkeypatch.setattr(tunings.Tunings, "cancel", cancel)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-tuning-sync"):
            submitted = client.tunings.tune(
                base_model="gemini-2.5-flash",
                training_dataset=types.TuningDataset(
                    gcs_uri="gs://private-training-bucket/private.jsonl"
                ),
                config=types.CreateTuningJobConfig(epoch_count=2),
            )
            assert client.tunings.cancel(name=submitted.name) is not None
            acknowledged = storage.get_provider_job(
                "google", "gemini", submitted.name
            )
            assert acknowledged is not None
            assert (acknowledged.status, acknowledged.revision) == ("submitted", 1)
            completed = client.tunings.get(name=submitted.name)
            replay = client.tunings.get(name=submitted.name)
    finally:
        client.close()

    assert completed.state == "JOB_STATE_SUCCEEDED"
    assert replay.state == "JOB_STATE_SUCCEEDED"
    job = storage.get_provider_job("google", "gemini", "tuningJobs/sync")
    assert job is not None
    assert (job.status, job.revision, job.event_type) == (
        "succeeded",
        2,
        "external_cost",
    )
    assert {line.metric: line.quantity for line in job.usage} == {
        "training_billable_tokens": Decimal(1250),
        "training_billable_characters": Decimal(4200),
        "training_example_count": Decimal(40),
        "training_step_count": Decimal(8),
        "training_epoch_count": Decimal(2),
    }
    assert job.cost_amount is None
    assert dict(job.billing_dimensions) == {
        "requested_epoch_count": "2",
    }
    assert "private-training" not in str(job.to_dict())
    assert len(storage.query_provider_job_history(str(job.event_id))) == 2


@pytest.mark.asyncio
async def test_async_batch_and_tuning_lifecycles(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    async def batch_create(self: Any, **kwargs: Any) -> types.BatchJob:
        return types.BatchJob(
            name="batches/async",
            model=kwargs["model"],
            state="JOB_STATE_PENDING",
        )

    async def batch_get(self: Any, **kwargs: Any) -> types.BatchJob:
        return _completed_batch(kwargs["name"])

    async def tune(self: Any, **kwargs: Any) -> types.TuningJob:
        return types.TuningJob(
            name="tuningJobs/async",
            base_model=kwargs["base_model"],
            state="JOB_STATE_QUEUED",
        )

    async def tuning_get(self: Any, **kwargs: Any) -> types.TuningJob:
        return _completed_tuning(kwargs["name"])

    monkeypatch.setattr(batches.AsyncBatches, "create", batch_create)
    monkeypatch.setattr(batches.AsyncBatches, "get", batch_get)
    monkeypatch.setattr(tunings.AsyncTunings, "tune", tune)
    monkeypatch.setattr(tunings.AsyncTunings, "get", tuning_get)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-durable-async") as task:
            batch = await client.aio.batches.create(
                model="gemini-3.1-pro-preview",
                src=[{"contents": "private-async-batch"}],
            )
            batch_result = await client.aio.batches.get(name=batch.name)
            tuning = await client.aio.tunings.tune(
                base_model="gemini-2.5-flash",
                training_dataset=types.TuningDataset(
                    gcs_uri="gs://private-async-tuning/data.jsonl"
                ),
            )
            tuning_result = await client.aio.tunings.get(name=tuning.name)
    finally:
        await client.aio.aclose()
        client.close()

    assert batch_result.state == "JOB_STATE_SUCCEEDED"
    assert tuning_result.state == "JOB_STATE_SUCCEEDED"
    batch_job = storage.get_provider_job("google", "gemini", "batches/async")
    tuning_job = storage.get_provider_job(
        "google", "gemini", "tuningJobs/async"
    )
    assert batch_job is not None and batch_job.revision == 2
    assert tuning_job is not None and tuning_job.revision == 2
    assert batch_job.task_id == task.task_id
    assert tuning_job.task_id == task.task_id
    durable = str(batch_job.to_dict()) + str(tuning_job.to_dict())
    assert "private-async" not in durable


def test_batch_submission_failure_preserves_native_exception(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    class NativeBatchError(RuntimeError):
        code = 429

    failure = NativeBatchError("private-provider-message")

    def create(self: Any, **kwargs: Any) -> types.BatchJob:
        raise failure

    monkeypatch.setattr(batches.Batches, "create", create)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with pytest.raises(NativeBatchError) as caught:
            client.batches.create(
                model="gemini-3.1-pro-preview",
                src=[{"contents": "private-failing-request"}],
            )
    finally:
        client.close()

    assert caught.value is failure
    events = storage.query_events()
    assert len(events) == 1
    assert events[0].details["attribution_operation_status"] == "failed"
    assert events[0].details["error_code"] == "429"
    assert "private-provider-message" not in str(events[0].to_dict())
    assert storage.query_provider_jobs_for_sync(limit=10) == []
