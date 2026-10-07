"""Real native response -> paired v3 -> server decimal fixtures; no paid calls."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.instruments import bedrock, openai
from dexcost.instruments._bedrock_converse import converse_call
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

FIXTURE = json.loads((Path(__file__).parents[2] / "tests/fixtures/llm-wave-four.json").read_text())
MESSAGE_CASES = json.loads(
    (Path(__file__).parents[2] / "tests/fixtures/openai-chat-message-admission.json").read_text()
)["cases"]
CHAT = [c for c in FIXTURE["cases"] if c["provider"] == "openai"]
CLAUDE = [c for c in FIXTURE["cases"] if c["provider"] == "aws"]


@pytest.fixture
def tracker(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    instance = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    yield instance
    openai.uninstrument_openai()
    bedrock.uninstrument_bedrock()
    storage.close()


def lane(event):
    if event is None:
        return None
    return next(
        (
            d["value"]["value"]
            for line in event["usage"]
            for d in line["dimensions"]
            if d["key"] in {"direct_llm_pricing_lane", "bedrock_pricing_lane"}
        ),
        None,
    )


def vector(event, case):
    assert event["provider"]["name"] == case["provider"]
    assert event["provider"]["service"] == case["service"]
    assert event["provider"]["record_id"]
    assert event["resource"] == {"type": "model", "id": case["model"]}
    assert {line["metric"]: line["quantity"] for line in event["usage"]} == case["expected_usage"]
    assert lane(event) == case["lane"]


def chat(tracker, case, stream, asynchronous, reason="positive"):
    from openai import AsyncOpenAI, OpenAI

    raw = copy.deepcopy(case["response"])
    request = {**copy.deepcopy(case["request"]), "stream": stream}
    if stream:
        request["stream_options"] = {"include_usage": True}
    if reason == "missing_usage":
        del raw["usage"]
    if reason == "missing_input":
        del raw["usage"]["prompt_tokens"]
    if reason == "wrong_total":
        raw["usage"]["total_tokens"] += 1
    if reason == "tier":
        raw["service_tier"] = "priority"
    if reason == "missing_tier":
        del raw["service_tier"]
    if reason == "cache_overlap":
        raw["usage"]["prompt_tokens_details"]["cached_tokens"] = raw["usage"]["prompt_tokens"]
    if reason == "reasoning_overlap":
        raw["usage"]["completion_tokens_details"]["reasoning_tokens"] = 5001
    if reason == "malformed_details":
        raw["usage"]["prompt_tokens_details"] = "not-an-object"
    if reason in {"audio", "bool_audio"}:
        raw["usage"]["prompt_tokens_details"]["audio_tokens"] = 1 if reason == "audio" else False
    if reason == "tools":
        request["tools"] = []
    if reason == "body_override":
        request["extra_body"] = {"messages": MESSAGE_CASES[3]["messages"]}
    if reason == "missing_model":
        del raw["model"]
    if reason == "missing_id":
        del raw["id"]
    if reason == "unfinished":
        raw["choices"][0]["finish_reason"] = None
    final = {**raw, "object": "chat.completion.chunk", "choices": []}
    if reason == "unfinished":
        final["choices"] = [{"index": 0, "delta": {}, "finish_reason": None}]
    chunks = [
        {
            **raw,
            "object": "chat.completion.chunk",
            "usage": None,
            "choices": [{"index": 0, "delta": {"content": "fixture"}, "finish_reason": "stop"}],
        },
        final,
    ]
    requests = []
    original_request = copy.deepcopy(request)

    def respond(http_request):
        requests.append(http_request)
        assert http_request.url.path == "/v1/chat/completions"
        if reason == "failed":
            return httpx.Response(500, json={"error": {"message": "fixture"}})
        if stream:
            content = (
                "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, content=content, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=raw)

    base = (
        "https://gateway.example/v1"
        if reason == "gateway"
        else ("https://eu.api.openai.com/v1" if reason == "regional" else case["endpoint"])
    )
    openai.instrument_openai(tracker)

    async def call():
        async with AsyncOpenAI(
            api_key="fixture",
            max_retries=0,
            base_url=base,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ) as client:
            if reason == "failed":
                with pytest.raises(Exception, match="fixture"):
                    await client.chat.completions.create(**request)
            else:
                result = await client.chat.completions.create(**request)
                if stream:
                    async for _ in result:
                        if reason == "cancelled":
                            await result.close()
                            break
                else:
                    assert result.choices[0].message.content == "fixture"

    with tracker.task("chat-wave-four") as task:
        if asynchronous:
            asyncio.run(call())
        else:
            with OpenAI(
                api_key="fixture",
                max_retries=0,
                base_url=base,
                http_client=httpx.Client(transport=httpx.MockTransport(respond)),
            ) as client:
                if reason == "failed":
                    with pytest.raises(Exception, match="fixture"):
                        client.chat.completions.create(**request)
                else:
                    result = client.chat.completions.create(**request)
                    if stream:
                        for _ in result:
                            if reason == "cancelled":
                                result.close()
                                break
                    else:
                        assert result.choices[0].message.content == "fixture"
    assert len(requests) == 1
    assert request == original_request
    sent_messages = json.loads(requests[0].content)["messages"]
    assert sent_messages == request.get("extra_body", {}).get("messages", request["messages"])
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    return to_attribution_observation_v3(events[0])


@pytest.mark.parametrize("case", CHAT, ids=lambda c: c["id"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_chat_shared_vector(case, stream, asynchronous, tracker):
    vector(chat(tracker, case, stream, asynchronous), case)


@pytest.mark.parametrize("message_case", MESSAGE_CASES, ids=lambda c: c["id"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_chat_text_only_messages(message_case, stream, asynchronous, tracker):
    case = copy.deepcopy(CHAT[0])
    case["request"]["messages"] = copy.deepcopy(message_case["messages"])
    event = chat(tracker, case, stream, asynchronous)
    assert lane(event) == (case["lane"] if message_case["admitted"] else None)


def test_chat_message_validation_does_not_consume_iterators():
    messages = iter([{"role": "user", "content": "fixture"}])
    assert not openai._chat_messages_text_only(messages)
    assert list(messages) == [{"role": "user", "content": "fixture"}]
    parts = iter([{"type": "text", "text": "fixture"}])
    assert not openai._chat_messages_text_only([{"role": "user", "content": parts}])
    assert list(parts) == [{"type": "text", "text": "fixture"}]
    assert not openai._chat_messages_text_only(None)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_chat_nontext_preserves_native_failure(stream, asynchronous, tracker):
    case = copy.deepcopy(CHAT[0])
    case["request"]["messages"] = copy.deepcopy(MESSAGE_CASES[3]["messages"])
    assert lane(chat(tracker, case, stream, asynchronous, "failed")) is None


@pytest.mark.parametrize(
    "reason",
    [
        "missing_usage",
        "missing_input",
        "wrong_total",
        "tier",
        "missing_tier",
        "cache_overlap",
        "reasoning_overlap",
        "malformed_details",
        "audio",
        "tools",
        "body_override",
        "missing_model",
        "missing_id",
        "unfinished",
        "gateway",
        "regional",
        "failed",
    ],
)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_chat_fail_open(reason, stream, asynchronous, tracker):
    assert lane(chat(tracker, CHAT[0], stream, asynchronous, reason)) is None


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_official_native_parser_normalizes_audio_false_before_capture(
    stream, asynchronous, tracker
):
    # OpenAI's Pydantic parser converts JSON false to integer 0 before native
    # method instrumentation. We validate native objects, not unseen raw JSON.
    vector(chat(tracker, CHAT[0], stream, asynchronous, "bool_audio"), CHAT[0])


def test_raw_chat_validator_rejects_boolean_counter():
    raw = copy.deepcopy(CHAT[0]["response"])
    raw["usage"]["prompt_tokens_details"]["audio_tokens"] = False
    assert not openai._direct_chat_complete(raw)
    raw["usage"]["prompt_tokens_details"]["audio_tokens"] = 0
    raw["usage"]["prompt_tokens"] = False
    assert not openai._direct_chat_complete(raw)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_chat_cancelled(asynchronous, tracker):
    assert lane(chat(tracker, CHAT[0], True, asynchronous, "cancelled")) is None


def claude(tracker, case, stream=False, reason="positive"):
    body, response = copy.deepcopy(case["request"]), copy.deepcopy(case["response"])
    response["ResponseMetadata"] = {"RequestId": "claude-global-request"}
    client = SimpleNamespace(
        meta=SimpleNamespace(region_name="us-east-1"),
        _endpoint=SimpleNamespace(host="https://bedrock-runtime.us-east-1.amazonaws.com"),
    )
    if reason == "region":
        client.meta.region_name = "us-west-2"
    if reason == "endpoint":
        client._endpoint.host = "https://gateway.example"
    if reason == "bare_model":
        body["modelId"] = body["modelId"].replace("global.", "")
    if reason == "geo_profile":
        body["modelId"] = body["modelId"].replace("global.", "us.")
    if reason == "tier":
        response["serviceTier"]["type"] = "reserved"
    if reason == "cache":
        response["usage"]["cacheWriteInputTokens"] = 1
    if reason == "tools":
        body["toolConfig"] = {"tools": []}
    if reason == "multimodal":
        body["messages"][0]["content"] = [{"image": {}}]
    if reason == "missing_tier":
        del response["serviceTier"]
    if reason == "wrong_total":
        response["usage"]["totalTokens"] += 1
    chunks = [
        {"messageStart": {"role": "assistant"}},
        {"messageStop": {"stopReason": response["stopReason"]}},
    ]
    if reason != "missing_terminal":
        chunks.append({"metadata": response})

    def wrapped():
        if reason == "failed":
            raise RuntimeError("native failure")
        return (
            {"ResponseMetadata": response["ResponseMetadata"], "stream": iter(chunks)}
            if stream
            else response
        )

    with tracker.task("claude-wave-four") as task:
        if reason == "failed":
            with pytest.raises(RuntimeError, match="native failure"):
                converse_call(wrapped, client, (), {}, body, tracker, stream)
        else:
            result = converse_call(wrapped, client, (), {}, body, tracker, stream)
            if stream:
                if reason == "cancelled":
                    next(result["stream"])
                    result["stream"].close()
                else:
                    list(result["stream"])
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    return to_attribution_observation_v3(events[0])


@pytest.mark.parametrize("case", CLAUDE, ids=lambda c: c["id"])
@pytest.mark.parametrize("stream", [False, True])
def test_claude_capture(case, stream, tracker):
    event = claude(tracker, case, stream)
    vector(event, case)
    assert event["provider"]["region"] == "us-east-1"


@pytest.mark.parametrize(
    "reason",
    [
        "region",
        "endpoint",
        "bare_model",
        "geo_profile",
        "tier",
        "cache",
        "tools",
        "multimodal",
        "missing_tier",
        "wrong_total",
        "failed",
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_claude_fail_open(reason, stream, tracker):
    assert lane(claude(tracker, CLAUDE[0], stream, reason)) is None


@pytest.mark.parametrize("reason", ["missing_terminal", "cancelled"])
def test_claude_stream_incomplete(reason, tracker):
    assert lane(claude(tracker, CLAUDE[0], True, reason)) is None


@pytest.mark.parametrize("case", CLAUDE, ids=lambda c: c["id"])
def test_real_boto3_claude(case, tracker):
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="fixture",
        aws_secret_access_key="fixture",
    )
    response = {**case["response"], "ResponseMetadata": {"RequestId": "real-claude"}}
    with Stubber(client) as stub:
        stub.add_response("converse", response, case["request"])
        bedrock.instrument_bedrock(tracker)
        with tracker.task("real-claude") as task:
            assert client.converse(**case["request"])["usage"]["totalTokens"] == 1500
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    event = to_attribution_observation_v3(events[0])
    vector(event, case)
    assert event["provider"]["region"] == "us-east-1"
    client.close()
