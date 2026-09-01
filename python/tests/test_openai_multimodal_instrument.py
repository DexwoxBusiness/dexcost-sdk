"""Real OpenAI-SDK surface tests for non-chat auto-instrumentation."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Generator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture()
def storage(tmp_path: Any) -> Generator[SQLiteStorage, None, None]:
    value = SQLiteStorage(db_path=tmp_path / "openai-multimodal.db")
    yield value
    value.close()


@pytest.fixture()
def tracker(storage: SQLiteStorage) -> CostTracker:
    return CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])


@pytest.fixture(autouse=True)
def _real_openai_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    # The legacy fake-OpenAI suite intentionally leaves import blockers behind.
    # Remove only those sentinels so this suite exercises the installed package.
    for name, module in list(sys.modules.items()):
        if (name == "openai" or name.startswith("openai.")) and module is None:
            sys.modules.pop(name, None)
    import openai  # noqa: F401

    yield
    from dexcost.instruments.openai import uninstrument_openai

    uninstrument_openai()


def _events(storage: SQLiteStorage) -> list[Any]:
    return storage.query_events()


def test_sync_embedding_uses_native_usage_and_auto_task(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.embeddings import Embeddings

    from dexcost.instruments.openai import instrument_openai

    response = SimpleNamespace(
        model="text-embedding-3-small",
        usage=SimpleNamespace(prompt_tokens=1_000, total_tokens=1_000),
    )
    monkeypatch.setattr(Embeddings, "create", staticmethod(lambda **kwargs: response))
    instrument_openai(tracker)

    assert Embeddings.create(model="text-embedding-3-small", input="private") is response

    event = _events(storage)[0]
    assert event.event_type == "external_cost"
    assert event.cost_usd == Decimal("0.00002000")
    assert event.details["provider_usage_privacy"] == "quantities_only"
    assert "private" not in str(event.details)
    converted = to_attribution_observation_v3(event)
    assert converted is not None
    assert converted["provider"]["name"] == "openai"
    assert converted["provider"]["service"] == "embeddings"
    assert converted["usage"][0]["metric"] == "input_tokens"
    tasks = storage.query_tasks(task_type="openai.embeddings.create")
    assert len(tasks) == 1
    assert tasks[0].status == "success"


def test_async_embedding_preserves_explicit_task(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.embeddings import AsyncEmbeddings

    from dexcost.instruments.openai import instrument_openai

    response = SimpleNamespace(
        model="text-embedding-3-large",
        usage=SimpleNamespace(prompt_tokens=200, total_tokens=200),
    )

    async def fake(**kwargs: Any) -> Any:
        return response

    monkeypatch.setattr(AsyncEmbeddings, "create", staticmethod(fake))
    instrument_openai(tracker)

    async def run() -> None:
        async with tracker.task(task_type="embedding-parent") as task:
            result = await AsyncEmbeddings.create(
                model="text-embedding-3-large", input=[]
            )
            assert result is response
        event = storage.query_events(task_id=str(task.task_id))[0]
        assert event.cost_usd == Decimal("0.00002600")

    asyncio.run(run())


def test_gpt_image_prices_text_image_and_output_tokens_separately(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.images import Images

    from dexcost.instruments.openai import instrument_openai

    response = SimpleNamespace(
        data=[SimpleNamespace()],
        quality="high",
        size="1024x1024",
        usage=SimpleNamespace(
            input_tokens=120,
            input_tokens_details=SimpleNamespace(text_tokens=100, image_tokens=20),
            output_tokens=400,
            output_tokens_details=SimpleNamespace(text_tokens=0, image_tokens=400),
        ),
    )
    monkeypatch.setattr(Images, "generate", staticmethod(lambda **kwargs: response))
    instrument_openai(tracker)

    Images.generate(model="gpt-image-1", prompt="private prompt")
    event = _events(storage)[0]
    assert event.cost_usd == Decimal("0.0167000")
    assert [line["dimension"] for line in event.details["pricing_breakdown"]] == [
        "input_image_tokens",
        "input_tokens",
        "output_image_tokens",
    ]
    assert [line["metric"] for line in event.details["attribution_usage_lines"]] == [
        "input_tokens",
        "input_image_tokens",
        "output_image_tokens",
        "image_count",
    ]
    assert "private prompt" not in str(event.details)


def test_dall_e_uses_resolution_and_quality_catalog_variant(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.images import Images

    from dexcost.instruments.openai import instrument_openai

    response = SimpleNamespace(data=[SimpleNamespace()], usage=None)
    monkeypatch.setattr(Images, "generate", staticmethod(lambda **kwargs: response))
    instrument_openai(tracker)

    Images.generate(
        model="dall-e-3",
        prompt="private",
        quality="hd",
        size="1792x1024",
    )
    event = _events(storage)[0]
    assert event.cost_usd == Decimal("0.11999117312")
    assert event.details["pricing_resolved_model"] == "hd/1792-x-1024/dall-e-3"


class _SyncStream:
    def __init__(self, values: list[Any]) -> None:
        self._values = iter(values)
        self.closed = False

    def __iter__(self) -> _SyncStream:
        return self

    def __next__(self) -> Any:
        return next(self._values)

    def close(self) -> None:
        self.closed = True


def test_image_stream_records_only_after_final_usage(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.images import Images

    from dexcost.instruments.openai import instrument_openai

    usage = SimpleNamespace(
        input_tokens=10,
        input_tokens_details=SimpleNamespace(text_tokens=10, image_tokens=0),
        output_tokens=100,
        output_tokens_details=None,
    )
    raw = _SyncStream(
        [
            SimpleNamespace(type="image_generation.partial_image", usage=None),
            SimpleNamespace(
                type="image_generation.completed",
                usage=usage,
                quality="low",
                size="1024x1024",
            ),
        ]
    )
    monkeypatch.setattr(Images, "generate", staticmethod(lambda **kwargs: raw))
    instrument_openai(tracker)

    stream = Images.generate(model="gpt-image-1-mini", prompt="x", stream=True)
    assert _events(storage) == []
    assert len(list(stream)) == 2
    event = _events(storage)[0]
    assert event.cost_usd == Decimal("0.0008200")
    assert event.details["attribution_operation_status"] == "succeeded"


def test_early_image_stream_close_is_cancelled_not_successful(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.images import Images

    from dexcost.instruments.openai import instrument_openai

    raw = _SyncStream([SimpleNamespace(type="image_generation.partial_image", usage=None)])
    monkeypatch.setattr(Images, "generate", staticmethod(lambda **kwargs: raw))
    instrument_openai(tracker)

    stream = Images.generate(model="gpt-image-1", prompt="x", stream=True)
    next(stream)
    stream.close()
    event = _events(storage)[0]
    assert event.cost_usd == 0
    assert event.cost_confidence == "unknown"
    assert event.details["attribution_operation_status"] == "cancelled"
    assert raw.closed


def test_whisper_duration_and_tts_characters_are_observed_without_content(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.audio.speech import Speech
    from openai.resources.audio.transcriptions import Transcriptions
    from openai.resources.audio.translations import Translations

    from dexcost.instruments.openai import instrument_openai

    transcription = SimpleNamespace(
        duration=60.0,
        usage=SimpleNamespace(type="duration", seconds=60.0),
    )
    monkeypatch.setattr(
        Transcriptions, "create", staticmethod(lambda **kwargs: transcription)
    )
    monkeypatch.setattr(
        Translations, "create", staticmethod(lambda **kwargs: transcription)
    )
    monkeypatch.setattr(Speech, "create", staticmethod(lambda **kwargs: object()))
    instrument_openai(tracker)

    Transcriptions.create(model="whisper-1", file=object())
    Translations.create(model="whisper-1", file=object())
    Speech.create(model="tts-1", voice="alloy", input="do not retain")
    events = _events(storage)
    by_service = {event.service_name: event for event in events}
    whisper_events = [event for event in events if event.service_name == "speech_to_text"]
    assert len(whisper_events) == 2
    assert all(event.cost_usd == 0 for event in whisper_events)
    assert all(event.cost_confidence == "unknown" for event in whisper_events)
    for event in whisper_events:
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"] == {
            "name": "openai",
            "service": "speech_to_text",
        }
        assert observation["resource"] == {"type": "model", "id": "whisper-1"}
        assert observation["usage"] == [
            {
                "line_id": observation["usage"][0]["line_id"],
                "metric": "audio_seconds",
                "quantity": "60",
                "unit": "Seconds",
                "dimensions": [],
            }
        ]
    assert by_service["text_to_speech"].cost_usd == Decimal("0.000195")
    assert "do not retain" not in str(by_service["text_to_speech"].details)
    assert by_service["text_to_speech"].details["attribution_usage_lines"] == [
        {"metric": "characters", "quantity": "13", "unit": "Characters"}
    ]


def test_token_transcription_does_not_price_unallocated_input_as_text(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.audio.transcriptions import Transcriptions

    from dexcost.instruments.openai import instrument_openai

    response = SimpleNamespace(
        usage=SimpleNamespace(
            type="tokens",
            input_tokens=120,
            output_tokens=20,
            input_token_details=SimpleNamespace(audio_tokens=100, text_tokens=20),
        )
    )
    monkeypatch.setattr(Transcriptions, "create", staticmethod(lambda **kwargs: response))
    instrument_openai(tracker)

    Transcriptions.create(model="gpt-4o-transcribe", file=object())
    event = _events(storage)[0]
    assert event.cost_usd == Decimal("0.0008500")
    assert event.cost_confidence == "computed"
    assert [line["dimension"] for line in event.details["pricing_breakdown"]] == [
        "input_audio_tokens",
        "input_tokens",
        "output_tokens",
    ]


def test_provider_failure_is_durable_and_original_exception_survives(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.embeddings import Embeddings

    from dexcost.instruments.openai import instrument_openai

    error = RuntimeError("provider failed")

    def fail(**kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(Embeddings, "create", staticmethod(fail))
    instrument_openai(tracker)

    with pytest.raises(RuntimeError) as captured:
        Embeddings.create(model="text-embedding-3-small", input="private")
    assert captured.value is error
    event = _events(storage)[0]
    assert event.details["attribution_operation_status"] == "failed"
    assert event.details["error_type"] == "runtimeerror"
    assert event.cost_confidence == "unknown"
    assert "private" not in str(event.details)


def test_chat_parse_and_legacy_completions_are_not_blind_spots(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.chat.completions import Completions as ChatCompletions
    from openai.resources.completions import Completions as LegacyCompletions

    from dexcost.instruments.openai import instrument_openai

    response = SimpleNamespace(
        id="completion_1",
        model="gpt-4o",
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )
    monkeypatch.setattr(ChatCompletions, "parse", staticmethod(lambda **kwargs: response))
    monkeypatch.setattr(LegacyCompletions, "create", staticmethod(lambda **kwargs: response))
    instrument_openai(tracker)

    ChatCompletions.parse(model="gpt-4o", messages=[])
    LegacyCompletions.create(model="gpt-3.5-turbo-instruct", prompt="private")
    assert len(_events(storage)) == 2
    assert all(event.event_type == "llm_call" for event in _events(storage))


def test_async_image_and_speech_public_methods(
    tracker: CostTracker, storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai.resources.audio.speech import AsyncSpeech
    from openai.resources.images import AsyncImages

    from dexcost.instruments.openai import instrument_openai

    async def fake_image(**kwargs: Any) -> Any:
        return SimpleNamespace(data=[SimpleNamespace()], usage=None)

    async def fake_speech(**kwargs: Any) -> Any:
        return object()

    monkeypatch.setattr(AsyncImages, "edit", staticmethod(fake_image))
    monkeypatch.setattr(AsyncSpeech, "create", staticmethod(fake_speech))
    instrument_openai(tracker)

    async def run() -> None:
        await AsyncImages.edit(model="dall-e-2", image=object(), prompt="private")
        await AsyncSpeech.create(model="tts-1-hd", voice="alloy", input="abcd")

    asyncio.run(run())
    by_operation = {
        event.details["attribution_operation_name"]: event for event in _events(storage)
    }
    assert by_operation["openai.images.edit"].cost_usd == Decimal("0.02")
    assert by_operation["openai.audio.speech.create"].cost_usd == Decimal("0.00012")
