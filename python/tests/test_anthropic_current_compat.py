"""Compatibility gates against the installed current Anthropic SDK."""

from __future__ import annotations

import json
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import anthropic
import httpx
import pytest

from dexcost.instruments.anthropic import instrument_anthropic, uninstrument_anthropic
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_anthropic() -> Generator[None, None, None]:
    uninstrument_anthropic()
    yield
    uninstrument_anthropic()


@pytest.fixture
def tracker(tmp_path: Any) -> Generator[CostTracker, None, None]:
    storage = SQLiteStorage(db_path=tmp_path / "anthropic-current.db")
    instance = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    yield instance
    storage.close()


def _message_body() -> dict[str, Any]:
    return {
        "id": "msg-current-1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [
            {"type": "text", "text": "private-current-output"},
            {
                "type": "tool_use",
                "id": "toolu-current-1",
                "name": "private_current_tool",
                "input": {"secret": "private-tool-input"},
            },
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 8,
            "cache_creation_input_tokens": 7,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 2,
                "ephemeral_1h_input_tokens": 5,
            },
            "cache_read_input_tokens": 4,
            "inference_geo": "global",
            "output_tokens_details": {"thinking_tokens": 3},
            "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
            "service_tier": "standard",
        },
    }


def _stream_body() -> bytes:
    events = (
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg-current-stream-1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 7,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 2,
                            "ephemeral_1h_input_tokens": 5,
                        },
                        "cache_read_input_tokens": 3,
                        "inference_geo": "global",
                        "server_tool_use": {
                            "web_search_requests": 1,
                            "web_fetch_requests": 2,
                        },
                        "service_tier": "standard",
                    },
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "private-stream-output"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 8,
                    "cache_creation_input_tokens": 7,
                    "cache_read_input_tokens": 3,
                    "output_tokens_details": {"thinking_tokens": 3},
                    "server_tool_use": {
                        "web_search_requests": 1,
                        "web_fetch_requests": 2,
                    },
                },
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    return "".join(
        f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
        for event_name, payload in events
    ).encode()


def _response(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/messages"
    assert b"private" in request.content
    body = json.loads(request.content)
    if body.get("stream") is True:
        return httpx.Response(
            200,
            content=_stream_body(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )
    return httpx.Response(200, json=_message_body(), request=request)


def test_current_sync_message_and_stream_preserve_usage_without_content(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(transport=httpx.MockTransport(_response)) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.message") as message_task:
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=20,
                messages=[{"role": "user", "content": "private-current-prompt"}],
            )
        with tracker.task(task_type="anthropic.current.stream") as stream_task:
            stream = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=20,
                messages=[{"role": "user", "content": "private-stream-prompt"}],
                stream=True,
            )
        chunks = list(stream)

    assert response.id == "msg-current-1"
    assert len(chunks) == 6
    message_event = tracker._storage.query_events(task_id=str(message_task.task_id))[0]
    assert message_event.input_tokens == 12
    assert message_event.output_tokens == 8
    assert message_event.cached_tokens == 4
    assert message_event.details["cache_creation_input_tokens"] == 7
    assert message_event.details["cache_creation_input_tokens_5m"] == 2
    assert message_event.details["cache_creation_input_tokens_1h"] == 5
    assert message_event.details["reasoning_output_tokens"] == 3
    assert message_event.details["provider_record_id"] == "msg-current-1"
    assert {
        line["metric"]: line["quantity"]
        for line in message_event.details["attribution_usage_lines"]
    } == {
        "input_tokens": "12",
        "output_tokens": "5",
        "reasoning_output_tokens": "3",
        "cache_write_input_tokens": "7",
        "cache_read_input_tokens": "4",
        "tool_call_count": "1",
        "web_search_calls": "2",
        "web_fetch_requests": "1",
    }

    stream_event = tracker._storage.query_events(task_id=str(stream_task.task_id))[0]
    assert stream_event.input_tokens == 11
    assert stream_event.output_tokens == 8
    assert stream_event.cached_tokens == 3
    assert stream_event.details["cache_creation_input_tokens"] == 7
    assert stream_event.details["cache_creation_input_tokens_5m"] == 2
    assert stream_event.details["cache_creation_input_tokens_1h"] == 5
    assert stream_event.details["reasoning_output_tokens"] == 3
    assert stream_event.details["provider_record_id"] == "msg-current-stream-1"
    assert stream_event.details["attribution_operation_status"] == "succeeded"

    encoded = json.dumps([message_event.details, stream_event.details], sort_keys=True)
    for secret in (
        "private-current-prompt",
        "private-current-output",
        "private_current_tool",
        "private-tool-input",
        "private-stream-prompt",
        "private-stream-output",
    ):
        assert secret not in encoded


@pytest.mark.asyncio
async def test_current_async_message_and_stream_are_captured(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    transport = httpx.MockTransport(_response)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = anthropic.AsyncAnthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.async") as task:
            await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=20,
                messages=[{"role": "user", "content": "private-async-prompt"}],
            )
            stream = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=20,
                messages=[{"role": "user", "content": "private-async-stream-prompt"}],
                stream=True,
            )
        chunks = [chunk async for chunk in stream]

    assert len(chunks) == 6
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    assert {event.details["provider_record_id"] for event in events} == {
        "msg-current-1",
        "msg-current-stream-1",
    }
    assert {(event.input_tokens, event.output_tokens) for event in events} == {(12, 8), (11, 8)}


