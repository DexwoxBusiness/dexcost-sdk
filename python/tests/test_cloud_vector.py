import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from dexcost.attribution import to_attribution_observation_v3
from dexcost.context import _current_task
from dexcost.instruments._capture import current_provider_capture_owner
from dexcost.instruments.cloud_vector import (
    cloud_vector_resource_id,
    instrument_qdrant,
    instrument_zilliz,
    uninstrument_qdrant,
    uninstrument_zilliz,
)
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage

QHOST = "cluster.us-west.cloud.qdrant.io"
ZHOST = "in01-sample.serverless.aws-us-west-2.vectordb.zillizcloud.com"


@pytest.fixture
def setup(tmp_path):
    storage = SQLiteStorage(tmp_path / "cloud.db")
    task = Task(task_id=uuid.uuid4(), task_type="memory")
    storage.insert_task(task)
    token = _current_task.set(task)
    try:
        yield SimpleNamespace(storage=storage), task
    finally:
        _current_task.reset(token)
        storage.close()


def bind(host):
    return dict(billing_account_id="account-a", region="aws-us-west-2", cluster_host=host)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cpu", [8, 0, None])
def test_real_qdrant_envelope_before_sdk_strips_usage(setup, asynchronous, cpu):
    from qdrant_client import AsyncQdrantClient, QdrantClient

    tracker, _task = setup
    calls = []

    def transport(request):
        calls.append(current_provider_capture_owner())
        body = {"status": "ok", "time": 0.1, "result": {"points": []}}
        if cpu is not None:
            body["usage"] = {
                "hardware": {
                    "cpu": cpu,
                    "payload_io_read": 0,
                    "payload_io_write": 0,
                    "payload_index_io_read": 0,
                    "payload_index_io_write": 0,
                    "vector_io_read": 0,
                    "vector_io_write": 0,
                }
            }
        return httpx.Response(200, json=body)

    async def execute():
        client = (AsyncQdrantClient if asynchronous else QdrantClient)(
            url=f"https://{QHOST}", check_compatibility=False
        )
        if asynchronous:
            await client.http.client._async_client.aclose()
            client.http.client._async_client = httpx.AsyncClient(
                transport=httpx.MockTransport(transport)
            )
        else:
            client.http.client._client.close()
            client.http.client._client = httpx.Client(transport=httpx.MockTransport(transport))
        original_api = client.http.search_api
        wrapped = instrument_qdrant(client, tracker, **bind(QHOST))
        result = wrapped.query_points(collection_name="PRIVATE", query=[0.1], limit=1)
        if asynchronous:
            result = await result
        assert result.points == []
        assert client.http.search_api is original_api
        uninstrument_qdrant(wrapped)
        if asynchronous:
            await client.close()
        else:
            client.close()

    asyncio.run(execute())
    events = tracker.storage.query_events_for_sync()
    assert calls == ["qdrant_cloud"]
    assert len(events) == (1 if cpu == 8 else 0)
    if events:
        wire = to_attribution_observation_v3(events[0])
        assert wire["provider"]["region"] == "aws-us-west-2"
        assert wire["usage"][0]["quantity"] == "8"
        assert "PRIVATE" not in json.dumps(wire)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cost", [6, 0, None])
def test_real_pymilvus_searchresult_meter(setup, monkeypatch, asynchronous, cost):
    from pymilvus import AsyncMilvusClient, MilvusClient
    from pymilvus.client.connection_manager import ConnectionManager
    from pymilvus.client.search_result import SearchResult
    from pymilvus.grpc_gen import common_pb2, schema_pb2

    tracker, _ = setup
    calls = []
    result = SearchResult(
        schema_pb2.SearchResultData(num_queries=1, top_k=0, topks=[0]),
        status=common_pb2.Status(extra_info={} if cost is None else {"report_value": str(cost)}),
    )

    def search(**kwargs):
        calls.append(current_provider_capture_owner())
        return result

    async def async_search(**kwargs):
        return search(**kwargs)

    handler = SimpleNamespace(search=async_search if asynchronous else search)
    monkeypatch.setattr(ConnectionManager, "get_or_create", lambda *a, **kw: handler)
    monkeypatch.setattr(MilvusClient, "get_server_type", lambda self: "zilliz")
    client = (AsyncMilvusClient if asynchronous else MilvusClient)(uri=f"https://{ZHOST}")
    if asynchronous:

        async def connection():
            return handler

        monkeypatch.setattr(client, "_get_connection", connection)
    wrapped = instrument_zilliz(client, tracker, **bind(ZHOST))
    response = wrapped.search(collection_name="PRIVATE", data=[[0.1]], limit=1)
    if asynchronous:
        response = asyncio.run(response)
    assert response is result
    assert calls == ["milvus_zilliz"]
    events = tracker.storage.query_events_for_sync()
    assert len(events) == (1 if cost == 6 else 0)
    if events:
        wire = to_attribution_observation_v3(events[0])
        assert wire["usage"][0]["metric"] == "zilliz.read_vcu"
        assert wire["usage"][0]["quantity"] == "6"


@pytest.mark.parametrize(
    "reason", ["route", "disabled", "missing", "negative", "unsafe", "failed"]
)
def test_ineligible_zilliz_remains_unpriced(setup, reason):
    tracker, _ = setup
    response = SimpleNamespace(
        extra={}
        if reason == "missing"
        else {
            "cost": -1 if reason == "negative" else 9007199254740992 if reason == "unsafe" else 6
        }
    )

    def search(**kwargs):
        if reason == "failed":
            raise RuntimeError("provider failed")
        return response

    client = SimpleNamespace(
        _config=SimpleNamespace(uri=f"https://{'localhost' if reason == 'route' else ZHOST}"),
        search=search,
    )
    wrapped = instrument_zilliz(client, tracker, **bind(ZHOST))
    if reason == "disabled":
        uninstrument_zilliz(wrapped)
    if reason == "failed":
        with pytest.raises(RuntimeError, match="provider failed"):
            wrapped.search(collection_name="PRIVATE")
    else:
        assert wrapped.search(collection_name="PRIVATE") is response
    assert not tracker.storage.query_events_for_sync()


def test_identity_host_and_account_boundaries():
    identity = cloud_vector_resource_id("milvus_zilliz", "account-a", "aws-us-west-2", ZHOST)
    assert identity != cloud_vector_resource_id(
        "milvus_zilliz", "account-b", "aws-us-west-2", ZHOST
    )
    for provider, host in [
        ("qdrant_cloud", "localhost"),
        ("milvus_zilliz", ZHOST.replace(".serverless", "")),
    ]:
        with pytest.raises(ValueError):
            cloud_vector_resource_id(provider, "account-a", "aws-us-west-2", host)
