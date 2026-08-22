"""Compatibility with the current real OpenAI client and HTTP serializers.

No provider request leaves the process: ``httpx.MockTransport`` exercises the
installed SDK's actual resources, request encoders, response models, and stream
parser against deterministic protocol-correct responses.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _real_openai_import() -> Generator[None, None, None]:
    for name, module in list(sys.modules.items()):
        if (name == "openai" or name.startswith("openai.")) and module is None:
            sys.modules.pop(name, None)
    pytest.importorskip("openai")
    pytest.importorskip("httpx")
    yield
    from dexcost.instruments.openai import uninstrument_openai

    uninstrument_openai()


def _json_response(httpx: Any, payload: dict[str, Any]) -> Any:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "x-request-id": "req_real"},
        json=payload,
    )


def _handler(httpx: Any, request: Any) -> Any:
    path = request.url.path
    if path == "/v1/embeddings":
        return _json_response(
            httpx,
            {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 100, "total_tokens": 100},
            },
        )
    if path == "/v1/images/generations":
        return _json_response(
            httpx,
            {
                "created": 1,
                "data": [{"b64_json": "aW1hZ2U="}],
                "quality": "low",
                "size": "1024x1024",
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"text_tokens": 10, "image_tokens": 0},
                    "output_tokens": 100,
                    "output_tokens_details": {"text_tokens": 0, "image_tokens": 100},
                    "total_tokens": 110,
                },
            },
        )
    if path == "/v1/audio/transcriptions":
        return _json_response(
            httpx,
            {"text": "not retained", "usage": {"type": "duration", "seconds": 60}},
        )
    if path == "/v1/audio/speech":
        return httpx.Response(
            200,
            headers={"content-type": "audio/mpeg", "x-request-id": "req_audio"},
            content=b"audio",
        )
    if path == "/v1/completions":
        return _json_response(
            httpx,
            {
                "id": "cmpl_real",
                "object": "text_completion",
                "created": 1,
                "model": "gpt-3.5-turbo-instruct",
                "choices": [
                    {
                        "text": "not retained",
                        "index": 0,
                        "logprobs": None,
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )
    if path == "/v1/chat/completions":
        return _json_response(
            httpx,
            {
                "id": "chatcmpl_real",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "not retained",
                            "refusal": None,
                            "annotations": [],
                        },
                        "finish_reason": "stop",
                        "logprobs": None,
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )
    raise AssertionError(f"unexpected OpenAI SDK request {request.method} {path}")


def test_current_sync_openai_resources_use_real_http_contracts(tmp_path: Path) -> None:
    import httpx
    from openai import OpenAI

    from dexcost.instruments.openai import instrument_openai

    storage = SQLiteStorage(db_path=tmp_path / "openai-real-sync.db")
    tracker = CostTracker(storage=storage, auto_instrument=[], auto_update_pricing=False)
    transport = httpx.MockTransport(lambda request: _handler(httpx, request))
    client = OpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.Client(transport=transport),
    )
    instrument_openai(tracker)

    client.embeddings.create(model="text-embedding-3-small", input="private")
    client.images.generate(model="gpt-image-1-mini", prompt="private")
    client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio.wav", b"private audio", "audio/wav"),
    )
    client.audio.speech.create(model="tts-1", voice="alloy", input="private")
    client.completions.create(model="gpt-3.5-turbo-instruct", prompt="private")
    client.chat.completions.parse(
        model="gpt-4o", messages=[{"role": "user", "content": "private"}]
    )

    events = storage.query_events()
    assert len(events) == 6
    assert {
        event.details.get("attribution_operation_name") for event in events
    } >= {
        "openai.embeddings.create",
        "openai.images.generate",
        "openai.audio.transcriptions.create",
        "openai.audio.speech.create",
    }
    assert sum(event.event_type == "llm_call" for event in events) == 2
    assert all("private" not in json.dumps(event.details) for event in events)
    storage.close()
    client.close()


def test_current_async_openai_resource_uses_real_http_contract(tmp_path: Path) -> None:
    import httpx
    from openai import AsyncOpenAI

    from dexcost.instruments.openai import instrument_openai

    storage = SQLiteStorage(db_path=tmp_path / "openai-real-async.db")
    tracker = CostTracker(storage=storage, auto_instrument=[], auto_update_pricing=False)
    transport = httpx.MockTransport(lambda request: _handler(httpx, request))
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.AsyncClient(transport=transport),
    )
    instrument_openai(tracker)

    async def run() -> None:
        await client.embeddings.create(model="text-embedding-3-small", input="private")
        await client.audio.speech.create(model="tts-1-hd", voice="alloy", input="abcd")
        await client.close()

    asyncio.run(run())
    events = storage.query_events()
    assert len(events) == 2
    assert {event.service_name for event in events} == {"embeddings", "text_to_speech"}
    storage.close()
