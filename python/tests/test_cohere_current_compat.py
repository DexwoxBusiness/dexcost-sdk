"""Compatibility gates against the installed current Cohere SDK."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Generator
from contextlib import ExitStack
from decimal import Decimal
from typing import Any

import httpx
import pytest

from dexcost.capabilities import capability_context
from dexcost.idempotency import idempotency_key
from dexcost.instruments.cohere import instrument_cohere, uninstrument_cohere
from dexcost.models.capability import CapabilityIdentity
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _current_cohere() -> Any:
    """Recover from fake-module isolation used by legacy adapter tests."""
    for name, module in list(sys.modules.items()):
        if (name == "cohere" or name.startswith("cohere.")) and module is None:
            sys.modules.pop(name, None)
    return importlib.import_module("cohere")


@pytest.fixture(autouse=True)
def _restore_cohere() -> Generator[None, None, None]:
    uninstrument_cohere()
    yield
    uninstrument_cohere()


@pytest.fixture
def tracker(tmp_path: Any) -> Generator[CostTracker, None, None]:
    storage = SQLiteStorage(db_path=tmp_path / "cohere-current.db")
    instance = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    yield instance
    storage.close()


def _json_response(request: httpx.Request) -> httpx.Response:
    assert b"private" in request.content
    if request.url.path == "/v2/chat":
        return httpx.Response(
            200,
            json={
                "id": "cohere-v2-chat-1",
                "finish_reason": "TOOL_CALL",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "tool-private-1",
                            "type": "function",
                            "function": {"name": "private_lookup", "arguments": "{}"},
                        }
                    ],
                },
                "usage": {
                    "billed_units": {"input_tokens": 12, "output_tokens": 3},
                    "tokens": {"input_tokens": 15, "output_tokens": 3},
                },
            },
            request=request,
        )
    if request.url.path == "/v2/embed":
        return httpx.Response(
            200,
            json={
                "id": "cohere-v2-embed-1",
                "response_type": "embeddings_by_type",
                "embeddings": {"float": [[0.1, 0.2]]},
                "meta": {
                    "api_version": {"version": "2"},
                    "billed_units": {"input_tokens": 7},
                },
            },
            request=request,
        )
    if request.url.path == "/v2/rerank":
        return httpx.Response(
            200,
            json={
                "id": "cohere-v2-rerank-1",
                "results": [{"index": 0, "relevance_score": 0.9}],
                "meta": {
                    "api_version": {"version": "2"},
                    "billed_units": {"input_tokens": 9, "search_units": 1},
                },
            },
            request=request,
        )
    raise AssertionError(f"unexpected Cohere endpoint {request.url.path}")


def _stream_response(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v2/chat"
    assert b"private-stream-prompt" in request.content
    items = (
        {
            "type": "message-start",
            "id": "cohere-v2-stream-1",
            "delta": {"message": {"role": "assistant"}},
        },
        {
            "type": "tool-call-start",
            "id": "cohere-v2-stream-1",
            "index": 0,
            "delta": {
                "message": {
                    "tool_calls": {
                        "id": "tool-private-stream",
                        "type": "function",
                        "function": {"name": "private_stream_tool", "arguments": ""},
                    }
                }
            },
        },
        {
            "type": "message-end",
            "id": "cohere-v2-stream-1",
            "delta": {
                "finish_reason": "COMPLETE",
                "usage": {
                    "billed_units": {"input_tokens": 11, "output_tokens": 4},
                    "tokens": {"input_tokens": 11, "output_tokens": 4},
                },
            },
        },
    )
    content = "".join(f"data: {json.dumps(item)}\n\n" for item in items)
    content += "data: [DONE]\n\n"
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": "text/event-stream"},
        request=request,
    )


def test_current_client_v2_chat_embed_and_rerank_are_all_metered(
    tracker: CostTracker,
) -> None:
    cohere = _current_cohere()
    transport = httpx.MockTransport(_json_response)
    capability = CapabilityIdentity(name="cohere.v2.current", kind="workflow")

    with httpx.Client(transport=transport) as http_client:
        instrument_cohere(tracker)
        client = cohere.ClientV2(
            api_key="private-api-key",
            base_url="https://unit.test",
            httpx_client=http_client,
        )
        with tracker.task(task_type="cohere.v2.current") as task:
            with ExitStack() as stack:
                stack.enter_context(capability_context(capability))
                stack.enter_context(idempotency_key("private-cohere-v2-idempotency"))
                chat = client.chat(
                    model="command-a-03-2025",
                    messages=[{"role": "user", "content": "private-chat-prompt"}],
                )
            embed = client.embed(
                model="embed-v4.0",
                input_type="search_document",
                texts=["private-embedding-input"],
            )
            rerank = client.rerank(
                model="rerank-v3.5",
                query="private-rerank-query",
                documents=["private-rerank-document"],
            )

    assert chat.id == "cohere-v2-chat-1"
    assert embed.id == "cohere-v2-embed-1"
    assert rerank.id == "cohere-v2-rerank-1"
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 3, [
        (event.service_name, event.details.get("provider_record_id")) for event in events
    ]
    by_service = {event.service_name: event for event in events}

    chat_event = by_service["chat"]
    assert chat_event.input_tokens == 12
    assert chat_event.output_tokens == 3
    assert chat_event.details["provider_record_id"] == "cohere-v2-chat-1"
    assert {
        line["metric"]: line["quantity"]
        for line in chat_event.details["attribution_usage_lines"]
    } == {"input_tokens": "12", "output_tokens": "3", "tool_call_count": "1"}

    embed_event = by_service["embeddings"]
    assert embed_event.model == "cohere/embed-v4.0"
    assert embed_event.input_tokens == 7
    assert embed_event.cost_usd == Decimal("0.00000084")
    assert embed_event.cost_confidence == "computed"
    assert embed_event.details["provider_record_id"] == "cohere-v2-embed-1"

    rerank_event = by_service["rerank"]
    assert rerank_event.model == "rerank-v3.5"
    assert rerank_event.cost_usd == Decimal("0.002")
    assert rerank_event.cost_confidence == "computed"
    assert rerank_event.details["provider_record_id"] == "cohere-v2-rerank-1"
    assert {
        line["metric"]: line["quantity"]
        for line in rerank_event.details["attribution_usage_lines"]
    } == {"input_tokens": "9", "search_units": "1"}

    encoded = json.dumps([event.details for event in events], sort_keys=True)
    for secret in (
        "private-chat-prompt",
        "private_lookup",
        "private-embedding-input",
        "private-rerank-query",
        "private-rerank-document",
        "private-cohere-v2-idempotency",
    ):
        assert secret not in encoded
    assert chat_event.details["attribution_capability"] == capability.to_dict()
    assert len(chat_event.details["_dexcost_idempotency_sha256"]) == 64


def test_current_client_v2_stream_reads_message_end_usage_after_task_exit(
    tracker: CostTracker,
) -> None:
    cohere = _current_cohere()
    transport = httpx.MockTransport(_stream_response)
    with httpx.Client(transport=transport) as http_client:
        instrument_cohere(tracker)
        client = cohere.ClientV2(
            api_key="private-api-key",
            base_url="https://unit.test",
            httpx_client=http_client,
        )
        with tracker.task(task_type="cohere.v2.stream") as task:
            stream = client.chat_stream(
                model="command-a-03-2025",
                messages=[{"role": "user", "content": "private-stream-prompt"}],
            )
        chunks = list(stream)

    assert len(chunks) == 3
    stream_events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(stream_events) == 1, type(stream)
    event = stream_events[0]
    assert event.input_tokens == 11
    assert event.output_tokens == 4
    assert event.details["provider_record_id"] == "cohere-v2-stream-1"
    assert event.details["attribution_operation_status"] == "succeeded"
    assert {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    } == {"input_tokens": "11", "output_tokens": "4", "tool_call_count": "1"}
    assert "private_stream_tool" not in json.dumps(event.details, sort_keys=True)


@pytest.mark.asyncio
async def test_current_async_client_v2_covers_chat_embed_rerank_and_stream(
    tracker: CostTracker,
) -> None:
    cohere = _current_cohere()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream") is True:
            return _stream_response(request)
        return _json_response(request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        instrument_cohere(tracker)
        client = cohere.AsyncClientV2(
            api_key="private-api-key",
            base_url="https://unit.test",
            httpx_client=http_client,
        )
        with tracker.task(task_type="cohere.v2.async") as task:
            await client.chat(
                model="command-a-03-2025",
                messages=[{"role": "user", "content": "private-async-chat"}],
            )
            await client.embed(
                model="embed-v4.0",
                input_type="search_document",
                texts=["private-async-embedding"],
            )
            await client.rerank(
                model="rerank-v3.5",
                query="private-async-query",
                documents=["private-async-document"],
            )
            stream = client.chat_stream(
                model="command-a-03-2025",
                messages=[{"role": "user", "content": "private-stream-prompt"}],
            )
        chunks = [chunk async for chunk in stream]

    assert len(chunks) == 3
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 4, [
        (event.service_name, event.details.get("provider_record_id")) for event in events
    ]
    assert [event.service_name for event in events].count("chat") == 2
    assert {event.service_name for event in events} == {"chat", "embeddings", "rerank"}
    stream_events = [
        event
        for event in events
        if event.details.get("provider_record_id") == "cohere-v2-stream-1"
    ]
    assert len(stream_events) == 1
    assert stream_events[0].input_tokens == 11
    assert stream_events[0].output_tokens == 4


@pytest.mark.asyncio
async def test_async_v2_auto_tasks_are_created_only_when_coroutines_are_awaited(
    tracker: CostTracker,
) -> None:
    cohere = _current_cohere()
    transport = httpx.MockTransport(_json_response)
    async with httpx.AsyncClient(transport=transport) as http_client:
        instrument_cohere(tracker)
        client = cohere.AsyncClientV2(
            api_key="private-api-key",
            base_url="https://unit.test",
            httpx_client=http_client,
        )
        pending = client.embed(
            model="embed-v4.0",
            input_type="search_document",
            texts=["private-auto-embedding"],
        )
        assert tracker._storage.query_tasks(task_type="cohere.embed") == []
        await pending

    tasks = tracker._storage.query_tasks(task_type="cohere.embed")
    assert len(tasks) == 1
    assert tasks[0].status == "success"
    assert tasks[0].total_input_tokens == 7
