"""Real OpenAI SDK wire gates for Azure and Perplexity routing."""

from __future__ import annotations

import json
from collections.abc import Generator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from openai import OpenAI

from dexcost.attribution.v3_convert import to_attribution_observation_v3
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
            "input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "citation_token_count",
            "search_query_count",
        }

        assert agent.provider == "perplexity"
        assert agent.cost_usd == Decimal("0.0165")
        assert agent.cached_tokens == 10
        assert agent.details["cache_write_input_tokens"] == 5
        assert {line["metric"] for line in agent.details["attribution_usage_lines"]} == {
            "input_tokens",
            "cache_read_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
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


def test_deepseek_openai_compatibility_preserves_provider_and_cache_usage(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _chat_response("deepseek-v4-flash")
        usage = payload["usage"]
        assert isinstance(usage, dict)
        usage.pop("prompt_tokens_details", None)
        usage["prompt_cache_hit_tokens"] = 4
        usage["prompt_cache_miss_tokens"] = 16
        return httpx.Response(200, json=payload, request=request)

    storage = SQLiteStorage(tmp_path / "deepseek-openai.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url="https://api.deepseek.com",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="deepseek-openai"):
            client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": "private-query"}],
            )

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "deepseek"
        assert event.model == "deepseek-v4-flash"
        assert event.input_tokens == 20
        assert event.output_tokens == 10
        assert event.cached_tokens == 4
        # DeepSeek's public tariff varies by weekday UTC time window. The SDK
        # captures usage but intentionally leaves money to the server catalog.
        assert event.cost_usd == Decimal("0")
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"]["name"] == "deepseek"
        assert observation["provider"]["service"] == "api"
        assert observation["provider"]["record_id"] == "chat-provider-1"
        assert observation["resource"] == {
            "type": "model",
            "id": "deepseek-v4-flash",
        }
        assert {
            line["metric"]: line["quantity"] for line in observation["usage"]
        } == {
            "input_tokens": "16",
            "cache_read_input_tokens": "4",
            "output_tokens": "7",
            "reasoning_output_tokens": "3",
        }
        assert "private-query" not in json.dumps(event.to_dict())
    finally:
        uninstrument_openai()
        client.close()
        storage.close()


@pytest.mark.parametrize(
    "base_url",
    ["https://api.together.ai/v1", "https://api.together.xyz/v1"],
)
def test_together_openai_compatibility_preserves_public_model_and_usage(
    tmp_path: Path,
    base_url: str,
) -> None:
    model = "deepseek-ai/DeepSeek-V4-Pro-0813"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _chat_response(model)
        usage = payload["usage"]
        assert isinstance(usage, dict)
        usage.pop("prompt_tokens_details", None)
        usage.pop("completion_tokens_details", None)
        usage["cached_tokens"] = 4
        usage["reasoning_tokens"] = 3
        return httpx.Response(200, json=payload, request=request)

    host_label = "current" if base_url == "https://api.together.ai/v1" else "legacy"
    storage = SQLiteStorage(tmp_path / f"together-{host_label}.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url=base_url,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="together-openai"):
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "private-query"}],
            )

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "together"
        assert event.model == model
        assert event.input_tokens == 20
        assert event.output_tokens == 10
        assert event.cached_tokens == 4
        assert event.cost_usd == Decimal("0")
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"] == {
            "name": "together",
            "service": "api",
            "record_id": "chat-provider-1",
        }
        assert observation["resource"] == {"type": "model", "id": model}
        assert {
            line["metric"]: line["quantity"] for line in observation["usage"]
        } == {
            "input_tokens": "16",
            "cache_read_input_tokens": "4",
            "output_tokens": "7",
            "reasoning_output_tokens": "3",
        }
        assert "private-query" not in json.dumps(event.to_dict())
    finally:
        uninstrument_openai()
        client.close()
        storage.close()


