"""Compatibility gates for the installed current Google Gen AI package."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator, Iterator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from dexcost.instruments.gemini import instrument_gemini, uninstrument_gemini
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

genai = pytest.importorskip("google.genai")
from google.genai import models, types  # noqa: E402


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    value = SQLiteStorage(db_path=tmp_path / "google-current.db")
    yield value
    value.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _restore_google(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    yield
    uninstrument_gemini()


def _usage_lines(event: Any) -> dict[str, str]:
    return {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    }


def _current_response() -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        model_version="models/gemini-2.5-flash",
        response_id="google-response-1",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100,
            cached_content_token_count=20,
            prompt_tokens_details=[
                types.ModalityTokenCount(modality="TEXT", token_count=70),
                types.ModalityTokenCount(modality="AUDIO", token_count=30),
            ],
            cache_tokens_details=[
                types.ModalityTokenCount(modality="TEXT", token_count=20),
            ],
            candidates_token_count=12,
            candidates_tokens_details=[
                types.ModalityTokenCount(modality="TEXT", token_count=8),
                types.ModalityTokenCount(modality="IMAGE", token_count=4),
            ],
            thoughts_token_count=5,
            tool_use_prompt_token_count=3,
            tool_use_prompt_tokens_details=[
                types.ModalityTokenCount(modality="TEXT", token_count=3),
            ],
            total_token_count=120,
        ),
    )


def test_real_models_sync_content_multimodal_and_early_close(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    response = _current_response()

    def generate_content(self: Any, **kwargs: Any) -> Any:
        return response

    def generate_content_stream(self: Any, **kwargs: Any) -> Iterator[Any]:
        yield types.GenerateContentResponse()
        yield response

    monkeypatch.setattr(models.Models, "generate_content", generate_content)
    monkeypatch.setattr(
        models.Models,
        "generate_content_stream",
        generate_content_stream,
    )
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-current-sync") as task:
            result = client.models.generate_content(
                model="gemini-2.5-flash",
                contents="secret-not-persisted",
            )
            stream = client.models.generate_content_stream(
                model="gemini-2.5-flash",
                contents="another-secret",
            )
            next(stream)
            stream.close()
    finally:
        client.close()

    assert result is response
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    success = next(
        event
        for event in events
        if event.details["attribution_operation_status"] == "succeeded"
    )
    cancelled = next(
        event
        for event in events
        if event.details["attribution_operation_status"] == "cancelled"
    )
    assert success.event_type == "llm_call"
    assert success.input_tokens == 103
    assert success.output_tokens == 17
    assert success.cached_tokens == 20
    assert success.details["provider_record_id"] == "google-response-1"
    assert success.details["attribution_operation_status"] == "succeeded"
    assert cancelled.details["attribution_operation_status"] == "cancelled"
    lines = _usage_lines(success)
    assert lines["input_tokens"] == "50"
    assert lines["input_audio_tokens"] == "30"
    assert lines["cache_read_input_tokens"] == "20"
    assert lines["output_tokens"] == "8"
    assert lines["output_image_tokens"] == "4"
    assert lines["reasoning_output_tokens"] == "5"
    assert lines["tool_input_tokens"] == "3"
    assert "secret-not-persisted" not in str(success.to_dict())


def test_real_models_preserve_call_and_stream_failures_without_messages(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    class GoogleProviderError(RuntimeError):
        code = 429

    direct_failure = GoogleProviderError("direct-secret-message")
    stream_failure = GoogleProviderError("stream-secret-message")

    def generate_content(self: Any, **kwargs: Any) -> Any:
        raise direct_failure

    def generate_content_stream(self: Any, **kwargs: Any) -> Iterator[Any]:
        yield types.GenerateContentResponse()
        raise stream_failure

    monkeypatch.setattr(models.Models, "generate_content", generate_content)
    monkeypatch.setattr(
        models.Models,
        "generate_content_stream",
        generate_content_stream,
    )
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-current-failure") as task:
            with pytest.raises(GoogleProviderError) as direct_info:
                client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents="private-direct",
                )
            stream = client.models.generate_content_stream(
                model="gemini-2.5-flash",
                contents="private-stream",
            )
            next(stream)
            with pytest.raises(GoogleProviderError) as stream_info:
                next(stream)
    finally:
        client.close()

    assert direct_info.value is direct_failure
    assert stream_info.value is stream_failure
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    assert all(
        event.details["attribution_operation_status"] == "failed"
        for event in events
    )
    serialized = str([event.to_dict() for event in events])
    assert "direct-secret-message" not in serialized
    assert "stream-secret-message" not in serialized
    assert "private-direct" not in serialized
    assert "private-stream" not in serialized


@pytest.mark.asyncio
async def test_real_async_models_content_and_stream(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    response = _current_response()

    async def generate_content(self: Any, **kwargs: Any) -> Any:
        return response

    async def generate_content_stream(
        self: Any, **kwargs: Any
    ) -> AsyncIterator[Any]:
        async def chunks() -> AsyncIterator[Any]:
            yield types.GenerateContentResponse()
            yield response

        return chunks()

    monkeypatch.setattr(models.AsyncModels, "generate_content", generate_content)
    monkeypatch.setattr(
        models.AsyncModels,
        "generate_content_stream",
        generate_content_stream,
    )
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-current-async") as task:
            result = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents="private-input",
            )
            stream = await client.aio.models.generate_content_stream(
                model="gemini-2.5-flash",
                contents="private-stream-input",
            )
            chunks = [chunk async for chunk in stream]
    finally:
        await client.aio.aclose()
        client.close()

    assert result is response
    assert len(chunks) == 2
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    assert all(
        event.details["attribution_operation_status"] == "succeeded"
        for event in events
    )


def test_real_models_embeddings_and_all_image_methods(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    embedding = types.EmbedContentResponse(
        embeddings=[
            types.ContentEmbedding(
                values=[0.1],
                statistics=types.ContentEmbeddingStatistics(token_count=7.0),
            ),
            types.ContentEmbedding(
                values=[0.2],
                statistics=types.ContentEmbeddingStatistics(token_count=5.0),
            ),
        ],
        metadata=types.EmbedContentMetadata(billable_character_count=40),
    )
    images = types.GenerateImagesResponse(
        generated_images=[types.GeneratedImage(), types.GeneratedImage()]
    )
    upscale = types.UpscaleImageResponse(
        generated_images=[types.GeneratedImage()]
    )
    edit = types.EditImageResponse(generated_images=[types.GeneratedImage()])
    recontext = types.RecontextImageResponse(
        generated_images=[types.GeneratedImage()]
    )
    segment = types.SegmentImageResponse(
        generated_masks=[types.GeneratedImageMask(), types.GeneratedImageMask()]
    )
    responses = {
        "embed_content": embedding,
        "generate_images": images,
        "upscale_image": upscale,
        "edit_image": edit,
        "recontext_image": recontext,
        "segment_image": segment,
    }
    for method, response in responses.items():
        monkeypatch.setattr(
            models.Models,
            method,
            lambda self, _response=response, **kwargs: _response,
        )

    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-current-media") as task:
            client.models.embed_content(
                model="gemini-embedding-001",
                contents="not-retained",
            )
            client.models.generate_images(
                model="imagen-4.0-generate-001",
                prompt="not-retained",
            )
            client.models.upscale_image(
                model="imagen-4.0-upscale-preview",
                image=types.Image(),
                upscale_factor="x2",
            )
            client.models.edit_image(
                model="imagen-3.0-capability-001",
                prompt="not-retained",
                reference_images=[],
            )
            client.models.recontext_image(
                model="imagen-product-recontext-preview-06-30",
                source=types.RecontextImageSource(),
            )
            client.models.segment_image(
                model="image-segmentation-001",
                source=types.SegmentImageSource(),
            )
    finally:
        client.close()

    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 6
    by_operation = {
        event.details["attribution_operation_name"]: event for event in events
    }
    embedding_event = by_operation["google.genai.models.embed_content"]
    assert _usage_lines(embedding_event) == {
        "input_tokens": "12",
        "characters": "40",
    }
    assert embedding_event.input_tokens == 12
    assert (
        _usage_lines(by_operation["google.genai.models.generate_images"])[
            "output_image_count"
        ]
        == "2"
    )
    assert (
        _usage_lines(by_operation["google.genai.models.segment_image"])[
            "output_image_count"
        ]
        == "2"
    )
    assert "not-retained" not in str([event.to_dict() for event in events])


def test_actual_google_http_encoder_and_response_model(
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "embed" in request.url.path.lower():
            return httpx.Response(
                200,
                json={
                    "embeddings": [
                        {
                            "values": [0.1],
                            "statistics": {"tokenCount": 9.0},
                        }
                    ],
                    "metadata": {"billableCharacterCount": 20},
                },
            )
        if "predict" in request.url.path.lower():
            return httpx.Response(
                200,
                json={
                    "predictions": [
                        {
                            "bytesBase64Encoded": "aGVsbG8=",
                            "mimeType": "image/png",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "ok"}],
                        },
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "modelVersion": "gemini-2.5-flash",
                "responseId": "http-response-1",
                "usageMetadata": {
                    "promptTokenCount": 11,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 15,
                },
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(
            base_url="https://google.test",
            httpx_client=http_client,
        ),
    )
    instrument_gemini(tracker)
    try:
        with tracker.task(task_type="google-http") as task:
            content = client.models.generate_content(
                model="gemini-2.5-flash",
                contents="wire-private",
            )
            embedding = client.models.embed_content(
                model="gemini-embedding-001",
                contents="wire-private-embedding",
            )
            images = client.models.generate_images(
                model="imagen-4.0-generate-001",
                prompt="wire-private-image",
            )
    finally:
        client.close()

    assert content.response_id == "http-response-1"
    assert embedding.embeddings is not None
    assert images.generated_images is not None
    assert len(images.generated_images) == 1
    assert len(requests) == 3
    assert all(request.method == "POST" for request in requests)
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 3
    by_operation = {
        event.details["attribution_operation_name"]: event for event in events
    }
    assert by_operation["google.genai.models.generate_content"].input_tokens == 11
    assert by_operation["google.genai.models.embed_content"].input_tokens == 9
    assert (
        _usage_lines(by_operation["google.genai.models.generate_images"])[
            "output_image_count"
        ]
        == "1"
    )
    assert "wire-private" not in str([event.to_dict() for event in events])


@pytest.mark.asyncio
async def test_actual_google_async_http_encoder_and_response_model(
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "ok"}],
                        },
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "modelVersion": "gemini-2.5-flash",
                "responseId": "async-http-response-1",
                "usageMetadata": {
                    "promptTokenCount": 13,
                    "candidatesTokenCount": 6,
                    "totalTokenCount": 19,
                },
            },
        )

    async_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(
            base_url="https://google.test",
            httpx_async_client=async_http_client,
        ),
    )
    instrument_gemini(tracker)
    try:
        with tracker.task(task_type="google-async-http") as task:
            content = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents="async-wire-private",
            )
    finally:
        await client.aio.aclose()
        client.close()

    assert content.response_id == "async-http-response-1"
    assert len(requests) == 1
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    assert events[0].input_tokens == 13
    assert events[0].output_tokens == 6
    assert "async-wire-private" not in str(events[0].to_dict())


def test_current_2x_interactions_foreground_and_background_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    try:
        from google.genai._interactions.resources.interactions import (
            InteractionsResource,
        )
        from google.genai._interactions.types.usage import (
            CachedTokensByModality,
            InputTokensByModality,
            OutputTokensByModality,
            ToolUseTokensByModality,
        )
        from google.genai.interactions import (
            Interaction,
            InteractionCompleteEvent,
            Usage,
        )
    except ImportError:
        pytest.skip("Google Gen AI 2.x Interactions API is not installed")

    now = datetime.now(timezone.utc)
    usage = Usage(
        total_input_tokens=100,
        total_cached_tokens=20,
        total_output_tokens=12,
        total_thought_tokens=5,
        total_tool_use_tokens=3,
        input_tokens_by_modality=[
            InputTokensByModality(modality="text", tokens=70),
            InputTokensByModality(modality="audio", tokens=30),
        ],
        cached_tokens_by_modality=[
            CachedTokensByModality(modality="text", tokens=20),
        ],
        output_tokens_by_modality=[
            OutputTokensByModality(modality="text", tokens=8),
            OutputTokensByModality(modality="image", tokens=4),
        ],
        tool_use_tokens_by_modality=[
            ToolUseTokensByModality(modality="text", tokens=3),
        ],
    )
    completed = Interaction(
        id="interaction-1",
        created=now,
        updated=now,
        status="completed",
        model="gemini-2.5-flash",
        usage=usage,
    )
    background = Interaction(
        id="interaction-background-1",
        created=now,
        updated=now,
        status="in_progress",
        agent="deep-research-pro-preview-12-2025",
    )
    background_completed = Interaction(
        id="interaction-background-1",
        created=now,
        updated=now,
        status="completed",
        agent="deep-research-pro-preview-12-2025",
        usage=usage,
    )
    cancellable = Interaction(
        id="interaction-cancel-1",
        created=now,
        updated=now,
        status="in_progress",
        model="gemini-2.5-flash",
    )
    cancelled = Interaction(
        id="interaction-cancel-1",
        created=now,
        updated=now,
        status="cancelled",
        model="gemini-2.5-flash",
    )

    def create(self: Any, **kwargs: Any) -> Any:
        if kwargs.get("background") is True:
            return (
                cancellable
                if kwargs.get("input") == "private-background-cancel"
                else background
            )
        if kwargs.get("stream") is True:
            return iter(
                [
                    InteractionCompleteEvent(
                        event_type="interaction.complete",
                        interaction=completed,
                    )
                ]
            )
        return completed

    def get(self: Any, id: str, **kwargs: Any) -> Any:
        assert id == "interaction-background-1"
        return background_completed

    def cancel(self: Any, id: str, **kwargs: Any) -> Any:
        assert id == "interaction-cancel-1"
        return cancelled

    monkeypatch.setattr(InteractionsResource, "create", create)
    monkeypatch.setattr(InteractionsResource, "get", get)
    monkeypatch.setattr(InteractionsResource, "cancel", cancel)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-interactions") as task:
            direct = client.interactions.create(
                model="gemini-2.5-flash",
                input="private-interaction-input",
            )
            stream = client.interactions.create(
                model="gemini-2.5-flash",
                input="private-interaction-stream",
                stream=True,
            )
            assert len(list(stream)) == 1
            pending = client.interactions.create(
                agent="deep-research-pro-preview-12-2025",
                input="private-background-input",
                background=True,
            )
            resolved = client.interactions.get(id=pending.id)
            replayed = client.interactions.get(id=pending.id)
            to_cancel = client.interactions.create(
                model="gemini-2.5-flash",
                input="private-background-cancel",
                background=True,
            )
            cancelled_result = client.interactions.cancel(id=to_cancel.id)
    finally:
        client.close()

    assert direct is completed
    assert pending is background
    assert resolved is background_completed
    assert replayed is background_completed
    assert cancelled_result is cancelled
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    assert all(
        event.details["attribution_operation_status"] == "succeeded"
        for event in events
    )
    assert all(event.input_tokens == 103 for event in events)
    assert all(event.output_tokens == 17 for event in events)
    assert all(event.cached_tokens == 20 for event in events)
    lines = _usage_lines(events[0])
    assert lines["input_audio_tokens"] == "30"
    assert lines["output_image_tokens"] == "4"
    assert lines["reasoning_output_tokens"] == "5"
    assert "private-interaction" not in str([event.to_dict() for event in events])
    assert "private-background-input" not in str(
        [event.to_dict() for event in events]
    )
    job = storage.get_provider_job(
        "google", "gemini", "interaction-background-1"
    )
    assert job is not None
    assert job.status == "succeeded"
    assert job.revision == 2
    assert job.task_id == task.task_id
    history = storage.query_provider_job_history(str(job.event_id))
    assert [revision.status for revision in history] == ["submitted", "succeeded"]
    assert history[-1].task_input_tokens == 103
    assert history[-1].task_output_tokens == 17
    assert "private-background-input" not in str(
        [revision.to_dict() for revision in history]
    )
    cancelled_job = storage.get_provider_job(
        "google", "gemini", "interaction-cancel-1"
    )
    assert cancelled_job is not None
    assert (cancelled_job.status, cancelled_job.revision) == ("cancelled", 2)


@pytest.mark.asyncio
async def test_current_2x_async_interactions_and_early_close(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    try:
        from google.genai._interactions.resources.interactions import (
            AsyncInteractionsResource,
        )
        from google.genai.interactions import Interaction, Usage
    except ImportError:
        pytest.skip("Google Gen AI 2.x Interactions API is not installed")

    now = datetime.now(timezone.utc)
    completed = Interaction(
        id="async-interaction-1",
        created=now,
        updated=now,
        status="completed",
        model="gemini-2.5-flash",
        usage=Usage(total_input_tokens=10, total_output_tokens=4),
    )
    background = Interaction(
        id="async-background-1",
        created=now,
        updated=now,
        status="in_progress",
        model="gemini-2.5-flash",
    )
    background_completed = Interaction(
        id="async-background-1",
        created=now,
        updated=now,
        status="completed",
        model="gemini-2.5-flash",
        usage=Usage(total_input_tokens=10, total_output_tokens=4),
    )
    cancellable = Interaction(
        id="async-cancel-1",
        created=now,
        updated=now,
        status="in_progress",
        model="gemini-2.5-flash",
    )
    cancelled = Interaction(
        id="async-cancel-1",
        created=now,
        updated=now,
        status="cancelled",
        model="gemini-2.5-flash",
    )

    async def create(self: Any, **kwargs: Any) -> Any:
        if kwargs.get("background") is True:
            return (
                cancellable
                if kwargs.get("input") == "async-private-cancel"
                else background
            )
        if kwargs.get("stream") is True:
            async def events() -> AsyncIterator[Any]:
                yield object()
                yield object()

            return events()
        return completed

    async def get(self: Any, id: str, **kwargs: Any) -> Any:
        assert id == "async-background-1"
        if kwargs.get("stream") is True:
            async def events() -> AsyncIterator[Any]:
                yield object()
                yield object()

            return events()
        return background_completed

    async def cancel(self: Any, id: str, **kwargs: Any) -> Any:
        assert id == "async-cancel-1"
        return cancelled

    monkeypatch.setattr(AsyncInteractionsResource, "create", create)
    monkeypatch.setattr(AsyncInteractionsResource, "get", get)
    monkeypatch.setattr(AsyncInteractionsResource, "cancel", cancel)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-async-interactions") as task:
            direct = await client.aio.interactions.create(
                model="gemini-2.5-flash",
                input="async-private-interaction",
            )
            stream = await client.aio.interactions.create(
                model="gemini-2.5-flash",
                input="async-private-stream",
                stream=True,
            )
            await stream.__anext__()
            await stream.aclose()
            pending = await client.aio.interactions.create(
                model="gemini-2.5-flash",
                input="async-private-background",
                background=True,
            )
            poll_stream = await client.aio.interactions.get(
                id=pending.id,
                stream=True,
            )
            await poll_stream.__anext__()
            await poll_stream.aclose()
            assert storage.get_provider_job(
                "google", "gemini", "async-background-1"
            ).revision == 1
            resolved = await client.aio.interactions.get(id=pending.id)
            to_cancel = await client.aio.interactions.create(
                model="gemini-2.5-flash",
                input="async-private-cancel",
                background=True,
            )
            cancelled_result = await client.aio.interactions.cancel(
                id=to_cancel.id
            )
    finally:
        await client.aio.aclose()
        client.close()

    assert direct is completed
    assert resolved is background_completed
    assert cancelled_result is cancelled
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    statuses = {
        event.details["attribution_operation_status"] for event in events
    }
    assert statuses == {"succeeded", "cancelled"}
    succeeded = next(
        event
        for event in events
        if event.details["attribution_operation_status"] == "succeeded"
    )
    assert succeeded.input_tokens == 10
    assert succeeded.output_tokens == 4
    assert "async-private" not in str([event.to_dict() for event in events])
    job = storage.get_provider_job("google", "gemini", "async-background-1")
    assert job is not None
    assert (job.status, job.revision) == ("succeeded", 2)
    assert "async-private-background" not in str(job.to_dict())
    cancelled_job = storage.get_provider_job(
        "google", "gemini", "async-cancel-1"
    )
    assert cancelled_job is not None
    assert (cancelled_job.status, cancelled_job.revision) == ("cancelled", 2)
