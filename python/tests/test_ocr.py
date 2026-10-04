import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost import instrument_document_ai, instrument_textract, uninstrument_document_ai
from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.context import _current_task
from dexcost.instruments import ocr
from dexcost.instruments._capture import provider_capture_scope
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage

DATA = json.loads((Path(__file__).parents[2] / "fixtures/ocr_conformance.json").read_text())
AWS_SCOPE = dict(
    billing_account_id=DATA["aws_payer"],
    usage_account_id=DATA["aws_usage_account"],
    region=DATA["region"],
)
GOOGLE_SCOPE = dict(
    billing_account_id=DATA["google_account"],
    processor_version=DATA["processor"],
    processor_type="OCR_PROCESSOR",
)
AWS_RESOURCE = (
    f"{DATA['aws_payer']}/{DATA['aws_usage_account']}.{DATA['region']}.detect_document_text"
)


@pytest.fixture
def setup(tmp_path):
    storage = SQLiteStorage(tmp_path / "ocr.db")
    task = Task(task_id=uuid.UUID(DATA["task_id"]), task_type="ocr")
    storage.insert_task(task)
    token = _current_task.set(task)
    try:
        yield SimpleNamespace(storage=storage), task
    finally:
        _current_task.reset(token)
        storage.close()


def aws(case, asynchronous=False):
    class Events:
        callback = None

        def register(self, name, callback, unique_id):
            self.callback = callback

    events = Events()
    result = dict(
        DocumentMetadata={"Pages": case.get("pages")},
        Blocks=[{"Text": "PRIVATE"}],
        ResponseMetadata=dict(
            RequestId=case["id"],
            HTTPStatusCode=case.get("status", 200),
            RetryAttempts=case.get("attempts", 1) - 1,
        ),
    )

    def invoke(**kwargs):
        for _ in range(case.get("transport_attempts", 1)):
            events.callback(
                SimpleNamespace(
                    url=f"https://{case.get('host', 'textract.us-east-1.amazonaws.com')}/",
                    method="POST",
                )
            )
        return result

    async def awaited(**kwargs):
        await asyncio.sleep(0)
        return invoke(**kwargs)

    client = SimpleNamespace(
        meta=SimpleNamespace(
            events=events,
            service_model=SimpleNamespace(service_name="textract"),
            region_name=case.get("region", DATA["region"]),
        ),
        detect_document_text=awaited if asynchronous else invoke,
    )
    return client, result


def google(case, asynchronous=False):
    request = dict(
        name=case.get("processor", DATA["processor"]),
        raw_document=dict(mime_type="application/pdf", content=b"PRIVATE"),
    )
    if case.get("mask"):
        request["field_mask"] = {"paths": ["text"]}
    if case.get("options"):
        request["process_options"] = {
            "ocr_config": {"premium_features": {"enable_math_ocr": True}}
        }
    result = dict(
        document=dict(
            pages=[dict(page_number=n, text="PRIVATE") for n in case["pages"]]
            if "pages" in case
            else None,
            text="PRIVATE",
            error={"code": case.get("error", 0)},
        )
    )

    def invoke(*args, **kwargs):
        return result

    async def awaited(*args, **kwargs):
        await asyncio.sleep(0)
        return result

    client = SimpleNamespace(
        transport=SimpleNamespace(_host=case.get("host", "us-documentai.googleapis.com:443")),
        process_document=awaited if asynchronous else invoke,
    )
    return client, request, result, {} if case.get("default_retry") else {"retry": None}


