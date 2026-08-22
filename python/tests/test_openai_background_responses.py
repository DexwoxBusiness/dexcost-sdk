"""OpenAI background Responses durable lifecycle and streaming-poll gates."""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI
from openai.resources.responses.responses import Responses
from openai.types.responses import Response

from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_openai(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    yield
    uninstrument_openai()


def _response(response_id: str, status: str) -> dict[str, Any]:
    completed = status == "completed"
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "model": "gpt-4o-mini-2024-07-18",
        "output": (
            [
                {
                    "id": "ws_private",
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "query": "private-background-search",
                    },
                }
            ]
            if completed
            else []
        ),
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [{"type": "web_search", "search_context_size": "medium"}],
        "status": status,
        "usage": (
            {
                "input_tokens": 100,
                "input_tokens_details": {
                    "cached_tokens": 20,
                    "cache_write_tokens": 0,
                },
                "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 5},
                "total_tokens": 130,
            }
            if completed
            else None
        ),
    }


def _handler() -> Any:
    creates = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        path = request.url.path
        if path == "/v1/responses" and request.method == "POST":
            creates += 1
            response_id = "resp_bg" if creates == 1 else "resp_cancel"
            payload = _response(response_id, "queued")
        elif path == "/v1/responses/resp_bg" and request.method == "GET":
            payload = _response("resp_bg", "completed")
        elif path == "/v1/responses/resp_cancel/cancel" and request.method == "POST":
            payload = _response("resp_cancel", "cancelled")
        else:
            raise AssertionError(f"unexpected OpenAI request {request.method} {path}")
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=payload,
        )

    return handle


def _assert_lifecycle(storage: SQLiteStorage, task_id: str) -> None:
    completed = storage.get_provider_job("openai", "responses", "resp_bg")
    cancelled = storage.get_provider_job("openai", "responses", "resp_cancel")
    assert completed is not None
    assert (completed.status, completed.revision, completed.component) == (
        "succeeded",
        2,
        "llm",
    )
    assert {line.metric: str(line.quantity) for line in completed.usage} == {
        "input_tokens": "80",
        "cache_read_input_tokens": "20",
        "output_tokens": "25",
        "reasoning_output_tokens": "5",
        "web_search_calls": "1",
    }
    assert completed.task_input_tokens == 100
    assert completed.task_output_tokens == 30
    assert completed.task_cached_tokens == 20
    assert completed.task_id.hex == task_id.replace("-", "")
    assert len(storage.query_provider_job_history(str(completed.event_id))) == 2
    assert cancelled is not None
    assert (cancelled.status, cancelled.revision) == ("cancelled", 2)
    durable = json.dumps(
        [completed.to_dict(), cancelled.to_dict()], sort_keys=True
    )
    assert "private" not in durable
    # Background work must not also emit a premature ordinary call event.
    assert storage.query_events(task_id=task_id) == []


def test_sync_background_response_real_http_lifecycle(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "openai-background-sync.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    client = OpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(_handler())),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="openai-background-sync") as task:
            pending = client.responses.create(
                model="gpt-4o-mini-2024-07-18",
                background=True,
                input="private-background-input",
                service_tier="flex",
            )
            first = storage.get_provider_job("openai", "responses", pending.id)
            assert first is not None and first.status == "submitted"
            completed = client.responses.retrieve(pending.id)
            replay = client.responses.retrieve(pending.id)
            cancelling = client.responses.create(
                model="gpt-4o-mini-2024-07-18",
                background=True,
                input="private-cancel-input",
            )
            cancelled = client.responses.cancel(cancelling.id)
        assert completed.status == "completed"
        assert replay.status == "completed"
        assert cancelled.status == "cancelled"
        _assert_lifecycle(storage, str(task.task_id))
    finally:
        client.close()
        storage.close()


@pytest.mark.asyncio
async def test_async_background_response_real_http_lifecycle(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "openai-background-async.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler())),
    )
    instrument_openai(tracker)
    try:
        with tracker.task(task_type="openai-background-async") as task:
            pending = await client.responses.create(
                model="gpt-4o-mini-2024-07-18",
                background=True,
                input="private-background-input",
            )
            await client.responses.retrieve(pending.id)
            cancelling = await client.responses.create(
                model="gpt-4o-mini-2024-07-18",
                background=True,
                input="private-cancel-input",
            )
            await client.responses.cancel(cancelling.id)
        _assert_lifecycle(storage, str(task.task_id))
    finally:
        await client.close()
        storage.close()


def test_streamed_background_poll_only_reconciles_on_terminal_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "openai-background-stream.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    calls = 0

    def create(self: Any, **kwargs: Any) -> Response:
        return Response.model_validate(_response("resp_stream", "queued"))

    def retrieve(
        self: Any, response_id: str, **kwargs: Any
    ) -> Iterator[SimpleNamespace]:
        nonlocal calls
        calls += 1
        completed = Response.model_validate(_response(response_id, "completed"))
        return iter(
            [
                SimpleNamespace(type="response.in_progress", response=None),
                SimpleNamespace(type="response.completed", response=completed),
            ]
        )

    monkeypatch.setattr(Responses, "create", create)
    monkeypatch.setattr(Responses, "retrieve", retrieve)
    instrument_openai(tracker)
    client = OpenAI(api_key="test-key")
    try:
        pending = client.responses.create(
            model="gpt-4o-mini-2024-07-18",
            background=True,
            input="private-stream-input",
        )
        early = client.responses.retrieve(pending.id, stream=True)
        next(early)
        early.close()
        unchanged = storage.get_provider_job("openai", "responses", pending.id)
        assert unchanged is not None
        assert (unchanged.status, unchanged.revision) == ("submitted", 1)

        complete = client.responses.retrieve(pending.id, stream=True)
        list(complete)
        final = storage.get_provider_job("openai", "responses", pending.id)
        assert final is not None
        assert (final.status, final.revision) == ("succeeded", 2)
        assert calls == 2
    finally:
        client.close()
        storage.close()
