"""Provider-reported OpenAI Responses built-in tool usage gates."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_openai() -> Generator[None, None, None]:
    yield
    uninstrument_openai()


def _response_payload() -> dict[str, object]:
    return {
        "id": "resp_tools_1",
        "object": "response",
        "created_at": 1,
        "model": "gpt-4o-mini-2024-07-18",
        "output": [
            {
                "id": "ws_1",
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "query": "private-search-query",
                    "sources": [],
                },
            },
            {
                "id": "ws_2",
                "type": "web_search_call",
                "status": "failed",
                "action": {"type": "search", "query": "private-failed-query"},
            },
            {
                "id": "fs_1",
                "type": "file_search_call",
                "status": "completed",
                "queries": ["private-file-query"],
                "results": [
                    {
                        "file_id": "file-private",
                        "filename": "private.txt",
                        "score": 0.9,
                        "text": "private-file-result",
                    }
                ],
            },
            {
                "id": "ci_1",
                "type": "code_interpreter_call",
                "status": "completed",
                "container_id": "container_private_1",
                "code": "print('private-code')",
                "outputs": [{"type": "logs", "logs": "private-code-output"}],
            },
            {
                "id": "ci_2",
                "type": "code_interpreter_call",
                "status": "failed",
                "container_id": "container_private_1",
                "code": "raise Exception('private')",
            },
            {
                "id": "img_1",
                "type": "image_generation_call",
                "status": "completed",
                "result": "private-base64-image",
            },
            {
                "id": "mcp_1",
                "type": "mcp_call",
                "name": "private_tool_name",
                "server_label": "private_server",
                "arguments": "{\"private\":true}",
                "output": "private-mcp-output",
                "status": "completed",
            },
        ],
        "parallel_tool_calls": True,
        "temperature": 1,
        "tool_choice": "auto",
        "tools": [
            {"type": "web_search", "search_context_size": "medium"},
            {"type": "file_search", "vector_store_ids": ["vs_private"]},
            {
                "type": "code_interpreter",
                "container": {"type": "auto", "memory_limit": "4g"},
            },
        ],
        "status": "completed",
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {
                "cached_tokens": 20,
                "cache_write_tokens": 0,
            },
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 5},
            "total_tokens": 130,
        },
    }


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert request.url.path == "/v1/responses"
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "x-request-id": "req_tools"},
        json=_response_payload(),
    )


def _assert_events(storage: SQLiteStorage, task_id: str) -> None:
    events = storage.query_events(task_id=task_id)
    assert len(events) == 6
    by_operation = {
        event.details.get("attribution_operation_name"): event
        for event in events
        if event.event_type == "external_cost"
    }
    expected = {
        "openai.responses.web_search": ("web_search_calls", "2"),
        "openai.responses.file_search": ("file_search_calls", "1"),
        "openai.responses.container": ("container_reference_count", "1"),
        "openai.responses.image_generation": ("output_image_count", "1"),
        "openai.responses.mcp": ("mcp_tool_calls", "1"),
    }
    assert set(by_operation) == set(expected)
    for operation, (metric, quantity) in expected.items():
        event = by_operation[operation]
        line = event.details["attribution_usage_lines"][0]
        assert (line["metric"], line["quantity"]) == (metric, quantity)
        wire = to_attribution_observation_v3(event)
        assert wire is not None
        assert wire["usage"][0]["metric"] == metric

    durable = "".join(str(event.to_dict()) for event in events)
    for private in (
        "private-search",
        "private-file",
        "private-code",
        "private-base64",
        "private_tool",
        "private_server",
        "private-mcp",
        "container_private",
        "vs_private",
    ):
        assert private not in durable


def test_sync_responses_tools_use_real_http_and_current_types(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "openai-tools-sync.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    client = OpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(_handler)),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="openai-tools-sync") as task:
            response = client.responses.create(
                model="gpt-4o-mini-2024-07-18",
                input="private-user-input",
                tools=[{"type": "web_search"}],
            )
        assert response.id == "resp_tools_1"
        _assert_events(storage, str(task.task_id))
    finally:
        client.close()
        storage.close()


@pytest.mark.asyncio
async def test_async_responses_tools_use_real_http_and_current_types(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "openai-tools-async.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="openai-tools-async") as task:
            response = await client.responses.create(
                model="gpt-4o-mini-2024-07-18",
                input="private-user-input",
                tools=[{"type": "web_search"}],
            )
        assert response.id == "resp_tools_1"
        _assert_events(storage, str(task.task_id))
    finally:
        await client.close()
        storage.close()
