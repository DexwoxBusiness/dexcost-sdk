import asyncio
import json
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost import (
    bind_llamaparse_job,
    instrument_llamaparse,
    record_llamaparse_job,
    uninstrument_llamaparse,
)
from dexcost.context import _current_task
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage

DATA = json.loads(
    (Path(__file__).parents[2] / "fixtures/document_parse_conformance.json").read_text()
)
SCOPE = {"billing_account_id": DATA["account"], "project_id": DATA["project"]}


@pytest.fixture
def setup(tmp_path):
    storage = SQLiteStorage(tmp_path / "parse.db")
    task = Task(task_id=uuid.UUID(DATA["task_id"]), task_type="parse")
    storage.insert_task(task)
    token = _current_task.set(task)
    try:
        yield SimpleNamespace(storage=storage), task
    finally:
        _current_task.reset(token)
        storage.close()


def latest(tracker):
    return tracker.storage.get_provider_job("llamaparse", "parse", "organization_one/pjb-one")


def test_full_snapshot_replay_zero_restore_and_no_content_or_money(setup):
    tracker, task = setup
    response = deepcopy(DATA["response"])
    assert bind_llamaparse_job(tracker, response, **SCOPE)
    token = _current_task.set(None)
    try:
        for revision, credits in [(2, 30.5), (2, 30.5), (3, 0), (4, 15)]:
            response["job"]["usage"]["credits"] = credits
            assert record_llamaparse_job(tracker, response, revision=revision, **SCOPE)
            assert (
                latest(tracker).usage == ()
                if credits == 0
                else latest(tracker).usage[0].quantity == credits
            )
    finally:
        _current_task.reset(token)
    job = latest(tracker)
    assert job.task_id == task.task_id and job.revision == 4
    assert len(tracker.storage.query_provider_job_history(str(job.event_id))) == 4
    observation = job.to_attribution_observation()
    assert observation["usage_period"] == DATA["expected_period"]
    assert "cost_evidence" not in observation
    assert "never-retain" not in json.dumps(observation)
    with pytest.raises(ValueError):
        bind_llamaparse_job(tracker, {"job": {**response["job"], "tier": "fast"}}, **SCOPE)


@pytest.mark.parametrize("usage", [None, {}, {"credits": None}])
def test_missing_meter_remains_pending_not_zero(setup, usage):
    tracker, _ = setup
    response = deepcopy(DATA["response"])
    assert bind_llamaparse_job(tracker, response, **SCOPE)
    response["job"]["usage"] = usage
    assert not record_llamaparse_job(tracker, response, **SCOPE)
    assert latest(tracker).revision == 1


@pytest.mark.parametrize(
    "value", [True, -1, float("inf"), float("nan"), 1e-13, 9007199254740992, "0x10"]
)
def test_rejects_invalid_credit_meters(setup, value):
    tracker, _ = setup
    response = deepcopy(DATA["response"])
    bind_llamaparse_job(tracker, response, **SCOPE)
    response["job"]["usage"]["credits"] = value
    with pytest.raises((ValueError, ArithmeticError)):
        record_llamaparse_job(tracker, response, **SCOPE)
    assert latest(tracker).revision == 1


@pytest.mark.parametrize("credits", [1e-7, 1e-12, "0.000000000001", "30.5000000000000"])
def test_exact_decimal_domain(setup, credits):
    tracker, _ = setup
    response = deepcopy(DATA["response"])
    bind_llamaparse_job(tracker, response, **SCOPE)
    response["job"]["usage"]["credits"] = credits
    assert record_llamaparse_job(tracker, response, **SCOPE)


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "FAILED", "CANCELLED", "unknown"])
def test_only_verified_completed_credit_snapshot_is_final(setup, status):
    tracker, _ = setup
    response = deepcopy(DATA["response"])
    bind_llamaparse_job(tracker, response, **SCOPE)
    response["job"]["status"] = status
    assert not record_llamaparse_job(tracker, response, **SCOPE)


def test_native_sync_create_async_get_no_polling_task_theft_or_options_mutation(setup):
    tracker, task = setup
    calls = []
    response = deepcopy(DATA["response"])

    class Parsing:
        def create(self, **kwargs):
            calls.append((self, kwargs))
            return {k: v for k, v in response["job"].items() if k not in {"tier", "usage"}}

        async def get(self, job_id, **kwargs):
            calls.append((self, job_id, kwargs))
            await asyncio.sleep(0)
            return response

    native = Parsing()
    client = instrument_llamaparse(SimpleNamespace(parsing=native), tracker, **SCOPE)
    client.parsing.create(tier="agentic", source_url="never-retain-url")
    other = Task(task_type="poller")
    tracker.storage.insert_task(other)
    token = _current_task.set(other)
    try:
        response["job"]["usage"]["credits"] = None
        assert asyncio.run(client.parsing.get("pjb-one", expand=["usage"])) is response
        assert latest(tracker).revision == 1
        response["job"]["usage"]["credits"] = 30.5
        assert asyncio.run(client.parsing.get("pjb-one", expand=["usage"])) is response
        assert asyncio.run(client.parsing.get("pjb-one", expand=["usage"])) is response
    finally:
        _current_task.reset(token)
    assert latest(tracker).revision == 2 and latest(tracker).task_id == task.task_id
    assert len(calls) == 4 and all(call[0] is native for call in calls)
    assert calls[1][2] == {"expand": ["usage"]}
    uninstrument_llamaparse(client)
    assert client.parsing is native


def test_native_parse_and_invalid_account_or_unbound_get_fail_open(setup):
    tracker, _ = setup
    response = deepcopy(DATA["response"])
    native = SimpleNamespace(parse=lambda **_: response, get=lambda *_: response)
    client = instrument_llamaparse(SimpleNamespace(parsing=native), tracker, **SCOPE)
    assert client.parsing.get("pjb-one") is response
    assert latest(tracker) is None
    response["job"]["project_id"] = "other_project"
    assert client.parsing.parse(expand=["usage"]) is response
    assert latest(tracker) is None
    response["job"]["project_id"] = DATA["project"]
    assert client.parsing.parse(expand=["usage"]) is response
    assert latest(tracker).usage[0].quantity == 30.5
    with pytest.raises(ValueError):
        bind_llamaparse_job(tracker, response, **{**SCOPE, "project_id": "other_project"})


def test_native_provider_errors_and_revisions_are_not_hidden(setup):
    tracker, _ = setup

    def fail(**kwargs):
        raise RuntimeError("native failure")

    client = instrument_llamaparse(
        SimpleNamespace(parsing=SimpleNamespace(parse=fail)), tracker, **SCOPE
    )
    with pytest.raises(RuntimeError, match="native failure"):
        client.parsing.parse()
    response = deepcopy(DATA["response"])
    bind_llamaparse_job(tracker, response, **SCOPE)
    with pytest.raises(ValueError):
        record_llamaparse_job(tracker, response, revision=3, **SCOPE)
    assert record_llamaparse_job(tracker, response, **SCOPE)
    response["job"]["usage"]["credits"] = 20
    with pytest.raises(ValueError):
        record_llamaparse_job(tracker, response, **SCOPE)
