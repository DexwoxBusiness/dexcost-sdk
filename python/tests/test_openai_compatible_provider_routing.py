"""Real OpenAI SDK wire gates for Azure and Perplexity routing."""

from __future__ import annotations

import json
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from openai import OpenAI

from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_openai() -> Generator[None, None, None]:
    uninstrument_openai()
    yield
    uninstrument_openai()


def _chat_response(model: str, *, perplexity: bool = False) -> dict[str, object]:
    usage: dict[str, object] = {
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "total_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 3},
    }
    if perplexity:
        usage.update(
            {
                "citation_tokens": 6,
                "num_search_queries": 2,
                "search_context_size": "high",
                "cost": {
                    "input_tokens_cost": 0.001,
                    "output_tokens_cost": 0.002,
                    "citation_tokens_cost": 0.0002,
                    "search_queries_cost": 0.01,
                    "total_cost": 0.0132,
                },
            }
        )
    return {
        "id": "chat-provider-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "private-output"},
            }
        ],
        "created": 1,
        "model": model,
        "object": "chat.completion",
        "usage": usage,
    }


def _agent_response() -> dict[str, object]:
    return {
        "created_at": 1,
        "id": "resp-perplexity-1",
        "model": "openai/gpt-5.4",
        "object": "response",
        "output": [],
        "status": "completed",
        "usage": {
            "input_tokens": 40,
            "input_tokens_details": {
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 10,
                "cache_write_tokens": 5,
                "cached_tokens": 10,
            },
            "output_tokens": 12,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 52,
            "cost": {
                "input_cost": 0.004,
                "output_cost": 0.006,
                "cache_creation_cost": 0.001,
                "cache_read_cost": 0.0005,
                "tool_calls_cost": 0.005,
                "total_cost": 0.0165,
            },
            "tool_calls_details": {
                "search_web": {"invocation": 1},
                "fetch_url": {"invocation": 2},
            },
        },
    }


def test_perplexity_openai_compatibility_is_exact_and_privacy_safe(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json=_chat_response("sonar-pro", perplexity=True), request=request
            )
        if request.url.path.endswith("/responses"):
            return httpx.Response(200, json=_agent_response(), request=request)
        return httpx.Response(404, request=request)

    storage = SQLiteStorage(tmp_path / "perplexity-openai.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenAI(
        api_key="test",
        base_url="https://api.perplexity.ai/v1",
        http_client=http_client,
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="perplexity-openai"):
            client.chat.completions.create(
                model="sonar-pro",
                messages=[{"role": "user", "content": "private-query"}],
            )
            client.responses.create(
                model="openai/gpt-5.4", input="private-agent-input"
            )

        events = storage.query_events()
        assert len(events) == 2
        by_model = {event.model: event for event in events}
        chat = by_model["perplexity/sonar-pro"]
        agent = by_model["perplexity/openai/gpt-5.4"]
        assert chat.provider == "perplexity"
        assert chat.cost_usd == Decimal("0.0132")
        assert chat.pricing_source == "provider_response"
        assert chat.cached_tokens == 4
        assert chat.details["provider_cost_breakdown_usd"]["search_queries_cost"] == "0.01"
        assert {line["metric"] for line in chat.details["attribution_usage_lines"]} == {
            "citation_token_count",
            "search_query_count",
        }

        assert agent.provider == "perplexity"
        assert agent.cost_usd == Decimal("0.0165")
        assert agent.cached_tokens == 10
        assert agent.details["cache_write_input_tokens"] == 5
        assert {line["metric"] for line in agent.details["attribution_usage_lines"]} == {
            "tool_fetch_url_invocation_count",
            "tool_search_web_invocation_count",
        }
        persisted = json.dumps([event.to_dict() for event in events])
        assert "private-query" not in persisted
        assert "private-output" not in persisted
        assert "private-agent-input" not in persisted
    finally:
        uninstrument_openai()
        client.close()
        storage.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://demo.openai.azure.com/openai/v1/",
        "https://demo.services.ai.azure.com/openai/v1/",
    ],
)
def test_current_azure_openai_endpoints_keep_deployment_attribution(
    tmp_path: Path, base_url: str
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json=_chat_response("gpt-5-mini"), request=request
        )
    )
    storage = SQLiteStorage(tmp_path / f"azure-{base_url.split('.')[1]}.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url=base_url,
        http_client=httpx.Client(transport=transport),
    )
    instrument_openai(tracker)
    try:
        client.chat.completions.create(
            model="private-deployment",
            messages=[{"role": "user", "content": "private-query"}],
        )
        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "azure_openai"
        assert event.model == "azure/gpt-5-mini"
        assert event.input_tokens == 20
        dimensions = {
            item["key"]: item["value"]["value"]
            for item in event.details["attribution_dimensions"]
        }
        assert dimensions["azure_deployment"] == "private-deployment"
        assert "private-query" not in json.dumps(event.to_dict())
    finally:
        uninstrument_openai()
        client.close()
        storage.close()
