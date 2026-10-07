"""Native Google response -> paired v3 -> exact server fixture; mocked I/O only."""

import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest
from google.auth.credentials import AnonymousCredentials

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.instruments import gemini
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

genai = pytest.importorskip("google.genai")
from google.genai import types  # noqa: E402

CASES = json.loads(
    (Path(__file__).parents[2] / "tests/fixtures/vertex-wave-five.json").read_text()
)["cases"]


@pytest.fixture
def tracker(tmp_path):
    # Other suites exercise global/default auto-instrumentation. This native
    # fixture owns its patches and must start as cleanly as it finishes.
    gemini.uninstrument_gemini()
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    yield tracker
    gemini.uninstrument_gemini()
    storage.close()


def lane(event):
    return next(
        (
            d["value"]["value"]
            for line in event["usage"]
            for d in line["dimensions"]
            if d["key"] == "vertex_pricing_lane"
        ),
        None,
    )


def vector(event, case):
    assert event["provider"] == {
        "name": "google",
        "service": "vertex_ai",
        "region": "global",
        "record_id": case["id"],
    }
    assert event["resource"] == {"type": "model", "id": case["model"]}
    assert {line["metric"]: line["quantity"] for line in event["usage"]} == case["expected_usage"]
    assert lane(event) == case["lane"]
    for line in event["usage"]:
        assert {
            "key": "cloud_project",
            "value": {"type": "string", "value": case["project"]},
        } in line["dimensions"]


