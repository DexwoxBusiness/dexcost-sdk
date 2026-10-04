"""Real pinned provider clients, local mock transports only; isolated optional imports."""

import asyncio
import importlib.metadata
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("provider", ["pinecone", "turbopuffer"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_native_vector_client(provider, asynchronous, tmp_path):
    try:
        importlib.metadata.version(provider)
    except importlib.metadata.PackageNotFoundError:
        pytest.skip(f"optional {provider} native SDK not installed")
    if os.environ.get("DEXCOST_VECTOR_NATIVE_CHILD") != "1":
        node = (
            f"{Path(__file__).resolve()}::test_real_native_vector_client"
            f"[{asynchronous}-{provider}]"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                node,
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(tmp_path / "child"),
            ],
            env={**os.environ, "DEXCOST_VECTOR_NATIVE_CHILD": "1"},
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0 and "1 passed" in result.stdout, (
            result.stdout + result.stderr
        )
        return
    import httpx

    from dexcost import instrument_pinecone, instrument_turbopuffer
    from dexcost.adapters import http
    from dexcost.context import _current_task
    from dexcost.instruments._capture import current_provider_capture_owner
    from dexcost.models.task import Task
    from dexcost.storage.sqlite import SQLiteStorage

    data = json.loads(
        (Path(__file__).parents[2] / "fixtures/vector_database_conformance.json").read_text()
    )
    storage = SQLiteStorage(tmp_path / "native.db")
    tracker = SimpleNamespace(storage=storage)
    task = Task(task_id=uuid.uuid4(), task_type="native-memory")
    storage.insert_task(task)
    token = _current_task.set(task)
    owners = []
    pinecone_usage = {"readUnits": 1}

    # Native decoding uses official shapes; generic HTTP observer sees the same
    # response while the facade owns the operation and must not record it again.
    def transport(request):
        owners.append(current_provider_capture_owner())
        payload = (
            {
                "namespace": data["namespace"],
                "matches": [],
                "vectors": {},
                "usage": pinecone_usage,
            }
            if provider == "pinecone"
            else {
                "rows": [],
                "billing": {
                    "billable_logical_bytes_queried": 1280000000,
                    "billable_logical_bytes_returned": 150,
                },
                "performance": {"server_total_ms": 1},
            }
        )
        response = httpx.Response(200, json=payload, request=request)
        http._handle_http_call(
            str(request.url),
            method=request.method,
            response=response,
            response_body=payload,
            request_body={"namespace": data["namespace"]},
            account_network=False,
        )
        return response

    http.clear_recorded_events()
    try:
        if provider == "pinecone":
            from pinecone import AsyncPinecone, Pinecone
            from pinecone.errors import ResponseParsingError

            if asynchronous:

                async def run_pinecone():
                    index = await AsyncPinecone(api_key="mock-not-a-credential").index(
                        host=data["pinecone_host"]
                    )
                    index._http._client = httpx.AsyncClient(
                        base_url=index.host, transport=httpx.MockTransport(transport)
                    )
                    try:
                        wrapped = instrument_pinecone(
                            index,
                            tracker,
                            billing_account_id="account-a",
                            region=data["pinecone_region"],
                            index_host=data["pinecone_host"],
                            namespace=data["namespace"],
                        )
                        result = await wrapped.query(
                            vector=[0.1], top_k=1, namespace=data["namespace"]
                        )
                        assert result.usage.read_units == 1
                        await wrapped.fetch(ids=["PRIVATE-ID"], namespace=data["namespace"])
                        # Upstream10.0.0 types RU as int despite official fractional RU.
                        # Preserve its native error; do not round/reparse or invent usage.
                        pinecone_usage["readUnits"] = 0.25
                        with pytest.raises(ResponseParsingError, match="readUnits"):
                            await wrapped.query(vector=[0.1], top_k=1, namespace=data["namespace"])
                    finally:
                        await index.close()

                asyncio.run(run_pinecone())
            else:
                index = Pinecone(api_key="mock-not-a-credential").index(host=data["pinecone_host"])
                index._http._client.close()
                index._http._client = httpx.Client(
                    base_url=index.host, transport=httpx.MockTransport(transport)
                )
                try:
                    wrapped = instrument_pinecone(
                        index,
                        tracker,
                        billing_account_id="account-a",
                        region=data["pinecone_region"],
                        index_host=data["pinecone_host"],
                        namespace=data["namespace"],
                    )
                    assert (
                        wrapped.query(
                            vector=[0.1], top_k=1, namespace=data["namespace"]
                        ).usage.read_units
                        == 1
                    )
                    wrapped.fetch(ids=["PRIVATE-ID"], namespace=data["namespace"])
                    pinecone_usage["readUnits"] = 0.25
                    with pytest.raises(ResponseParsingError, match="readUnits"):
                        wrapped.query(vector=[0.1], top_k=1, namespace=data["namespace"])
                finally:
                    index.close()
        else:
            from turbopuffer import AsyncTurbopuffer, Turbopuffer

            if asynchronous:

                async def run_turbo():
                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(transport)
                    ) as transport_client:
                        native = AsyncTurbopuffer(
                            api_key="mock-not-a-credential",
                            region=data["turbopuffer_region"],
                            http_client=transport_client,
                        )
                        wrapped = instrument_turbopuffer(
                            native.namespace(data["namespace"]),
                            tracker,
                            billing_account_id="account-a",
                            region=data["turbopuffer_region"],
                            namespace=data["namespace"],
                        )
                        assert (
                            await wrapped.query(top_k=1)
                        ).billing.billable_logical_bytes_returned == 150

                asyncio.run(run_turbo())
            else:
                with httpx.Client(transport=httpx.MockTransport(transport)) as transport_client:
                    native = Turbopuffer(
                        api_key="mock-not-a-credential",
                        region=data["turbopuffer_region"],
                        http_client=transport_client,
                    )
                    wrapped = instrument_turbopuffer(
                        native.namespace(data["namespace"]),
                        tracker,
                        billing_account_id="account-a",
                        region=data["turbopuffer_region"],
                        namespace=data["namespace"],
                    )
                    assert wrapped.query(top_k=1).billing.billable_logical_bytes_returned == 150
        assert owners and set(owners) == {provider}
        assert http.get_recorded_events() == []
        jobs = storage.query_events_for_sync()
        assert len(jobs) == 2
        assert {j.details["attribution_component"] for j in jobs} == (
            {"storage"} if provider == "pinecone" else {"storage", "network"}
        )
        assert "PRIVATE" not in json.dumps([j.to_dict() for j in jobs])
    finally:
        _current_task.reset(token)
        storage.close()
