"""Current official OpenRouter 1.x real-package compatibility gates."""

from __future__ import annotations

import json
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import OpenAI as OpenAICompatibleClient
from openrouter import OpenRouter

from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.instruments.openrouter import instrument_openrouter, uninstrument_openrouter
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_openrouter() -> Generator[None, None, None]:
    uninstrument_openai()
    uninstrument_openrouter()
    yield
    uninstrument_openrouter()
    uninstrument_openai()


def _chat_body(*, cost: float = 0.012) -> dict[str, Any]:
    return {
        "id": "gen-chat-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "native_finish_reason": "stop",
                "message": {"role": "assistant", "content": "private-chat-output"},
            }
        ],
        "created": 1,
        "model": "openai/gpt-5",
        "object": "chat.completion",
        "system_fingerprint": None,
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 1},
            "completion_tokens_details": {"reasoning_tokens": 2},
            "cost": cost,
            "cost_details": {"upstream_inference_cost": 0.01},
            "is_byok": False,
            "server_tool_use_details": {
                "web_search_requests": 1,
                "tool_calls_requested": 1,
                "tool_calls_executed": 1,
            },
        },
    }


def _responses_body() -> dict[str, Any]:
    return {
        "completed_at": 2,
        "created_at": 1,
        "error": None,
        "frequency_penalty": 0,
        "id": "gen-responses-1",
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "model": "anthropic/claude-sonnet-4",
        "object": "response",
        "output": [],
        "parallel_tool_calls": False,
        "presence_penalty": 0,
        "status": "completed",
        "temperature": 1,
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1,
        "usage": {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
            "output_tokens": 6,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 18,
            "cost": 0.02,
            "cost_details": {
                "upstream_inference_input_cost": 0.005,
                "upstream_inference_output_cost": 0.01,
                "upstream_inference_cost": 0.015,
            },
        },
    }


def _generation_body() -> dict[str, Any]:
    data = {
        "api_type": "completions",
        "app_id": None,
        "cache_discount": 0.1,
        "cancelled": False,
        "created_at": "2026-08-21T00:00:00Z",
        "data_region": "us",
        "external_user": None,
        "finish_reason": "stop",
        "generation_time": 100,
        "http_referer": None,
        "id": "gen-chat-1",
        "is_byok": False,
        "latency": 120,
        "model": "openai/gpt-5",
        "moderation_latency": None,
        "native_finish_reason": "stop",
        "native_tokens_cached": 2,
        "native_tokens_completion": 5,
        "native_tokens_completion_images": 0,
        "native_tokens_prompt": 10,
        "native_tokens_reasoning": 2,
        "num_fetches": 1,
        "num_input_audio_prompt": 0,
        "num_media_completion": 0,
        "num_media_prompt": 0,
        "num_search_results": 3,
        "origin": "https://private.example",
        "preset_id": None,
        "provider_name": "Fireworks",
        "provider_responses": None,
        "router": "openrouter/auto",
        "service_tier": "default",
        "streamed": False,
        "tokens_completion": 5,
        "tokens_prompt": 10,
        "total_cost": 0.013,
        "upstream_id": "private-upstream-id",
        "upstream_inference_cost": 0.011,
        "usage": 0.013,
        "user_agent": "private-agent",
        "web_search_engine": "exa",
        "workspace_id": None,
    }
    return {"data": data}


