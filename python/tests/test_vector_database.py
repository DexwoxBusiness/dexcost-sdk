import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost import (
    instrument_pinecone,
    instrument_turbopuffer,
    uninstrument_pinecone,
    vector_database_resource_id,
)
from dexcost.attribution import to_attribution_observation_v3
from dexcost.context import _current_task
from dexcost.instruments._capture import current_provider_capture_owner, provider_capture_scope
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage

DATA = json.loads(
    (Path(__file__).parents[2] / "fixtures/vector_database_conformance.json").read_text()
)


@pytest.fixture
def setup(tmp_path):
    storage = SQLiteStorage(tmp_path / "vectors.db")
    task = Task(task_id=uuid.uuid4(), task_type="memory")
    storage.insert_task(task)
    token = _current_task.set(task)
    try:
        yield SimpleNamespace(storage=storage), task
    finally:
        _current_task.reset(token)
        storage.close()


def bind(client, tracker, provider, account="account-a", ns=DATA["namespace"]):
    if provider == "pinecone":
        return instrument_pinecone(
            client,
            tracker,
            billing_account_id=account,
            region=DATA["pinecone_region"],
            index_host=DATA["pinecone_host"],
            namespace=ns,
        )
    return instrument_turbopuffer(
        client,
        tracker,
        billing_account_id=account,
        region=DATA["turbopuffer_region"],
        namespace=ns,
    )


def native(case, asynchronous=False):
    called = []

    def operation(**kwargs):
        called.append(current_provider_capture_owner())
        return case["response"]

    async def awaited(**kwargs):
        await asyncio.sleep(0)
        return operation(**kwargs)

    client = SimpleNamespace(
        host="https://" + DATA["pinecone_host"],
        _client=SimpleNamespace(
            base_url=f"https://{DATA['turbopuffer_region']}.turbopuffer.com",
            default_namespace=DATA["namespace"],
        ),
    )
    setattr(client, case["operation"], awaited if asynchronous else operation)
    return client, called


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("case", DATA["cases"], ids=lambda case: case["id"])
def test_paired_native_vectors(setup, case, asynchronous):
    tracker, task = setup
    client, called = native(case, asynchronous)
    wrapped = bind(client, tracker, case["provider"])
    result = getattr(wrapped, case["operation"])(
        namespace=DATA["namespace"], filter="PRIVATE-FILTER", vector="PRIVATE-VECTOR"
    )
    if asynchronous:
        result = asyncio.run(result)
    assert result is case["response"]
    jobs = tracker.storage.query_events_for_sync()
    assert len(jobs) == (2 if case["id"] == "turbo_query" else 1 if "usage" in case else 0)
    assert called == ([None] if case["operation"] == "upsert" else [case["provider"]])
    if jobs:
        event = to_attribution_observation_v3(jobs[0])
        assert [
            [u["metric"], u["quantity"], u["unit"]]
            for job in jobs
            for u in to_attribution_observation_v3(job)["usage"]
        ] == case["usage"]
        assert jobs[0].task_id == task.task_id
        assert "PRIVATE" not in json.dumps(jobs[0].to_dict())
        assert DATA["namespace"] not in json.dumps(jobs[0].to_dict())
        assert "cost" not in event
        assert "cost_evidence" not in event
        assert "provider_record_id" not in event
        assert event["usage_period"]["start_at"] < event["usage_period"]["end_at"]
        assert tracker.storage.query_provider_jobs_for_sync() == []


def test_binding_identity_all_boundaries():
    base = ["pinecone", "account-a", "us-east-1", DATA["pinecone_host"], "agent-memory"]
    identity = vector_database_resource_id(*base)
    assert identity.startswith("account-a/") and len(identity.split("/")[1]) == 64
    for index, value in [
        (1, "account-b"),
        (2, "us-west-2"),
        (3, "other.svc.pinecone.io"),
        (4, "other"),
    ]:
        changed = base.copy()
        changed[index] = value
        assert vector_database_resource_id(*changed) != identity
    assert vector_database_resource_id(*base[:-1], "") != vector_database_resource_id(
        *base[:-1], "__default__"
    )
    for index, value in [
        (1, "account/secret"),
        (2, ""),
        (3, "https://private.invalid"),
        (4, "bad\x00namespace"),
    ]:
        changed = base.copy()
        changed[index] = value
        with pytest.raises(ValueError):
            vector_database_resource_id(*changed)


@pytest.mark.parametrize(
    "change", ["host", "namespace", "response_namespace", "headers", "async_req"]
)
def test_mismatched_native_identity_is_unobserved(setup, change):
    tracker, _ = setup
    case = DATA["cases"][0]
    client, _ = native(case)
    kwargs = {"namespace": DATA["namespace"]}
    if change == "host":
        client.host = "https://other.svc.pinecone.io"
    if change == "namespace":
        kwargs["namespace"] = "other"
    if change == "response_namespace":
        client.query = lambda **_: {"namespace": "other", "usage": {"readUnits": 1}}
    if change == "headers":
        kwargs["extra_headers"] = {"authorization": "PRIVATE"}
    if change == "async_req":
        kwargs["async_req"] = True
    bind(client, tracker, "pinecone").query(**kwargs)
    assert tracker.storage.query_events_for_sync() == []


def test_disable_outer_capture_error_and_concurrent_account_isolation(setup):
    tracker, _ = setup
    case = DATA["cases"][0]
    client, _ = native(case)
    wrapped = bind(client, tracker, "pinecone")
    with provider_capture_scope("outer"):
        wrapped.query(namespace=DATA["namespace"])
    uninstrument_pinecone(wrapped)
    assert wrapped.query(namespace=DATA["namespace"]) is case["response"]
    assert not tracker.storage.query_events_for_sync()
    error = RuntimeError("native")

    def fail(**kwargs):
        raise error

    client.query = fail
    with pytest.raises(RuntimeError) as exc:
        bind(client, tracker, "pinecone").query(namespace=DATA["namespace"])
    assert exc.value is error and current_provider_capture_owner() is None

    async def both():
        await asyncio.gather(
            *(
                bind(native(case, True)[0], tracker, "pinecone", account).query(
                    namespace=DATA["namespace"]
                )
                for account in ["account-a", "account-b"]
            )
        )

    asyncio.run(both())
    assert (
        len(
            {j.details["attribution_resource_id"] for j in tracker.storage.query_events_for_sync()}
        )
        == 2
    )
