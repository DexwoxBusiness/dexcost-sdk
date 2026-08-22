"""Real google-genai long-running video operation compatibility gates."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from dexcost.instruments.gemini import instrument_gemini, uninstrument_gemini
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

genai = pytest.importorskip("google.genai")
from google.genai import models, operations, types  # noqa: E402


@pytest.fixture()
def storage(tmp_path: Path) -> Generator[SQLiteStorage, None, None]:
    value = SQLiteStorage(tmp_path / "google-video.db")
    yield value
    value.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _restore_google() -> Generator[None, None, None]:
    yield
    uninstrument_gemini()


def _completed(name: str) -> types.GenerateVideosOperation:
    return types.GenerateVideosOperation(
        name=name,
        done=True,
        response=types.GenerateVideosResponse(
            generated_videos=[
                types.GeneratedVideo(
                    video=types.Video(uri="gs://private-bucket/private-video.mp4")
                )
            ]
        ),
    )


def test_sync_video_submission_poll_failure_and_privacy(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    pending = types.GenerateVideosOperation(name="operations/video-success", done=False)
    failed_pending = types.GenerateVideosOperation(
        name="operations/video-failed", done=False
    )

    def generate_videos(self: Any, **kwargs: Any) -> Any:
        return (
            failed_pending
            if kwargs.get("prompt") == "private-failing-video-prompt"
            else pending
        )

    def get(self: Any, operation: Any, **kwargs: Any) -> Any:
        if operation.name == "operations/video-failed":
            return types.GenerateVideosOperation(
                name=operation.name,
                done=True,
                error={"code": 13, "status": "INTERNAL", "message": "private"},
            )
        return _completed(operation.name)

    monkeypatch.setattr(models.Models, "generate_videos", generate_videos)
    monkeypatch.setattr(operations.Operations, "get", get)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-video-sync") as task:
            submitted = client.models.generate_videos(
                model="veo-3.1-fast-generate-preview",
                prompt="private-success-video-prompt",
                config=types.GenerateVideosConfig(
                    duration_seconds=8,
                    resolution="1080p",
                    generate_audio=True,
                ),
            )
            first = storage.get_provider_job(
                "google", "gemini", "operations/video-success"
            )
            assert first is not None
            assert first.status == "submitted"
            assert first.usage == ()
            assert first.cost_amount is None
            completed = client.operations.get(submitted)
            replay = client.operations.get(submitted)

            failed_submission = client.models.generate_videos(
                model="veo-3.1-fast-generate-preview",
                prompt="private-failing-video-prompt",
                config=types.GenerateVideosConfig(duration_seconds=5),
            )
            failure = client.operations.get(failed_submission)
    finally:
        client.close()

    assert completed.done is True
    assert replay.done is True
    assert failure.error is not None
    job = storage.get_provider_job("google", "gemini", "operations/video-success")
    assert job is not None
    assert (job.status, job.revision) == ("succeeded", 2)
    usage = {line.metric: str(line.quantity) for line in job.usage}
    assert usage == {"output_video_count": "1", "output_video_seconds": "8"}
    assert dict(job.billing_dimensions) == {
        "video_audio": "true",
        "video_duration_seconds": "8",
        "video_resolution": "1080p",
    }
    wire = job.to_attribution_observation()
    assert wire["usage"][0]["dimensions"] == [
        {"key": "video_audio", "value": {"type": "string", "value": "true"}},
        {
            "key": "video_duration_seconds",
            "value": {"type": "string", "value": "8"},
        },
        {
            "key": "video_resolution",
            "value": {"type": "string", "value": "1080p"},
        },
    ]
    assert len(storage.query_provider_job_history(str(job.event_id))) == 2

    failed = storage.get_provider_job("google", "gemini", "operations/video-failed")
    assert failed is not None
    assert (failed.status, failed.revision, failed.error_code) == ("failed", 2, "13")
    assert failed.usage == ()
    durable = [
        revision.to_dict()
        for revision in storage.query_provider_job_history(str(job.event_id))
    ] + [
        revision.to_dict()
        for revision in storage.query_provider_job_history(str(failed.event_id))
    ]
    assert "private-" not in str(durable)
    assert "private-bucket" not in str(durable)
    stored_task = storage.get_task(str(task.task_id))
    assert stored_task is not None
    assert stored_task.status == "success"


@pytest.mark.asyncio
async def test_async_video_submission_and_operation_poll(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    pending = types.GenerateVideosOperation(name="operations/video-async", done=False)

    async def generate_videos(self: Any, **kwargs: Any) -> Any:
        return pending

    async def get(self: Any, operation: Any, **kwargs: Any) -> Any:
        return _completed(operation.name)

    monkeypatch.setattr(models.AsyncModels, "generate_videos", generate_videos)
    monkeypatch.setattr(operations.AsyncOperations, "get", get)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-video-async") as task:
            submitted = await client.aio.models.generate_videos(
                model="veo-3.1-fast-generate-preview",
                prompt="async-private-video-prompt",
                config=types.GenerateVideosConfig(duration_seconds=5),
            )
            completed = await client.aio.operations.get(submitted)
    finally:
        await client.aio.aclose()
        client.close()

    assert completed.done is True
    job = storage.get_provider_job("google", "gemini", "operations/video-async")
    assert job is not None
    assert (job.status, job.revision) == ("succeeded", 2)
    assert {line.metric: str(line.quantity) for line in job.usage} == {
        "output_video_count": "1",
        "output_video_seconds": "5",
    }
    assert "async-private" not in str(job.to_dict())
    assert job.task_id == task.task_id
