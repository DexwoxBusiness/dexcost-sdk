"""OpenAI provider jobs through the installed SDK's real HTTP boundary."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_openai() -> Generator[None, None, None]:
    yield
    uninstrument_openai()


def _response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "x-request-id": "req_job"},
        json=payload,
    )


def _batch(status: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "batch_http",
        "completion_window": "24h",
        "created_at": 1,
        "endpoint": "/v1/responses",
        "input_file_id": "file-private-http",
        "object": "batch",
        "status": status,
        "model": "gpt-4o-mini-2024-07-18",
    }
    if status == "completed":
        value.update(
            request_counts={"completed": 1, "failed": 0, "total": 1},
            usage={
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 5},
                "output_tokens": 8,
                "output_tokens_details": {"reasoning_tokens": 2},
                "total_tokens": 28,
            },
            output_file_id="file-private-output",
        )
    return value


def _tuning(status: str) -> dict[str, Any]:
    return {
        "id": "ftjob_http",
        "created_at": 1,
        "error": None,
        "fine_tuned_model": "ft:private" if status == "succeeded" else None,
        "finished_at": 2 if status == "succeeded" else None,
        "hyperparameters": {
            "batch_size": 1,
            "learning_rate_multiplier": 1,
            "n_epochs": 2,
        },
        "model": "gpt-4o-mini-2024-07-18",
        "object": "fine_tuning.job",
        "organization_id": "org-private",
        "result_files": [],
        "seed": 3,
        "status": status,
        "trained_tokens": 500 if status == "succeeded" else None,
        "training_file": "file-private-training",
        "validation_file": None,
    }


def _video(status: str) -> dict[str, Any]:
    return {
        "id": "video_http",
        "completed_at": 2 if status == "completed" else None,
        "created_at": 1,
        "error": None,
        "expires_at": 99,
        "model": "sora-2-pro",
        "object": "video",
        "progress": 100 if status == "completed" else 0,
        "prompt": "private-http-video-prompt",
        "remixed_from_video_id": None,
        "seconds": "8",
        "size": "1024x1792",
        "status": status,
    }


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/batches" and request.method == "POST":
        return _response(_batch("in_progress"))
    if path == "/v1/batches/batch_http" and request.method == "GET":
        return _response(_batch("completed"))
    if path == "/v1/fine_tuning/jobs" and request.method == "POST":
        return _response(_tuning("running"))
    if path == "/v1/fine_tuning/jobs/ftjob_http" and request.method == "GET":
        return _response(_tuning("succeeded"))
    if path == "/v1/videos" and request.method == "POST":
        return _response(_video("queued"))
    if path == "/v1/videos/video_http" and request.method == "GET":
        return _response(_video("completed"))
    raise AssertionError(f"unexpected OpenAI request {request.method} {path}")


def _assert_jobs(storage: SQLiteStorage) -> None:
    batch = storage.get_provider_job("openai", "batches", "batch_http")
    tuning = storage.get_provider_job("openai", "fine_tuning", "ftjob_http")
    video = storage.get_provider_job("openai", "videos", "video_http")
    assert batch is not None and (batch.status, batch.revision) == ("succeeded", 2)
    assert tuning is not None and (tuning.status, tuning.revision) == (
        "succeeded",
        2,
    )
    assert video is not None and (video.status, video.revision) == ("succeeded", 2)
    durable = str(batch.to_dict()) + str(tuning.to_dict()) + str(video.to_dict())
    assert "private" not in durable


def test_sync_job_resources_use_real_openai_http_contract(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "openai-jobs-http-sync.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    client = OpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(_handler)),
    )
    instrument_openai(tracker)
    try:
        batch = client.batches.create(
            completion_window="24h",
            endpoint="/v1/responses",
            input_file_id="file-private-request",
        )
        client.batches.retrieve(batch.id)
        tuning = client.fine_tuning.jobs.create(
            model="gpt-4o-mini-2024-07-18",
            training_file="file-private-training",
        )
        client.fine_tuning.jobs.retrieve(tuning.id)
        video = client.videos.create(
            model="sora-2-pro",
            prompt="private-request-video",
            seconds="8",
            size="1024x1792",
        )
        client.videos.retrieve(video.id)
        _assert_jobs(storage)
    finally:
        client.close()
        storage.close()


@pytest.mark.asyncio
async def test_async_job_resources_use_real_openai_http_contract(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "openai-jobs-http-async.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )
    instrument_openai(tracker)
    try:
        batch = await client.batches.create(
            completion_window="24h",
            endpoint="/v1/responses",
            input_file_id="file-private-request",
        )
        await client.batches.retrieve(batch.id)
        tuning = await client.fine_tuning.jobs.create(
            model="gpt-4o-mini-2024-07-18",
            training_file="file-private-training",
        )
        await client.fine_tuning.jobs.retrieve(tuning.id)
        video = await client.videos.create(
            model="sora-2-pro",
            prompt="private-request-video",
            seconds="8",
            size="1024x1792",
        )
        await client.videos.retrieve(video.id)
        _assert_jobs(storage)
    finally:
        await client.close()
        storage.close()
