import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from dexcost import (
    bind_apify_run,
    bind_firecrawl_job,
    instrument_apify,
    instrument_firecrawl,
    record_apify_run,
    record_firecrawl_job,
    record_firecrawl_search,
    uninstrument_apify,
    uninstrument_firecrawl,
)
from dexcost.context import _current_task
from dexcost.instruments import web_tools
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage

DATA = json.loads((Path(__file__).parents[2] / "fixtures/web_tool_conformance.json").read_text())
START = datetime.fromisoformat(DATA["started_at"].replace("Z", "+00:00"))


@pytest.fixture
def setup(tmp_path):
    storage = SQLiteStorage(tmp_path / "web.db")
    task = Task(task_id=uuid.UUID(DATA["task_id"]), task_type="web")
    storage.insert_task(task)
    token = _current_task.set(task)
    try:
        yield SimpleNamespace(storage=storage), task
    finally:
        _current_task.reset(token)
        storage.close()


def test_credit_snapshot_replay_zero_restore_and_owner(setup):
    tracker, task = setup
    binding = dict(
        billing_account_id="account",
        resource_id="resource",
        job_id="job",
        started_at=START,
        operation="crawl",
    )
    assert bind_firecrawl_job(tracker, **binding)
    token = _current_task.set(None)
    try:
        for revision, credits in [(2, 12), (2, 12), (3, 0), (4, 8)]:
            assert record_firecrawl_job(
                tracker,
                {**DATA["firecrawl"], "creditsUsed": credits},
                billing_account_id="account",
                job_id="job",
                revision=revision,
            )
    finally:
        _current_task.reset(token)
    job = tracker.storage.get_provider_job("firecrawl", "web", "account/job")
    assert job.task_id == task.task_id and job.revision == 4
    assert job.usage[0].quantity == 8
    assert len(tracker.storage.query_provider_job_history(str(job.event_id))) == 4
    assert "not-retained" not in json.dumps(job.to_attribution_observation())
    assert "cost_evidence" not in job.to_attribution_observation()
    with pytest.raises(ValueError):
        bind_firecrawl_job(tracker, **{**binding, "resource_id": "other"})


@pytest.mark.parametrize("bad", [None, True, -1, 1.5, "2", 9007199254740992])
def test_missing_or_invalid_credits_are_not_free(setup, bad):
    tracker, _ = setup
    with pytest.raises(ValueError):
        record_firecrawl_search(
            tracker,
            {"success": True, "id": "search", "creditsUsed": bad},
            billing_account_id="account",
            resource_id="resource",
            occurred_at=START,
            observed_at=START,
        )
    assert tracker.storage.get_provider_job("firecrawl", "web", "account/search") is None


def test_apify_records_terminal_identity_without_sdk_money(setup):
    tracker, _ = setup
    assert not record_apify_run(tracker, DATA["apify"])
    assert bind_apify_run(tracker, DATA["apify"])
    assert not record_apify_run(tracker, {**DATA["apify"], "status": "RUNNING"})
    assert record_apify_run(tracker, DATA["apify"])
    assert record_apify_run(tracker, DATA["apify"])
    job = tracker.storage.get_provider_job("apify", "actor_runs", "run_one")
    assert "cost_evidence" not in job.to_attribution_observation()
    assert "999" not in json.dumps(job.to_attribution_observation())


def test_native_apify_async_start_then_poll_has_no_hidden_calls(setup):
    tracker, task = setup
    calls = []

    async def start():
        calls.append("start")
        return {**DATA["apify"], "status": "RUNNING", "finishedAt": None}

    async def get():
        calls.append("get")
        return DATA["apify"]

    native = SimpleNamespace(
        actor=lambda _: SimpleNamespace(start=start), run=lambda _: SimpleNamespace(get=get)
    )
    client = instrument_apify(native, tracker, billing_account_id="account")

    async def run():
        await client.actor("actor_one").start()
        token = _current_task.set(None)
        try:
            assert await client.run("run_one").get() is DATA["apify"]
            await client.run("run_one").get()
        finally:
            _current_task.reset(token)

    asyncio.run(run())
    job = tracker.storage.get_provider_job("apify", "actor_runs", "run_one")
    assert job.task_id == task.task_id and job.revision == 2
    assert calls == ["start", "get", "get"]
    uninstrument_apify(client)
    assert client.actor("actor_one").start is start


