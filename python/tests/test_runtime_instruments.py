import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost import instrument_e2b_sandbox, uninstrument_e2b_sandbox, wrap_runtime_handler
from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.context import _current_task
from dexcost.models.task import Task

CASES = json.loads((Path(__file__).parents[2] / "fixtures/runtime_conformance.json").read_text())[
    "cases"
]


@pytest.fixture
def recording(monkeypatch):
    events, clock = [], [0]
    monkeypatch.setattr(
        "dexcost.instruments.runtime.time.monotonic_ns", lambda: clock[0] * 1_000_000
    )
    tracker = SimpleNamespace(storage=SimpleNamespace(insert_event=events.append))
    token = _current_task.set(Task(task_type="agent"))
    try:
        yield tracker, events, clock
    finally:
        _current_task.reset(token)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_shared_runtime_corpus(case, recording):
    tracker, events, clock = recording
    error = ValueError("private-code")

    def sync(arg):
        clock[0] += case["ms"]
        if case["failed"]:
            raise error
        return arg

    async def asynchronous(arg):
        return sync(arg)

    fn = wrap_runtime_handler(
        asynchronous if case["async"] else sync,
        tracker,
        service_key=case["service"],
        billing_account_id="acct",
        resource_id="resource",
        vcpu_count=2,
        memory_mib=4096,
    )

    def run():
        return asyncio.run(fn("private-result")) if case["async"] else fn("private-result")

    if case["failed"]:
        with pytest.raises(ValueError) as caught:
            run()
        assert caught.value is error
    else:
        assert run() == "private-result"
    if case["quantity"] is None:
        assert events == []
        return
    assert len(events) == 1
    obs = to_attribution_observation_v3(events[0])
    assert obs["provider"] == {"name": case["service"], "service": "runtime"}
    assert obs["resource"] == {"type": "instance", "id": "acct/resource"}
    assert obs["usage"][0]["quantity"] == case["quantity"]
    assert obs["usage"][0]["metric"] == "runtime.task_seconds"
    assert obs["operation"]["status"] == ("failed" if case["failed"] else "succeeded")
    assert "cost_evidence" not in obs
    assert events[0].cost_confidence == "unknown"
    assert "private" not in json.dumps(events[0].to_dict())
    period = obs["usage_period"]
    elapsed = datetime.fromisoformat(
        period["end_at"].replace("Z", "+00:00")
    ) - datetime.fromisoformat(period["start_at"].replace("Z", "+00:00"))
    assert elapsed == timedelta(milliseconds=case["ms"])


def test_nested_missing_task_and_telemetry_failure(recording):
    tracker, events, clock = recording
    config = dict(service_key="modal_compute", billing_account_id="acct", resource_id="app")

    def work():
        clock[0] += 10
        return 42

    inner = wrap_runtime_handler(work, tracker, **config)
    outer = wrap_runtime_handler(inner, tracker, **config)
    assert outer() == 42
    assert len(events) == 1
    token = _current_task.set(None)
    try:
        assert outer() == 42
    finally:
        _current_task.reset(token)
    assert len(events) == 1
    tracker.storage.insert_event = lambda _: (_ for _ in ()).throw(RuntimeError("storage"))
    assert outer() == 42


def test_async_e2b_command_and_failed_pause(recording):
    tracker, events, clock = recording

    async def run(command):
        clock[0] += 100
        return command

    async def pause():
        raise RuntimeError("provider pause unavailable")

    raw = SimpleNamespace(sandbox_id="sb-async", commands=SimpleNamespace(run=run), pause=pause)
    tracked = instrument_e2b_sandbox(raw, tracker, billing_account_id="acct")
    assert asyncio.run(tracked.commands.run("private")) == "private"
    with pytest.raises(RuntimeError, match="pause unavailable"):
        asyncio.run(tracked.pause())
    assert len(events) == 1
    assert "private" not in json.dumps(events[0].to_dict())


def test_e2b_facade_background_lifecycle_and_close(recording):
    tracker, events, clock = recording
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        clock[0] += 100
        return "private-output"

    raw = SimpleNamespace(
        sandbox_id="sb-1",
        commands=SimpleNamespace(run=run),
        run_code=run,
        pause=lambda: calls.append("pause"),
        kill=lambda: calls.append("kill"),
        get_info=lambda: SimpleNamespace(end_at="2099-01-01"),
    )
    sandbox = instrument_e2b_sandbox(raw, tracker, billing_account_id="acct")
    assert calls == []  # no provider request or timeout-derived usage at setup
    assert sandbox.commands.run("private-command") == "private-output"
    sandbox.run_code("private-code")
    sandbox.commands.run("background", background=True)
    sandbox.get_info()
    sandbox.pause()
    sandbox.kill()
    assert len(events) == 2
    with pytest.raises(ValueError, match="already"):
        instrument_e2b_sandbox(sandbox, tracker, billing_account_id="acct")
    uninstrument_e2b_sandbox(sandbox)
    uninstrument_e2b_sandbox(sandbox)
    uninstrument_e2b_sandbox(raw)  # raw provider objects are never closed/killed
    sandbox.run_code("after-close")
    assert len(events) == 2
    assert raw.run_code("raw") == "private-output"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_generator_setup_is_not_completed_work(recording, asynchronous):
    tracker, events, clock = recording

    def setup():
        clock[0] += 100
        return (x for x in [1, 2])

    async def async_setup():
        return setup()

    wrapped = wrap_runtime_handler(
        async_setup if asynchronous else setup,
        tracker,
        service_key="modal_compute",
        billing_account_id="acct",
        resource_id="app",
    )
    assert list(asyncio.run(wrapped()) if asynchronous else wrapped()) == [1, 2]
    assert events == []


def test_cancelled_async_work_does_not_claim_provider_termination(recording):
    tracker, events, clock = recording

    async def work():
        clock[0] += 100
        raise asyncio.CancelledError()

    wrapped = wrap_runtime_handler(
        work, tracker, service_key="modal_compute", billing_account_id="acct", resource_id="app"
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(wrapped())
    assert len(events) == 1
    assert (
        events[0].details["runtime_capture_basis"] == "observed_task_wall_time_not_billed_runtime"
    )


@pytest.mark.parametrize(
    "config",
    [
        {"billing_account_id": "https://private"},
        {"vcpu_count": True},
        {"memory_mib": 0},
        {"service_key": "unknown"},
    ],
)
def test_invalid_explicit_config(config, recording):
    tracker, _, _ = recording
    options = dict(service_key="modal_compute", billing_account_id="acct", resource_id="app")
    options.update(config)
    with pytest.raises(ValueError):
        wrap_runtime_handler(lambda: None, tracker, **options)
