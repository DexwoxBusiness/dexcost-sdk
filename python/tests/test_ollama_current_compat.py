"""Current official Ollama 0.6.x real-package compatibility gates."""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import httpx
import ollama
import pytest

from dexcost.instruments.ollama import instrument_ollama, uninstrument_ollama
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _restore_ollama() -> Generator[None, None, None]:
    uninstrument_ollama()
    yield
    uninstrument_ollama()


def _json_request(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode("utf-8"))


def _sync_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _json_request(request)
        if request.url.path == "/api/chat":
            assert body["messages"][0]["content"] == "private-chat-prompt"
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "message": {
                        "role": "assistant",
                        "content": "private-chat-output",
                        "tool_calls": [
                            {"function": {"name": "private_tool", "arguments": {"secret": 1}}}
                        ],
                    },
                    "done": True,
                    "done_reason": "stop",
                    "total_duration": 1000,
                    "load_duration": 100,
                    "prompt_eval_count": 12,
                    "prompt_eval_duration": 200,
                    "eval_count": 5,
                    "eval_duration": 700,
                },
            )
        if request.url.path == "/api/generate":
            assert body["prompt"] == "private-generate-prompt"
            if body.get("stream"):
                content = "\n".join(
                    (
                        json.dumps(
                            {
                                "model": body["model"],
                                "response": "private-fragment",
                                "done": False,
                            }
                        ),
                        json.dumps(
                            {
                                "model": body["model"],
                                "response": "private-final",
                                "done": True,
                                "done_reason": "stop",
                                "total_duration": 2000,
                                "load_duration": 300,
                                "prompt_eval_count": 7,
                                "prompt_eval_duration": 400,
                                "eval_count": 9,
                                "eval_duration": 1200,
                            }
                        ),
                    )
                )
                return httpx.Response(
                    200,
                    content=(content + "\n").encode(),
                    headers={"content-type": "application/x-ndjson"},
                )
        if request.url.path == "/api/embed":
            assert body["input"] == "private-embedding-input"
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "embeddings": [[0.1, 0.2]],
                    "total_duration": 300,
                    "load_duration": 25,
                    "prompt_eval_count": 4,
                },
            )
        if request.url.path == "/api/embeddings":
            return httpx.Response(200, json={"embedding": [0.3, 0.4]})
        if request.url.path == "/api/web_search":
            assert body["query"] == "private-search-query"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "private-title",
                            "url": "https://private.example",
                            "content": "private-search-result",
                        },
                        {
                            "title": "private-title-two",
                            "url": "https://private-two.example",
                            "content": "private-search-result-two",
                        },
                    ]
                },
            )
        if request.url.path == "/api/web_fetch":
            assert body["url"] == "https://private.example/page"
            return httpx.Response(
                200,
                json={
                    "title": "private-fetch-title",
                    "content": "private-fetch-content",
                    "links": ["https://private.example/secret-link"],
                },
            )
        return httpx.Response(404, json={"error": "private-provider-error"})

    return httpx.MockTransport(handler)


def _details_text(events: list[Any]) -> str:
    return json.dumps([event.to_dict() for event in events], sort_keys=True)


def test_sync_clients_streams_embeddings_and_web_use_real_http(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "ollama-sync.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = ollama.Client(
        host="http://localhost:11434",
        headers={"authorization": "Bearer test"},
        transport=_sync_transport(),
    )
    instrument_ollama(tracker)
    try:
        with tracker.task(task_type="ollama-sync") as task:
            chat = client.chat(
                model="llama3:8b",
                messages=[{"role": "user", "content": "private-chat-prompt"}],
            )
            stream = client.generate(
                model="llama3:8b",
                prompt="private-generate-prompt",
                stream=True,
            )
            chunks = list(stream)
            embedded = client.embed(
                model="nomic-embed-text",
                input="private-embedding-input",
            )
            legacy = client.embeddings(
                model="nomic-embed-text",
                prompt="private-legacy-input",
            )
            searched = client.web_search("private-search-query", max_results=2)
            fetched = client.web_fetch("https://private.example/page")

        assert chat.eval_count == 5
        assert chunks[-1].done is True
        assert embedded.prompt_eval_count == 4
        assert legacy.embedding == [0.3, 0.4]
        assert len(searched.results) == 2
        assert len(fetched.links or []) == 1

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 6
        by_operation = {
            event.details["attribution_operation_name"]: event for event in events
        }
        assert by_operation["ollama.chat"].input_tokens == 12
        assert by_operation["ollama.chat"].output_tokens == 5
        assert by_operation["ollama.generate"].input_tokens == 7
        assert by_operation["ollama.generate"].output_tokens == 9
        assert by_operation["ollama.embed"].input_tokens == 4
        assert by_operation["ollama.chat"].service_name == "ollama_local"
        assert by_operation["ollama.web_search"].details["attribution_usage_lines"] == [
            {"metric": "query_count", "quantity": "1", "unit": "Queries"},
            {"metric": "request_count", "quantity": "1", "unit": "Requests"},
            {"metric": "result_count", "quantity": "2", "unit": "Results"},
        ]

        persisted = _details_text(events)
        for secret in (
            "private-chat-prompt",
            "private-chat-output",
            "private_tool",
            "private-generate-prompt",
            "private-fragment",
            "private-embedding-input",
            "private-legacy-input",
            "private-search-query",
            "private-search-result",
            "private-fetch-content",
            "private.example",
        ):
            assert secret not in persisted
    finally:
        client.close()
        uninstrument_ollama()
        storage.close()