@pytest.mark.parametrize(
    ("response_tier", "expected_lane"),
    [("standard", "global_standard"), ("priority", None), (None, None)],
)
def test_mistral_openai_compatibility_only_admits_confirmed_global_standard_lane(
    tmp_path: Path,
    response_tier: str | None,
    expected_lane: str | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _chat_response("mistral-large-latest")
        usage = payload["usage"]
        assert isinstance(usage, dict)
        if response_tier is not None:
            usage["service_tier"] = response_tier
        return httpx.Response(200, json=payload, request=request)

    storage = SQLiteStorage(tmp_path / f"mistral-openai-{response_tier}.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url="https://api.mistral.ai/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="mistral-openai"):
            client.chat.completions.create(
                model="mistral-large-latest",
                messages=[{"role": "user", "content": "private-query"}],
            )

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "mistral"
        assert event.model == "mistral-large-2512"
        assert event.cost_usd == Decimal("0")
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"] == {
            "name": "mistral",
            "service": "api",
            "record_id": "chat-provider-1",
        }
        assert observation["resource"] == {
            "type": "model",
            "id": "mistral-large-2512",
        }
        assert {
            line["metric"]: line["quantity"] for line in observation["usage"]
        } == {
            "input_tokens": "16",
            "cache_read_input_tokens": "4",
            "output_tokens": "7",
            "reasoning_output_tokens": "3",
        }
        dimensions = {
            item["key"]: item["value"]["value"]
            for item in observation["usage"][0]["dimensions"]
        }
        assert dimensions.get("mistral_pricing_lane") == expected_lane
        assert "private-query" not in json.dumps(event.to_dict())
    finally:
        uninstrument_openai()
        client.close()
        storage.close()


def test_mistral_legacy_completions_fail_open_even_with_standard_response_tier(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/completions"
        return httpx.Response(
            200,
            json={
                "id": "completion-mistral-legacy-1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "index": 0,
                        "logprobs": None,
                        "text": "private-output",
                    }
                ],
                "created": 1,
                "model": "mistral-large-latest",
                "object": "text_completion",
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                    "service_tier": "standard",
                },
            },
            request=request,
        )

    storage = SQLiteStorage(tmp_path / "mistral-legacy-completions.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url="https://api.mistral.ai/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="mistral-openai-legacy"):
            client.completions.create(
                model="mistral-large-latest",
                prompt="private-query",
            )

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "mistral"
        assert event.details["attribution_operation_name"] == "openai.completions.create"
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        dimensions = {
            item["key"]: item["value"]["value"]
            for item in observation["usage"][0]["dimensions"]
        }
        assert "mistral_pricing_lane" not in dimensions
        assert "private-query" not in json.dumps(event.to_dict())
    finally:
        uninstrument_openai()
        client.close()
        storage.close()


@pytest.mark.parametrize(
    ("tool_count", "reported_model", "expected_model", "expected_lane"),
    [
        (0, "grok-4.6", "grok-4.6", "priority_short"),
        (1, "grok-4-1-fast-reasoning", "grok-4.3", None),
    ],
)
def test_xai_openai_compatibility_captures_exact_cost_and_safe_pricing_lane(
    tmp_path: Path,
    tool_count: int,
    reported_model: str,
    expected_model: str,
    expected_lane: str | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _chat_response(reported_model)
        payload["service_tier"] = "priority"
        usage = payload["usage"]
        assert isinstance(usage, dict)
        usage["cost_in_usd_ticks"] = 12_345_678
        usage["num_server_side_tools_used"] = tool_count
        if tool_count > 0:
            payload["output"] = [{"type": "web_search_call", "status": "completed"}]
        return httpx.Response(200, json=payload, request=request)

    storage = SQLiteStorage(tmp_path / f"xai-openai-{tool_count}.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url="https://api.x.ai/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="xai-openai"):
            client.chat.completions.create(
                model=reported_model,
                messages=[{"role": "user", "content": "private-query"}],
            )

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "xai"
        assert event.model == expected_model
        assert event.cost_usd == Decimal("0.0012345678")
        assert event.cost_confidence == "exact"
        assert event.pricing_source == "provider_response"
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"] == {
            "name": "xai",
            "service": "api",
            "record_id": "chat-provider-1",
        }
        assert observation["cost_evidence"] == {
            "amount": "0.0012345678",
            "currency": "USD",
            "source": "provider_reported",
            "confidence": "exact",
        }
        dimensions = {
            item["key"]: item["value"]["value"] for item in observation["usage"][0]["dimensions"]
        }
        assert dimensions.get("xai_pricing_lane") == expected_lane
        assert "private-query" not in json.dumps(event.to_dict())
    finally:
        uninstrument_openai()
        client.close()
        storage.close()


@pytest.mark.parametrize(
    ("request_tier", "response_tier", "executed_tools", "expected_lane"),
    [
        ("on_demand", "on_demand", [], "public_sync"),
        ("flex", "flex", [], "public_sync"),
        ("performance", "performance", [], None),
        ("performance", None, [], None),
        ("auto", "auto", [], None),
        ("on_demand", "on_demand", [{"type": "browser_search"}], None),
    ],
)
def test_groq_openai_compatibility_only_admits_public_token_lane(
    tmp_path: Path,
    request_tier: str,
    response_tier: str | None,
    executed_tools: list[dict[str, str]],
    expected_lane: str | None,
) -> None:
    model = "openai/gpt-oss-120b"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _chat_response(model)
        if response_tier is not None:
            payload["service_tier"] = response_tier
        payload["choices"] = [{"message": {"executed_tools": executed_tools}}]
        return httpx.Response(200, json=payload, request=request)

    storage = SQLiteStorage(
        tmp_path / f"groq-openai-{request_tier}-{response_tier}-{len(executed_tools)}.db"
    )
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url="https://api.groq.com/openai/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="groq-openai"):
            client.chat.completions.create(
                model=model,
                service_tier=request_tier,  # type: ignore[arg-type]
                messages=[{"role": "user", "content": "private-query"}],
            )

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "groq"
        assert event.model == model
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"] == {
            "name": "groq",
            "service": "api",
            "record_id": "chat-provider-1",
        }
        dimensions = {
            item["key"]: item["value"]["value"]
            for item in observation["usage"][0]["dimensions"]
        }
        assert dimensions.get("groq_pricing_lane") == expected_lane
        assert "private-query" not in json.dumps(event.to_dict())
    finally:
        uninstrument_openai()
        client.close()
        storage.close()


@pytest.mark.parametrize(
    ("base_url", "requested_tier", "expected_tier"),
    [
        ("https://api.fireworks.ai/inference/v1", None, "default"),
        ("https://api.fireworks.ai/inference/v1", "priority", "priority"),
        ("https://us.api.fireworks.ai/inference/v1", "standard", "default"),
    ],
)
def test_fireworks_openai_compatibility_preserves_exact_model_and_tier(
    tmp_path: Path,
    base_url: str,
    requested_tier: str | None,
    expected_tier: str,
) -> None:
    model = "accounts/fireworks/models/kimi-k3"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json=_chat_response(model), request=request
        )
    )
    storage = SQLiteStorage(tmp_path / f"fireworks-{expected_tier}.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url=base_url,
        http_client=httpx.Client(transport=transport),
    )
    instrument_openai(tracker)
    try:
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": "private-query"}],
        }
        if requested_tier is not None:
            kwargs["extra_body"] = {"service_tier": requested_tier}
        with tracker.task(task_type="fireworks-openai"):
            client.chat.completions.create(**kwargs)  # type: ignore[arg-type]

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "fireworks_ai"
        assert event.model == model
        assert event.cost_usd == Decimal("0")
        dimensions = {
            item["key"]: item["value"]["value"]
            for item in event.details["attribution_dimensions"]
        }
        assert dimensions["service_tier"] == expected_tier
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"] == {
            "name": "fireworks_ai",
            "service": "api",
            "record_id": "chat-provider-1",
        }
        assert observation["resource"] == {"type": "model", "id": model}
        assert {
            item["key"]: item["value"]["value"]
            for item in observation["usage"][0]["dimensions"]
        }["service_tier"] == expected_tier
        assert "private-query" not in json.dumps(event.to_dict())
    finally:
        uninstrument_openai()
        client.close()
        storage.close()


def test_fireworks_openai_compatible_embeddings_keep_exact_resource_identity(
    tmp_path: Path,
) -> None:
    model = "accounts/fireworks/models/qwen3-embedding-8b"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "object": "list",
                "model": model,
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                "usage": {"prompt_tokens": 125, "total_tokens": 125},
            },
            request=request,
        )
    )
    storage = SQLiteStorage(tmp_path / "fireworks-embeddings.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = OpenAI(
        api_key="test",
        base_url="https://api.fireworks.ai/inference/v1",
        http_client=httpx.Client(transport=transport),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="fireworks-embeddings"):
            client.embeddings.create(model=model, input="private embedding input")

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "fireworks_ai"
        assert event.model == model
        assert event.service_name == "embeddings"
        assert event.cost_usd == Decimal("0")
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"]["name"] == "fireworks_ai"
        assert observation["provider"]["service"] == "embeddings"
        assert observation["resource"] == {"type": "model", "id": model}
        assert [(line["metric"], line["quantity"]) for line in observation["usage"]] == [
            ("input_tokens", "125")
        ]
        assert "private embedding input" not in json.dumps(event.to_dict())
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
