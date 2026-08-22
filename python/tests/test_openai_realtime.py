"""OpenAI Realtime real-SDK and WebSocket protocol metering gates."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from openai import AsyncOpenAI, OpenAI

from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)  # type: ignore[misc]
def _restore_openai(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    yield
    uninstrument_openai()


def _response(response_id: str, status: str) -> dict[str, Any]:
    response: dict[str, Any] = {
        "id": response_id,
        "object": "realtime.response",
        "status": status,
        "output": [],
        "output_modalities": ["audio"],
    }
    if status == "completed":
        response["usage"] = {
            "input_tokens": 100,
            "input_token_details": {
                "text_tokens": 60,
                "audio_tokens": 30,
                "image_tokens": 10,
                "cached_tokens": 20,
                "cached_tokens_details": {
                    "text_tokens": 10,
                    "audio_tokens": 5,
                    "image_tokens": 5,
                },
            },
            "output_tokens": 50,
            "output_token_details": {"text_tokens": 20, "audio_tokens": 30},
            "total_tokens": 150,
        }
    return response


def _event(event_type: str, response_id: str, status: str) -> bytes:
    return json.dumps(
        {
            "event_id": f"evt_{event_type}_{response_id}",
            "type": event_type,
            "response": _response(response_id, status),
        }
    ).encode()


class _SyncWebSocket:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []
        self.closed = False

    def recv(self, *, decode: bool) -> bytes:
        assert decode is False
        return self.messages.pop(0)

    def send(self, data: str) -> None:
        self.sent.append(data)

    def close(self, *, code: int, reason: str) -> None:
        self.closed = True


class _FailingSyncWebSocket(_SyncWebSocket):
    def __init__(self, messages: list[bytes], error: BaseException) -> None:
        super().__init__(messages)
        self.error = error

    def recv(self, *, decode: bool) -> bytes:
        if self.messages:
            return super().recv(decode=decode)
        raise self.error


class _AsyncWebSocket:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []
        self.closed = False

    async def recv(self, *, decode: bool) -> bytes:
        assert decode is False
        return self.messages.pop(0)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = True


def _tracker(path: Path) -> tuple[SQLiteStorage, CostTracker]:
    storage = SQLiteStorage(db_path=path)
    tracker = CostTracker(
        storage=storage,
        auto_instrument=[],
        auto_update_pricing=False,
    )
    return storage, tracker


def test_sync_realtime_terminal_usage_is_metered_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_id = "resp_rt_123"
    websocket = _SyncWebSocket(
        [
            _event("response.created", response_id, "in_progress"),
            _event("response.done", response_id, "completed"),
            _event("response.done", response_id, "completed"),
        ]
    )
    monkeypatch.setattr("websockets.sync.client.connect", lambda *a, **k: websocket)
    storage, tracker = _tracker(tmp_path / "realtime-sync.db")
    client = OpenAI(api_key="test-key", base_url="https://example.test/v1")
    instrument_openai(tracker)

    with client.realtime.connect(model="gpt-realtime") as connection:
        connection.response.create(
            response={
                "output_modalities": ["audio"],
                "instructions": "private instructions",
            }
        )
        assert connection.recv().type == "response.created"
        assert connection.recv().type == "response.done"
        assert connection.recv().type == "response.done"

    [event] = storage.query_events()
    assert event.event_type == "llm_call"
    assert event.model == "gpt-realtime"
    assert event.details["attribution_operation_status"] == "succeeded"
    assert (event.input_tokens, event.output_tokens, event.cached_tokens) == (100, 50, 20)
    metrics = {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    }
    assert metrics == {
        "input_tokens": "50",
        "cache_read_input_tokens": "10",
        "input_audio_tokens": "25",
        "cache_read_input_audio_tokens": "5",
        "input_image_tokens": "5",
        "cache_read_input_image_tokens": "5",
        "output_tokens": "20",
        "output_audio_tokens": "30",
    }
    assert "private" not in json.dumps(event.to_dict(), sort_keys=True)
    assert websocket.closed
    client.close()
    storage.close()


def test_sync_realtime_reconnect_keeps_response_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    response_id = "resp_rt_reconnect"
    first = _FailingSyncWebSocket(
        [_event("response.created", response_id, "in_progress")],
        ConnectionClosedError(Close(1012, "restart"), None),
    )
    second = _SyncWebSocket([_event("response.done", response_id, "completed")])
    sockets = [first, second]
    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *a, **k: sockets.pop(0),
    )
    storage, tracker = _tracker(tmp_path / "realtime-reconnect.db")
    client = OpenAI(api_key="test-key", base_url="https://example.test/v1")
    instrument_openai(tracker)

    with client.realtime.connect(
        model="gpt-realtime",
        on_reconnecting=lambda event: None,
        initial_delay=0,
        max_delay=0,
    ) as connection:
        connection.response.create(response={"output_modalities": ["text"]})
        iterator = iter(connection)
        assert next(iterator).type == "response.created"
        assert next(iterator).type == "response.done"

    [event] = storage.query_events()
    assert event.details["provider_record_id"] == response_id
    assert event.details["attribution_operation_status"] == "succeeded"
    assert second.closed
    client.close()
    storage.close()


def test_sync_realtime_receive_failure_preserves_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_id = "resp_rt_failure"
    native_error = RuntimeError("private provider error")
    websocket = _FailingSyncWebSocket(
        [_event("response.created", response_id, "in_progress")],
        native_error,
    )
    monkeypatch.setattr("websockets.sync.client.connect", lambda *a, **k: websocket)
    storage, tracker = _tracker(tmp_path / "realtime-failure.db")
    client = OpenAI(api_key="test-key", base_url="https://example.test/v1")
    instrument_openai(tracker)

    with client.realtime.connect(model="gpt-realtime") as connection:
        connection.response.create(response={"output_modalities": ["text"]})
        assert connection.recv().type == "response.created"
        with pytest.raises(RuntimeError) as raised:
            connection.recv()
        assert raised.value is native_error

    [event] = storage.query_events()
    assert event.details["attribution_operation_status"] == "failed"
    assert event.details["error_type"] == "runtimeerror"
    assert "private provider error" not in json.dumps(event.to_dict(), sort_keys=True)
    client.close()
    storage.close()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_async_realtime_early_connection_close_cancels_inflight_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_id = "resp_rt_cancel"
    websocket = _AsyncWebSocket(
        [_event("response.created", response_id, "in_progress")]
    )

    async def connect(*args: Any, **kwargs: Any) -> _AsyncWebSocket:
        return websocket

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    storage, tracker = _tracker(tmp_path / "realtime-async.db")
    client = AsyncOpenAI(api_key="test-key", base_url="https://example.test/v1")
    instrument_openai(tracker)

    async with client.realtime.connect(model="gpt-realtime-mini") as connection:
        await connection.response.create(
            response={
                "output_modalities": ["text"],
                "instructions": "private instructions",
            }
        )
        assert (await connection.recv()).type == "response.created"

    [event] = storage.query_events()
    assert event.model == "gpt-realtime-mini"
    assert event.details["attribution_operation_status"] == "cancelled"
    assert event.cost_usd == 0
    assert event.cost_confidence == "unknown"
    assert "private" not in json.dumps(event.to_dict(), sort_keys=True)
    assert websocket.closed
    await client.close()
    storage.close()
