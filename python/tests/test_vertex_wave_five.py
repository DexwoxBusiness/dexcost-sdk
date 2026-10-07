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


@pytest.fixture(autouse=True)
def native_httpx_io(monkeypatch):
    # Keep the pinned native client/transport and redirect pipeline. Only the
    # network I/O boundary is mocked; MockTransport is an explicit negative.
    gemini.uninstrument_gemini()
    original_send, original_async_send = httpx.Client.send, httpx.AsyncClient.send

    def send(transport, request):
        return transport._fixture_handler(request)

    async def send_async(transport, request):
        return transport._fixture_handler(request)

    def native_send(client, *args, **kwargs):
        response = original_send(client, *args, **kwargs)
        if response.extensions.get("fixture_missing_route"):
            response._request = None
        return response

    async def native_async_send(client, *args, **kwargs):
        response = await original_async_send(client, *args, **kwargs)
        if response.extensions.get("fixture_missing_route"):
            response._request = None
        return response

    monkeypatch.setattr(httpx.Client, "send", native_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", native_async_send)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send_async)
    yield
    gemini.uninstrument_gemini()


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
        if reason == "redirect" and len(requests) == 1:
            return httpx.Response(307, headers={"location": "https://gateway.example/replay"})
        if reason == "same-route redirect" and len(requests) == 1:
            return httpx.Response(307, headers={"location": str(request.url)})
        if reason == "recovered transport" and len(requests) == 1:
            return httpx.Response(503, json={"error": {"code": 503, "message": "retry"}})
        if reason == "wrong final URL":
            request.url = httpx.URL("https://gateway.example/replay")
        if reason == "changed method":
            request.method = "GET"
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
                extensions={"fixture_missing_route": reason == "missing final URL"},
            )
        return httpx.Response(
            200,
            json=raw,
            extensions={"fixture_missing_route": reason == "missing final URL"},
        )

    credentials = AnonymousCredentials()
    credentials.token = "fixture"
    transport = httpx.HTTPTransport()
    async_transport = httpx.AsyncHTTPTransport()
    transport._fixture_handler = handler
    async_transport._fixture_handler = handler
    if reason == "custom transport":
        transport = httpx.MockTransport(handler)
        async_transport = httpx.MockTransport(handler)
    sync_type, async_type = httpx.Client, httpx.AsyncClient
    if reason == "custom client":

        class CustomClient(httpx.Client):
            pass

        class CustomAsyncClient(httpx.AsyncClient):
            pass

        sync_type, async_type = CustomClient, CustomAsyncClient
    if reason == "recovered transport":
        http["retry_options"] = {
            "attempts": 2,
            "initial_delay": 0.001,
            "max_delay": 0.001,
            "jitter": 0,
        }
    sync_client = sync_type(transport=transport, follow_redirects=True)
    async_client = async_type(transport=async_transport, follow_redirects=True)
    if reason == "custom response hook":
        sync_client.event_hooks["response"].append(lambda response: None)

        async def hook(response):
            pass

        async_client.event_hooks["response"].append(hook)
    client = genai.Client(
        **opts,
        credentials=credentials,
        http_options=types.HttpOptions(
            **http,
            httpx_client=sync_client,
            httpx_async_client=async_client,
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
    assert len(requests) == (
        2
        if reason
        in (
            "redirect",
            "same-route redirect",
            "recovered transport",
        )
        else 1
    )
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


@pytest.mark.parametrize(
    "reason",
    [
        "wrong final URL",
        "missing final URL",
        "changed method",
        "redirect",
        "same-route redirect",
        "recovered transport",
        "custom transport",
        "custom client",
        "custom response hook",
    ],
)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_unverified_native_route_preserves_response_without_pricing(
    tracker, reason, stream, asynchronous
):
    event, _ = invoke(tracker, CASES[0], stream, asynchronous, reason)
    assert lane(event) is None


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_api_replay_without_transport_is_unpriced(
    tracker, monkeypatch, stream, asynchronous
):
    from google.genai._api_client import HttpResponse

    client, body, raw, requests = make_client(CASES[0], stream=stream)

    def replay(*_args, **_kwargs):
        return HttpResponse({}, [json.dumps(raw)])

    async def replay_async(*_args, **_kwargs):
        return replay()

    monkeypatch.setattr(client._api_client, "_request_once", replay)
    monkeypatch.setattr(client._api_client, "_async_request_once", replay_async)
    gemini.instrument_gemini(tracker)
    with tracker.task(task_type="vertex-replay") as task:
        if asynchronous:

            async def run():
                try:
                    if stream:
                        chunks = [
                            c
                            async for c in await client.aio.models.generate_content_stream(**body)
                        ]
                        assert len(chunks) == 1
                        return chunks[0]
                    return await client.aio.models.generate_content(**body)
                finally:
                    await client.aio.aclose()

            result = asyncio.run(run())
        elif stream:
            chunks = list(client.models.generate_content_stream(**body))
            assert len(chunks) == 1
            result = chunks[0]
        else:
            result = client.models.generate_content(**body)
    client.close()
    assert result.response_id == raw["responseId"]
    assert requests == []
    events = tracker.storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    assert lane(to_attribution_observation_v3(events[0])) is None


@pytest.mark.parametrize("transport", ["aiohttp", "mtls"])
def test_unobserved_native_backend_is_ineligible(monkeypatch, transport):
    from google.genai import _api_client

    client, body, _, _ = make_client(CASES[0])
    api = client._api_client
    if transport == "aiohttp":
        # The native SDK selects aiohttp automatically when available and no
        # explicit async HTTPX client was supplied. Do not force a different one.
        monkeypatch.setattr(_api_client, "has_aiohttp", True)
        api._http_options.httpx_async_client = None
        assert api._use_aiohttp()
    else:
        monkeypatch.setattr(_api_client.BaseApiClient, "_use_google_auth_sync", lambda _: True)
    try:
        project = gemini._vertex_project(client.models, body)
        assert project == CASES[0]["project"]
        assert (
            gemini._vertex_transport_evidence(
                client.models,
                project,
                asynchronous=transport == "aiohttp",
            )
            is None
        )
    finally:
        client.close()


def test_transport_observers_restore_and_reinstall_on_same_client(tracker):
    client, body, _, requests = make_client(CASES[0])
    originals = (
        httpx.Client.send,
        httpx.AsyncClient.send,
        httpx.HTTPTransport.handle_request,
        httpx.AsyncHTTPTransport.handle_async_request,
    )
    try:
        for _ in range(2):
            gemini.instrument_gemini(tracker)
            assert gemini._vertex_project(client.models, body) == CASES[0]["project"]
            with tracker.task(task_type="vertex-reinstall") as task:
                client.models.generate_content(**body)
            events = tracker.storage.query_events(task_id=str(task.task_id))
            vector(to_attribution_observation_v3(events[0]), CASES[0])
            assert gemini._vertex_transport_context.get() is None
            gemini.uninstrument_gemini()
            assert originals == (
                httpx.Client.send,
                httpx.AsyncClient.send,
                httpx.HTTPTransport.handle_request,
                httpx.AsyncHTTPTransport.handle_async_request,
            )
        assert len(requests) == 2
    finally:
        client.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("consume_first", [False, True])
@pytest.mark.parametrize("reinstall", [False, True])
def test_retired_stream_evidence_never_reactivates(
    tracker, tmp_path, asynchronous, consume_first, reinstall
):
    client, body, raw, requests = make_client(CASES[0], stream=True)
    next_storage = SQLiteStorage(str(tmp_path / "next-events.db"))
    next_tracker = CostTracker(
        storage=next_storage, auto_update_pricing=False, auto_instrument=[]
    )
    gemini.instrument_gemini(tracker)

    def retire(evidence):
        assert len(requests) == int(consume_first)
        assert evidence.verified is consume_first
        gemini.uninstrument_gemini()
        assert evidence.admitted_project() is None
        if reinstall:
            gemini.instrument_gemini(next_tracker)

    def check_retired(evidence, chunks):
        assert len(chunks) == 2
        assert all(chunk.response_id == raw["responseId"] for chunk in chunks)
        # A new generation's HTTPX observers must not populate an old proof.
        assert evidence.sends == evidence.attempts == int(consume_first)
        assert evidence.admitted_project() is None

    try:
        if asynchronous:

            async def run():
                try:
                    stream = await client.aio.models.generate_content_stream(**body)
                    evidence = stream._stream._evidence
                    chunks = [await anext(stream)] if consume_first else []
                    retire(evidence)
                    chunks.extend([chunk async for chunk in stream])
                    check_retired(evidence, chunks)
                    if reinstall:
                        fresh = await client.aio.models.generate_content_stream(**body)
                        assert len([chunk async for chunk in fresh]) == 2
                finally:
                    await client.aio.aclose()

            asyncio.run(run())
        else:
            stream = client.models.generate_content_stream(**body)
            evidence = stream._stream._evidence
            chunks = [next(stream)] if consume_first else []
            retire(evidence)
            chunks.extend(stream)
            check_retired(evidence, chunks)
            if reinstall:
                assert len(list(client.models.generate_content_stream(**body))) == 2

        old_events = tracker.storage.query_events()
        new_events = next_storage.query_events()
        assert len(old_events) == 1
        old_event = to_attribution_observation_v3(old_events[0])
        assert old_event["operation"]["status"] == "succeeded"
        assert old_event["usage"]
        assert lane(old_event) is None
        assert len(new_events) == int(reinstall)
        if reinstall:
            vector(to_attribution_observation_v3(new_events[0]), CASES[0])
        assert len(requests) == 1 + int(reinstall)
        assert gemini._vertex_transport_context.get() is None
    finally:
        gemini.uninstrument_gemini()
        next_storage.close()
        client.close()


@pytest.mark.parametrize("gemini_first", [False, True])
def test_http_capture_uninstalled_out_of_order_does_not_reactivate_observers(
    tracker, gemini_first
):
    from dexcost.adapters import http

    client, body, _, requests = make_client(CASES[0])
    try:
        if gemini_first:
            gemini.instrument_gemini(tracker)
            http.track_http()
        else:
            http.track_http()
            gemini.instrument_gemini(tracker)
        gemini.uninstrument_gemini()
        http.untrack_http()
        gemini.instrument_gemini(tracker)
        # Real HTTP auto-capture can coexist with the provider-specific wrapper.
        http.track_http()
        with tracker.task(task_type="vertex-wrapper-lifecycle") as task:
            client.models.generate_content(**body)
        assert len(requests) == 1
        events = tracker.storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        vector(to_attribution_observation_v3(events[0]), CASES[0])
    finally:
        http.untrack_http()
        gemini.uninstrument_gemini()
        client.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
def test_retained_public_method_cannot_acquire_new_generation_proof(
    tracker, tmp_path, asynchronous, streamed
):
    client, body, raw, requests = make_client(CASES[0], stream=streamed)
    next_storage = SQLiteStorage(str(tmp_path / "next-retained-events.db"))
    next_tracker = CostTracker(
        storage=next_storage, auto_update_pricing=False, auto_instrument=[]
    )
    gemini.instrument_gemini(tracker)
    models = client.aio.models if asynchronous else client.models
    method_name = "generate_content_stream" if streamed else "generate_content"
    retained = getattr(models, method_name)
    gemini.uninstrument_gemini()
    gemini.instrument_gemini(next_tracker)

    def check_retained():
        assert tracker.storage.query_events() == []
        events = next_storage.query_events()
        assert len(events) == 1
        event = to_attribution_observation_v3(events[0])
        assert event["operation"]["status"] == "succeeded"
        assert event["usage"]
        assert lane(event) is None

    try:
        if asynchronous:

            async def run():
                try:
                    result = await retained(**body)
                    chunks = [chunk async for chunk in result] if streamed else [result]
                    assert all(chunk.response_id == raw["responseId"] for chunk in chunks)
                    assert len(chunks) == (2 if streamed else 1)
                    check_retained()
                    fresh = await getattr(models, method_name)(**body)
                    if streamed:
                        assert len([chunk async for chunk in fresh]) == 2
                    else:
                        assert fresh.response_id == raw["responseId"]
                finally:
                    await client.aio.aclose()

            asyncio.run(run())
        else:
            result = retained(**body)
            chunks = list(result) if streamed else [result]
            assert all(chunk.response_id == raw["responseId"] for chunk in chunks)
            assert len(chunks) == (2 if streamed else 1)
            check_retained()
            fresh = getattr(models, method_name)(**body)
            if streamed:
                assert len(list(fresh)) == 2
            else:
                assert fresh.response_id == raw["responseId"]
        assert tracker.storage.query_events() == []
        events = [to_attribution_observation_v3(event) for event in next_storage.query_events()]
        assert len(events) == 2
        admitted = [event for event in events if lane(event) is not None]
        assert len(admitted) == 1
        vector(admitted[0], CASES[0])
        assert len(requests) == 2
    finally:
        gemini.uninstrument_gemini()
        next_storage.close()
        client.close()


def test_concurrent_same_client_calls_have_separate_transport_evidence(tracker):
    client, body, _, requests = make_client(CASES[0])
    gemini.instrument_gemini(tracker)

    async def call():
        with tracker.task(task_type="vertex-concurrent") as task:
            result = await client.aio.models.generate_content(**body)
        assert result.response_id == CASES[0]["response"]["responseId"]
        assert gemini._vertex_transport_context.get() is None
        return tracker.storage.query_events(task_id=str(task.task_id))

    async def run():
        try:
            return await asyncio.gather(call(), call())
        finally:
            await client.aio.aclose()

    try:
        groups = asyncio.run(run())
        assert len(requests) == 2
        for events in groups:
            assert len(events) == 1
            vector(to_attribution_observation_v3(events[0]), CASES[0])
    finally:
        client.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_repeated_native_requests_remain_admitted_after_sdk_adds_auth(tracker, asynchronous):
    client, body, _, requests = make_client(CASES[0])
    gemini.instrument_gemini(tracker)
    try:
        with tracker.task(task_type="vertex-repeated") as task:
            if asynchronous:

                async def run():
                    try:
                        for _ in range(2):
                            await client.aio.models.generate_content(**body)
                    finally:
                        await client.aio.aclose()

                asyncio.run(run())
            else:
                for _ in range(2):
                    client.models.generate_content(**body)
        events = tracker.storage.query_events(task_id=str(task.task_id))
        assert len(events) == len(requests) == 2
        for event in events:
            vector(to_attribution_observation_v3(event), CASES[0])
    finally:
        client.close()


@pytest.mark.parametrize(
    "header,value,token,quota,admitted",
    [
        ("Authorization", "Bearer fixture", "fixture", None, True),
        ("Authorization", "Bearer stale", "fixture", None, False),
        ("Authorization", "Bearer fixture", None, None, False),
        ("Authorization", "Bearer ", "", None, False),
        ("X-Goog-User-Project", "billing-project", "fixture", "billing-project", True),
        ("X-Goog-User-Project", "other-project", "fixture", "billing-project", False),
        ("X-Goog-User-Project", "billing-project", "fixture", None, False),
    ],
)
def test_only_exact_native_credential_headers_are_eligible(header, value, token, quota, admitted):
    client, body, _, _ = make_client(CASES[0])
    client._api_client._credentials.token = token
    client._api_client._credentials._quota_project_id = quota
    client._api_client._http_options.headers[header] = value
    try:
        assert (gemini._vertex_project(client.models, body) is not None) is admitted
    finally:
        client.close()


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
                vertex_generation=gemini._vertex_transport_generation,
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