def test_current_catalog_prices_native_cache_buckets(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(transport=httpx.MockTransport(_response)) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.pricing") as task:
            client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=20,
                messages=[{"role": "user", "content": "private-pricing-prompt"}],
            )

    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    expected = (
        Decimal(12) * Decimal("0.000003")
        + Decimal(2) * Decimal("0.00000375")
        + Decimal(5) * Decimal("0.000003")
        + Decimal(4) * Decimal("0.0000003")
        + Decimal(8) * Decimal("0.000015")
        + Decimal(2) * Decimal("0.01")
    )
    assert event.cost_usd == expected
    assert event.cost_confidence == "unknown"
    assert event.details["pricing_unpriced_dimensions"] == [
        "cache_creation_input_tokens_1h"
    ]


def _beta_iteration_response(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/v1/messages"
    assert "compact-2026-01-12" in request.headers["anthropic-beta"]
    return httpx.Response(
        200,
        json={
            "id": "msg-beta-iterations-1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "private-beta-output"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 12,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "iterations": [
                    {
                        "type": "message",
                        "model": "claude-sonnet-4-5",
                        "input_tokens": 7,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    {
                        "type": "compaction",
                        "input_tokens": 20,
                        "output_tokens": 3,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    {
                        "type": "advisor_message",
                        "model": "claude-haiku-4-5",
                        "input_tokens": 4,
                        "output_tokens": 2,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    {
                        "type": "message",
                        "model": "claude-sonnet-4-5",
                        "input_tokens": 5,
                        "output_tokens": 3,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                ],
            },
        },
        request=request,
    )


def test_current_beta_messages_prices_every_sampling_iteration(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(transport=httpx.MockTransport(_beta_iteration_response)) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.beta.iterations") as task:
            response = client.beta.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=20,
                messages=[{"role": "user", "content": "private-beta-prompt"}],
                betas=["compact-2026-01-12"],
            )

    assert response.id == "msg-beta-iterations-1"
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert event.service_name == "beta_messages"
    assert event.details["attribution_operation_name"] == "anthropic.beta.messages.create"
    assert event.input_tokens == 36
    assert event.output_tokens == 10
    assert event.cost_usd == Decimal("0.00023")
    assert event.cost_confidence == "computed"
    assert event.details["anthropic_top_level_usage"] == {
        "input_tokens": 12,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    iterations = event.details["anthropic_usage_iterations"]
    assert [item["type"] for item in iterations] == [
        "message",
        "compaction",
        "advisor_message",
        "message",
    ]
    assert [item["model"] for item in iterations] == [
        "claude-sonnet-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        "claude-sonnet-4-5",
    ]
    assert json.dumps(event.details).find("private-beta") == -1


def _beta_refusal_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg-beta-refusal-1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [],
            "stop_reason": "refusal",
            "stop_sequence": None,
            "stop_details": {
                "type": "refusal",
                "category": "cyber",
                "explanation": "private-refusal-explanation",
            },
            "usage": {
                "input_tokens": 17,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "iterations": [
                    {
                        "type": "message",
                        "model": "claude-sonnet-4-5",
                        "input_tokens": 17,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    }
                ],
            },
        },
        request=request,
    )


def test_current_beta_preoutput_refusal_is_observed_but_not_charged(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(transport=httpx.MockTransport(_beta_refusal_response)) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.beta.refusal") as task:
            response = client.beta.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=20,
                messages=[{"role": "user", "content": "private-refusal-prompt"}],
                betas=["server-side-fallback-2026-07-01"],
            )

    assert response.stop_reason == "refusal"
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert event.cost_usd == Decimal(0)
    assert event.cost_confidence == "exact"
    assert event.pricing_source == "provider_response"
    assert event.input_tokens == 0
    assert event.output_tokens == 0
    assert event.details["anthropic_usage_iterations"][0]["billed"] is False
    usage = {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    }
    assert usage == {"unbilled_refusal_input_tokens": "17"}
    assert "private-refusal" not in json.dumps(event.details)


def _utility_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/messages/count_tokens":
        assert b"private-count-prompt" in request.content
        return httpx.Response(200, json={"input_tokens": 13}, request=request)
    if request.url.path == "/v1/complete":
        assert b"private-legacy-prompt" in request.content
        return httpx.Response(
            200,
            json={
                "id": "compl-private-1",
                "completion": "private-legacy-output",
                "model": "claude-2.1",
                "stop_reason": "stop_sequence",
                "type": "completion",
            },
            request=request,
        )
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


def test_current_token_counting_and_legacy_completion_are_honest(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(transport=httpx.MockTransport(_utility_transport)) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.count") as count_task:
            count = client.messages.count_tokens(
                model="claude-sonnet-4-5",
                messages=[{"role": "user", "content": "private-count-prompt"}],
            )
        with tracker.task(task_type="anthropic.current.legacy") as completion_task:
            completion = client.completions.create(
                model="claude-2.1",
                max_tokens_to_sample=20,
                prompt="private-legacy-prompt",
            )

    assert count.input_tokens == 13
    count_event = tracker._storage.query_events(task_id=str(count_task.task_id))[0]
    assert count_event.cost_usd == Decimal(0)
    assert count_event.cost_confidence == "exact"
    assert count_event.pricing_source == "provider_response"
    assert count_event.input_tokens is None
    assert count_event.details["attribution_usage_lines"] == [
        {"metric": "counted_input_tokens", "quantity": "13", "unit": "Tokens"}
    ]

    assert completion.completion == "private-legacy-output"
    completion_event = tracker._storage.query_events(
        task_id=str(completion_task.task_id)
    )[0]
    assert completion_event.cost_usd == Decimal(0)
    assert completion_event.cost_confidence == "unknown"
    assert completion_event.pricing_source == "unknown"
    assert completion_event.details["pricing_unpriced_dimensions"] == [
        "provider_usage_unreported"
    ]
    encoded = json.dumps([count_event.details, completion_event.details], sort_keys=True)
    for secret in (
        "private-count-prompt",
        "private-legacy-prompt",
        "private-legacy-output",
        "compl-private-1",
    ):
        assert secret not in encoded


def _managed_session_resource(*, idle: bool) -> dict[str, Any]:
    return {
        "id": "session-current-1",
        "type": "session",
        "agent": {
            "id": "private-agent-id",
            "description": "private-agent-description",
            "mcp_servers": [],
            "model": {
                "id": "claude-sonnet-4-5",
                "effort": None,
                "inference_geo": "global",
                "speed": "standard",
            },
            "multiagent": None,
            "name": "private-agent-name",
            "skills": [],
            "system": "private-agent-system",
            "tools": [],
            "type": "agent",
            "version": 1,
        },
        "archived_at": None,
        "budget": None,
        "created_at": "2026-08-24T00:00:00Z",
        "environment_id": "private-environment-id",
        "metadata": {"private-metadata": "private-value"},
        "outcome_evaluations": (
            [
                {
                    "completed_at": "2026-08-24T00:01:00Z",
                    "description": "private-outcome-description",
                    "explanation": "private-outcome-explanation",
                    "iteration": 1,
                    "outcome_id": "private-outcome-id",
                    "result": "passed",
                    "type": "outcome_evaluation",
                }
            ]
            if idle
            else []
        ),
        "resources": [],
        "stats": {
            "active_seconds": 1.5 if idle else 0,
            "duration_seconds": 60 if idle else 0,
        },
        "status": "idle" if idle else "running",
        "title": "private-session-title",
        "updated_at": "2026-08-24T00:01:00Z" if idle else "2026-08-24T00:00:00Z",
        "usage": {
            "active_seconds": 1.5 if idle else 0,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 2 if idle else 0,
                "ephemeral_1h_input_tokens": 1 if idle else 0,
            },
            "cache_read_input_tokens": 3 if idle else 0,
            "input_tokens": 10 if idle else 0,
            "list_cost": {"amount": "2" if idle else "0", "currency": "USD"},
            "output_tokens": 4 if idle else 0,
            "server_tool_use": {
                "web_fetch_requests": 1 if idle else 0,
                "web_search_requests": 1 if idle else 0,
            },
        },
        "vault_ids": [],
        "deployment_id": None,
    }


def _managed_session_usage_event() -> dict[str, Any]:
    return {
        "id": "private-session-event-id",
        "processed_at": "2026-08-24T00:02:00Z",
        "type": "session.usage",
        "budget": None,
        "usage": {
            "active_seconds": 2.5,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 3,
                "ephemeral_1h_input_tokens": 1,
            },
            "cache_read_input_tokens": 4,
            "input_tokens": 15,
            "list_cost": {"amount": "3", "currency": "USD"},
            "output_tokens": 6,
            "server_tool_use": {
                "web_fetch_requests": 1,
                "web_search_requests": 2,
            },
        },
    }


def _managed_session_transport(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/v1/sessions":
        assert b"private-outcome-request" in request.content
        assert b"private-outcome-rubric" in request.content
        return httpx.Response(
            200, json=_managed_session_resource(idle=False), request=request
        )
    if request.method == "GET" and request.url.path == "/v1/sessions/session-current-1":
        return httpx.Response(
            200, json=_managed_session_resource(idle=True), request=request
        )
    if (
        request.method == "GET"
        and request.url.path == "/v1/sessions/session-current-1/events/stream"
    ):
        data = json.dumps(_managed_session_usage_event())
        return httpx.Response(
            200,
            content=f"event: session.usage\ndata: {data}\n\n".encode(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


def test_current_managed_session_reconciles_outcomes_and_streamed_list_cost(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(
        transport=httpx.MockTransport(_managed_session_transport)
    ) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.session") as task:
            session = client.beta.sessions.create(
                agent="private-agent-id",
                environment_id="private-environment-id",
                initial_events=[
                    {
                        "type": "user.define_outcome",
                        "description": "private-outcome-request",
                        "rubric": {
                            "type": "text",
                            "content": "private-outcome-rubric",
                        },
                        "max_iterations": 3,
                    }
                ],
            )
        observed = client.beta.sessions.retrieve(session.id)
        stream_events = list(client.beta.sessions.events.stream(session.id))

    assert observed.status == "idle"
    # Anthropic 0.125.0 currently filters this declared event from the public
    # iterator. Dexcost taps the raw SSE without changing that SDK behaviour.
    assert stream_events == []
    revision = tracker._storage.get_provider_job(
        "anthropic", "managed_sessions", "session-current-1"
    )
    assert revision is not None
    assert revision.revision == 3
    assert revision.status == "succeeded"
    assert revision.resource_id == "claude-sonnet-4-5"
    assert revision.cost_amount == Decimal("0.03")
    assert revision.cost_source == "provider_reported"
    assert revision.cost_confidence == "exact"
    assert revision.pricing_version is None
    assert revision.task_input_tokens == 15
    assert revision.task_output_tokens == 6
    assert revision.task_cached_tokens == 4
    usage = {(line.metric, line.unit): line.quantity for line in revision.usage}
    assert usage[("outcome_evaluation_count", "Evaluations")] == Decimal(1)
    assert usage[("managed_agent_active_seconds", "Seconds")] == Decimal("2.5")
    assert usage[("web_search_calls", "Calls")] == Decimal(2)
    stored_task = tracker._storage.get_task(str(task.task_id))
    assert stored_task is not None
    assert stored_task.total_cost_usd == Decimal("0.03")
    encoded = json.dumps(revision.to_dict(), sort_keys=True)
    for secret in (
        "private-agent",
        "private-environment",
        "private-metadata",
        "private-session-title",
        "private-outcome",
        "private-session-event-id",
    ):
        assert secret not in encoded


def _dream_resource(*, completed: bool) -> dict[str, Any]:
    return {
        "id": "dream-current-1",
        "type": "dream",
        "created_at": "2026-08-24T00:00:00Z",
        "ended_at": "2026-08-24T00:01:00Z" if completed else None,
        "archived_at": None,
        "error": None,
        "inputs": [
            {"type": "memory_store", "memory_store_id": "private-memory-store"},
            {"type": "sessions", "session_ids": ["private-session-id"]},
        ],
        "instructions": "private-dream-instructions",
        "model": {"id": "claude-sonnet-4-5", "speed": "standard"},
        "output_behavior": {"type": "create_new"},
        "outputs": (
            [{"type": "memory_store", "memory_store_id": "private-output-store"}]
            if completed
            else []
        ),
        "session_id": "private-provider-session",
        "status": "completed" if completed else "pending",
        "usage": {
            "input_tokens": 10 if completed else 0,
            "output_tokens": 4 if completed else 0,
            "cache_creation_input_tokens": 2 if completed else 0,
            "cache_read_input_tokens": 3 if completed else 0,
        },
    }


def _dream_transport(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/v1/dreams":
        assert b"private-dream-instructions" in request.content
        return httpx.Response(200, json=_dream_resource(completed=False), request=request)
    if request.method == "GET" and request.url.path == "/v1/dreams/dream-current-1":
        return httpx.Response(200, json=_dream_resource(completed=True), request=request)
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


def test_current_dream_reconciles_documented_usage_without_inputs(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(transport=httpx.MockTransport(_dream_transport)) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.dream") as task:
            dream = client.beta.dreams.create(
                inputs=[
                    {"type": "memory_store", "memory_store_id": "private-memory-store"},
                    {"type": "sessions", "session_ids": ["private-session-id"]},
                ],
                model="claude-sonnet-4-5",
                instructions="private-dream-instructions",
            )
        completed = client.beta.dreams.retrieve(dream.id)

    assert completed.status == "completed"
    revision = tracker._storage.get_provider_job(
        "anthropic", "dreams", "dream-current-1"
    )
    assert revision is not None
    assert revision.revision == 2
    assert revision.status == "succeeded"
    assert revision.resource_id == "claude-sonnet-4-5"
    assert revision.cost_amount == Decimal("0.0000984")
    assert revision.cost_source == "sdk_catalog"
    assert revision.cost_confidence == "computed"
    assert revision.task_input_tokens == 10
    assert revision.task_output_tokens == 4
    assert revision.task_cached_tokens == 3
    stored_task = tracker._storage.get_task(str(task.task_id))
    assert stored_task is not None
    assert stored_task.total_cost_usd == Decimal("0.0000984")
    encoded = json.dumps(revision.to_dict(), sort_keys=True)
    for secret in (
        "private-memory-store",
        "private-session-id",
        "private-dream-instructions",
        "private-output-store",
        "private-provider-session",
    ):
        assert secret not in encoded


def _batch_resource(*, ended: bool) -> dict[str, Any]:
    return {
        "id": "msgbatch-current-1",
        "type": "message_batch",
        "processing_status": "ended" if ended else "in_progress",
        "request_counts": {
            "processing": 0 if ended else 1,
            "succeeded": 1 if ended else 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        },
        "created_at": "2026-08-24T00:00:00Z",
        "ended_at": "2026-08-24T00:01:00Z" if ended else None,
        "expires_at": "2026-08-25T00:00:00Z",
        "cancel_initiated_at": None,
        "archived_at": None,
        "results_url": (
            "/v1/messages/batches/msgbatch-current-1/results" if ended else None
        ),
    }


def _batch_transport(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/v1/messages/batches":
        assert b"private-batch-prompt" in request.content
        assert b"private-custom-id" in request.content
        return httpx.Response(200, json=_batch_resource(ended=False), request=request)
    if request.url.path == "/v1/messages/batches/msgbatch-current-1":
        return httpx.Response(200, json=_batch_resource(ended=True), request=request)
    if request.url.path == "/v1/messages/batches/msgbatch-current-1/results":
        row = {
            "custom_id": "private-custom-id",
            "result": {
                "type": "succeeded",
                "message": {
                    "id": "msg-private-batch-result",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": "private-batch-output"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "server_tool_use": {
                            "web_search_requests": 1,
                            "web_fetch_requests": 1,
                        },
                    },
                },
            },
        }
        return httpx.Response(
            200,
            content=(json.dumps(row) + "\n").encode(),
            headers={"content-type": "application/binary"},
            request=request,
        )
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


def test_current_message_batch_reconciles_discounted_jsonl_usage(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(transport=httpx.MockTransport(_batch_transport)) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.batch") as task:
            batch = client.messages.batches.create(
                requests=[
                    {
                        "custom_id": "private-custom-id",
                        "params": {
                            "model": "claude-sonnet-4-5",
                            "max_tokens": 20,
                            "messages": [
                                {"role": "user", "content": "private-batch-prompt"}
                            ],
                        },
                    }
                ]
            )
        rows = list(client.messages.batches.results(batch.id))

    assert len(rows) == 1
    revision = tracker._storage.get_provider_job(
        "anthropic", "message_batches", "msgbatch-current-1"
    )
    assert revision is not None
    assert revision.status == "succeeded"
    # The current SDK fetches the JSONL result stream directly; it does not
    # perform an implicit retrieve before results reconciliation.
    assert revision.revision == 2
    assert revision.resource_id == "claude-sonnet-4-5"
    assert revision.cost_source == "sdk_catalog"
    assert revision.cost_confidence == "computed"
    assert revision.cost_amount == Decimal("0.0100492")
    assert revision.task_input_tokens == 10
    assert revision.task_output_tokens == 4
    assert revision.task_cached_tokens == 3
    stored_task = tracker._storage.get_task(str(task.task_id))
    assert stored_task is not None
    assert stored_task.total_cost_usd == Decimal("0.0100492")
    encoded = json.dumps(revision.to_dict(), sort_keys=True)
    for secret in (
        "private-custom-id",
        "private-batch-prompt",
        "private-batch-output",
        "msg-private-batch-result",
    ):
        assert secret not in encoded


def _deployment_run_transport(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert request.url.path == "/v1/deployments/private-deployment-id/run"
    return httpx.Response(
        200,
        json={
            "id": "private-deployment-run-id",
            "agent": {"id": "private-agent-id", "type": "agent", "version": 1},
            "created_at": "2026-08-24T00:00:00Z",
            "deployment_id": "private-deployment-id",
            "error": None,
            "session_id": "session-from-deployment-1",
            "trigger_context": {"type": "manual"},
            "type": "deployment_run",
        },
        request=request,
    )


def test_current_deployment_run_adopts_future_session(tracker: CostTracker) -> None:
    instrument_anthropic(tracker)
    with httpx.Client(
        transport=httpx.MockTransport(_deployment_run_transport)
    ) as http_client:
        client = anthropic.Anthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.deployment") as task:
            run = client.beta.deployments.run("private-deployment-id")

    assert run.session_id == "session-from-deployment-1"
    revision = tracker._storage.get_provider_job(
        "anthropic", "managed_sessions", "session-from-deployment-1"
    )
    assert revision is not None
    assert revision.revision == 1
    assert revision.status == "submitted"
    assert revision.operation == "anthropic.beta.deployments.run"
    assert revision.resource_id == "anthropic-managed-agent"
    assert revision.task_id == task.task_id
    encoded = json.dumps(revision.to_dict(), sort_keys=True)
    for secret in (
        "private-deployment-id",
        "private-deployment-run-id",
        "private-agent-id",
    ):
        assert secret not in encoded


def _async_surface_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/messages/count_tokens":
        return _utility_transport(request)
    if request.url.path.startswith("/v1/sessions"):
        return _managed_session_transport(request)
    if request.url.path.startswith("/v1/dreams"):
        return _dream_transport(request)
    if request.url.path.startswith("/v1/messages/batches"):
        return _batch_transport(request)
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


@pytest.mark.asyncio
async def test_current_async_beta_jobs_and_session_usage_are_captured(
    tracker: CostTracker,
) -> None:
    instrument_anthropic(tracker)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_async_surface_transport)
    ) as http_client:
        client = anthropic.AsyncAnthropic(
            api_key="private-api-key",
            base_url="https://unit.test",
            http_client=http_client,
        )
        with tracker.task(task_type="anthropic.current.async.count") as count_task:
            count = await client.beta.messages.count_tokens(
                model="claude-sonnet-4-5",
                messages=[{"role": "user", "content": "private-count-prompt"}],
            )
        with tracker.task(task_type="anthropic.current.async.session"):
            session = await client.beta.sessions.create(
                agent="private-agent-id",
                environment_id="private-environment-id",
                initial_events=[
                    {
                        "type": "user.define_outcome",
                        "description": "private-outcome-request",
                        "rubric": {
                            "type": "text",
                            "content": "private-outcome-rubric",
                        },
                    }
                ],
            )
        await client.beta.sessions.retrieve(session.id)
        session_stream = await client.beta.sessions.events.stream(session.id)
        session_events = [event async for event in session_stream]

        with tracker.task(task_type="anthropic.current.async.dream"):
            dream = await client.beta.dreams.create(
                inputs=[
                    {"type": "memory_store", "memory_store_id": "private-memory-store"},
                    {"type": "sessions", "session_ids": ["private-session-id"]},
                ],
                model="claude-sonnet-4-5",
                instructions="private-dream-instructions",
            )
        await client.beta.dreams.retrieve(dream.id)

        with tracker.task(task_type="anthropic.current.async.batch"):
            batch = await client.beta.messages.batches.create(
                requests=[
                    {
                        "custom_id": "private-custom-id",
                        "params": {
                            "model": "claude-sonnet-4-5",
                            "max_tokens": 20,
                            "messages": [
                                {"role": "user", "content": "private-batch-prompt"}
                            ],
                        },
                    }
                ]
            )
        decoder = await client.beta.messages.batches.results(batch.id)
        batch_rows = [row async for row in decoder]

    assert count.input_tokens == 13
    count_event = tracker._storage.query_events(task_id=str(count_task.task_id))[0]
    assert count_event.cost_usd == Decimal(0)
    assert count_event.cost_confidence == "exact"
    assert session_events == []
    session_revision = tracker._storage.get_provider_job(
        "anthropic", "managed_sessions", "session-current-1"
    )
    assert session_revision is not None
    assert session_revision.revision == 3
    assert session_revision.cost_amount == Decimal("0.03")
    dream_revision = tracker._storage.get_provider_job(
        "anthropic", "dreams", "dream-current-1"
    )
    assert dream_revision is not None
    assert dream_revision.revision == 2
    assert dream_revision.cost_amount == Decimal("0.0000984")
    assert len(batch_rows) == 1
    batch_revision = tracker._storage.get_provider_job(
        "anthropic", "beta_message_batches", "msgbatch-current-1"
    )
    assert batch_revision is not None
    assert batch_revision.revision == 2
    assert batch_revision.cost_amount == Decimal("0.0100492")