def test_native_firecrawl_metadata_and_unknown_search_passthrough(setup):
    tracker, _ = setup
    document = SimpleNamespace(
        metadata=SimpleNamespace(scrape_id="scrape", credits_used=4), markdown="secret"
    )
    native = SimpleNamespace(scrape=lambda _: document, search=lambda _: SimpleNamespace(web=[]))
    client = instrument_firecrawl(
        native, tracker, billing_account_id="account", resource_id="resource"
    )
    assert client.scrape("private-url") is document
    assert client.search("private-query").web == []
    job = tracker.storage.get_provider_job("firecrawl", "web", "account/scrape")
    assert job.operation == "firecrawl.scrape" and job.usage[0].quantity == 4
    assert "secret" not in json.dumps(job.to_attribution_observation())
    uninstrument_firecrawl(client)
    document.metadata.scrape_id = "after"
    client.scrape("private-url")
    assert tracker.storage.get_provider_job("firecrawl", "web", "account/after") is None


def test_scope_mismatch_and_native_error_preserved(setup):
    tracker, _ = setup
    native = SimpleNamespace(actor=lambda _: SimpleNamespace(call=lambda: DATA["apify"]))
    client = instrument_apify(native, tracker, billing_account_id="different")
    assert client.actor("actor_one").call() is DATA["apify"]
    assert tracker.storage.get_provider_job("apify", "actor_runs", "run_one") is None

    def fail(*args):
        raise RuntimeError("native-error")

    client = instrument_firecrawl(
        SimpleNamespace(scrape=fail), tracker, billing_account_id="account", resource_id="resource"
    )
    with pytest.raises(RuntimeError, match="native-error"):
        client.scrape("url")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["scrape", "search"])
def test_native_request_preserves_cross_period_response_time(
    setup, monkeypatch, asynchronous, operation
):
    tracker, _ = setup
    started = datetime.fromisoformat(DATA["crossing_start"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(DATA["crossing_end"].replace("Z", "+00:00"))
    clock = iter([started, ended])
    monkeypatch.setattr(web_tools, "_now", lambda: next(clock))
    response = (
        {"metadata": {"scrapeId": "crossing", "creditsUsed": 4}}
        if operation == "scrape"
        else {"id": "crossing", "creditsUsed": 4}
    )

    def sync_call():
        return response

    async def async_call():
        await asyncio.sleep(0)
        return response

    client = instrument_firecrawl(
        SimpleNamespace(**{operation: async_call if asynchronous else sync_call}),
        tracker,
        billing_account_id="account",
        resource_id="resource",
    )
    result = getattr(client, operation)()
    assert (asyncio.run(result) if asynchronous else result) is response
    job = tracker.storage.get_provider_job("firecrawl", "web", "account/crossing")
    assert job.submitted_at == started and job.observed_at == ended
    observation = job.to_attribution_observation()
    assert observation["usage_period"] == {
        "start_at": DATA["crossing_start"],
        "end_at": DATA["crossing_end"],
    }


def test_raw_request_requires_original_times_and_replays_without_retiming(setup):
    tracker, _ = setup
    started = datetime.fromisoformat(DATA["crossing_start"].replace("Z", "+00:00"))
    ended = datetime.fromisoformat(DATA["crossing_end"].replace("Z", "+00:00"))
    response = {"success": True, "id": "raw", "creditsUsed": 4}
    options = dict(
        billing_account_id="account",
        resource_id="resource",
        occurred_at=started,
        observed_at=ended,
    )
    assert record_firecrawl_search(tracker, response, **options)
    assert record_firecrawl_search(tracker, response, **options)
    job = tracker.storage.get_provider_job("firecrawl", "web", "account/raw")
    assert job.observed_at == ended and job.submitted_at == started and job.revision == 2
    with pytest.raises(ValueError):
        record_firecrawl_search(tracker, response, **{**options, "occurred_at": ended})
    for invalid in [
        {"occurred_at": None},
        {"observed_at": None},
        {"observed_at": started.replace(day=29)},
    ]:
        with pytest.raises(ValueError):
            record_firecrawl_search(
                tracker, {**response, "id": "invalid"}, **{**options, **invalid}
            )
        assert tracker.storage.get_provider_job("firecrawl", "web", "account/invalid") is None
