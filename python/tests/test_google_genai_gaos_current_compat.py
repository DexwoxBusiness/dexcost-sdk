"""Real-package gates for the google-genai 2.x GAOS Interactions layout."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from dexcost.instruments.gemini import instrument_gemini, uninstrument_gemini
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker

genai = pytest.importorskip("google.genai")

try:
    from google.genai._gaos.google_genai import (
        AsyncGeminiNextGenInteractions,
        GeminiNextGenInteractions,
    )
    from google.genai._gaos.types.interactions import (
        Interaction,
        InteractionCompletedEvent,
        InteractionSseEventInteraction,
        ModalityTokens,
        Usage,
    )
except ImportError:
    pytest.skip("google-genai GAOS Interactions layout is not installed", allow_module_level=True)


@pytest.fixture()
def storage(tmp_path: Path) -> Generator[SQLiteStorage, None, None]:
    value = SQLiteStorage(tmp_path / "google-gaos.db")
    yield value
    value.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _restore_google() -> Generator[None, None, None]:
    yield
    uninstrument_gemini()


def _usage() -> Usage:
    return Usage(
        total_input_tokens=100,
        total_cached_tokens=20,
        total_output_tokens=12,
        total_thought_tokens=5,
        total_tool_use_tokens=3,
        input_tokens_by_modality=[
            ModalityTokens(modality="text", tokens=70),
            ModalityTokens(modality="audio", tokens=30),
        ],
        cached_tokens_by_modality=[ModalityTokens(modality="text", tokens=20)],
        output_tokens_by_modality=[
            ModalityTokens(modality="text", tokens=8),
            ModalityTokens(modality="image", tokens=4),
        ],
        tool_use_tokens_by_modality=[
            ModalityTokens(modality="text", tokens=3)
        ],
    )


def _interaction(
    record_id: str,
    status: str,
    *,
    usage: Usage | None = None,
) -> Interaction:
    now = datetime.now(timezone.utc).isoformat()
    return Interaction(
        id=record_id,
        created=now,
        updated=now,
        status=status,
        model="gemini-3.6-flash",
        usage=usage,
    )


def _completed_event(interaction: Interaction) -> InteractionCompletedEvent:
    return InteractionCompletedEvent(
        interaction=InteractionSseEventInteraction(
            id=interaction.id,
            status="completed",
            model=interaction.model,
            usage=interaction.usage,
        )
    )


def _lines(event: Any) -> dict[str, str]:
    return {
        line["metric"]: line["quantity"]
        for line in event.details["attribution_usage_lines"]
    }


def test_gaos_sync_foreground_background_get_stream_and_cancel(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    foreground = _interaction("gaos-foreground-1", "completed", usage=_usage())
    background = _interaction("gaos-background-1", "in_progress")
    completed = _interaction("gaos-background-1", "completed", usage=_usage())
    cancellable = _interaction("gaos-cancel-1", "in_progress")
    cancelled = _interaction("gaos-cancel-1", "cancelled")

    def create(self: Any, **kwargs: Any) -> Any:
        if kwargs.get("background") is True:
            return (
                cancellable
                if kwargs.get("input") == "gaos-private-cancel"
                else background
            )
        if kwargs.get("stream") is True:
            return iter([_completed_event(foreground)])
        return foreground

    def get(self: Any, id: str, **kwargs: Any) -> Any:
        assert id == "gaos-background-1"
        if kwargs.get("stream") is True:
            return iter([_completed_event(completed)])
        return completed

    def cancel(self: Any, id: str, **kwargs: Any) -> Any:
        assert id == "gaos-cancel-1"
        return cancelled

    monkeypatch.setattr(GeminiNextGenInteractions, "create", create)
    monkeypatch.setattr(GeminiNextGenInteractions, "get", get)
    monkeypatch.setattr(GeminiNextGenInteractions, "cancel", cancel)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-gaos-sync") as task:
            direct = client.interactions.create(
                model="gemini-3.6-flash",
                input="gaos-private-direct",
            )
            stream = client.interactions.create(
                model="gemini-3.6-flash",
                input="gaos-private-stream",
                stream=True,
            )
            assert len(list(stream)) == 1
            pending = client.interactions.create(
                model="gemini-3.6-flash",
                input="gaos-private-background",
                background=True,
            )
            poll = client.interactions.get(id=pending.id, stream=True)
            assert len(list(poll)) == 1
            assert storage.get_provider_job(
                "google", "gemini", pending.id
            ).revision == 2
            replay = client.interactions.get(id=pending.id)
            to_cancel = client.interactions.create(
                model="gemini-3.6-flash",
                input="gaos-private-cancel",
                background=True,
            )
            cancelled_result = client.interactions.cancel(id=to_cancel.id)
    finally:
        client.close()

    assert direct is foreground
    assert replay is completed
    assert cancelled_result is cancelled
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    assert all(event.input_tokens == 103 for event in events)
    assert all(event.output_tokens == 17 for event in events)
    assert _lines(events[0])["input_audio_tokens"] == "30"
    job = storage.get_provider_job("google", "gemini", "gaos-background-1")
    assert job is not None
    assert (job.status, job.revision) == ("succeeded", 2)
    cancelled_job = storage.get_provider_job("google", "gemini", "gaos-cancel-1")
    assert cancelled_job is not None
    assert (cancelled_job.status, cancelled_job.revision) == ("cancelled", 2)
    durable = [event.to_dict() for event in events]
    durable.extend(
        revision.to_dict()
        for revision in storage.query_provider_job_history(str(job.event_id))
    )
    assert "gaos-private" not in str(durable)


@pytest.mark.asyncio
async def test_gaos_async_background_poll_close_and_terminal_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    tracker: CostTracker,
    storage: SQLiteStorage,
) -> None:
    foreground = _interaction("gaos-async-foreground", "completed", usage=_usage())
    background = _interaction("gaos-async-background", "in_progress")
    completed = _interaction("gaos-async-background", "completed", usage=_usage())

    async def create(self: Any, **kwargs: Any) -> Any:
        return background if kwargs.get("background") is True else foreground

    async def get(self: Any, id: str, **kwargs: Any) -> Any:
        assert id == "gaos-async-background"
        if kwargs.get("stream") is True:
            async def events() -> AsyncIterator[Any]:
                yield _completed_event(completed)

            return events()
        return completed

    monkeypatch.setattr(AsyncGeminiNextGenInteractions, "create", create)
    monkeypatch.setattr(AsyncGeminiNextGenInteractions, "get", get)
    instrument_gemini(tracker)
    client = genai.Client(api_key="test-key")
    try:
        with tracker.task(task_type="google-gaos-async") as task:
            direct = await client.aio.interactions.create(
                model="gemini-3.6-flash",
                input="gaos-async-private-direct",
            )
            pending = await client.aio.interactions.create(
                model="gemini-3.6-flash",
                input="gaos-async-private-background",
                background=True,
            )
            poll = await client.aio.interactions.get(id=pending.id, stream=True)
            await poll.__anext__()
            await poll.aclose()
            latest = storage.get_provider_job("google", "gemini", pending.id)
            assert latest is not None and latest.revision == 1
            resolved = await client.aio.interactions.get(id=pending.id)
    finally:
        await client.aio.aclose()
        client.close()

    assert direct is foreground
    assert resolved is completed
    events = storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    job = storage.get_provider_job("google", "gemini", "gaos-async-background")
    assert job is not None
    assert (job.status, job.revision) == ("succeeded", 2)
    assert "gaos-async-private" not in str(
        [event.to_dict() for event in events] + [job.to_dict()]
    )
