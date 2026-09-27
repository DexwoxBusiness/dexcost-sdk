import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost import (
    bind_browser_session,
    instrument_browserbase,
    record_browser_session,
    uninstrument_browserbase,
)
from dexcost.context import _current_task
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage

DATA = json.loads(
    (Path(__file__).parents[2] / "fixtures/browser_session_conformance.json").read_text()
)
START = datetime.fromisoformat(DATA["started_at"].replace("Z", "+00:00"))
END = datetime.fromisoformat(DATA["ended_at"].replace("Z", "+00:00"))


@pytest.fixture
def setup(tmp_path):
    storage = SQLiteStorage(tmp_path / "browser.db")
    task = Task(task_id=uuid.UUID(DATA["task_id"]), task_type="browser")
    storage.insert_task(task)
    token = _current_task.set(task)
    try:
        yield SimpleNamespace(storage=storage), task
    finally:
        _current_task.reset(token)
        storage.close()


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["id"])
def test_paired_meters_durable_identity_and_replay(case, setup):
    tracker, task = setup
    identity = dict(
        service_key=case["service"], billing_account_id="account", session_id=case["id"]
    )
    binding = dict(
        **identity, resource_id="resource", started_at=START, managed_proxy=case["managed"]
    )
    assert bind_browser_session(tracker, **binding)
    token = _current_task.set(None)
    try:
        assert record_browser_session(
            tracker, **identity, ended_at=END, status=case["status"], **case["meters"]
        )
        assert record_browser_session(
            tracker, **identity, ended_at=END, status=case["status"], **case["meters"]
        )
    finally:
        _current_task.reset(token)
    job = tracker.storage.get_provider_job(case["service"], "browser", "account/" + case["id"])
    assert job.revision == 2
    assert job.task_id == task.task_id
    assert bind_browser_session(tracker, **binding)
    obs = job.to_attribution_observation()
    assert {u["metric"]: u["quantity"] for u in obs["usage"]} == case["usage"]
    assert "cost_evidence" not in obs
    assert obs["resource"]["id"] == "account/resource"
    assert datetime.fromisoformat(obs["usage_period"]["end_at"].replace("Z", "+00:00")) == END
    assert len(tracker.storage.query_provider_job_history(str(job.event_id))) == 2


