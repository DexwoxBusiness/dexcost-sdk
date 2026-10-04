"""Paired raw provider fixtures, real packages and mocked network only."""

import asyncio
import copy
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.instruments import bedrock, perplexity
from dexcost.instruments._bedrock_converse import converse_call
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

FIXTURE = json.loads(
    (Path(__file__).parents[2] / "tests/fixtures/bedrock-perplexity-wave-three.json").read_text()
)


@pytest.fixture
def tracker(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    instance = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    yield instance
    perplexity.uninstrument_perplexity()
    bedrock.uninstrument_bedrock()
    storage.close()


def _lane(observation):
    return next(
        (
            d["value"]["value"]
            for line in observation["usage"]
            for d in line.get("dimensions", [])
            if d["key"] == "bedrock_pricing_lane"
        ),
        None,
    )


def _nova(tracker, case, stream=False, reason=None):
    body, response = copy.deepcopy(case["request"]), copy.deepcopy(case["response"])
    response["ResponseMetadata"] = {"RequestId": "nova-request"}
    client = SimpleNamespace(
        meta=SimpleNamespace(region_name="us-east-1"),
        _endpoint=SimpleNamespace(host="https://bedrock-runtime.us-east-1.amazonaws.com"),
    )
    if reason == "region":
        client.meta.region_name = "us-west-2"
    if reason == "endpoint":
        client._endpoint.host = "https://gateway.example"
    if reason == "profile":
        body["modelId"] = "us.amazon.nova-micro-v1:0"
    if reason == "tier":
        response["serviceTier"]["type"] = "priority"
    if reason == "latency":
        response["performanceConfig"]["latency"] = "optimized"
    if reason == "cache":
        response["usage"]["cacheReadInputTokens"] = 1
    if reason == "cache_bool":
        response["usage"]["cacheReadInputTokens"] = False
    if reason == "usage":
        del response["usage"]["inputTokens"]
    if reason == "total":
        response["usage"]["totalTokens"] += 1
    if reason == "guardrail":
        body["guardrailConfig"] = {"guardrailIdentifier": "id"}
    if reason == "multimodal":
        body["messages"][0]["content"] = [{"image": {}}]
    if reason == "missing_tier":
        del response["serviceTier"]
    if stream:
        raw = {
            "ResponseMetadata": response["ResponseMetadata"],
            "stream": iter(
                [
                    {"messageStop": {"stopReason": response["stopReason"]}},
                    {"metadata": response},
                ]
            ),
        }
    else:
        raw = response
    with tracker.task("nova-fixture") as task:
        result = converse_call(lambda: raw, client, (), {}, body, tracker, stream)
        if stream:
            list(result["stream"])
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    return events[0], to_attribution_observation_v3(events[0])


@pytest.mark.parametrize("case", FIXTURE["bedrock_cases"], ids=lambda case: case["id"])
@pytest.mark.parametrize("stream", [False, True])
def test_nova_paired_capture(case, stream, tracker):
    event, observation = _nova(tracker, case, stream)
    assert observation["provider"] == {
        "name": "aws",
        "service": "bedrock",
        "record_id": "nova-request",
    }
    assert observation["resource"] == {"type": "model", "id": case["model"]}
    assert {line["metric"]: line["quantity"] for line in observation["usage"]} == case[
        "expected_usage"
    ]
    assert _lane(observation) == "us_east_1_nova_standard_no_cache"
    assert event.pricing_source == "unknown"


@pytest.mark.parametrize(
    "reason",
    [
        "region",
        "endpoint",
        "profile",
        "tier",
        "latency",
        "cache",
        "cache_bool",
        "usage",
        "total",
        "guardrail",
        "multimodal",
        "missing_tier",
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_nova_fail_open(reason, stream, tracker):
    _, observation = _nova(tracker, FIXTURE["bedrock_cases"][0], stream, reason)
    assert _lane(observation) is None


def test_real_boto3_converse(tracker):
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="fixture",
        aws_secret_access_key="fixture",
    )
    case = FIXTURE["bedrock_cases"][0]
    response = {
        **case["response"],
        "metrics": {"latencyMs": 1},
        "output": {"message": {"role": "assistant", "content": [{"text": "fixture"}]}},
        "ResponseMetadata": {"RequestId": "native-nova"},
    }
    with Stubber(client) as stub:
        stub.add_response("converse", response, case["request"])
        bedrock.instrument_bedrock(tracker)
        with tracker.task("native-nova") as task:
            result = client.converse(**case["request"])
        assert result["usage"]["totalTokens"] == 1250
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert _lane(to_attribution_observation_v3(event)) == "us_east_1_nova_standard_no_cache"
    client.close()


def test_real_perplexity_background_zero(tracker):
    from perplexity import Perplexity

    raw = copy.deepcopy(FIXTURE["perplexity_response"])
    raw["usage"]["cost"]["total_cost"] = 0

    def respond(request):
        result = {**raw, "status": "queued", "usage": None} if request.method == "POST" else raw
        return httpx.Response(200, json=result)

    perplexity.instrument_perplexity(tracker)
    with Perplexity(
        api_key="fixture", http_client=httpx.Client(transport=httpx.MockTransport(respond))
    ) as client:
        with tracker.task("background-zero") as task:
            client.responses.create(model=raw["model"], input="fixture", background=True)
            pending = tracker._storage.get_provider_job("perplexity", "responses", raw["id"])
            assert pending.cost_amount is None
            client.responses.retrieve(raw["id"])
            final = tracker._storage.get_provider_job("perplexity", "responses", raw["id"])
            assert final.cost_amount == Decimal(0)
            assert final.cost_source == "provider_reported"
            client.responses.retrieve(raw["id"])
            assert (
                tracker._storage.get_provider_job("perplexity", "responses", raw["id"]).revision
                == final.revision
            )
        assert tracker._storage.query_events(task_id=str(task.task_id)) == []


@pytest.mark.parametrize("field", ["serviceTier", "performanceConfig", "ResponseMetadata"])
def test_null_bedrock_metadata_does_not_replace_native_success(field, tracker):
    case = FIXTURE["bedrock_cases"][0]
    response = copy.deepcopy(case["response"])
    response["ResponseMetadata"] = {"RequestId": "native-nova"}
    response[field] = None
    client = SimpleNamespace(
        meta=SimpleNamespace(region_name="us-east-1"),
        _endpoint=SimpleNamespace(host="https://bedrock-runtime.us-east-1.amazonaws.com"),
    )
    with tracker.task("nullable-metadata") as task:
        assert (
            converse_call(lambda: response, client, (), {}, case["request"], tracker, False)
            is response
        )
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert _lane(to_attribution_observation_v3(event)) is None


@pytest.mark.parametrize(
    "reason", ["positive", "zero", "missing", "currency", "incomplete", "malformed", "gateway"]
)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_perplexity_agent(reason, stream, asynchronous, tracker):
    from perplexity import AsyncPerplexity, Perplexity

    raw = copy.deepcopy(FIXTURE["perplexity_response"])
    if reason == "zero":
        raw["usage"]["cost"]["total_cost"] = 0
    if reason == "missing":
        del raw["usage"]["cost"]["total_cost"]
    if reason == "currency":
        raw["usage"]["cost"]["currency"] = "EUR"
    if reason == "incomplete":
        raw["status"] = "in_progress"
    if reason == "malformed":
        raw["usage"]["cost"]["total_cost"] = True
    requests = []

    def respond(request):
        requests.append(request.url)
        if stream:
            content = (
                "event: response.completed\ndata: "
                + json.dumps({"type": "response.completed", "response": raw})
                + "\n\ndata: [DONE]\n\n"
            )
            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=raw)

    endpoint = "https://gateway.example" if reason == "gateway" else "https://api.perplexity.ai"
    perplexity.instrument_perplexity(tracker)
    with tracker.task("native-perplexity") as task:
        if asynchronous:

            async def call():
                async with AsyncPerplexity(
                    api_key="fixture",
                    base_url=endpoint,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
                ) as client:
                    result = await client.responses.create(
                        model=raw["model"], input="fixture", stream=stream
                    )
                    if stream:
                        async for _ in result:
                            pass

            asyncio.run(call())
        else:
            with Perplexity(
                api_key="fixture",
                base_url=endpoint,
                http_client=httpx.Client(transport=httpx.MockTransport(respond)),
            ) as client:
                result = client.responses.create(
                    model=raw["model"], input="fixture", stream=stream
                )
                if stream:
                    list(result)
    assert len(requests) == 1
    assert requests[0].path == "/v1/responses"
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    event = events[0]
    observation = to_attribution_observation_v3(event)
    assert observation is not None
    assert observation["provider"] == {
        "name": "perplexity", "service": "responses", "record_id": raw["id"]
    }
    assert event.input_tokens == 5870
    assert event.output_tokens == 679
    assert event.details["attribution_usage_lines"] == [
        {"metric": "request_count", "quantity": "1", "unit": "Requests"}
    ]
    if reason in {"positive", "zero"}:
        assert event.pricing_source == "provider_response"
        assert event.cost_usd == Decimal("0" if reason == "zero" else "0.02665")
        assert event.details["provider_reported_cost_usd"] == (
            "0" if reason == "zero" else "0.02665"
        )
        assert observation["cost_evidence"] == {
            "amount": "0" if reason == "zero" else "0.02665",
            "currency": "USD",
            "source": "provider_reported",
            "confidence": "exact",
        }
    else:
        assert event.pricing_source == "unknown"
        assert "cost_evidence" not in observation
