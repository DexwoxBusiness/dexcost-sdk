import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from youdotcom import You
from youdotcom.errors import YouError
from youdotcom.utils.retries import BackoffStrategy, RetryConfig

from dexcost.context import _current_task
from dexcost.instruments._capture import current_provider_capture_owner, provider_capture_scope
from dexcost.instruments.you_search import instrument_you_search, uninstrument_you_search
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage

DATA = json.loads((Path(__file__).parents[2] / "tests/fixtures/you-search.json").read_text())


@pytest.fixture
def setup(tmp_path):
    storage = SQLiteStorage(tmp_path / "you.db")
    task = Task(task_id=uuid.uuid4(), task_type="search")
    storage.insert_task(task)
    token = _current_task.set(task)
    try:
        yield SimpleNamespace(storage=storage), task
    finally:
        _current_task.reset(token)
        storage.close()


def client(
    tracker, *, tier="paid", endpoint=DATA["endpoint"], response=None, status=200, mutate=None
):
    calls = []

    def send(request):
        assert current_provider_capture_owner() == "you_com"
        calls.append(request)
        if mutate:
            mutate(request)
        return httpx.Response(status, json=response if response is not None else DATA["response"])

    sync = httpx.Client(transport=httpx.MockTransport(send))
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(send))
    native = You(api_key_auth="PRIVATE_KEY", client=sync, async_client=async_client)
    wrapped = instrument_you_search(
        native, tracker, billing_account_id=DATA["account"], endpoint=endpoint, billing_tier=tier
    )
    return native, wrapped, calls, sync, async_client


def close(sync, async_client):
    sync.close()
    asyncio.run(async_client.aclose())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("tier", ["paid", "free", "unknown"])
def test_real_native_base_response_and_durable_dedup(setup, asynchronous, tier):
    tracker, task = setup
    native, wrapped, calls, sync, async_client = client(tracker, tier=tier)
    original_configuration = native.sdk_configuration
    try:

        async def execute():
            return await wrapped.search_async(**DATA["request"])

        for _ in range(2):
            response = (
                asyncio.run(execute()) if asynchronous else wrapped.search(**DATA["request"])
            )
            assert response.results.web[0].title == "PRIVATE_RESULT"
        jobs = tracker.storage.query_provider_jobs_for_sync()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.task_id == task.task_id
        assert str(job.usage[0].quantity) == DATA["quantity"]
        assert job.usage[0].metric == DATA["metric"]
        assert job.cost_amount is None
        assert (
            job.billing_dimensions == (("you_search_billing_lane", "public_payg_base"),)
            if tier == "paid"
            else job.billing_dimensions == ()
        )
        assert "PRIVATE" not in json.dumps(job.to_dict())
        assert len(calls) == 2
        assert native.sdk_configuration is original_configuration
        assert native.sdk_configuration.client is sync
    finally:
        close(sync, async_client)


@pytest.mark.parametrize("parameters", DATA["excluded_parameters"])
def test_addons_remain_native_and_unpriced(setup, parameters):
    tracker, _task = setup
    _native, wrapped, calls, sync, async_client = client(tracker)
    try:
        assert wrapped.search(**DATA["request"], **parameters).metadata.search_uuid
        assert len(calls) == 1
        assert tracker.storage.query_provider_jobs_for_sync() == []
    finally:
        close(sync, async_client)


