"""Provider-native paid/trial isolation and raw response -> canonical v3 fixtures."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.instruments import cohere, gemini
from dexcost.provider_billing import _has_paid_billing, bind_provider_billing
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

CASES = json.loads(
    (Path(__file__).parents[2] / "tests/fixtures/direct-paid-pricing.json").read_text()
)["cases"]


class Native:
    pass


def objects(value):
    if isinstance(value, dict):
        return SimpleNamespace(
            **{
                re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): objects(item)
                for key, item in value.items()
            }
        )
    return [objects(item) for item in value] if isinstance(value, list) else value


@pytest.fixture
def tracker(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    cohere._active_tracker = gemini._active_tracker = tracker
    yield tracker
    cohere._active_tracker = gemini._active_tracker = None
    storage.close()


def capture(
    case,
    tracker,
    *,
    stream=False,
    asynchronous=False,
    tier="paid",
    endpoint=None,
    options=None,
    rebind=False,
    response=None,
):
    resource = Native()
    resource._client_wrapper = Native()
    resource._client_wrapper.get_base_url = lambda: endpoint or case["endpoint"]
    resource._api_client = Native()
    resource._api_client.vertexai = False
    resource._api_client._http_options = objects({"base_url": endpoint or case["endpoint"]})
    if tier is not None:
        bind_provider_billing(
            resource, provider=case["provider"], tier=tier, endpoint=case["endpoint"]
        )
    # A different paid client must never confer billing eligibility to this one.
    other = Native()
    bind_provider_billing(other, provider=case["provider"], tier="paid", endpoint=case["endpoint"])
    raw = copy.deepcopy(case["response"] if response is None else response)
    result = objects(raw)
    body = {"model": case["model"], **(options or {})}
    wrapper_options = {}
    if case["provider"] == "cohere":
        if case["service"] == "chat":
            if stream:
                chunks = [
                    objects({"type": "message-start", "id": raw.get("id")}),
                    objects({"type": "message-end", "delta": raw}),
                ]
                wrapper = (
                    cohere._async_chat_stream_wrapper
                    if asynchronous
                    else cohere._sync_chat_stream_wrapper
                )
            else:
                wrapper = cohere._async_chat_wrapper if asynchronous else cohere._sync_chat_wrapper
        else:
            wrapper = (
                cohere._async_metered_wrapper if asynchronous else cohere._sync_metered_wrapper
            )(case["service"])
    else:
        chunks = [result]
        if stream:
            wrapper = gemini._async_stream_call if asynchronous else gemini._sync_stream_call
        else:
            wrapper = gemini._async_direct_call if asynchronous else gemini._sync_direct_call
            wrapper_options = {
                "operation": "google.genai.models.generate_content",
                "component": "llm",
                "event_type": "llm_call",
                "extract": gemini._content_extract,
            }

    def during_request():
        if rebind:
            bind_provider_billing(
                resource, provider=case["provider"], tier="paid", endpoint=case["endpoint"]
            )

    def wrapped(**_):
        during_request()
        return iter(chunks) if stream else result

    async def async_chunks():
        for chunk in chunks:
            yield chunk

    async def async_wrapped(**_):
        during_request()
        await asyncio.sleep(0)
        return async_chunks() if stream else result

    def cohere_async_stream(**_):
        during_request()
        return async_chunks()

    async def consume():
        original = (
            cohere_async_stream if stream and case["provider"] == "cohere" else async_wrapped
        )
        returned = wrapper(original, resource, (), body, **wrapper_options)
        if inspect.isawaitable(returned):
            returned = await returned
        if stream:
            async for _ in returned:
                pass

    with tracker.task("direct-paid") as task:
        if asynchronous:
            asyncio.run(consume())
        else:
            returned = wrapper(wrapped, resource, (), body, **wrapper_options)
            if stream:
                list(returned)
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    return to_attribution_observation_v3(events[0])


def admitted(observation):
    return bool(
        observation
        and any(
            dim["key"] in {"provider_billing_lane", "direct_llm_pricing_lane"}
            for line in observation["usage"]
            for dim in line.get("dimensions", [])
        )
    )


@pytest.mark.parametrize(
    "case,stream",
    [
        (case, stream)
        for case in CASES
        for stream in ([False, True] if case["component"] == "llm" else [False])
    ],
    ids=lambda value: value["id"] if isinstance(value, dict) else str(value),
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_raw_provider_to_server_vector(case, stream, asynchronous, tracker):
    observation = capture(case, tracker, stream=stream, asynchronous=asynchronous)
    assert admitted(observation)
    assert observation["component"] == case["component"]
    assert observation["provider"]["name"] == case["provider"]
    assert observation["provider"]["service"] == case["service"]
    assert observation["provider"].get("record_id")
    assert observation["resource"] == {"type": "model", "id": case["model"]}
    assert {line["metric"]: line["quantity"] for line in observation["usage"]} == case[
        "expected_usage"
    ]
    for line in observation["usage"]:
        assert {
            "key": case["dimension"],
            "value": {"type": "string", "value": case["lane"]},
        } in line["dimensions"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("tier", [None, "free", "unknown"])
def test_other_paid_client_cannot_price_free_trial_or_unknown(case, tier, tracker):
    assert not admitted(capture(case, tracker, tier=tier, asynchronous=True))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize(
    "reason", ["route", "headers", "pending-rebind", "missing-usage", "invalid-counter"]
)
def test_admission_is_complete_route_scoped_and_snapshot_before_request(case, reason, tracker):
    options = {}
    endpoint = None
    response = copy.deepcopy(case["response"])
    tier = "paid"
    if reason == "route":
        endpoint = "https://gateway.example.invalid"
    if reason == "headers":
        options = (
            {"config": {"http_options": {"headers": {"authorization": "test-only"}}}}
            if case["provider"] == "google"
            else {"request_options": {"additional_headers": {"authorization": "test-only"}}}
        )
    if reason == "pending-rebind":
        tier = "free"
    if reason == "missing-usage":
        response.pop("usageMetadata", None)
        response.pop("usage", None)
        response.pop("meta", None)
    if reason == "invalid-counter":
        if case["provider"] == "google":
            response["usageMetadata"]["promptTokenCount"] = True
        else:
            billed = response.get("usage", response.get("meta"))["billed_units"]
            billed["search_units" if case["service"] == "rerank" else "input_tokens"] = True
    assert not admitted(
        capture(
            case,
            tracker,
            asynchronous=True,
            tier=tier,
            endpoint=endpoint,
            options=options,
            rebind=reason == "pending-rebind",
            response=response,
        )
    )


def test_binding_replacement_unbind_and_endpoint_validation():
    client = Native()
    old = bind_provider_billing(
        client, provider="cohere", tier="free", endpoint="https://api.cohere.com"
    )
    current = bind_provider_billing(
        client, provider="cohere", tier="paid", endpoint="https://api.cohere.com"
    )
    old()
    assert _has_paid_billing(client, "cohere", "https://api.cohere.com")
    assert not _has_paid_billing(client, "google", "https://generativelanguage.googleapis.com")
    assert not _has_paid_billing(client, "cohere", "https://api.cohere.com:8443")
    current()
    current()
    assert not _has_paid_billing(client, "cohere", "https://api.cohere.com")
    for endpoint in [
        "https://api.cohere.com/proxy",
        "https://api.cohere.com?api_key=x",
        "https://user@api.cohere.com",
        "https://proxy.example.invalid",
        "http://api.cohere.com",
    ]:
        with pytest.raises(ValueError):
            bind_provider_billing(client, provider="cohere", tier="paid", endpoint=endpoint)


@pytest.mark.parametrize(
    "case", [case for case in CASES if case["provider"] == "google"], ids=lambda case: case["id"]
)
@pytest.mark.parametrize(
    "missing", ["promptTokensDetails", "cacheTokensDetails", "candidatesTokensDetails"]
)
def test_new_gemini_models_require_positive_text_modality_evidence(case, missing, tracker):
    response = copy.deepcopy(case["response"])
    del response["usageMetadata"][missing]
    assert not admitted(capture(case, tracker, response=response, asynchronous=True))


@pytest.mark.parametrize("case", [CASES[0], CASES[4]], ids=lambda case: case["id"])
def test_concurrent_native_clients_do_not_share_billing_evidence(case, tracker):
    async def run_one(tier):
        resource = Native()
        resource._client_wrapper = Native()
        resource._client_wrapper.get_base_url = lambda: case["endpoint"]
        resource._api_client = Native()
        resource._api_client.vertexai = False
        resource._api_client._http_options = objects({"base_url": case["endpoint"]})
        bind_provider_billing(
            resource, provider=case["provider"], tier=tier, endpoint=case["endpoint"]
        )

        async def wrapped(**_):
            await asyncio.sleep(0)
            return objects(case["response"])

        with tracker.task(tier) as task:
            if case["provider"] == "cohere":
                await cohere._async_chat_wrapper(wrapped, resource, (), {"model": case["model"]})
            else:
                await gemini._async_direct_call(
                    wrapped,
                    resource,
                    (),
                    {"model": case["model"]},
                    operation="google.genai.models.generate_content",
                    component="llm",
                    event_type="llm_call",
                    extract=gemini._content_extract,
                )
        return to_attribution_observation_v3(
            tracker._storage.query_events(task_id=str(task.task_id))[0]
        )

    async def both():
        return await asyncio.gather(run_one("paid"), run_one("free"))

    paid, free = asyncio.run(both())
    assert admitted(paid)
    assert not admitted(free)
