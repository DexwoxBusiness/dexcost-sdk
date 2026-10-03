"""Raw provider response -> native capture -> v3 shared server pricing vectors."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.instruments import anthropic, gemini, openai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

FIXTURE = json.loads(
    (Path(__file__).parents[2] / "tests/fixtures/direct-foundation-pricing.json").read_text()
)


def objects(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: objects(item) for key, item in value.items()})
    if isinstance(value, list):
        return [objects(item) for item in value]
    return value


def capture(case, tracker, stream=False, asynchronous=False):
    provider = case["provider"]
    body = {"model": case["model"], "stream": stream}
    raw = copy.deepcopy(case["response"])
    if provider == "google":

        def snake(value):
            import re

            if isinstance(value, dict):
                return {
                    re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): snake(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [snake(item) for item in value]
            return value

        raw = snake(raw)
    response = objects(raw)
    module = {"openai": openai, "anthropic": anthropic, "google": gemini}[provider]
    module._active_tracker = tracker
    endpoint = case["endpoint"]
    resource = objects(
        {
            "_client": {"base_url": endpoint},
            "_api_client": {"vertexai": False, "_http_options": {"base_url": endpoint}},
        }
    )
    options = {}
    chunks = [response]
    if provider == "openai":
        chunks = [objects({"type": "response.completed", "response": raw})]
        wrapper = openai._routed_wrapper(
            openai._async_responses_create_wrapper
            if asynchronous
            else openai._sync_responses_create_wrapper
        )
    elif provider == "anthropic":
        chunks = [
            objects({"type": "message_start", "message": raw}),
            objects(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": raw.get("stop_reason")},
                    "usage": {"output_tokens": 50},
                }
            ),
            objects({"type": "message_stop"}),
        ]
        wrapper = (
            anthropic._async_message_create_wrapper
            if asynchronous
            else anthropic._sync_message_create_wrapper
        )
        options = {
            "task_type": "anthropic.messages",
            "service_name": "messages",
            "operation_name": "anthropic.messages.create",
        }
    elif stream:
        wrapper = gemini._async_stream_call if asynchronous else gemini._sync_stream_call
    else:
        wrapper = gemini._async_direct_call if asynchronous else gemini._sync_direct_call
        options = {
            "operation": "google.genai.models.generate_content",
            "component": "llm",
            "event_type": "llm_call",
            "extract": gemini._content_extract,
        }

    def wrapped(**_):
        return iter(chunks) if stream else response

    async def async_chunks():
        for chunk in chunks:
            yield chunk

    async def async_wrapped(**_):
        return async_chunks() if stream else response

    async def consume():
        returned = await wrapper(async_wrapped, resource, (), body, **options)
        if stream:
            async for _chunk in returned:
                pass

    with tracker.task("direct-foundation") as task:
        if asynchronous:
            asyncio.run(consume())
        else:
            returned = wrapper(wrapped, resource, (), body, **options)
            if stream:
                list(returned)
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    return to_attribution_observation_v3(event)


@pytest.fixture
def tracker(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    instance = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    yield instance
    for module in (openai, anthropic, gemini):
        module._active_tracker = None
    storage.close()


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_raw_capture_matches_server_price_vector(case, stream, asynchronous, tracker):
    observation = capture(case, tracker, stream, asynchronous)
    assert observation is not None
    assert observation["component"] == "llm"
    assert observation["provider"]["name"] == case["provider"]
    assert observation["provider"]["service"] == case["service"]
    assert observation["provider"].get("record_id")
    assert observation["resource"] == {"type": "model", "id": case["model"]}
    assert {line["metric"]: line["quantity"] for line in observation["usage"]} == case[
        "expected_usage"
    ]
    for line in observation["usage"]:
        assert {
            "key": "direct_llm_pricing_lane",
            "value": {"type": "string", "value": case["lane"]},
        } in line["dimensions"]


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
@pytest.mark.parametrize("reason", ["route", "tier"])
def test_unknown_route_or_tier_stays_unpriced(provider, reason, tracker):
    case = copy.deepcopy(next(item for item in FIXTURE["cases"] if item["provider"] == provider))
    if reason == "route":
        case["endpoint"] = "https://proxy.example.invalid"
    elif provider == "openai":
        case["response"].pop("service_tier")
    elif provider == "anthropic":
        case["response"]["usage"].pop("service_tier")
    else:
        case["response"]["usageMetadata"].pop("serviceTier")
    observation = capture(case, tracker)
    assert observation is not None
    assert not any(
        d["key"] == "direct_llm_pricing_lane"
        for line in observation["usage"]
        for d in line["dimensions"]
    )


@pytest.mark.parametrize("case", FIXTURE["fail_open_cases"], ids=lambda case: case["id"])
@pytest.mark.parametrize("stream", [False, True])
def test_unsupported_or_inconsistent_billing_stays_unpriced(case, stream, tracker):
    observation = capture(case, tracker, stream)
    if case["id"] in {"unknown-openai-cache-overlap", "unknown-openai-reasoning-overflow"}:
        assert observation is None  # Existing strict usage diagnostics suppress invalid v3.
        return
    assert observation is not None
    assert not any(
        d["key"] == "direct_llm_pricing_lane"
        for line in observation["usage"]
        for d in line["dimensions"]
    )