def _sync_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            assert b"private-chat-prompt" in request.content
            return httpx.Response(200, json=_chat_body(), request=request)
        if path.endswith("/responses"):
            return httpx.Response(200, json=_responses_body(), request=request)
        if path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.1, 0.2], "object": "embedding", "index": 0}],
                    "model": "openai/text-embedding-3-small",
                    "object": "list",
                    "id": "gen-embed-1",
                    "usage": {
                        "prompt_tokens": 4,
                        "total_tokens": 4,
                        "cost": 0.0001,
                        "is_byok": False,
                    },
                },
                request=request,
            )
        if path.endswith("/images"):
            return httpx.Response(
                200,
                json={
                    "created": 1,
                    "data": [{"b64_json": "private-image-bytes"}],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 8,
                        "total_tokens": 10,
                        "cost": 0.04,
                    },
                },
                request=request,
            )
        if path.endswith("/audio/transcriptions"):
            return httpx.Response(
                200,
                json={
                    "text": "private-transcript",
                    "usage": {
                        "cost": 0.005,
                        "input_tokens": 8,
                        "output_tokens": 2,
                        "seconds": 9.2,
                        "total_tokens": 10,
                    },
                },
                request=request,
            )
        if path.endswith("/audio/speech"):
            return httpx.Response(
                200,
                content=b"private-audio-bytes",
                headers={
                    "content-type": "audio/mpeg",
                    "x-generation-id": "gen-tts-1",
                },
                request=request,
            )
        if path.endswith("/rerank"):
            return httpx.Response(
                200,
                json={
                    "id": "gen-rerank-1",
                    "model": "cohere/rerank-v3.5",
                    "provider": "Cohere",
                    "results": [
                        {
                            "document": {"text": "private-document"},
                            "index": 0,
                            "relevance_score": 0.9,
                        }
                    ],
                    "usage": {"cost": 0.002, "search_units": 1, "total_tokens": 20},
                },
                request=request,
            )
        if path.endswith("/videos/job-1"):
            return httpx.Response(
                200,
                json={
                    "id": "job-1",
                    "polling_url": "/api/v1/videos/job-1",
                    "status": "completed",
                    "generation_id": "gen-video-1",
                    "unsigned_urls": ["https://private.example/video"],
                    "usage": {"cost": 0.5, "is_byok": False},
                },
                request=request,
            )
        if path.endswith("/videos"):
            return httpx.Response(
                202,
                json={
                    "id": "job-1",
                    "polling_url": "/api/v1/videos/job-1",
                    "status": "pending",
                    "generation_id": "gen-video-1",
                },
                request=request,
            )
        if path.endswith("/generation"):
            return httpx.Response(200, json=_generation_body(), request=request)
        return httpx.Response(404, json={"error": "private-not-found"}, request=request)

    return httpx.MockTransport(handler)


def _persisted_text(storage: SQLiteStorage) -> str:
    events = [event.to_dict() for event in storage.query_events()]
    jobs = [job.to_dict() for job in storage.query_provider_jobs_for_sync()]
    return json.dumps({"events": events, "jobs": jobs}, sort_keys=True)