@pytest.mark.asyncio
async def test_async_client_stream_and_early_close_lifecycle(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "ollama-async.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    client = ollama.AsyncClient(
        host="https://ollama.com",
        headers={"authorization": "Bearer test"},
        transport=_sync_transport(),
    )
    instrument_ollama(tracker)
    try:
        with tracker.task(task_type="ollama-async") as task:
            chat = await client.chat(
                model="llama3:8b",
                messages=[{"role": "user", "content": "private-chat-prompt"}],
            )
            stream = await client.generate(
                model="llama3:8b",
                prompt="private-generate-prompt",
                stream=True,
            )
            first = await stream.__anext__()
            await stream.aclose()
            embedded = await client.embed(
                model="nomic-embed-text",
                input="private-embedding-input",
            )
            searched = await client.web_search("private-search-query", max_results=2)

        assert chat.done is True
        assert first.done is False
        assert embedded.prompt_eval_count == 4
        assert len(searched.results) == 2
        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 4
        by_operation = {
            event.details["attribution_operation_name"]: event for event in events
        }
        assert by_operation["ollama.chat"].service_name == "ollama_cloud"
        assert by_operation["ollama.chat"].model == "ollama-cloud:llama3:8b"
        assert (
            by_operation["ollama.generate"].details["attribution_operation_status"]
            == "cancelled"
        )
    finally:
        await client.close()
        uninstrument_ollama()
        storage.close()


def test_module_singleton_surface_and_native_failure_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "ollama-module.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    response = ollama.ChatResponse(
        model="llama3:8b",
        message={"role": "assistant", "content": "private-module-output"},
        done=True,
        prompt_eval_count=3,
        eval_count=2,
    )

    def chat(*args: Any, **kwargs: Any) -> Any:
        return response

    class NativeFailureError(RuntimeError):
        pass

    def web_fetch(*args: Any, **kwargs: Any) -> Any:
        raise NativeFailureError("private-native-error-message")

    monkeypatch.setattr(ollama, "chat", chat)
    monkeypatch.setattr(ollama, "web_fetch", web_fetch)
    instrument_ollama(tracker)
    try:
        with tracker.task(task_type="ollama-module") as task:
            assert ollama.chat(model="llama3:8b", messages=[]) is response
            with pytest.raises(
                NativeFailureError,
                match="private-native-error-message",
            ) as caught:
                ollama.web_fetch("https://private.example")

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 2
        assert type(caught.value) is NativeFailureError
        failed = next(
            event
            for event in events
            if event.details["attribution_operation_name"] == "ollama.web_fetch"
        )
        assert failed.details["attribution_operation_status"] == "failed"
        persisted = _details_text(events)
        assert "private-native-error-message" not in persisted
        assert "private.example" not in persisted
        assert "private-module-output" not in persisted
    finally:
        uninstrument_ollama()
        storage.close()


def test_sync_stream_natural_exhaustion_without_terminal_usage_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "ollama-incomplete.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])

    def generate(*args: Any, **kwargs: Any) -> Iterator[Any]:
        yield ollama.GenerateResponse(
            model="llama3:8b",
            response="private-partial",
            done=False,
        )

    monkeypatch.setattr(ollama, "generate", generate)
    instrument_ollama(tracker)
    try:
        with tracker.task(task_type="ollama-incomplete") as task:
            assert len(list(ollama.generate(model="llama3:8b", stream=True))) == 1
        event = storage.query_events(task_id=str(task.task_id))[0]
        assert event.details["attribution_operation_status"] == "unknown"
        assert event.cost_confidence == "unknown"
    finally:
        uninstrument_ollama()
        storage.close()