def test_ownership_validation_corrections_and_restart(setup, tmp_path):
    tracker, _ = setup
    identity = dict(
        service_key="browserless", billing_account_id="account", session_id="connection"
    )
    binding = dict(**identity, resource_id="resource", started_at=START)
    assert bind_browser_session(tracker, **binding)
    with pytest.raises(ValueError):
        bind_browser_session(tracker, **{**binding, "resource_id": "wrong"})
    token = _current_task.set(Task(task_type="wrong"))
    try:
        with pytest.raises(ValueError):
            bind_browser_session(tracker, **binding)
    finally:
        _current_task.reset(token)
    for bad in ("-1", "NaN", "1e3", "0.0000000000001", True, 1.0):
        with pytest.raises(ValueError):
            record_browser_session(
                tracker, **identity, ended_at=END, status="succeeded", time_units=bad
            )
    with pytest.raises(ValueError):
        record_browser_session(
            tracker, **identity, ended_at=END, status="succeeded", proxy_bytes="1"
        )
    second = SQLiteStorage(tmp_path / "browser.db")
    try:
        restarted = SimpleNamespace(storage=second)
        for revision, amount in enumerate(("3", "0", "2"), start=2):
            record_browser_session(
                restarted,
                **identity,
                ended_at=END,
                status="succeeded",
                time_units=amount,
                revision=revision,
            )
        job = second.get_provider_job("browserless", "browser", "account/connection")
        assert job.revision == 4
        assert str(job.usage[0].quantity) == "2"
        assert not record_browser_session(
            restarted, **identity, ended_at=END, status="succeeded", time_units="3", revision=2
        )
        with pytest.raises(ValueError):
            record_browser_session(
                restarted, **identity, ended_at=END, status="succeeded", time_units="3", revision=4
            )
    finally:
        second.close()
    for field in ("session_id", "resource_id", "billing_account_id"):
        with pytest.raises(ValueError):
            bind_browser_session(tracker, **{**binding, field: "wss://secret?token=private"})


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_facade_terminal_only_no_secrets_or_provider_side_effects(setup, asynchronous):
    tracker, _ = setup
    response = dict(
        id="session",
        projectId="project",
        startedAt=START,
        status="RUNNING",
        expiresAt="2099-01-01",
        connectUrl="wss://private",
        signingKey="private",
        proxyBytes=25,
    )

    class Sessions:
        def create(self, **kwargs):
            return response

        def retrieve(self, session):
            return response

        def update(self, session, **kwargs):
            return response

    sessions = Sessions()
    if asynchronous:
        for name in ("create", "retrieve", "update"):
            fn = getattr(sessions, name)

            async def wrapped(*args, _fn=fn, **kwargs):
                return _fn(*args, **kwargs)

            setattr(sessions, name, wrapped)
    facade = instrument_browserbase(
        SimpleNamespace(sessions=sessions), tracker, billing_account_id="account"
    )

    def call(name, *args, **kwargs):
        result = getattr(facade.sessions, name)(*args, **kwargs)
        return asyncio.run(result) if asynchronous else result

    assert call("create", proxies=True) is response
    assert call("update", "session", status="REQUEST_RELEASE") is response
    job = tracker.storage.get_provider_job("browserbase", "browser", "account/session")
    assert job.revision == 1 and not job.usage
    response.update(status="TIMED_OUT", endedAt=END)
    assert call("retrieve", "session") is response
    assert call("retrieve", "session") is response
    job = tracker.storage.get_provider_job("browserbase", "browser", "account/session")
    assert job.revision == 2 and job.status == "failed"
    assert "private" not in json.dumps(job.to_dict())
    response["proxyBytes"] = 1  # stale terminal polling cannot revise usage
    call("retrieve", "session")
    assert tracker.storage.get_provider_job("browserbase", "browser", "account/session") == job
    uninstrument_browserbase(facade)
    response["proxyBytes"] = 26
    call("retrieve", "session")
    assert (
        tracker.storage.get_provider_job("browserbase", "browser", "account/session").revision == 2
    )
    with pytest.raises(ValueError):
        instrument_browserbase(facade, tracker, billing_account_id="account")


def test_unknown_sessions_external_proxy_and_telemetry_failure(setup):
    tracker, _ = setup
    identity = dict(service_key="browserbase", billing_account_id="account", session_id="session")
    assert not record_browser_session(tracker, **identity, ended_at=END, status="failed")
    bind_browser_session(tracker, **identity, resource_id="resource", started_at=START)
    with pytest.raises(ValueError):
        record_browser_session(
            tracker, **identity, ended_at=END, status="failed", proxy_bytes="10"
        )
    with pytest.raises(ValueError):
        record_browser_session(tracker, **identity, ended_at=END, status="failed", time_units="10")
    with pytest.raises(ValueError):
        record_browser_session(tracker, **identity, ended_at=END, status="failed", revision=3)
    result = dict(id="new", projectId="resource", startedAt=START, status="COMPLETED", endedAt=END)
    error = RuntimeError("private-error")

    class Sessions:
        def create(self, **kwargs):
            return result

        def retrieve(self, session):
            raise error

    broken = SimpleNamespace(storage=SimpleNamespace(get_provider_job=lambda *args: 1 / 0))
    facade = instrument_browserbase(
        SimpleNamespace(sessions=Sessions()), broken, billing_account_id="account"
    )
    assert facade.sessions.create() is result
    with pytest.raises(RuntimeError) as caught:
        facade.sessions.retrieve("new")
    assert caught.value is error