@pytest.mark.parametrize(
    "reason",
    [
        "missing-id",
        "malformed-id",
        "wrong-endpoint",
        "disabled",
        "failed",
        "unknown-parameter",
        "auth-override",
        "redirect",
        "malformed-response",
    ],
)
def test_unknown_or_failed_evidence_never_creates_money(setup, reason):
    tracker, _task = setup
    response = json.loads(json.dumps(DATA["response"]))
    if reason == "missing-id":
        response["metadata"].pop("search_uuid")
    if reason == "malformed-id":
        response["metadata"]["search_uuid"] = "PRIVATE-not-an-id"
    if reason == "malformed-response":
        response = {"metadata": response["metadata"]}

    def mutate(request):
        if reason == "unknown-parameter":
            request.url = request.url.copy_add_param("unverified_billable_feature", "true")
        if reason == "auth-override":
            request.headers["Authorization"] = "PRIVATE_OTHER_AUTH"
        if reason == "redirect":
            request.url = httpx.URL("https://proxy.invalid/v1/search")

    _native, wrapped, _calls, sync, async_client = client(
        tracker,
        endpoint="https://api.you.com" if reason == "wrong-endpoint" else DATA["endpoint"],
        response=response,
        status=500 if reason == "failed" else 200,
        mutate=mutate,
    )
    try:
        if reason == "disabled":
            uninstrument_you_search(wrapped)
            # Uninstrumented calls do not claim capture ownership.
            with provider_capture_scope("you_com"):
                wrapped.search(**DATA["request"])
        elif reason == "failed":
            with pytest.raises(YouError):
                wrapped.search(**DATA["request"])
        else:
            wrapped.search(**DATA["request"])
        assert tracker.storage.query_provider_jobs_for_sync() == []
    finally:
        close(sync, async_client)


def test_real_native_retry_only_counts_terminal_success(setup):
    tracker, _task = setup
    attempts = []

    def send(request):
        attempts.append(request)
        return httpx.Response(
            429 if len(attempts) == 1 else 200, json={} if len(attempts) == 1 else DATA["response"]
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as transport:
        native = You(api_key_auth="PRIVATE_KEY", client=transport)
        wrapped = instrument_you_search(
            native,
            tracker,
            billing_account_id=DATA["account"],
            endpoint=DATA["endpoint"],
            billing_tier="paid",
        )
        retries = RetryConfig("backoff", BackoffStrategy(1, 1, 1, 1000, 0), False)
        assert wrapped.search(**DATA["request"], retries=retries).results.web
    assert len(attempts) == 2
    assert len(tracker.storage.query_provider_jobs_for_sync()) == 1


def test_replay_cannot_move_original_task_or_account_identity(setup):
    tracker, task = setup
    native, wrapped, _calls, sync, async_client = client(tracker)
    try:
        wrapped.search(**DATA["request"])
        other = Task(task_id=uuid.uuid4(), task_type="search")
        tracker.storage.insert_task(other)
        token = _current_task.set(other)
        try:
            instrument_you_search(
                wrapped,
                tracker,
                billing_account_id=DATA["account"],
                endpoint=DATA["endpoint"],
                billing_tier="paid",
            ).search(**DATA["request"])
            wrapped.search(**DATA["request"])
        finally:
            _current_task.reset(token)
        assert [job.task_id for job in tracker.storage.query_provider_jobs_for_sync()] == [
            task.task_id
        ]
        second = instrument_you_search(
            native,
            tracker,
            billing_account_id="account-b",
            endpoint=DATA["endpoint"],
            billing_tier="paid",
        )
        second.search(**DATA["request"])
        assert len(tracker.storage.query_provider_jobs_for_sync()) == 2
    finally:
        close(sync, async_client)


def test_native_explicit_direct_route_and_auth_override(setup):
    tracker, _task = setup
    _native, wrapped, calls, sync, async_client = client(tracker, endpoint="https://api.you.com")
    try:
        wrapped.search(
            **DATA["request"],
            server_url="https://api.you.com",
            http_headers={"X-API-Key": "PRIVATE_DIFFERENT_ACCOUNT"},
        )
        assert tracker.storage.query_provider_jobs_for_sync() == []
        wrapped.search(**DATA["request"], server_url="https://api.you.com")
        assert len(tracker.storage.query_provider_jobs_for_sync()) == 1
        assert len(calls) == 2
    finally:
        close(sync, async_client)


def test_native_cancellation_preserves_provider_exception(setup):
    tracker, _task = setup

    async def execute():
        async def send(request):
            raise asyncio.CancelledError("cancelled")

        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as transport:
            native = You(api_key_auth="PRIVATE_KEY", async_client=transport)
            wrapped = instrument_you_search(
                native,
                tracker,
                billing_account_id=DATA["account"],
                endpoint=DATA["endpoint"],
                billing_tier="paid",
            )
            with pytest.raises(asyncio.CancelledError, match="cancelled"):
                await wrapped.search_async(**DATA["request"])

    asyncio.run(execute())
    assert tracker.storage.query_provider_jobs_for_sync() == []
