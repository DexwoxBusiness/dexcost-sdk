"""Real OpenAI 2.x durable batch, fine-tuning, and video job gates."""

from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openai import AsyncOpenAI, OpenAI
from openai.resources import batches, videos
from openai.resources.fine_tuning import jobs
from openai.types import Batch, BatchRequestCounts, BatchUsage, Video
from openai.types.batch_usage import InputTokensDetails, OutputTokensDetails
from openai.types.fine_tuning import FineTuningJob

from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture()
def storage(tmp_path: Path) -> Generator[SQLiteStorage, None, None]:
    value = SQLiteStorage(tmp_path / "openai-jobs.db")
    yield value
    value.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _restore_openai(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    yield
    uninstrument_openai()


def _batch(batch_id: str, status: str) -> Batch:
    values: dict[str, Any] = {
        "id": batch_id,
        "completion_window": "24h",
        "created_at": 1,
        "endpoint": "/v1/responses",
        "input_file_id": "file-private-input",
        "object": "batch",
        "status": status,
        "model": "gpt-4o-mini-2024-07-18",
    }
    if status == "completed":
        values.update(
            request_counts=BatchRequestCounts(completed=2, failed=1, total=3),
            usage=BatchUsage(
                input_tokens=100,
                input_tokens_details=InputTokensDetails(cached_tokens=20),
                output_tokens=50,
                output_tokens_details=OutputTokensDetails(reasoning_tokens=10),
                total_tokens=150,
            ),
            output_file_id="file-private-output",
        )
    return Batch.model_validate(values)


def _fine_tuning(job_id: str, status: str) -> FineTuningJob:
    return FineTuningJob.model_validate(
        {
            "id": job_id,
            "created_at": 1,
            "error": None,
            "fine_tuned_model": (
                "ft:gpt-4o-mini:private-name" if status == "succeeded" else None
            ),
            "finished_at": 2 if status == "succeeded" else None,
            "hyperparameters": {
                "batch_size": 2,
                "learning_rate_multiplier": 1,
                "n_epochs": 3,
            },
            "model": "gpt-4o-mini-2024-07-18",
            "object": "fine_tuning.job",
            "organization_id": "org-private",
            "result_files": ["file-private-result"] if status == "succeeded" else [],
            "seed": 7,
            "status": status,
            "trained_tokens": 12000 if status == "succeeded" else None,
            "training_file": "file-private-training",
            "validation_file": "file-private-validation",
        }
    )


def _video(video_id: str, status: str) -> Video:
    return Video.model_validate(
        {
            "id": video_id,
            "completed_at": 2 if status == "completed" else None,
            "created_at": 1,
            "error": None,
            "expires_at": 99,
            "model": "sora-2-pro",
            "object": "video",
            "progress": 100 if status == "completed" else 0,
            "prompt": "private-video-prompt-returned-by-provider",
            "remixed_from_video_id": None,
            "seconds": "8",
            "size": "1024x1792",
            "status": status,
        }
    )


def test_sync_openai_batch_fine_tuning_and_video_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    def batch_create(self: Any, **kwargs: Any) -> Batch:
        suffix = "cancel" if kwargs["input_file_id"].endswith("cancel") else "success"
        return _batch(f"batch_{suffix}", "in_progress")

    def batch_retrieve(self: Any, batch_id: str, **kwargs: Any) -> Batch:
        return _batch(batch_id, "completed")

    def batch_cancel(self: Any, batch_id: str, **kwargs: Any) -> Batch:
        return _batch(batch_id, "cancelled")

    def tuning_create(self: Any, **kwargs: Any) -> FineTuningJob:
        return _fine_tuning("ftjob_sync", "running")

    def tuning_retrieve(
        self: Any, fine_tuning_job_id: str, **kwargs: Any
    ) -> FineTuningJob:
        return _fine_tuning(fine_tuning_job_id, "succeeded")

    def tuning_running(
        self: Any, fine_tuning_job_id: str, **kwargs: Any
    ) -> FineTuningJob:
        return _fine_tuning(fine_tuning_job_id, "running")

    def video_create(self: Any, **kwargs: Any) -> Video:
        return _video("video_sync", "queued")

    def video_retrieve(self: Any, video_id: str, **kwargs: Any) -> Video:
        return _video(video_id, "completed")

    monkeypatch.setattr(batches.Batches, "create", batch_create)
    monkeypatch.setattr(batches.Batches, "retrieve", batch_retrieve)
    monkeypatch.setattr(batches.Batches, "cancel", batch_cancel)
    monkeypatch.setattr(jobs.Jobs, "create", tuning_create)
    monkeypatch.setattr(jobs.Jobs, "retrieve", tuning_retrieve)
    monkeypatch.setattr(jobs.Jobs, "pause", tuning_running)
    monkeypatch.setattr(jobs.Jobs, "resume", tuning_running)
    monkeypatch.setattr(videos.Videos, "create", video_create)
    monkeypatch.setattr(videos.Videos, "retrieve", video_retrieve)
    instrument_openai(tracker)
    client = OpenAI(api_key="test-key")
    try:
        with tracker.task(task_type="openai-provider-jobs") as task:
            batch = client.batches.create(
                completion_window="24h",
                endpoint="/v1/responses",
                input_file_id="file-private-success",
                metadata={"private": "batch-metadata"},
            )
            client.batches.retrieve(batch.id)
            client.batches.retrieve(batch.id)
            cancelling = client.batches.create(
                completion_window="24h",
                endpoint="/v1/responses",
                input_file_id="file-private-cancel",
            )
            client.batches.cancel(cancelling.id)

            tuning = client.fine_tuning.jobs.create(
                model="gpt-4o-mini-2024-07-18",
                training_file="file-private-training",
                hyperparameters={"n_epochs": 3},
                method={"type": "supervised"},
                metadata={"private": "tuning-metadata"},
            )
            client.fine_tuning.jobs.pause(tuning.id)
            client.fine_tuning.jobs.resume(tuning.id)
            client.fine_tuning.jobs.retrieve(tuning.id)
            client.fine_tuning.jobs.retrieve(tuning.id)

            video = client.videos.create(
                model="sora-2-pro",
                prompt="private-video-request",
                seconds="8",
                size="1024x1792",
            )
            client.videos.retrieve(video.id)
            client.videos.retrieve(video.id)
    finally:
        client.close()

    batch_job = storage.get_provider_job("openai", "batches", "batch_success")
    assert batch_job is not None
    assert (batch_job.status, batch_job.revision) == ("succeeded", 2)
    assert {line.metric: line.quantity for line in batch_job.usage} == {
        "batch_input_tokens": Decimal(80),
        "batch_cache_read_input_tokens": Decimal(20),
        "batch_output_tokens": Decimal(50),
        "batch_reasoning_output_tokens": Decimal(10),
        "batch_request_count": Decimal(3),
        "batch_successful_request_count": Decimal(2),
        "batch_failed_request_count": Decimal(1),
    }
    assert batch_job.task_input_tokens == 100
    assert batch_job.task_output_tokens == 50
    assert batch_job.task_cached_tokens == 20
    assert batch_job.cost_amount == Decimal("0.00002100")
    assert dict(batch_job.billing_dimensions) == {
        "batch_completion_window": "24h",
        "batch_endpoint": "/v1/responses",
    }
    cancelled = storage.get_provider_job("openai", "batches", "batch_cancel")
    assert cancelled is not None
    assert (cancelled.status, cancelled.revision) == ("cancelled", 2)

    tuning_job = storage.get_provider_job(
        "openai", "fine_tuning", "ftjob_sync"
    )
    assert tuning_job is not None
    assert (tuning_job.status, tuning_job.revision) == ("succeeded", 3)
    assert {line.metric: line.quantity for line in tuning_job.usage} == {
        "training_billable_tokens": Decimal(12000)
    }
    assert tuning_job.cost_amount is None
    assert dict(tuning_job.billing_dimensions) == {
        "fine_tuning_method": "supervised",
        "requested_epoch_count": "3",
    }

    video_job = storage.get_provider_job("openai", "videos", "video_sync")
    assert video_job is not None
    assert (video_job.status, video_job.revision) == ("succeeded", 2)
    assert {line.metric: line.quantity for line in video_job.usage} == {
        "output_video_count": Decimal(1),
        "output_video_seconds": Decimal(8),
    }
    assert video_job.cost_amount == Decimal(4)
    assert dict(video_job.billing_dimensions) == {
        "requested_video_seconds": "8",
        "video_size": "1024x1792",
    }
    assert video_job.task_id == task.task_id

    durable = str(batch_job.to_dict()) + str(tuning_job.to_dict()) + str(
        video_job.to_dict()
    )
    for private in (
        "file-private",
        "batch-metadata",
        "tuning-metadata",
        "private-name",
        "org-private",
        "private-video",
    ):
        assert private not in durable


@pytest.mark.asyncio
async def test_async_openai_batch_fine_tuning_and_video_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    async def batch_create(self: Any, **kwargs: Any) -> Batch:
        return _batch("batch_async", "validating")

    async def batch_retrieve(self: Any, batch_id: str, **kwargs: Any) -> Batch:
        return _batch(batch_id, "completed")

    async def tuning_create(self: Any, **kwargs: Any) -> FineTuningJob:
        return _fine_tuning("ftjob_async", "queued")

    async def tuning_retrieve(
        self: Any, fine_tuning_job_id: str, **kwargs: Any
    ) -> FineTuningJob:
        return _fine_tuning(fine_tuning_job_id, "succeeded")

    async def video_create(self: Any, **kwargs: Any) -> Video:
        return _video("video_async", "in_progress")

    async def video_retrieve(self: Any, video_id: str, **kwargs: Any) -> Video:
        return _video(video_id, "completed")

    monkeypatch.setattr(batches.AsyncBatches, "create", batch_create)
    monkeypatch.setattr(batches.AsyncBatches, "retrieve", batch_retrieve)
    monkeypatch.setattr(jobs.AsyncJobs, "create", tuning_create)
    monkeypatch.setattr(jobs.AsyncJobs, "retrieve", tuning_retrieve)
    monkeypatch.setattr(videos.AsyncVideos, "create", video_create)
    monkeypatch.setattr(videos.AsyncVideos, "retrieve", video_retrieve)
    instrument_openai(tracker)
    client = AsyncOpenAI(api_key="test-key")
    try:
        with tracker.task(task_type="openai-provider-jobs-async") as task:
            batch = await client.batches.create(
                completion_window="24h",
                endpoint="/v1/responses",
                input_file_id="file-private-async-batch",
            )
            await client.batches.retrieve(batch.id)
            tuning = await client.fine_tuning.jobs.create(
                model="gpt-4o-mini-2024-07-18",
                training_file="file-private-async-tuning",
            )
            await client.fine_tuning.jobs.retrieve(tuning.id)
            video = await client.videos.create(
                model="sora-2-pro",
                prompt="private-async-video",
                seconds="8",
                size="1024x1792",
            )
            await client.videos.retrieve(video.id)
    finally:
        await client.close()

    batch_job = storage.get_provider_job("openai", "batches", "batch_async")
    tuning_job = storage.get_provider_job(
        "openai", "fine_tuning", "ftjob_async"
    )
    video_job = storage.get_provider_job("openai", "videos", "video_async")
    assert batch_job is not None and batch_job.revision == 2
    assert tuning_job is not None and tuning_job.revision == 2
    assert video_job is not None and video_job.revision == 2
    assert batch_job.task_id == task.task_id
    assert tuning_job.task_id == task.task_id
    assert video_job.task_id == task.task_id
    assert "private-async" not in (
        str(batch_job.to_dict())
        + str(tuning_job.to_dict())
        + str(video_job.to_dict())
    )


def test_openai_job_submission_error_preserves_native_exception(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    class NativeOpenAIJobError(RuntimeError):
        code = "rate_limit_exceeded"

    failure = NativeOpenAIJobError("private-native-message")

    def create(self: Any, **kwargs: Any) -> Batch:
        raise failure

    monkeypatch.setattr(batches.Batches, "create", create)
    instrument_openai(tracker)
    client = OpenAI(api_key="test-key")
    try:
        with pytest.raises(NativeOpenAIJobError) as caught:
            client.batches.create(
                completion_window="24h",
                endpoint="/v1/responses",
                input_file_id="file-private-error",
            )
    finally:
        client.close()

    assert caught.value is failure
    events = storage.query_events()
    assert len(events) == 1
    assert events[0].details["error_code"] == "rate_limit_exceeded"
    assert "private-native-message" not in str(events[0].to_dict())
    assert storage.query_provider_jobs_for_sync(limit=10) == []
