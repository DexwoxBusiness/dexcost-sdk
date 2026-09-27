"""Current OpenAI SDK wire gates for ordinary stream lifecycle metering."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _restore_openai(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    yield
    uninstrument_openai()


def _chunk(
    *,
    usage: bool,
    model: str = "gpt-4o-mini-2024-07-18",
    service_tier: object = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "chatcmpl_stream_123",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "private output"},
                "finish_reason": None,
                "logprobs": None,
            }
        ],
    }
    if usage:
        payload["usage"] = {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 1},
        }
    if service_tier is not None:
        payload["service_tier"] = service_tier
    return payload


def _stream_response(*, usage_on_first_chunk: bool) -> httpx.Response:
    events = [
        _chunk(usage=usage_on_first_chunk),
        {
            **_chunk(usage=True),
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
        },
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    body += "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "x-request-id": "req_stream"},
        content=body.encode(),
    )


def _groq_stream_response(service_tier: object) -> httpx.Response:
    model = "openai/gpt-oss-120b"
    events = [
        _chunk(usage=False, model=model, service_tier=service_tier),
        {
            **_chunk(usage=True, model=model, service_tier=service_tier),
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
        },
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    body += "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "x-request-id": "req_groq"},
        content=body.encode(),
    )


def _tracker(path: Path) -> tuple[SQLiteStorage, CostTracker]:
    storage = SQLiteStorage(db_path=path)
    return storage, CostTracker(
        storage=storage,
        auto_instrument=[],
        auto_update_pricing=False,
    )


def test_sync_early_close_is_cancelled_without_fabricated_usage(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: _stream_response(usage_on_first_chunk=False)
    )
    client = OpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.Client(transport=transport),
    )
    storage, tracker = _tracker(tmp_path / "sync-stream.db")
    instrument_openai(tracker)

    stream = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "private input"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    next(stream)
    stream.close()

    [event] = storage.query_events()
    assert event.details["attribution_operation_status"] == "cancelled"
    assert event.cost_usd == 0
    assert event.cost_confidence == "unknown"
    assert (event.input_tokens, event.output_tokens, event.cached_tokens) == (0, 0, 0)
    assert "private" not in json.dumps(event.to_dict(), sort_keys=True)
    client.close()
    storage.close()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_async_close_preserves_only_provider_observed_partial_usage(
    tmp_path: Path,
) -> None:
    transport = httpx.MockTransport(
        lambda request: _stream_response(usage_on_first_chunk=True)
    )
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.AsyncClient(transport=transport),
    )
    storage, tracker = _tracker(tmp_path / "async-stream.db")
    instrument_openai(tracker)

    stream = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "private input"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    await stream.__anext__()
    # OpenAI's current AsyncStream exposes async close(), while aclose() is the
    # conventional async-iterator spelling.  DexCost deliberately supports both.
    await stream.close()

    [event] = storage.query_events()
    assert event.details["attribution_operation_status"] == "cancelled"
    assert (event.input_tokens, event.output_tokens, event.cached_tokens) == (10, 2, 3)
    assert event.details["reasoning_output_tokens"] == 1
    assert event.cost_confidence == "computed"
    assert "private" not in json.dumps(event.to_dict(), sort_keys=True)
    await client.close()
    storage.close()


def test_sync_groq_stream_malformed_response_tier_overrides_request_fallback(
    tmp_path: Path,
) -> None:
    transport = httpx.MockTransport(
        lambda request: _groq_stream_response({"future": "tier"})
    )
    client = OpenAI(
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        http_client=httpx.Client(transport=transport),
    )
    storage, tracker = _tracker(tmp_path / "sync-groq-tier.db")
    instrument_openai(tracker)

    stream = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": "private input"}],
        service_tier="on_demand",  # type: ignore[arg-type]
        stream=True,
        stream_options={"include_usage": True},
    )
    list(stream)

    [event] = storage.query_events()
    dimensions = {
        item["key"]: item["value"]["value"]
        for item in event.details["attribution_dimensions"]
    }
    assert dimensions == {"gateway": "groq"}
    client.close()
    storage.close()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_async_groq_stream_malformed_response_tier_overrides_request_fallback(
    tmp_path: Path,
) -> None:
    transport = httpx.MockTransport(
        lambda request: _groq_stream_response({"future": "tier"})
    )
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        http_client=httpx.AsyncClient(transport=transport),
    )
    storage, tracker = _tracker(tmp_path / "async-groq-tier.db")
    instrument_openai(tracker)

    stream = await client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": "private input"}],
        service_tier="on_demand",  # type: ignore[arg-type]
        stream=True,
        stream_options={"include_usage": True},
    )
    async for _ in stream:
        pass

    [event] = storage.query_events()
    dimensions = {
        item["key"]: item["value"]["value"]
        for item in event.details["attribution_dimensions"]
    }
    assert dimensions == {"gateway": "groq"}
    await client.close()
    storage.close()