@pytest.mark.parametrize("case", DATA["textract"], ids=lambda c: c["id"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_textract(setup, case, asynchronous):
    tracker, _ = setup
    client, result = aws(case, asynchronous)
    wrapped = instrument_textract(client, tracker, **AWS_SCOPE)
    value = wrapped.detect_document_text(Document={"Bytes": b"PRIVATE"})
    assert (asyncio.run(value) if asynchronous else value) is result
    job = tracker.storage.get_provider_job(
        "amazon_textract", "ocr", f"{AWS_RESOURCE}/{case['id']}"
    )
    if not case.get("capture"):
        assert job is None
        return
    event = job.to_attribution_observation()
    assert event["usage"][0]["quantity"] == "2"
    assert event["resource"]["id"] == AWS_RESOURCE
    assert "cost_evidence" not in event
    assert "PRIVATE" not in json.dumps(job.to_dict())
    value = wrapped.detect_document_text(Document={"Bytes": b"PRIVATE"})
    if asynchronous:
        asyncio.run(value)
    assert len(tracker.storage.query_provider_job_history(str(job.event_id))) == 1


@pytest.mark.parametrize("case", DATA["document_ai"], ids=lambda c: c["id"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_document_ai(setup, case, asynchronous):
    tracker, _ = setup
    client, request, result, call = google(case, asynchronous)
    value = instrument_document_ai(client, tracker, **GOOGLE_SCOPE).process_document(
        request=request, **call
    )
    assert (asyncio.run(value) if asynchronous else value) is result
    events = tracker.storage.query_events_for_sync()
    assert len(events) == (1 if case.get("capture") else 0)
    if case.get("capture"):
        event = to_attribution_observation_v3(events[0])
        assert event["usage"][0]["quantity"] == "2"
        assert (
            event["resource"]["id"]
            == f"{DATA['google_account']}/agent-project.us.abc123.enterprise_ocr"
        )
        assert "cost_evidence" not in event
        assert "PRIVATE" not in json.dumps(events[0].to_dict())


def test_interval_and_zero_duration(setup, monkeypatch):
    tracker, _ = setup
    times = iter(
        map(
            datetime.fromisoformat,
            [
                "2026-09-30T23:59:59+00:00",
                "2026-10-01T00:00:01+00:00",
                "2026-10-01T00:00:01+00:00",
                "2026-10-01T00:00:01+00:00",
            ],
        )
    )
    monkeypatch.setattr(ocr, "_now", lambda: next(times))
    client, request, _, call = google(DATA["document_ai"][0])
    wrapped = instrument_document_ai(client, tracker, **GOOGLE_SCOPE)
    wrapped.process_document(request, **call)
    wrapped.process_document(request, **call)
    periods = [
        to_attribution_observation_v3(e)["usage_period"]
        for e in tracker.storage.query_events_for_sync()
    ]
    assert periods[0] == {
        "start_at": "2026-09-30T23:59:59.000000Z",
        "end_at": "2026-10-01T00:00:01.000000Z",
    }
    assert periods[1] == {
        "start_at": "2026-10-01T00:00:01.000000Z",
        "end_at": "2026-10-01T00:00:01.000000Z",
    }


def test_transparency(setup):
    tracker, _ = setup
    client, request, result, call = google(DATA["document_ai"][0])
    wrapped = instrument_document_ai(client, tracker, **GOOGLE_SCOPE)
    with provider_capture_scope("outer"):
        assert wrapped.process_document(request, **call) is result
    uninstrument_document_ai(wrapped)
    assert wrapped.process_document(request, **call) is result
    assert tracker.storage.query_events_for_sync() == []
    error = RuntimeError("PRIVATE")

    def fail(*args, **kwargs):
        raise error

    client.process_document = fail
    with pytest.raises(RuntimeError) as caught:
        instrument_document_ai(client, tracker, **GOOGLE_SCOPE).process_document(request, **call)
    assert caught.value is error
    assert tracker.storage.query_events_for_sync() == []


def test_actual_native_packages(setup, monkeypatch, tmp_path):
    # Isolate native dependencies from older tests which replace provider modules.
    if os.environ.get("DEXCOST_OCR_NATIVE_CHILD") != "1":
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"{Path(__file__).resolve()}::test_actual_native_packages",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(tmp_path / "child"),
            ],
            env={**os.environ, "DEXCOST_OCR_NATIVE_CHILD": "1"},
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        return
    import boto3
    from botocore.awsrequest import AWSResponse
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import documentai_v1

    tracker, _ = setup
    aws_client = boto3.client(
        "textract",
        region_name=DATA["region"],
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
    )

    class Raw:
        def stream(self, **kwargs):
            yield b'{"DocumentMetadata":{"Pages":2},"Blocks":[{"Text":"PRIVATE"}]}'

    monkeypatch.setattr(
        aws_client._endpoint.http_session,
        "send",
        lambda request: AWSResponse(request.url, 200, {"x-amzn-requestid": "real-sdk"}, Raw()),
    )
    response = instrument_textract(aws_client, tracker, **AWS_SCOPE).detect_document_text(
        Document={"Bytes": b"PRIVATE"}
    )
    assert response["DocumentMetadata"]["Pages"] == 2
    assert (
        tracker.storage.get_provider_job("amazon_textract", "ocr", f"{AWS_RESOURCE}/real-sdk")
        is not None
    )

    for asynchronous in [False, True]:

        async def run():
            cls = (
                documentai_v1.DocumentProcessorServiceAsyncClient
                if asynchronous
                else documentai_v1.DocumentProcessorServiceClient
            )
            client = cls(
                credentials=AnonymousCredentials(),
                client_options={"api_endpoint": "us-documentai.googleapis.com"},
            )
            response = documentai_v1.ProcessResponse(
                document={"pages": [{"page_number": 1}, {"page_number": 2}], "text": "PRIVATE"}
            )
            calls = []

            def rpc(request, **kwargs):
                calls.append(request.name)
                assert kwargs["retry"] is None
                return response

            async def async_rpc(request, **kwargs):
                return rpc(request, **kwargs)

            client.transport._wrapped_methods[client.transport.process_document] = (
                async_rpc if asynchronous else rpc
            )
            request = documentai_v1.ProcessRequest(
                name=DATA["processor"],
                raw_document={"mime_type": "application/pdf", "content": b"PRIVATE"},
            )
            result = instrument_document_ai(client, tracker, **GOOGLE_SCOPE).process_document(
                request, retry=None
            )
            assert (await result if asynchronous else result) is response
            assert calls == [DATA["processor"]]
            closed = client.transport.close()
            if asyncio.iscoroutine(closed):
                await closed

        asyncio.run(run())
    assert len(tracker.storage.query_events_for_sync()) == 2


def test_google_async_ownership_and_account_override(setup):
    tracker, owner = setup
    client, request, response, call = google(DATA["document_ai"][0], asynchronous=True)
    wrapped = instrument_document_ai(client, tracker, **GOOGLE_SCOPE)
    pending = wrapped.process_document(request, **call)
    token = _current_task.set(Task(task_type="other"))
    try:
        assert asyncio.run(pending) is response
    finally:
        _current_task.reset(token)
    events = tracker.storage.query_events_for_sync()
    assert len(events) == 1 and events[0].task_id == owner.task_id
    asyncio.run(
        wrapped.process_document(request, retry=None, metadata=(("x-goog-user-project", "other"),))
    )
    assert len(tracker.storage.query_events_for_sync()) == 1


@pytest.mark.parametrize("change", ["shard", "inline", "format"])
def test_google_partial_or_unsupported(setup, change):
    tracker, _ = setup
    client, request, response, call = google(DATA["document_ai"][0])
    if change == "shard":
        response["document"]["shard_info"] = {"shard_count": 2}
    if change == "inline":
        request["inline_document"] = {"text": "PRIVATE"}
    if change == "format":
        request["raw_document"]["mime_type"] = "text/html"
    assert (
        instrument_document_ai(client, tracker, **GOOGLE_SCOPE).process_document(request, **call)
        is response
    )
    assert tracker.storage.query_events_for_sync() == []
