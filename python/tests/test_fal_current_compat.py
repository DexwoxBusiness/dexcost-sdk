"""Current fal-client wire, stream, and durable queue compatibility gates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import fal_client
import httpx
import pytest

from dexcost.capabilities import capability_context
from dexcost.idempotency import idempotency_key
from dexcost.instruments.fal import instrument_fal, uninstrument_fal
from dexcost.models.capability import CapabilityIdentity
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_fal() -> Generator[None, None, None]:
    uninstrument_fal()
    yield
    uninstrument_fal()


def _image_result() -> dict[str, object]:
    return {
        "images": [
            {
                "url": "https://private.example/image.png",
                "width": 1024,
                "height": 1024,
            }
        ],
        "prompt": "private-output-prompt",
    }


def _queue_payload(application: str, request_id: str) -> dict[str, object]:
    root = f"https://queue.fal.run/{application}/requests/{request_id}"
    return {
        "request_id": request_id,
        "response_url": root,
        "status_url": f"{root}/status",
        "cancel_url": f"{root}/cancel",
    }


def _transport() -> httpx.MockTransport:
    status_calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "fal.run" and path.endswith("/flux/schnell"):
            return httpx.Response(200, json=_image_result(), request=request)
        if request.url.host == "fal.run" and path.endswith("/video-model"):
            return httpx.Response(
                200,
                json={
                    "video": {
                        "url": "https://private.example/video.mp4",
                        "duration": 6.5,
                    }
                },
                request=request,
            )
        if request.url.host == "fal.run" and path.endswith("/audio-model/stream"):
            body = (
                'data: {"progress":0.5,"private":"private-fragment"}\n\n'
                'data: {"audio":{"url":"https://private.example/audio.mp3",'
                '"duration":3.25}}\n\n'
            )
            return httpx.Response(
                200,
                content=body.encode(),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        if request.method == "POST" and request.url.host == "queue.fal.run":
            application = path.strip("/")
            if application == "fal-ai/video-model":
                request_id = "fal-video-1"
            else:
                request_id = "fal-job-cancel" if b"cancel-me" in request.content else "fal-job-1"
            return httpx.Response(
                200,
                json=_queue_payload(application, request_id),
                request=request,
            )
        if path.endswith("/status"):
            request_id = path.split("/")[-2]
            status_calls[request_id] = status_calls.get(request_id, 0) + 1
            status = "IN_PROGRESS" if status_calls[request_id] == 1 else "COMPLETED"
            payload: dict[str, object] = {"status": status, "logs": []}
            if status == "COMPLETED":
                payload["metrics"] = {"inference_time": 2.7}
            return httpx.Response(200, json=payload, request=request)
        if request.method == "GET" and "/requests/" in path:
            if "/video-model/" in path:
                return httpx.Response(
                    200,
                    json={
                        "video": {
                            "url": "https://private.example/video.mp4",
                            "duration": 6.5,
                        }
                    },
                    request=request,
                )
            return httpx.Response(200, json=_image_result(), request=request)
        if request.method == "PUT" and path.endswith("/cancel"):
            return httpx.Response(202, json={"status": "CANCELLATION_REQUESTED"}, request=request)
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def _client_with_transport(transport: httpx.MockTransport) -> fal_client.SyncClient:
    client = fal_client.SyncClient(key="test")
    client.__dict__["_client"] = httpx.Client(transport=transport)
    return client


def test_current_client_sync_stream_and_queue_are_attributed_once(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "fal-current.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = _client_with_transport(_transport())
    instrument_fal(tracker)
    try:
        with tracker.task(task_type="fal-current") as task:
            client.run(
                "fal-ai/flux/schnell",
                {"prompt": "private-image-prompt", "num_images": 1},
            )
            client.subscribe(
                "fal-ai/video-model",
                {"prompt": "private-video-prompt", "duration": 6.5},
            )
            assert (
                len(
                    list(
                        client.stream(
                            "fal-ai/audio-model",
                            {"text": "private-audio-input"},
                        )
                    )
                )
                == 2
            )

            handle = client.submit("fal-ai/flux/schnell", {"prompt": "private-queue-prompt"})
            assert type(handle.status()).__name__ == "InProgress"
            handle.get()

            cancel_handle = client.submit("fal-ai/flux/schnell", {"prompt": "cancel-me"})
            cancel_handle.cancel()

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 3
        by_operation = {event.details["attribution_operation_name"]: event for event in events}
        image = by_operation["fal_ai.run"]
        assert image.provider == "fal_ai"
        assert image.model == "fal_ai/fal-ai/flux/schnell"
        assert image.cost_usd == Decimal("0.003")
        assert image.cost_confidence == "computed"
        image_lines = {
            line["metric"]: line["quantity"] for line in image.details["attribution_usage_lines"]
        }
        assert image_lines == {"output_image_count": "1"}

        video = by_operation["fal_ai.subscribe"]
        video_lines = {
            line["metric"]: line["quantity"] for line in video.details["attribution_usage_lines"]
        }
        assert video_lines["output_video_seconds"] == "6.5"
        assert "inference_time" not in json.dumps(video.to_dict())

        audio = by_operation["fal_ai.stream"]
        audio_lines = {
            line["metric"]: line["quantity"] for line in audio.details["attribution_usage_lines"]
        }
        assert audio_lines["output_audio_seconds"] == "3.25"

        job = storage.get_provider_job("fal_ai", "queue", "fal-job-1")
        assert job is not None
        assert job.status == "succeeded"
        assert job.cost_amount == Decimal("0.003")
        cancelled = storage.get_provider_job("fal_ai", "queue", "fal-job-cancel")
        assert cancelled is not None
        assert cancelled.status == "submitted"

        persisted = json.dumps(
            {
                "events": [event.to_dict() for event in events],
                "jobs": [item.to_dict() for item in storage.query_provider_jobs_for_sync()],
            }
        )
        for secret in (
            "private-image-prompt",
            "private-output-prompt",
            "private.example",
            "private-video-prompt",
            "private-fragment",
            "private-audio-input",
            "private-queue-prompt",
            "cancel-me",
        ):
            assert secret not in persisted
    finally:
        uninstrument_fal()
        client._client.close()
        storage.close()


def test_module_level_bound_helpers_are_covered_without_double_counting(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "fal-module.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    original_client = fal_client.sync_client.__dict__.get("_client")
    http_client = httpx.Client(transport=_transport())
    fal_client.sync_client.__dict__["_client"] = http_client
    instrument_fal(tracker)
    try:
        fal_client.run("fal-ai/flux/schnell", {"prompt": "private-module-prompt"})
        events = storage.query_events()
        assert len(events) == 1
        assert events[0].model == "fal_ai/fal-ai/flux/schnell"
        assert "private-module-prompt" not in json.dumps(events[0].to_dict())
    finally:
        uninstrument_fal()
        http_client.close()
        if original_client is None:
            fal_client.sync_client.__dict__.pop("_client", None)
        else:
            fal_client.sync_client.__dict__["_client"] = original_client
        storage.close()


def test_current_async_client_stream_and_queue_lifecycle(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "fal-async.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = fal_client.AsyncClient(key="test")
    http_client = httpx.AsyncClient(transport=_transport())
    client.__dict__["_client"] = http_client
    instrument_fal(tracker)

    async def run() -> None:
        await client.run("fal-ai/flux/schnell", {"prompt": "private-async-image"})
        stream = client.stream("fal-ai/audio-model", {"text": "private-async-audio"})
        chunks = [chunk async for chunk in stream]
        assert len(chunks) == 2
        handle = await client.submit("fal-ai/flux/schnell", {"prompt": "private-async-queue"})
        await handle.get()
        await http_client.aclose()

    try:
        asyncio.run(run())
        events = storage.query_events()
        assert len(events) == 2
        assert {event.details["attribution_operation_name"] for event in events} == {
            "fal_ai.run",
            "fal_ai.stream",
        }
        job = storage.get_provider_job("fal_ai", "queue", "fal-job-1")
        assert job is not None
        assert job.status == "succeeded"
        persisted = json.dumps(
            {
                "events": [event.to_dict() for event in events],
                "jobs": [item.to_dict() for item in storage.query_provider_jobs_for_sync()],
            }
        )
        assert "private-async-image" not in persisted
        assert "private-async-audio" not in persisted
        assert "private-async-queue" not in persisted
    finally:
        uninstrument_fal()
        storage.close()


def test_capability_idempotency_job_and_native_failure_contract(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"detail": "private-provider-failure"},
            request=request,
        )

    storage = SQLiteStorage(tmp_path / "fal-contract.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = _client_with_transport(_transport())
    failing_client = _client_with_transport(httpx.MockTransport(handler))
    capability = CapabilityIdentity(
        name="media.generate",
        kind="workflow",
        namespace="dexcost.agent",
        source="project",
        source_id="media.generate/v1",
        invocation="automatic",
    )
    instrument_fal(tracker)
    try:
        with (
            tracker.task(task_type="fal-contract") as task,
            capability_context(capability),
            idempotency_key("private-fal-idempotency"),
        ):
            client.run("fal-ai/flux/schnell", {"prompt": "private-capability-prompt"})
            client.submit("fal-ai/flux/schnell", {"prompt": "private-queue-capability"})

        event = storage.query_events(task_id=str(task.task_id))[0]
        assert event.details["attribution_capability"] == capability.to_dict()
        assert len(event.details["_dexcost_idempotency_sha256"]) == 64
        job = storage.get_provider_job("fal_ai", "queue", "fal-job-1")
        assert job is not None
        assert job.capability == capability

        with pytest.raises(Exception) as caught, tracker.task(
            task_type="fal-native-failure"
        ) as failed_task:
            failing_client.run(
                "fal-ai/flux/schnell", {"prompt": "private-failure-prompt"}
            )
        assert type(caught.value).__module__.startswith("fal_client")
        failed = storage.query_events(task_id=str(failed_task.task_id))[0]
        assert failed.details["attribution_operation_status"] == "failed"
        assert failed.details["error_type"] == type(caught.value).__name__.lower()
        persisted = json.dumps(
            {
                "events": [item.to_dict() for item in storage.query_events()],
                "jobs": [item.to_dict() for item in storage.query_provider_jobs_for_sync()],
            }
        )
        for secret in (
            "private-fal-idempotency",
            "private-capability-prompt",
            "private-queue-capability",
            "private-failure-prompt",
            "private-provider-failure",
        ):
            assert secret not in persisted
    finally:
        uninstrument_fal()
        client._client.close()
        failing_client._client.close()
        storage.close()