def test_sync_official_sdk_covers_every_current_billable_resource(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "openrouter-sync.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    http_client = httpx.Client(transport=_sync_transport())
    client = OpenRouter(api_key="test", client=http_client)
    instrument_openrouter(tracker)
    try:
        with tracker.task(task_type="openrouter-sync") as task:
            client.chat.send(
                model="openai/gpt-5",
                messages=[{"role": "user", "content": "private-chat-prompt"}],
            )
            client.responses.send(
                model="anthropic/claude-sonnet-4",
                input="private-responses-input",
            )
            client.embeddings.generate(
                model="openai/text-embedding-3-small",
                input="private-embedding-input",
            )
            client.images.generate(model="google/gemini-image", prompt="private-image-prompt")
            client.stt.create_transcription(
                model="openai/whisper-large-v3",
                input_audio={"data": "private-audio-input", "format": "wav"},
            )
            client.tts.create_speech(
                model="openai/gpt-4o-mini-tts",
                input="private-speech-input",
                voice="nova",
            )
            client.rerank.rerank(
                model="cohere/rerank-v3.5",
                query="private-query",
                documents=[{"text": "private-document"}],
            )
            client.video_generation.generate(
                model="google/veo-3.1",
                prompt="private-video-prompt",
                duration=8,
                resolution="720p",
            )
            client.video_generation.get_generation(job_id="job-1")
            client.generations.get_generation(id="gen-chat-1")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 7
        by_operation = {
            event.details["attribution_operation_name"]: event for event in events
        }
        chat = by_operation["openrouter.chat.send"]
        assert chat.provider == "openrouter"
        assert chat.model == "openrouter/openai/gpt-5"
        assert chat.cost_usd == Decimal("0.013")
        assert chat.pricing_source == "provider_response"
        assert chat.input_tokens == 10
        assert chat.output_tokens == 5
        assert chat.cached_tokens == 2
        assert chat.details["provider_upstream_cost_usd"] == "0.011"
        reconciled_usage = {
            line["metric"]: line["quantity"]
            for line in chat.details["attribution_usage_lines"]
        }
        assert reconciled_usage["input_tokens"] == "8"
        assert reconciled_usage["cache_read_input_tokens"] == "2"
        assert reconciled_usage["output_tokens"] == "3"
        assert reconciled_usage["reasoning_output_tokens"] == "2"
        assert {
            dimension["key"]: dimension["value"]["value"]
            for dimension in chat.details["attribution_dimensions"]
        }["upstream_provider"] == "Fireworks"

        responses = by_operation["openrouter.responses.send"]
        assert responses.cost_usd == Decimal("0.02")
        assert responses.cached_tokens == 3
        assert by_operation["openrouter.image_generation.generate"].cost_usd == Decimal(
            "0.04"
        )
        assert by_operation["openrouter.speech_to_text.create_transcription"].cost_usd == Decimal(
            "0.005"
        )
        assert by_operation["openrouter.text_to_speech.create_speech"].details[
            "provider_record_id"
        ] == "gen-tts-1"

        video = storage.get_provider_job("openrouter", "video_generation", "job-1")
        assert video is not None
        assert video.status == "succeeded"
        assert video.cost_amount == Decimal("0.5")
        assert video.cost_source == "provider_reported"
        assert dict(video.billing_dimensions) == {"duration": "8", "resolution": "720p"}

        persisted = _persisted_text(storage)
        for secret in (
            "private-chat-prompt",
            "private-chat-output",
            "private-responses-input",
            "private-embedding-input",
            "private-image-prompt",
            "private-image-bytes",
            "private-audio-input",
            "private-transcript",
            "private-speech-input",
            "private-audio-bytes",
            "private-query",
            "private-document",
            "private-video-prompt",
            "private.example",
            "private-upstream-id",
        ):
            assert secret not in persisted
    finally:
        uninstrument_openrouter()
        http_client.close()
        storage.close()


def _stream_transport() -> httpx.MockTransport:
    first = {
        "choices": [{"delta": {"content": "private-fragment"}, "finish_reason": None, "index": 0}],
        "created": 1,
        "id": "gen-stream-1",
        "model": "openai/gpt-5",
        "object": "chat.completion.chunk",
    }
    final = {
        "choices": [],
        "created": 2,
        "id": "gen-stream-1",
        "model": "openai/gpt-5",
        "object": "chat.completion.chunk",
        "usage": _chat_body()["usage"],
    }
    content = f"data: {json.dumps(first)}\n\ndata: {json.dumps(final)}\n\ndata: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content.encode(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    return httpx.MockTransport(handler)


def test_real_sync_stream_terminal_usage_and_early_close(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "openrouter-stream.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    http_client = httpx.Client(transport=_stream_transport())
    client = OpenRouter(api_key="test", client=http_client)
    instrument_openrouter(tracker)
    try:
        with tracker.task(task_type="openrouter-stream") as task:
            completed = client.chat.send(
                model="openai/gpt-5",
                messages=[{"role": "user", "content": "private-stream-input"}],
                stream=True,
            )
            assert len(list(completed)) == 2
            cancelled = client.chat.send(
                model="openai/gpt-5",
                messages=[{"role": "user", "content": "private-close-input"}],
                stream=True,
            )
            next(cancelled)
            cancelled.close()

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 2
        statuses = sorted(
            event.details["attribution_operation_status"] for event in events
        )
        assert statuses == ["cancelled", "succeeded"]
        succeeded = next(
            event
            for event in events
            if event.details["attribution_operation_status"] == "succeeded"
        )
        assert succeeded.cost_usd == Decimal("0.012")
        persisted = _persisted_text(storage)
        assert "private-fragment" not in persisted
        assert "private-stream-input" not in persisted
        assert "private-close-input" not in persisted
    finally:
        uninstrument_openrouter()
        http_client.close()
        storage.close()


@pytest.mark.asyncio
async def test_real_async_sdk_and_native_error_identity_are_preserved(
    tmp_path: Path,
) -> None:
    class NativeOpenRouterError(RuntimeError):
        pass

    async def failing_handler(request: httpx.Request) -> httpx.Response:
        raise NativeOpenRouterError("private-native-error")

    storage = SQLiteStorage(tmp_path / "openrouter-async.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    sync_client = httpx.Client(transport=_sync_transport())
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
    client = OpenRouter(api_key="test", client=sync_client, async_client=async_client)
    instrument_openrouter(tracker)
    try:
        with (
            tracker.task(task_type="openrouter-async") as task,
            pytest.raises(NativeOpenRouterError, match="private-native-error") as caught,
        ):
            await client.chat.send_async(
                model="openai/gpt-5",
                messages=[{"role": "user", "content": "private-async-input"}],
            )
        event = storage.query_events(task_id=str(task.task_id))[0]
        assert type(caught.value) is NativeOpenRouterError
        assert event.provider == "openrouter"
        assert event.details["attribution_operation_status"] == "failed"
        persisted = _persisted_text(storage)
        assert "private-native-error" not in persisted
        assert "private-async-input" not in persisted
    finally:
        uninstrument_openrouter()
        await async_client.aclose()
        sync_client.close()
        storage.close()


def test_openai_compatible_client_is_attributed_to_openrouter_once(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json=_chat_body(), request=request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.1], "index": 0, "object": "embedding"}],
                    "model": "openai/text-embedding-3-small",
                    "object": "list",
                    "usage": {
                        "prompt_tokens": 4,
                        "total_tokens": 4,
                        "cost": 0.0001,
                        "cost_details": {"upstream_inference_cost": 0.00008},
                        "is_byok": False,
                    },
                },
                request=request,
            )
        if request.url.path.endswith("/images/generations"):
            partial = {
                "b64_json": "private-compatible-partial-image",
                "background": "opaque",
                "created_at": 1,
                "output_format": "png",
                "partial_image_index": 0,
                "quality": "low",
                "size": "1024x1024",
                "type": "image_generation.partial_image",
            }
            completed = {
                "b64_json": "private-compatible-completed-image",
                "background": "opaque",
                "created_at": 2,
                "output_format": "png",
                "quality": "low",
                "size": "1024x1024",
                "type": "image_generation.completed",
                "usage": {
                    "input_tokens": 3,
                    "input_tokens_details": {"image_tokens": 0, "text_tokens": 3},
                    "output_tokens": 9,
                    "total_tokens": 12,
                    "cost": 0.045,
                    "cost_details": {"upstream_inference_cost": 0.04},
                    "is_byok": False,
                },
            }
            content = "".join(
                f"data: {json.dumps(payload)}\n\n" for payload in (partial, completed)
            )
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(404, request=request)

    storage = SQLiteStorage(tmp_path / "openrouter-openai-compatible.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAICompatibleClient(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        http_client=http_client,
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="openrouter-openai-compatible") as task:
            client.chat.completions.create(
                model="openai/gpt-5",
                messages=[{"role": "user", "content": "private-compatible-prompt"}],
            )
            client.embeddings.create(
                model="openai/text-embedding-3-small",
                input="private-compatible-embedding",
            )
            image_stream = client.images.generate(
                model="openai/gpt-image-1",
                prompt="private-compatible-image-prompt",
                quality="low",
                size="1024x1024",
                stream=True,
            )
            assert len(list(image_stream)) == 2
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 3
        assert {event.provider for event in events} == {"openrouter"}
        assert {event.model for event in events} == {
            "openrouter/openai/gpt-5",
            "openrouter/openai/text-embedding-3-small",
            "openrouter/openai/gpt-image-1",
        }
        assert {event.cost_usd for event in events} == {
            Decimal("0.012"),
            Decimal("0.0001"),
            Decimal("0.045"),
        }
        assert {event.pricing_source for event in events} == {"provider_response"}
        persisted = _persisted_text(storage)
        assert "private-compatible-prompt" not in persisted
        assert "private-compatible-embedding" not in persisted
        assert "private-compatible-image-prompt" not in persisted
        assert "private-compatible-partial-image" not in persisted
        assert "private-compatible-completed-image" not in persisted
    finally:
        uninstrument_openai()
        client.close()
        storage.close()
