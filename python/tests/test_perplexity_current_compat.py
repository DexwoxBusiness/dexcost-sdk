"""Current official perplexityai package compatibility and completeness gates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from perplexity import AsyncPerplexity, Perplexity

from dexcost.capabilities import capability_context
from dexcost.idempotency import idempotency_key
from dexcost.instruments.perplexity import (
    instrument_perplexity,
    uninstrument_perplexity,
)
from dexcost.models.capability import CapabilityIdentity
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_perplexity() -> Generator[None, None, None]:
    uninstrument_perplexity()
    yield
    uninstrument_perplexity()


def _chat_body() -> dict[str, Any]:
    message = {"role": "assistant", "content": "private-output"}
    return {
        "choices": [
            {
                "delta": message,
                "finish_reason": "stop",
                "index": 0,
                "message": message,
            }
        ],
        "citations": ["https://private.example"],
        "created": 1,
        "id": "pplx-chat-1",
        "model": "sonar-deep-research",
        "object": "chat.completion",
        "usage": {
            "citation_tokens": 11,
            "completion_tokens": 20,
            "cost": {
                "citation_tokens_cost": 0.000022,
                "input_tokens_cost": 0.00002,
                "output_tokens_cost": 0.00016,
                "reasoning_tokens_cost": 0.00009,
                "request_cost": 0.005,
                "search_queries_cost": 0.01,
                "total_cost": 0.015292,
            },
            "num_search_queries": 2,
            "prompt_tokens": 10,
            "reasoning_tokens": 30,
            "search_context_size": "high",
            "total_tokens": 60,
        },
    }


def _responses_body(
    *, status: str = "completed", response_id: str = "pplx-resp-1"
) -> dict[str, Any]:
    usage = None
    if status == "completed":
        usage = {
            "cost": {
                "cache_creation_cost": 0.001,
                "cache_read_cost": 0.0005,
                "currency": "USD",
                "input_cost": 0.004,
                "output_cost": 0.006,
                "tool_calls_cost": 0.005,
                "total_cost": 0.0165,
            },
            "input_tokens": 40,
            "input_tokens_details": {
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 10,
            },
            "output_tokens": 12,
            "tool_calls_details": {
                "fetch_url": {"invocation": 2},
                "search_web": {"invocation": 1},
            },
            "total_tokens": 52,
        }
    return {
        "background": status != "completed",
        "created_at": 1,
        "error": None,
        "id": response_id,
        "model": "openai/gpt-5.4",
        "object": "response",
        "output": [],
        "previous_response_id": None,
        "status": status,
        "store": True,
        "usage": usage,
    }


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/chat/completions":
        return httpx.Response(200, json=_chat_body(), request=request)
    if path == "/v1/responses/pplx-job-1" and request.method == "GET":
        return httpx.Response(
            200,
            json=_responses_body(status="completed", response_id="pplx-job-1"),
            request=request,
        )
    if path == "/v1/responses" and b'"background":true' in request.content:
        return httpx.Response(
            200,
            json=_responses_body(status="queued", response_id="pplx-job-1"),
            request=request,
        )
    if path == "/v1/responses":
        return httpx.Response(200, json=_responses_body(), request=request)
    if path == "/search":
        return httpx.Response(
            200,
            json={
                "id": "search-1",
                "results": [
                    {
                        "title": "private-title",
                        "url": "https://private.example",
                        "snippet": "private-snippet",
                    },
                    {
                        "title": "private-title-2",
                        "url": "https://private.example/2",
                        "snippet": "private-snippet-2",
                    },
                ],
            },
            request=request,
        )
    if path == "/v1/embeddings":
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": "private-vector", "index": 0, "object": "embedding"}],
                "model": "pplx-embed-v1-0.6b",
                "object": "list",
                "usage": {
                    "cost": {"currency": "USD", "input_cost": 0.000004, "total_cost": 0.000004},
                    "prompt_tokens": 100,
                    "total_tokens": 100,
                },
            },
            request=request,
        )
    if path == "/v1/contextualizedembeddings":
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "data": [
                            {"embedding": "private-vector-1", "index": 0, "object": "embedding"},
                            {"embedding": "private-vector-2", "index": 1, "object": "embedding"},
                        ],
                        "index": 0,
                        "object": "list",
                    }
                ],
                "model": "pplx-embed-context-v1-0.6b",
                "object": "list",
                "usage": {
                    "cost": {"currency": "USD", "input_cost": 0.000016, "total_cost": 0.000016},
                    "prompt_tokens": 200,
                    "total_tokens": 200,
                },
            },
            request=request,
        )
    return httpx.Response(404, request=request)


def test_official_sdk_all_core_billable_resources_and_background_lifecycle(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "perplexity-current.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = Perplexity(
        api_key="test",
        http_client=httpx.Client(transport=httpx.MockTransport(_handler)),
    )
    instrument_perplexity(tracker)
    try:
        with tracker.task(task_type="perplexity-current") as task:
            client.chat.completions.create(
                model="sonar-deep-research",
                messages=[{"role": "user", "content": "private-chat-input"}],
            )
            client.responses.create(model="openai/gpt-5.4", input="private-agent-input")
            client.search.create(query="private-search-query")
            client.embeddings.create(model="pplx-embed-v1-0.6b", input="private-embedding-input")
            client.contextualized_embeddings.create(
                model="pplx-embed-context-v1-0.6b",
                input=[["private-context-1", "private-context-2"]],
            )
            client.responses.create(
                model="openai/gpt-5.4",
                input="private-background-input",
                background=True,
            )
            client.responses.retrieve("pplx-job-1")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 5
        by_service = {event.service_name: event for event in events}
        chat = by_service["chat"]
        assert chat.provider == "perplexity"
        assert chat.model == "perplexity/sonar-deep-research"
        assert chat.cost_usd == Decimal("0.015292")
        assert chat.input_tokens == 10
        assert chat.output_tokens == 20
        chat_lines = {
            line["metric"]: line["quantity"] for line in chat.details["attribution_usage_lines"]
        }
        assert chat_lines["reasoning_output_tokens"] == "30"
        assert chat_lines["citation_tokens"] == "11"
        assert chat_lines["query_count"] == "2"

        responses = by_service["responses"]
        assert responses.model == "perplexity/openai/gpt-5.4"
        assert responses.cost_usd == Decimal("0.0165")
        assert responses.cached_tokens == 10
        response_lines = {
            line["metric"]: line["quantity"]
            for line in responses.details["attribution_usage_lines"]
        }
        assert response_lines["cache_write_input_tokens"] == "5"
        assert response_lines["tool_fetch_url_invocation_count"] == "2"
        assert response_lines["tool_search_web_invocation_count"] == "1"

        search = by_service["search"]
        assert search.model == "perplexity/search"
        assert search.cost_usd == Decimal("0.005")
        assert search.cost_confidence == "computed"
        assert by_service["embeddings"].cost_usd == Decimal("0.000004")
        assert by_service["contextualized_embeddings"].cost_usd == Decimal("0.000016")

        job = storage.get_provider_job("perplexity", "responses", "pplx-job-1")
        assert job is not None
        assert job.status == "succeeded"
        assert job.cost_amount == Decimal("0.0165")
        assert job.cost_source == "provider_reported"

        persisted = json.dumps(
            {
                "events": [event.to_dict() for event in events],
                "jobs": [item.to_dict() for item in storage.query_provider_jobs_for_sync()],
            }
        )
        for secret in (
            "private-chat-input",
            "private-output",
            "private.example",
            "private-agent-input",
            "private-search-query",
            "private-title",
            "private-snippet",
            "private-vector",
            "private-embedding-input",
            "private-context-1",
            "private-background-input",
        ):
            assert secret not in persisted
    finally:
        uninstrument_perplexity()
        client.close()
        storage.close()


def test_official_agent_stream_terminal_usage_and_early_close(tmp_path: Path) -> None:
    terminal = {
        "type": "response.completed",
        "sequence_number": 1,
        "response": _responses_body(),
    }
    content = (
        'data: {"type":"response.in_progress","sequence_number":0,"response":null}\n\n'
        f"data: {json.dumps(terminal)}\n\n"
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content.encode(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    storage = SQLiteStorage(tmp_path / "perplexity-stream.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = Perplexity(
        api_key="test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instrument_perplexity(tracker)
    try:
        with tracker.task(task_type="perplexity-stream-complete") as complete_task:
            stream = client.responses.create(
                model="openai/gpt-5.4", input="private-stream-input", stream=True
            )
            assert len(list(stream)) == 2
        complete = storage.query_events(task_id=str(complete_task.task_id))[0]
        assert complete.cost_usd == Decimal("0.0165")
        assert complete.details["attribution_operation_status"] == "succeeded"

        with tracker.task(task_type="perplexity-stream-cancel") as cancel_task:
            cancelled_stream = client.responses.create(
                model="openai/gpt-5.4", input="private-cancel-input", stream=True
            )
            next(cancelled_stream)
            cancelled_stream.close()
        cancelled = storage.query_events(task_id=str(cancel_task.task_id))[0]
        assert cancelled.details["attribution_operation_status"] == "cancelled"
        assert cancelled.cost_confidence == "unknown"
        persisted = json.dumps([event.to_dict() for event in storage.query_events()])
        assert "private-stream-input" not in persisted
        assert "private-cancel-input" not in persisted
    finally:
        uninstrument_perplexity()
        client.close()
        storage.close()


def test_official_async_resources_use_current_generated_classes(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "perplexity-async.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    client = AsyncPerplexity(api_key="test", http_client=http_client)
    instrument_perplexity(tracker)

    async def run() -> None:
        await client.chat.completions.create(
            model="sonar-deep-research",
            messages=[{"role": "user", "content": "private-async-chat"}],
        )
        await client.embeddings.create(model="pplx-embed-v1-0.6b", input="private-async-embedding")
        await client.close()

    try:
        asyncio.run(run())
        events = storage.query_events()
        assert len(events) == 2
        assert {event.service_name for event in events} == {"chat", "embeddings"}
        assert all(event.provider == "perplexity" for event in events)
        persisted = json.dumps([event.to_dict() for event in events])
        assert "private-async-chat" not in persisted
        assert "private-async-embedding" not in persisted
    finally:
        uninstrument_perplexity()
        storage.close()


def test_capability_idempotency_job_and_native_failure_contract(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search" and b"private-failure-query" in request.content:
            return httpx.Response(
                500,
                json={"error": {"message": "private-provider-failure"}},
                request=request,
            )
        return _handler(request)

    storage = SQLiteStorage(tmp_path / "perplexity-contract.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = Perplexity(
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    capability = CapabilityIdentity(
        name="research.answer",
        kind="workflow",
        namespace="dexcost.agent",
        source="project",
        source_id="research.answer/v1",
        invocation="automatic",
    )
    instrument_perplexity(tracker)
    try:
        with (
            tracker.task(task_type="perplexity-contract") as task,
            capability_context(capability),
            idempotency_key("private-perplexity-idempotency"),
        ):
            client.search.create(query="private-capability-query")
            client.responses.create(
                model="openai/gpt-5.4",
                input="private-background-capability",
                background=True,
            )

        event = storage.query_events(task_id=str(task.task_id))[0]
        assert event.details["attribution_capability"] == capability.to_dict()
        assert len(event.details["_dexcost_idempotency_sha256"]) == 64
        job = storage.get_provider_job("perplexity", "responses", "pplx-job-1")
        assert job is not None
        assert job.capability == capability

        with pytest.raises(Exception) as caught, tracker.task(
            task_type="perplexity-native-failure"
        ) as failed_task:
            client.search.create(query="private-failure-query")
        assert type(caught.value).__module__.startswith("perplexity")
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
            "private-perplexity-idempotency",
            "private-capability-query",
            "private-background-capability",
            "private-failure-query",
            "private-provider-failure",
        ):
            assert secret not in persisted
    finally:
        uninstrument_perplexity()
        client.close()
        storage.close()
