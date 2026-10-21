import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from dexcost.attribution import to_attribution_observation_v3
from dexcost.context import _current_task
from dexcost.instruments._capture import current_provider_capture_owner, provider_capture_scope
from dexcost.instruments.upstash_redis import (
    instrument_upstash_redis,
    uninstrument_upstash_redis,
    upstash_redis_resource_id,
)
from dexcost.models.task import Task

HOST = "agent-memory.upstash.io"
BINDING = dict(
    billing_account_id="account-a",
    region="us-east-1",
    database_id="database-a",
    endpoint_host=HOST,
    billing_plan="pay_as_you_go",
    topology="single_region",
)


@pytest.fixture
def setup():
    events = []
    tracker = SimpleNamespace(storage=SimpleNamespace(insert_event=events.append))
    task = Task(task_id=uuid.uuid4(), task_type="memory")
    token = _current_task.set(task)
    try:
        yield tracker, events, task
    finally:
        _current_task.reset(token)


async def client(asynchronous, response=None, status=200, **options):
    from upstash_redis import Redis
    from upstash_redis.asyncio import Redis as AsyncRedis

    native = (AsyncRedis if asynchronous else Redis)(
        url=f"https://{options.pop('host', HOST)}",
        token="PRIVATE-TOKEN",
        rest_encoding=None,
        rest_retries=options.pop("retries", 0),
        rest_retry_interval=0,
        allow_telemetry=False,
        **options,
    )
    calls = []

    def transport(request):
        calls.append((json.loads(request.content), current_provider_capture_owner()))
        return httpx.Response(status, json={"result": None} if response is None else response)

    if asynchronous:
        await native._http._client.aclose()
        native._http._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    else:
        native._http._client.close()
        native._http._client = httpx.Client(transport=httpx.MockTransport(transport))
    return native, calls


async def close(native, asynchronous):
    if asynchronous:
        await native.close()
    else:
        native.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "method,args,response,expected",
    [
        ("get", ("PRIVATE-KEY",), {"result": None}, None),
        ("set", ("PRIVATE-KEY", "PRIVATE-VALUE"), {"result": "OK"}, True),
        ("mget", ("PRIVATE-1", "PRIVATE-2"), {"result": ["one", None]}, ["one", None]),
        ("delete", ("PRIVATE-1", "PRIVATE-2"), {"result": 2}, 2),
        ("exists", ("PRIVATE-KEY",), {"result": 1}, 1),
        ("incr", ("PRIVATE-KEY",), {"result": 2}, 2),
    ],
)
def test_native_one_command_not_keys_or_http_count(
    setup, asynchronous, method, args, response, expected
):
    tracker, events, task = setup

    async def run():
        native, calls = await client(asynchronous, response)
        original_http = native._http
        tracked = instrument_upstash_redis(native, tracker, **BINDING)
        result = getattr(tracked, method)(*args)
        if asynchronous:
            result = await result
        assert result == expected
        assert len(calls) == 1 and calls[0][1] == "upstash_redis"
        assert native._http is original_http
        assert len(events) == 1
        event = to_attribution_observation_v3(events[0])
        assert event["task_id"] == str(task.task_id)
        assert event["provider"] == {
            "name": "upstash_redis",
            "service": "redis",
            "region": "us-east-1",
        }
        assert event["usage"][0]["metric"] == "upstash_redis.payg_single_region_commands"
        assert event["usage"][0]["quantity"] == "1"
        assert event["usage"][0]["unit"] == "Commands"
        assert (
            event["resource"]["id"]
            == "account-a/fce4ffd12fd1d7649a4d7c2e40a7a841a994ad08fb872eb172d549061cd417f6"
        )
        assert "PRIVATE" not in json.dumps(event)
        assert "reported_cost" not in event
        await close(native, asynchronous)

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "reason",
    [
        "retry",
        "route",
        "disabled",
        "failed",
        "missing-result",
        "status",
        "redirects",
        "no-task",
        "outer-capture",
    ],
)
def test_native_ambiguous_evidence_stays_unpriced(setup, asynchronous, reason):
    tracker, events, _task = setup

    async def run():
        response = (
            {"error": "PRIVATE failure"}
            if reason == "failed"
            else {}
            if reason == "missing-result"
            else {"result": "OK"}
        )
        native, calls = await client(
            asynchronous,
            response,
            status=401 if reason == "status" else 200,
            retries=1 if reason == "retry" else 0,
            host="other.upstash.io" if reason == "route" else HOST,
        )
        if reason == "redirects":
            native._http._client.follow_redirects = True
        tracked = instrument_upstash_redis(native, tracker, **BINDING)
        if reason == "disabled":
            uninstrument_upstash_redis(tracked)
        token = _current_task.set(None) if reason == "no-task" else None

        async def execute():
            try:
                result = tracked.get("PRIVATE")
                if asynchronous:
                    await result
            except Exception:
                pass  # Caller recovery is never provider success evidence.

        if reason == "outer-capture":
            with provider_capture_scope("outer"):
                await execute()
        else:
            await execute()
        if token is not None:
            _current_task.reset(token)
        assert len(calls) == 1
        assert events == []
        await close(native, asynchronous)

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_operational_pipeline_and_raw_execute_delegate_without_capture(setup, asynchronous):
    tracker, events, _ = setup

    async def run():
        native, calls = await client(asynchronous, {"result": "PONG"})
        tracked = instrument_upstash_redis(native, tracker, **BINDING)
        response = tracked.ping()
        if asynchronous:
            response = await response
        assert response == "PONG"
        response = tracked.execute(["GET", "PRIVATE"])
        if asynchronous:
            await response
        # A pipeline object remains native; it isn't one command or one task.
        assert tracked.pipeline().__class__.__module__.startswith("upstash_redis")
        assert len(calls) == 2
        assert events == []
        await close(native, asynchronous)

    asyncio.run(run())


def test_concurrent_tasks_and_disable_preserve_ownership(setup):
    tracker, events, _ = setup

    async def run():
        native, _calls = await client(True, {"result": "OK"})
        tracked = instrument_upstash_redis(native, tracker, **BINDING)
        tasks = [Task(task_id=uuid.uuid4(), task_type="memory") for _ in range(2)]

        async def work(task):
            token = _current_task.set(task)
            try:
                return await tracked.get("PRIVATE")
            finally:
                _current_task.reset(token)

        assert await asyncio.gather(*(work(task) for task in tasks)) == ["OK", "OK"]
        assert {event.task_id for event in events} == {task.task_id for task in tasks}
        uninstrument_upstash_redis(tracked)
        uninstrument_upstash_redis(tracked)
        assert await tracked.get("PRIVATE") == "OK"
        assert len(events) == 2
        await close(native, True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes",
    [
        {"billing_plan": "free"},
        {"billing_plan": "fixed"},
        {"topology": "global"},
        {"endpoint_host": "agent-memory.upstash.io.attacker.example"},
        {"region": ""},
    ],
)
def test_invalid_binding_is_not_inferred(setup, changes):
    tracker, _, _ = setup
    with pytest.raises(ValueError):
        instrument_upstash_redis(object(), tracker, **(BINDING | changes))


def test_identity_isolates_account_database_region_and_host():
    args = ["account-a", "us-east-1", "database-a", HOST]
    original = upstash_redis_resource_id(*args)
    for index, value in enumerate(["account-b", "eu-west-1", "database-b", "other.upstash.io"]):
        changed = args.copy()
        changed[index] = value
        assert upstash_redis_resource_id(*changed) != original
