import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost import instrument_object_storage, uninstrument_object_storage
from dexcost.context import _current_task
from dexcost.instruments import object_storage
from dexcost.instruments._capture import provider_capture_scope
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage

DATA = json.loads(
    (Path(__file__).parents[2] / "fixtures/object_storage_conformance.json").read_text()
)


@pytest.fixture
def setup(tmp_path):
    storage = SQLiteStorage(tmp_path / "storage.db")
    task = Task(task_id=uuid.UUID(DATA["task_id"]), task_type="storage")
    storage.insert_task(task)
    token = _current_task.set(task)
    try:
        yield SimpleNamespace(storage=storage), task
    finally:
        _current_task.reset(token)
        storage.close()


def scope(provider):
    return dict(
        provider=provider,
        billing_account_id=DATA["aws_payer"] if provider == "aws_s3" else DATA["r2_account"],
        bucket=DATA["bucket"],
        region="us-east-1" if provider == "aws_s3" else "auto",
        **(
            dict(bucket_owner_account_id=DATA["aws_owner"], owner_pays=True)
            if provider == "aws_s3"
            else {}
        ),
    )


def native(case, asynchronous=False):
    class Events:
        callback = None

        def register(self, event, callback, unique_id):
            self.callback = callback

        def emit(self):
            if self.callback and not case.get("missing_transport"):
                endpoint = case.get(
                    "transport_endpoint",
                    case.get(
                        "endpoint",
                        "https://s3.us-east-1.amazonaws.com"
                        if case["provider"] == "aws_s3"
                        else f"https://{DATA['r2_account']}.r2.cloudflarestorage.com",
                    ),
                )
                for _ in range(case.get("transport_attempts", 1)):
                    self.callback(
                        SimpleNamespace(
                            url=(
                                f"{endpoint}/{case.get('transport_bucket', DATA['bucket'])}/"
                                "PRIVATE-KEY"
                            ),
                            method="PUT" if case["operation"] == "put_object" else "GET",
                        )
                    )

    events = Events()
    kwargs = dict(Bucket=DATA["bucket"], Key="PRIVATE-KEY", Body="PRIVATE-CONTENT") | case.get(
        "request", {}
    )
    attempts = case.get("attempts", 1)
    result = dict(
        ResponseMetadata=dict(
            RequestId=case["id"],
            HTTPStatusCode=case.get("status", 200),
            RetryAttempts=attempts - 1 if attempts is not None else None,
        ),
        **case.get("response", {}),
    )

    def invoke(**received):
        events.emit()
        assert received == kwargs
        return result

    async def awaited(**received):
        await asyncio.sleep(0)
        return invoke(**received)

    client = SimpleNamespace(
        meta=SimpleNamespace(
            events=events,
            service_model=SimpleNamespace(service_name="s3"),
            region_name=case.get("client_region", scope(case["provider"])["region"]),
            endpoint_url=case.get(
                "endpoint",
                "https://s3.us-east-1.amazonaws.com"
                if case["provider"] == "aws_s3"
                else f"https://{DATA['r2_account']}.r2.cloudflarestorage.com",
            ),
        )
    )
    setattr(client, case["operation"], awaited if asynchronous else invoke)
    return client, kwargs, result


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["id"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_paired_native_evidence(setup, case, asynchronous):
    tracker, task = setup
    client, kwargs, result = native(case, asynchronous)
    facade = instrument_object_storage(client, tracker, **scope(case["provider"]))
    value = getattr(facade, case["operation"])(**kwargs)
    assert (asyncio.run(value) if asynchronous else value) is result
    resource = (
        f"{DATA['aws_payer']}/{DATA['aws_owner']}.us-east-1.{DATA['bucket']}"
        if case["provider"] == "aws_s3"
        else f"{DATA['r2_account']}/r2"
    )
    job = tracker.storage.get_provider_job(
        case["provider"], "object_storage", f"{resource}/{case['id']}"
    )
    if "metric" not in case:
        assert job is None
        return
    assert job is not None and job.task_id == task.task_id
    event = job.to_attribution_observation()
    assert event["usage"][0]["metric"] == case["metric"]
    assert event["usage"][0]["quantity"] == "1"
    assert event["resource"]["id"] == resource
    assert "cost" not in event
    assert "PRIVATE" not in json.dumps(job.to_dict())
    repeated = getattr(facade, case["operation"])(**kwargs)
    if asynchronous:
        asyncio.run(repeated)
    assert (
        tracker.storage.get_provider_job(
            case["provider"], "object_storage", f"{resource}/{case['id']}"
        )
        == job
    )


def test_disable_nested_and_native_errors(setup):
    tracker, _ = setup
    case = next(c for c in DATA["cases"] if c["id"] == "aws_get")
    client, kwargs, response = native(case)
    wrapped = instrument_object_storage(client, tracker, **scope("aws_s3"))
    with provider_capture_scope("outer"):
        assert wrapped.get_object(**kwargs) is response
    uninstrument_object_storage(wrapped)
    assert wrapped.get_object(**kwargs) is response
    key = f"{DATA['aws_payer']}/{DATA['aws_owner']}.us-east-1.{DATA['bucket']}/{case['id']}"
    assert tracker.storage.get_provider_job("aws_s3", "object_storage", key) is None
    error = RuntimeError("native")

    def failed(**kwargs):
        raise error

    client.get_object = failed
    with pytest.raises(RuntimeError) as caught:
        instrument_object_storage(client, tracker, **scope("aws_s3")).get_object(**kwargs)
    assert caught.value is error


def test_cross_period_retains_request_window(setup, monkeypatch):
    tracker, _ = setup
    dates = iter(
        [
            datetime.fromisoformat("2026-09-30T23:59:59+00:00"),
            datetime.fromisoformat("2026-10-01T00:00:01+00:00"),
        ]
    )
    monkeypatch.setattr(object_storage, "_now", lambda: next(dates))
    case = next(c for c in DATA["cases"] if c["id"] == "aws_get")
    client, kwargs, _ = native(case)
    instrument_object_storage(client, tracker, **scope("aws_s3")).get_object(**kwargs)
    key = f"{DATA['aws_payer']}/{DATA['aws_owner']}.us-east-1.{DATA['bucket']}/{case['id']}"
    job = tracker.storage.get_provider_job("aws_s3", "object_storage", key)
    assert job.to_attribution_observation()["usage_period"] == {
        "start_at": "2026-09-30T23:59:59.000000Z",
        "end_at": "2026-10-01T00:00:01.000000Z",
    }


def test_mapping_requires_explicit_ownership(setup):
    tracker, _ = setup
    for changes in [
        dict(owner_pays=False),
        dict(bucket_owner_account_id=None),
        dict(billing_account_id="invalid"),
        dict(bucket="bucket--x-s3"),
    ]:
        with pytest.raises(ValueError):
            instrument_object_storage(object(), tracker, **(scope("aws_s3") | changes))


def test_parallel_r2_accounts_do_not_share_route_evidence(setup):
    tracker, _ = setup

    async def run():
        clients = []
        for account in [DATA["r2_account"], "b" * 32]:
            case = dict(
                id="same-request",
                provider="r2_cloudflare",
                operation="get_object",
                response=dict(StorageClass="STANDARD"),
                endpoint=f"https://{account}.r2.cloudflarestorage.com",
            )
            client, kwargs, _ = native(case, True)
            clients.append(
                instrument_object_storage(
                    client, tracker, **(scope("r2_cloudflare") | dict(billing_account_id=account))
                ).get_object(**kwargs)
            )
        await asyncio.gather(*clients)

    asyncio.run(run())
    for account in [DATA["r2_account"], "b" * 32]:
        job = tracker.storage.get_provider_job(
            "r2_cloudflare", "object_storage", f"{account}/r2/same-request"
        )
        assert job is not None and job.resource_id == f"{account}/r2"
    assert object_storage._transport.get() is None


def test_real_botocore_before_send_with_mock_transport(setup, monkeypatch, tmp_path):
    # Earlier fake-provider tests intentionally replace botocore entries in
    # sys.modules with None. Test the installed package in a fresh interpreter,
    # without repairing global imports or treating an installed provider as absent.
    if os.environ.get("DEXCOST_TEST_OBJECT_STORAGE_NATIVE_CHILD") != "1":
        try:
            version("boto3")
        except PackageNotFoundError:
            pytest.skip("optional boto3 distribution is not installed")
        source = Path(__file__).resolve().parents[1] / "src"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"{Path(__file__).resolve()}::test_real_botocore_before_send_with_mock_transport",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(tmp_path / "native-child"),
            ],
            env={
                **os.environ,
                "PYTHONPATH": str(source),
                "DEXCOST_TEST_OBJECT_STORAGE_NATIVE_CHILD": "1",
            },
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout, result.stdout + result.stderr
        return
    import boto3
    from botocore.awsrequest import AWSResponse

    tracker, _ = setup
    # Synthetic credentials and an in-memory HTTP transport: never contacts AWS.
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
    )

    class Raw:
        def read(self, amt=None):
            return b"test"

        def stream(self, **kwargs):
            yield b"test"

    sent = []

    def send(request):
        sent.append(1)
        return AWSResponse(
            request.url, 200, {"x-amz-request-id": "real-botocore", "content-length": "4"}, Raw()
        )

    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    wrapped = instrument_object_storage(client, tracker, **scope("aws_s3"))
    response = wrapped.get_object(Bucket=DATA["bucket"], Key="PRIVATE-KEY")
    assert response["Body"].read() == b"test" and sent == [1]
    key = f"{DATA['aws_payer']}/{DATA['aws_owner']}.us-east-1.{DATA['bucket']}/real-botocore"
    assert tracker.storage.get_provider_job("aws_s3", "object_storage", key) is not None
    assert object_storage._transport.get() is None
