import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import redis
import redis.asyncio
from pymongo import AsyncMongoClient, MongoClient

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.context import _current_task
from dexcost.instruments.database import (
    database_resource_id,
    instrument_redis_client,
    mongodb_command_listener,
)
from dexcost.models.task import Task

CASES = json.loads(
    (Path(__file__).parents[2] / "fixtures/native_database_conformance.json").read_text()
)["cases"]


@pytest.fixture
def recording():
    events = []
    tracker = SimpleNamespace(storage=SimpleNamespace(insert_event=events.append))
    token = _current_task.set(Task(task_type="agent"))
    try:
        yield tracker, events
    finally:
        _current_task.reset(token)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_shared_native_corpus(case, recording, monkeypatch):
    tracker, events = recording
    if case["service"] == "mongodb_atlas":
        listener = mongodb_command_listener(tracker, billing_account_id="acct", resource_id="db")
        # Real constructors enforce the official listener contract for BOTH APIs.
        with MongoClient(connect=False, event_listeners=[listener]):
            pass
        async_client = AsyncMongoClient(connect=False, event_listeners=[listener])
        asyncio.run(async_client.close())
        event = SimpleNamespace(
            connection_id=("host", 27017),
            request_id=3,
            command_name=case["command"],
            command={"secret": "value"},
        )
        listener.started(event)
        (listener.failed if case["failed"] else listener.succeeded)(event)
        listener.succeeded(event)  # duplicate notification must not duplicate money/usage
        listener.close()
    else:
        client = redis.Redis()

        def execute(*args, **kwargs):
            if case["failed"]:
                raise ValueError("secret-key and secret-value")
            return "private-response"

        monkeypatch.setattr(client, "_execute_command", execute)
        undo = instrument_redis_client(
            client, tracker, billing_account_id="acct", resource_id="db"
        )
        if case["failed"]:
            with pytest.raises(ValueError):
                client.execute_command(case["command"], "secret-key", "secret-value")
        else:
            assert client.execute_command(case["command"], "secret-key") == "private-response"
        undo()
        client.close()
    assert len(events) == 1
    ev = events[0]
    observation = to_attribution_observation_v3(ev)
    assert observation["provider"] == {"name": case["service"], "service": "database"}
    assert observation["resource"] == {"type": "endpoint", "id": "acct/db"}
    assert observation["usage"][0]["metric"] == f"{case['service']}.commands"
    assert observation["usage"][0]["quantity"] == "1"
    assert observation["operation"]["name"] == f"database.{case['category']}"
    assert observation["operation"]["status"] == ("failed" if case["failed"] else "succeeded")
    assert ev.cost_confidence == "unknown"
    assert "cost_evidence" not in observation
    assert "secret" not in json.dumps(ev.to_dict())
    assert "private-response" not in json.dumps(ev.to_dict())


def test_pipeline_dispatch_partial_failure_and_undo(recording, monkeypatch):
    tracker, events = recording
    monkeypatch.setattr(
        redis.client.Pipeline, "execute", lambda *_a, **_k: ["ok", ValueError("private")]
    )
    client = redis.Redis()
    undo = instrument_redis_client(client, tracker, billing_account_id="acct", resource_id="db")
    with pytest.raises(ValueError, match="already"):
        instrument_redis_client(client, tracker, billing_account_id="acct", resource_id="db")
    pipe = client.pipeline().set("private", "private").get("private")
    assert events == []
    pipe.execute(raise_on_error=False)
    assert len(events) == 1
    assert events[0].details["attribution_usage_lines"][0]["quantity"] == "2"
    assert events[0].details["attribution_operation_status"] == "failed"
    undo()
    pipe.execute()
    assert len(events) == 1


def test_mongo_duplicate_collectors_and_partial_failure(recording):
    tracker, events = recording
    listener = mongodb_command_listener(tracker, billing_account_id="acct", resource_id="db")
    assert (
        mongodb_command_listener(tracker, billing_account_id="acct", resource_id="db") is listener
    )
    event = SimpleNamespace(
        connection_id=("host", 27017),
        request_id=10,
        command_name="insert",
        reply={"ok": 1, "writeErrors": [{"secret": "x"}]},
    )
    listener.started(event)
    listener.succeeded(event)
    assert events[0].details["attribution_operation_status"] == "failed"
    listener.close()


@pytest.mark.asyncio
async def test_async_pipeline_counts_only_executed_commands(recording, monkeypatch):
    tracker, events = recording

    async def execute(*args, **kwargs):
        return ["ok", "ok"]

    monkeypatch.setattr(redis.asyncio.client.Pipeline, "execute", execute)
    client = redis.asyncio.Redis()
    undo = instrument_redis_client(client, tracker, billing_account_id="acct", resource_id="db")
    pipe = client.pipeline().set("private", "private").get("private")
    assert events == []
    await pipe.execute()
    assert events[0].details["attribution_usage_lines"][0]["quantity"] == "2"
    undo()
    await client.aclose()


@pytest.mark.asyncio
async def test_async_task_isolation_and_cancellation(recording, monkeypatch):
    tracker, events = recording

    async def execute(*args, **kwargs):
        await asyncio.sleep(0)
        if args[2] == "cancel":
            raise asyncio.CancelledError()
        return "ok"

    monkeypatch.setattr(redis.asyncio.Redis, "execute_command", execute)
    client = redis.asyncio.Redis()
    undo = instrument_redis_client(client, tracker, billing_account_id="acct", resource_id="db")
    tasks = [Task(task_type="a"), Task(task_type="b")]

    async def run(task, key):
        token = _current_task.set(task)
        try:
            await client.get(key)
        except asyncio.CancelledError:
            pass
        finally:
            _current_task.reset(token)

    await asyncio.gather(run(tasks[0], "ok"), run(tasks[1], "cancel"))
    assert {e.task_id for e in events} == {t.task_id for t in tasks}
    assert [e.details["attribution_operation_status"] for e in events] == ["succeeded", "failed"]
    undo()
    await client.aclose()


def test_no_task_no_capture_and_recorder_failure_is_safe(recording, monkeypatch):
    tracker, events = recording
    client = redis.Redis()
    monkeypatch.setattr(client, "_execute_command", lambda *a, **k: "ok")
    undo = instrument_redis_client(client, tracker, billing_account_id="acct", resource_id="db")
    token = _current_task.set(None)
    try:
        assert client.get("private") == "ok"
    finally:
        _current_task.reset(token)
    assert not events
    tracker.storage.insert_event = lambda _: (_ for _ in ()).throw(ValueError("storage"))
    assert client.get("private") == "ok"
    undo()


@pytest.mark.parametrize("value", ["", "user:password@host", "https://host/db", " x", "a" * 101])
def test_reject_connection_strings(value):
    with pytest.raises(ValueError):
        database_resource_id("acct", value)