def make_client(case, stream=False, reason="positive", status=200):
    raw = copy.deepcopy(case["response"])
    body = {"model": case["model"], "contents": "private fixture prompt"}
    opts = {"vertexai": True, "project": case["project"], "location": "global"}
    http = {"api_version": "v1"}
    usage = raw["usageMetadata"]
    if reason.startswith("missing:"):
        del usage[reason.split(":")[1]]
    if reason in (
        "PROVISIONED_THROUGHPUT",
        "ON_DEMAND_PRIORITY",
        "ON_DEMAND_FLEX",
        "TRAFFIC_TYPE_UNSPECIFIED",
    ):
        usage["trafficType"] = reason
    if reason == "missing model":
        del raw["modelVersion"]
    if reason == "missing id":
        del raw["responseId"]
    if reason == "alias":
        raw["modelVersion"] += "-preview"
    if reason == "wrong total":
        usage["totalTokenCount"] += 1
    if reason == "overlapping cache":
        usage["cachedContentTokenCount"] = 1001
    if reason == "missing modality":
        del usage["promptTokensDetails"]
    if reason == "audio modality":
        usage["promptTokensDetails"][0]["modality"] = "AUDIO"
    if reason == "contradictory modality":
        usage["promptTokensDetails"][0]["tokenCount"] += 1
    if reason == "unfinished":
        del raw["candidates"][0]["finishReason"]
    if reason == "grounded":
        raw["candidates"][0]["groundingMetadata"] = {}
    if reason == "output media":
        raw["candidates"][0]["content"]["parts"] = [
            {"inlineData": {"mimeType": "image/png", "data": "AA=="}}
        ]
    if reason == "tool usage":
        usage["toolUsePromptTokenCount"] = 1
    if reason == "regional":
        opts["location"] = "us-central1"
    if reason == "gateway":
        http["base_url"] = "https://gateway.example"
    if reason == "custom headers":
        http["headers"] = {"Authorization": "fixture"}
    if reason == "extra body":
        http["extra_body"] = {}
    if reason == "per-call headers":
        body["config"] = {"http_options": {"headers": {"Authorization": "fixture"}}}
    if reason == "explicit cache":
        body["config"] = {
            "cached_content": "projects/fixture-project/locations/global/cachedContents/fixture"
        }
    if reason == "tools":
        body["config"] = {"tools": [{"google_search": {}}]}
    if reason == "input media":
        body["contents"] = [
            {
                "role": "user",
                "parts": [
                    {"text": "safe"},
                    {"inline_data": {"mime_type": "image/png", "data": "AA=="}},
                ],
            }
        ]
    if reason == "cross project model":
        body["model"] = (
            "projects/other-project/locations/global/publishers/google/models/" + body["model"]
        )
    requests = []

    def handler(request):
        requests.append(request)
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "error": {
                        "code": status,
                        "message": "fixture failure",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )
        if stream:
            interim = copy.deepcopy(raw)
            interim["candidates"][0].pop("finishReason", None)
            return httpx.Response(
                200,
                text="data: " + json.dumps(interim) + "\n\ndata: " + json.dumps(raw) + "\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=raw)

    credentials = AnonymousCredentials()
    credentials.token = "fixture"
    client = genai.Client(
        **opts,
        credentials=credentials,
        http_options=types.HttpOptions(
            **http,
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
            httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )
    return client, body, raw, requests


def invoke(tracker, case, stream=False, asynchronous=False, reason="positive"):
    client, body, raw, requests = make_client(case, stream, reason)
    before = copy.deepcopy(body)
    gemini.instrument_gemini(tracker)
    with tracker.task(task_type="vertex-fixture") as task:
        if asynchronous:

            async def run():
                try:
                    if stream:
                        result = [
                            c
                            async for c in await client.aio.models.generate_content_stream(**body)
                        ]
                        assert len(result) == 2
                        return result[-1]
                    return await client.aio.models.generate_content(**body)
                finally:
                    await client.aio.aclose()

            result = asyncio.run(run())
        elif stream:
            result = list(client.models.generate_content_stream(**body))
            assert len(result) == 2
            result = result[-1]
        else:
            result = client.models.generate_content(**body)
    client.close()
    assert result.response_id == raw.get("responseId")
    assert body == before
    assert len(requests) == 1
    events = tracker.storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    assert "private fixture prompt" not in str(events[0].to_dict())
    return to_attribution_observation_v3(events[0]), requests


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_capture(tracker, case, stream, asynchronous):
    event, requests = invoke(tracker, case, stream, asynchronous)
    vector(event, case)
    assert str(requests[0].url).startswith(
        "https://aiplatform.googleapis.com/v1/projects/"
        + case["project"]
        + "/locations/global/publishers/google/models/"
        + case["model"]
    )


NEGATIVES = [
    *[
        "missing:" + key
        for key in (
            "trafficType",
            "promptTokenCount",
            "cachedContentTokenCount",
            "candidatesTokenCount",
            "thoughtsTokenCount",
            "totalTokenCount",
        )
    ],
    "PROVISIONED_THROUGHPUT",
    "ON_DEMAND_PRIORITY",
    "ON_DEMAND_FLEX",
    "TRAFFIC_TYPE_UNSPECIFIED",
    "missing model",
    "missing id",
    "alias",
    "wrong total",
    "overlapping cache",
    "missing modality",
    "audio modality",
    "contradictory modality",
    "unfinished",
    "grounded",
    "output media",
    "tool usage",
    "regional",
    "gateway",
    "custom headers",
    "extra body",
    "per-call headers",
    "explicit cache",
    "tools",
    "input media",
    "cross project model",
]


@pytest.mark.parametrize("reason", NEGATIVES)
@pytest.mark.parametrize("stream", [False, True])
def test_native_unknown_is_unpriced(tracker, reason, stream):
    event, _ = invoke(tracker, CASES[0], stream, False, reason)
    assert lane(event) is None


def test_native_error_preserved(tracker):
    client, body, _, _ = make_client(CASES[0], status=400)
    gemini.instrument_gemini(tracker)
    try:
        with (
            tracker.task(task_type="vertex-failure") as task,
            pytest.raises(Exception, match="fixture failure"),
        ):
            client.models.generate_content(**body)
    finally:
        client.close()
    assert all(
        lane(to_attribution_observation_v3(e)) is None
        for e in tracker.storage.query_events(task_id=str(task.task_id))
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_early_close_is_cancelled(tracker, asynchronous):
    client, body, _, _ = make_client(CASES[0], stream=True)
    gemini.instrument_gemini(tracker)
    with tracker.task(task_type="vertex-cancel") as task:
        if asynchronous:

            async def run():
                stream = await client.aio.models.generate_content_stream(**body)
                await anext(stream)
                await stream.aclose()
                await client.aio.aclose()

            asyncio.run(run())
        else:
            stream = client.models.generate_content_stream(**body)
            next(stream)
            stream.close()
    client.close()
    events = tracker.storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    assert to_attribution_observation_v3(events[0])["operation"]["status"] == "cancelled"


def test_text_validation_does_not_consume_one_shot_iterable():
    seen = []

    def values():
        seen.append(True)
        yield "text"

    assert not gemini._vertex_text(values())
    assert seen == []


def test_two_native_clients_keep_project_and_region_isolated(tracker):
    first, body_a, _, _ = make_client(CASES[0])
    second, body_b, _, _ = make_client(
        {**CASES[0], "project": "second-project"}, reason="regional"
    )
    gemini.instrument_gemini(tracker)
    with tracker.task(task_type="vertex-isolation") as task:

        async def run():
            try:
                await asyncio.gather(
                    first.aio.models.generate_content(**body_a),
                    second.aio.models.generate_content(**body_b),
                )
            finally:
                await first.aio.aclose()
                await second.aio.aclose()

        asyncio.run(run())
    first.close()
    second.close()
    events = [
        to_attribution_observation_v3(e)
        for e in tracker.storage.query_events(task_id=str(task.task_id))
    ]
    admitted = [e for e in events if lane(e) is not None]
    assert len(admitted) == 1
    vector(admitted[0], CASES[0])


def test_async_eligibility_snapshot_precedes_native_io(tracker, monkeypatch):
    client, body, raw, _ = make_client(CASES[0], reason="regional")

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def native_response(**_kwargs):
            started.set()
            await release.wait()
            return types.GenerateContentResponse.model_validate(raw)

        monkeypatch.setattr(client.aio.models, "generate_content", native_response)
        # Class instrumentation cannot patch an instance override; invoke the same
        # production wrapper explicitly around that controlled native boundary.
        pending = asyncio.create_task(
            gemini._async_direct_call(
                native_response,
                client.aio.models,
                (),
                body,
                operation="google.genai.models.generate_content",
                component="llm",
                event_type="llm_call",
                extract=lambda response, kwargs, vertex: gemini._content_measurement(
                    response, kwargs, vertex=vertex
                ),
            )
        )
        await started.wait()
        client.aio.models._api_client.location = "global"
        client.aio.models._api_client._http_options.base_url = "https://aiplatform.googleapis.com/"
        release.set()
        await pending
        await client.aio.aclose()

    gemini.instrument_gemini(tracker)
    with tracker.task(task_type="vertex-inflight") as task:
        asyncio.run(run())
    client.close()
    events = tracker.storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    assert lane(to_attribution_observation_v3(events[0])) is None


def test_raw_helper_rejects_bool_without_claiming_native_precoercion_validation():
    # The official Pydantic model can normalize wire scalars. Admission validates
    # the native parsed model, not inaccessible raw transport JSON.
    from dexcost.instruments._provider_metering import OperationMeasurement

    response = types.GenerateContentResponse.model_validate(CASES[0]["response"])
    raw = response.model_dump(exclude_none=True)
    raw["usage_metadata"]["cached_content_token_count"] = False
    measured = OperationMeasurement(pricing_usage=None, usage_lines=())
    assert gemini._admit_vertex_content(measured, raw, "fixture-project") is measured
