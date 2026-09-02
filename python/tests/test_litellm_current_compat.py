"""Compatibility gates against the installed current LiteLLM package."""

from __future__ import annotations

import importlib
import sys
from contextlib import ExitStack
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from dexcost.attribution.v3_convert import to_attribution_observation_v3
from dexcost.capabilities import capability_context
from dexcost.idempotency import idempotency_key
from dexcost.instruments.litellm import instrument_litellm, uninstrument_litellm
from dexcost.models.capability import CapabilityIdentity
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture(autouse=True)
def _reset_instrumentation() -> Any:
    uninstrument_litellm()
    yield
    uninstrument_litellm()


@pytest.fixture
def tracker(tmp_path: Any) -> Any:
    storage = SQLiteStorage(db_path=tmp_path / "litellm-current.db")
    instance = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    yield instance
    storage.close()


def _load_current_litellm() -> tuple[Any, Any, Any, Any]:
    """Recover from the fake-module isolation used by the legacy unit tests."""
    for name, module in list(sys.modules.items()):
        if (name == "litellm" or name.startswith("litellm.")) and module is None:
            sys.modules.pop(name, None)
    module = pytest.importorskip("litellm")
    types = importlib.import_module("litellm.types.utils")
    return module, types.ModelResponse, types.ModelResponseStream, types.Usage


def _openrouter_response(model_response: Any, usage: Any) -> Any:
    response = model_response(
        id="gen-current-1",
        model="openai/gpt-4.1",
        choices=[],
        usage=usage(
            prompt_tokens=100,
            completion_tokens=40,
            total_tokens=140,
            cost=0.0123,
            prompt_tokens_details={"cached_tokens": 20, "cache_write_tokens": 5},
            completion_tokens_details={"reasoning_tokens": 10},
            cost_details={"upstream_inference_cost": 0.009},
        ),
    )
    response._hidden_params = {
        "custom_llm_provider": "openrouter",
        "additional_headers": {"llm_provider-x-litellm-response-cost": 0.0123},
    }
    return response


def test_current_model_response_preserves_openrouter_exact_attribution(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, model_response, _, usage = _load_current_litellm()
    response = _openrouter_response(model_response, usage)
    monkeypatch.setattr(current_litellm, "completion", lambda **_: response)
    instrument_litellm(tracker)

    with tracker.task(task_type="litellm.openrouter.current") as task:
        result = current_litellm.completion(model="openrouter/openai/gpt-4.1", messages=[])

    assert result is response
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert event.provider == "openrouter"
    assert event.model == "openrouter/openai/gpt-4.1"
    assert event.cost_usd == Decimal("0.0123")
    assert event.cost_confidence == "exact"
    assert event.pricing_source == "provider_response"
    assert event.input_tokens == 100
    assert event.output_tokens == 40
    assert event.cached_tokens == 20
    assert event.details["reasoning_output_tokens"] == 10
    assert event.details["cache_creation_input_tokens"] == 5
    assert event.details["provider_reported_cost_usd"] == "0.0123"
    assert event.details["provider_upstream_cost_usd"] == "0.009"
    assert event.details["provider_record_id"] == "gen-current-1"
    assert "messages" not in str(event.details).lower()


def test_current_fal_route_uses_request_cost_reconciliation_identity(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, model_response, _, usage = _load_current_litellm()
    response = model_response(
        id="fal-request-current-1",
        model="fal-ai/flux/schnell",
        choices=[],
        usage=usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    response._hidden_params = {"custom_llm_provider": "fal_ai"}
    monkeypatch.setattr(current_litellm, "completion", lambda **_: response)
    instrument_litellm(tracker)

    with tracker.task(task_type="litellm.fal.current") as task:
        current_litellm.completion(model="fal_ai/fal-ai/flux/schnell", messages=[])

    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert to_attribution_observation_v3(event)["provider"] == {
        "name": "fal_ai",
        "service": "inference",
        "record_id": "fal-request-current-1",
    }


@pytest.mark.parametrize(
    ("raw_provider", "request_model", "response_model", "provider", "model"),
    [
        ("azure", "azure/private-deployment", "gpt-5-mini", "azure_openai", "azure/gpt-5-mini"),
        ("azure_ai", "azure_ai/model-router", "model-router", "azure_ai", "azure_ai/model-router"),
        (
            "vertex_ai",
            "vertex_ai/gemini-3-flash",
            "gemini-3-flash",
            "google",
            "vertex_ai/gemini-3-flash",
        ),
        (
            "gemini",
            "gemini/gemini-2.5-flash",
            "gemini-2.5-flash",
            "google",
            "gemini/gemini-2.5-flash",
        ),
        ("cohere", "cohere/command-r", "command-r", "cohere", "command-r"),
        (
            "huggingface",
            "huggingface/meta-llama/model",
            "meta-llama/model",
            "huggingface",
            "huggingface/meta-llama/model",
        ),
        (
            "together_ai",
            "meta-llama/model",
            "meta-llama/model",
            "together",
            "meta-llama/model",
        ),
        ("ollama", "ollama/llama3.1", "llama3.1", "ollama", "ollama/llama3.1"),
        ("mistral", "mistral/mistral-large", "mistral-large", "mistral", "mistral/mistral-large"),
        ("groq", "groq/llama-3.3-70b", "llama-3.3-70b", "groq", "groq/llama-3.3-70b"),
        (
            "fal_ai",
            "fal_ai/fal-ai/flux/schnell",
            "fal-ai/flux/schnell",
            "fal_ai",
            "fal_ai/fal-ai/flux/schnell",
        ),
        ("perplexity", "perplexity/sonar-pro", "sonar-pro", "perplexity", "perplexity/sonar-pro"),
        (
            "bedrock",
            "bedrock/anthropic.claude-v2",
            "anthropic.claude-v2",
            "bedrock",
            "bedrock/anthropic.claude-v2",
        ),
        (
            "anthropic",
            "anthropic/claude-sonnet-4",
            "claude-sonnet-4",
            "anthropic",
            "claude-sonnet-4",
        ),
        ("xai", "xai/grok-4", "grok-4", "xai", "xai/grok-4"),
        (
            "deepseek",
            "deepseek/deepseek-chat",
            "deepseek-chat",
            "deepseek",
            "deepseek/deepseek-chat",
        ),
        (
            "fireworks_ai",
            "fireworks_ai/accounts/fireworks/models/llama-v3",
            "accounts/fireworks/models/llama-v3",
            "fireworks_ai",
            "fireworks_ai/accounts/fireworks/models/llama-v3",
        ),
        (
            "nvidia_nim",
            "nvidia_nim/meta/llama-3.3-70b",
            "meta/llama-3.3-70b",
            "nvidia_nim",
            "nvidia_nim/meta/llama-3.3-70b",
        ),
        ("nano-gpt", "nano-gpt/gpt-5", "gpt-5", "nano_gpt", "nano-gpt/gpt-5"),
        ("openai", "openai/gpt-5", "gpt-5", "openai", "gpt-5"),
    ],
)
def test_current_hidden_provider_map_matches_every_revenium_gateway_attribution(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
    raw_provider: str,
    request_model: str,
    response_model: str,
    provider: str,
    model: str,
) -> None:
    current_litellm, model_response, _, usage = _load_current_litellm()
    response = model_response(
        id=f"provider-{raw_provider}",
        model=response_model,
        choices=[],
        usage=usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    response._hidden_params = {"custom_llm_provider": raw_provider}
    monkeypatch.setattr(current_litellm, "completion", lambda **_: response)
    instrument_litellm(tracker)

    current_litellm.completion(model=request_model, messages=[])

    event = tracker._storage.query_events()[0]
    assert event.provider == provider
    assert event.model == model


def test_current_stream_uses_terminal_openrouter_usage_and_early_close_is_cancelled(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, _, model_response_stream, usage = _load_current_litellm()
    first = model_response_stream(
        id="gen-current-stream",
        model="openai/gpt-4.1",
        created=1,
        choices=[],
    )
    first._hidden_params = {"custom_llm_provider": "openrouter"}
    terminal = model_response_stream(
        id="gen-current-stream",
        model="openai/gpt-4.1",
        created=2,
        choices=[],
        usage=usage(prompt_tokens=25, completion_tokens=5, total_tokens=30, cost=0.0025),
    )
    terminal._hidden_params = {"custom_llm_provider": "openrouter"}

    def completion(**_: Any) -> Any:
        return iter((first, terminal))

    monkeypatch.setattr(current_litellm, "completion", completion)
    instrument_litellm(tracker)

    capability = CapabilityIdentity(name="gateway.route", kind="workflow")
    with ExitStack() as stack:
        completed_task = stack.enter_context(tracker.task(task_type="litellm.openrouter.stream"))
        stack.enter_context(capability_context(capability))
        stack.enter_context(idempotency_key("private-litellm-stream-idempotency"))
        completed_stream = current_litellm.completion(
            model="openrouter/openai/gpt-4.1", stream=True
        )

    assert len(list(completed_stream)) == 2

    completed = tracker._storage.query_events(task_id=str(completed_task.task_id))[0]
    assert completed.details["attribution_operation_status"] == "succeeded"
    assert completed.cost_usd == Decimal("0.0025")
    assert completed.pricing_source == "provider_response"
    assert completed.details["provider_record_id"] == "gen-current-stream"
    assert completed.details["attribution_capability"] == capability.to_dict()
    assert len(completed.details["_dexcost_idempotency_sha256"]) == 64
    assert "private-litellm-stream-idempotency" not in str(completed.details)

    with tracker.task(task_type="litellm.openrouter.cancel") as cancelled_task:
        stream = current_litellm.completion(model="openrouter/openai/gpt-4.1", stream=True)
        next(stream)
        stream.close()

    cancelled = tracker._storage.query_events(task_id=str(cancelled_task.task_id))[0]
    assert cancelled.provider == "openrouter"
    assert cancelled.details["attribution_operation_status"] == "cancelled"
    assert cancelled.cost_confidence == "unknown"


@pytest.mark.asyncio
async def test_current_async_response_keeps_invocation_context_and_response_id(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, model_response, _, usage = _load_current_litellm()
    response = model_response(
        id="gen-current-async",
        model="gpt-5-mini",
        choices=[],
        usage=usage(prompt_tokens=12, completion_tokens=3, total_tokens=15),
    )

    async def acompletion(**_: Any) -> Any:
        return response

    monkeypatch.setattr(current_litellm, "acompletion", acompletion)
    instrument_litellm(tracker)
    capability = CapabilityIdentity(name="gateway.async_route", kind="workflow")

    with ExitStack() as stack:
        task = stack.enter_context(tracker.task(task_type="litellm.async.current"))
        stack.enter_context(capability_context(capability))
        stack.enter_context(idempotency_key("private-litellm-async-idempotency"))
        pending = current_litellm.acompletion(model="openai/gpt-5-mini", messages=[])

    assert await pending is response
    event = tracker._storage.query_events(task_id=str(task.task_id))[0]
    assert event.details["provider_record_id"] == "gen-current-async"
    assert event.details["attribution_capability"] == capability.to_dict()
    assert len(event.details["_dexcost_idempotency_sha256"]) == 64
    assert "private-litellm-async-idempotency" not in str(event.details)


def _operation_response(
    *,
    model: str | None = None,
    usage: Any = None,
    record_id: str | None = None,
    provider: str = "openai",
    **fields: Any,
) -> Any:
    payload = dict(fields)
    if model is not None:
        payload["model"] = model
    if usage is not None:
        payload["usage"] = usage
    if record_id is not None:
        payload["id"] = record_id
    payload["_hidden_params"] = {
        "custom_llm_provider": provider,
        "model": model,
    }
    return SimpleNamespace(**payload)


def test_current_public_operation_families_are_metered_without_payload_capture(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, _, _, _ = _load_current_litellm()
    responses = {
        "responses": _operation_response(
            model="gpt-5-mini",
            usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
            record_id="resp-litellm-current",
            status="completed",
        ),
        "embedding": _operation_response(
            model="text-embedding-3-small",
            usage={"prompt_tokens": 9, "total_tokens": 9},
            data=[{"embedding": [0.1, 0.2]}],
        ),
        "image_generation": _operation_response(
            model="gpt-image-1",
            usage={"input_tokens": 3, "output_tokens": 8, "total_tokens": 11},
            data=[{"url": "https://private.example/image"}],
        ),
        "transcription": _operation_response(
            model="whisper-1",
            usage={"type": "duration", "seconds": 7},
            text="private transcript",
        ),
        "speech": _operation_response(model="tts-1"),
        "rerank": _operation_response(
            model="rerank-v3.5",
            record_id="rerank-litellm-current",
            meta={"billed_units": {"search_units": 2, "total_tokens": 31}},
            results=[{"index": 0, "relevance_score": 0.9}],
            provider="cohere",
        ),
        "moderation": _operation_response(model="omni-moderation-latest", results=[]),
        "search": _operation_response(
            results=[{"title": "private result", "url": "https://private.example"}],
            provider="tavily",
        ),
        "ocr": _operation_response(
            model="mistral-ocr-latest",
            usage_info={"pages_processed": 3, "doc_size_bytes": 2048},
            pages=[{"markdown": "private document"}],
            provider="mistral",
        ),
    }
    for name, response in responses.items():
        monkeypatch.setattr(current_litellm, name, lambda _response=response, **_: _response)
    monkeypatch.setattr(current_litellm, "completion_cost", lambda **_: 0.001)
    instrument_litellm(tracker)

    with tracker.task(task_type="litellm.public.operations") as task:
        current_litellm.responses(model="openai/gpt-5-mini", input="private response input")
        current_litellm.embedding(
            model="openai/text-embedding-3-small",
            input=["private embedding input"],
        )
        current_litellm.image_generation(
            model="openai/gpt-image-1",
            prompt="private image prompt",
        )
        current_litellm.transcription(
            model="openai/whisper-1",
            file=b"private-audio",
        )
        current_litellm.speech(
            model="openai/tts-1",
            input="private speech text",
            voice="alloy",
        )
        current_litellm.rerank(
            model="cohere/rerank-v3.5",
            query="private rerank query",
            documents=["private rerank document"],
        )
        current_litellm.moderation(
            model="openai/omni-moderation-latest",
            input="private moderation input",
        )
        current_litellm.search(
            query=["private search one", "private search two"],
            search_provider="tavily",
        )
        current_litellm.ocr(
            model="mistral/mistral-ocr-latest",
            document={"type": "document_url", "document_url": "private-url"},
        )

    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == len(responses)
    by_operation = {
        event.details["attribution_operation_name"]: event for event in events
    }
    assert set(by_operation) == {f"litellm.{name}" for name in responses}
    assert by_operation["litellm.responses"].input_tokens == 12
    assert by_operation["litellm.responses"].output_tokens == 4
    assert by_operation["litellm.embedding"].input_tokens == 9
    assert by_operation["litellm.image_generation"].details[
        "attribution_usage_lines"
    ][-1] == {"metric": "image_count", "quantity": "1", "unit": "Images"}
    assert {
        line["metric"]
        for line in by_operation["litellm.transcription"].details[
            "attribution_usage_lines"
        ]
    } == {"audio_seconds"}
    assert by_operation["litellm.speech"].details["attribution_usage_lines"] == [
        {"metric": "characters", "quantity": "19", "unit": "Characters"}
    ]
    assert {
        line["metric"]
        for line in by_operation["litellm.rerank"].details["attribution_usage_lines"]
    } == {"search_units", "input_tokens"}
    assert by_operation["litellm.search"].details["attribution_usage_lines"] == [
        {"metric": "query_count", "quantity": "2", "unit": "Queries"}
    ]
    assert {
        line["metric"]
        for line in by_operation["litellm.ocr"].details["attribution_usage_lines"]
    } == {"page_count", "document_bytes"}
    assert all(event.pricing_source == "litellm" for event in events)
    assert all(event.cost_confidence == "computed" for event in events)
    serialized = " ".join(str(event.to_dict()) for event in events).lower()
    for secret in (
        "private response input",
        "private embedding input",
        "private image prompt",
        "private-audio",
        "private transcript",
        "private speech text",
        "private rerank query",
        "private rerank document",
        "private moderation input",
        "private search one",
        "private result",
        "private document",
        "private-url",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_current_async_operation_owns_nested_sync_call_exactly_once(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, _, _, _ = _load_current_litellm()
    response = _operation_response(
        model="text-embedding-3-small",
        usage={"prompt_tokens": 5, "total_tokens": 5},
    )

    def embedding(**_: Any) -> Any:
        return response

    async def aembedding(**kwargs: Any) -> Any:
        return current_litellm.embedding(**kwargs)

    monkeypatch.setattr(current_litellm, "embedding", embedding)
    monkeypatch.setattr(current_litellm, "aembedding", aembedding)
    monkeypatch.setattr(current_litellm, "completion_cost", lambda **_: 0.0002)
    instrument_litellm(tracker)

    with tracker.task(task_type="litellm.embedding.nested") as task:
        result = await current_litellm.aembedding(
            model="openai/text-embedding-3-small",
            input=["private nested embedding"],
        )

    assert result is response
    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 1
    assert events[0].details["attribution_operation_name"] == "litellm.aembedding"


@pytest.mark.asyncio
async def test_current_native_google_and_anthropic_entry_points_are_metered(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, _, _, _ = _load_current_litellm()
    google_response = _operation_response(
        model="gemini-2.5-flash",
        response_id="gemini-native-current",
        usage_metadata={
            "prompt_token_count": 30,
            "cached_content_token_count": 10,
            "candidates_token_count": 7,
            "thoughts_token_count": 3,
            "tool_use_prompt_token_count": 2,
        },
        provider="gemini",
    )
    anthropic_response = _operation_response(
        model="claude-sonnet-4",
        record_id="anthropic-native-current",
        usage={"input_tokens": 11, "output_tokens": 4},
        provider="anthropic",
    )

    async def agenerate_content(**_: Any) -> Any:
        return google_response

    async def anthropic_messages(**_: Any) -> Any:
        return anthropic_response

    monkeypatch.setattr(current_litellm, "agenerate_content", agenerate_content)
    monkeypatch.setattr(current_litellm, "anthropic_messages", anthropic_messages)
    monkeypatch.setattr(current_litellm, "completion_cost", lambda **_: 0.003)
    instrument_litellm(tracker)

    with tracker.task(task_type="litellm.native.entrypoints") as task:
        await current_litellm.agenerate_content(
            model="gemini/gemini-2.5-flash",
            contents="private Gemini content",
        )
        await current_litellm.anthropic_messages(
            max_tokens=64,
            messages=[{"role": "user", "content": "private Anthropic content"}],
            model="claude-sonnet-4",
        )

    events = tracker._storage.query_events(task_id=str(task.task_id))
    assert len(events) == 2
    by_operation = {
        event.details["attribution_operation_name"]: event for event in events
    }
    google = by_operation["litellm.agenerate_content"]
    assert google.provider == "google"
    assert google.input_tokens == 32
    assert google.output_tokens == 10
    assert google.cached_tokens == 10
    assert google.details["provider_record_id"] == "gemini-native-current"
    anthropic = by_operation["litellm.anthropic_messages"]
    assert anthropic.provider == "anthropic"
    assert anthropic.input_tokens == 11
    assert anthropic.output_tokens == 4
    serialized = str([event.to_dict() for event in events])
    assert "private Gemini content" not in serialized
    assert "private Anthropic content" not in serialized


def test_current_responses_stream_uses_terminal_usage_and_close_cancels(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, _, _, _ = _load_current_litellm()
    terminal = _operation_response(
        model="gpt-5-mini",
        usage={"input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
        record_id="resp-stream-current",
        status="completed",
    )

    def responses(**_: Any) -> Any:
        return iter(
            (
                SimpleNamespace(type="response.output_text.delta", delta="private output"),
                SimpleNamespace(type="response.completed", response=terminal),
            )
        )

    monkeypatch.setattr(current_litellm, "responses", responses)
    monkeypatch.setattr(current_litellm, "completion_cost", lambda **_: 0.004)
    instrument_litellm(tracker)

    with tracker.task(task_type="litellm.responses.stream") as completed_task:
        stream = current_litellm.responses(
            model="openai/gpt-5-mini",
            input="private input",
            stream=True,
        )
        assert len(list(stream)) == 2

    completed = tracker._storage.query_events(task_id=str(completed_task.task_id))[0]
    assert completed.input_tokens == 20
    assert completed.output_tokens == 6
    assert completed.details["provider_record_id"] == "resp-stream-current"
    assert completed.details["attribution_operation_status"] == "succeeded"
    assert "private output" not in str(completed.to_dict())

    with tracker.task(task_type="litellm.responses.cancel") as cancelled_task:
        stream = current_litellm.responses(
            model="openai/gpt-5-mini",
            input="private input",
            stream=True,
        )
        next(stream)
        stream.close()

    cancelled = tracker._storage.query_events(task_id=str(cancelled_task.task_id))[0]
    assert cancelled.details["attribution_operation_status"] == "cancelled"


def test_current_delayed_response_video_batch_and_fine_tuning_jobs_reconcile(
    tracker: CostTracker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_litellm, _, _, _ = _load_current_litellm()
    submitted = {
        "response": _operation_response(
            model="gpt-5-mini",
            record_id="resp-background-current",
            status="queued",
        ),
        "video": _operation_response(
            model="sora-2",
            record_id="video-current",
            status="queued",
        ),
        "batch": _operation_response(
            record_id="batch-current",
            status="validating",
        ),
        "fine": _operation_response(
            model="gpt-4.1-mini",
            record_id="fine-current",
            status="validating_files",
        ),
    }
    terminal = {
        "response": _operation_response(
            model="gpt-5-mini",
            usage={"input_tokens": 15, "output_tokens": 5, "total_tokens": 20},
            record_id="resp-background-current",
            status="completed",
        ),
        "video": _operation_response(
            model="sora-2",
            record_id="video-current",
            status="completed",
            seconds="8",
            size="1280x720",
        ),
        "batch": _operation_response(
            record_id="batch-current",
            status="completed",
            usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
            request_counts={"total": 4, "completed": 3, "failed": 1},
        ),
        "fine": _operation_response(
            model="gpt-4.1-mini",
            record_id="fine-current",
            status="succeeded",
            trained_tokens=900,
        ),
    }
    monkeypatch.setattr(current_litellm, "responses", lambda **_: submitted["response"])
    monkeypatch.setattr(
        current_litellm, "get_responses", lambda **_: terminal["response"]
    )
    monkeypatch.setattr(
        current_litellm, "video_generation", lambda **_: submitted["video"]
    )
    monkeypatch.setattr(current_litellm, "video_status", lambda **_: terminal["video"])
    monkeypatch.setattr(current_litellm, "create_batch", lambda **_: submitted["batch"])
    monkeypatch.setattr(current_litellm, "retrieve_batch", lambda **_: terminal["batch"])
    monkeypatch.setattr(
        current_litellm,
        "create_fine_tuning_job",
        lambda **_: submitted["fine"],
    )
    monkeypatch.setattr(
        current_litellm,
        "retrieve_fine_tuning_job",
        lambda **_: terminal["fine"],
    )
    monkeypatch.setattr(current_litellm, "completion_cost", lambda **_: 0.01)
    instrument_litellm(tracker)

    with tracker.task(task_type="litellm.jobs") as task:
        current_litellm.responses(
            model="openai/gpt-5-mini",
            input="private background input",
            background=True,
        )
        current_litellm.video_generation(
            model="openai/sora-2",
            prompt="private video prompt",
            seconds="8",
            size="1280x720",
        )
        current_litellm.create_batch(
            completion_window="24h",
            endpoint="/v1/chat/completions",
            input_file_id="private-batch-file",
            custom_llm_provider="openai",
        )
        current_litellm.create_fine_tuning_job(
            model="openai/gpt-4.1-mini",
            training_file="private-training-file",
        )

    current_litellm.get_responses(response_id="resp-background-current")
    current_litellm.video_status(video_id="video-current")
    current_litellm.retrieve_batch(batch_id="batch-current")
    current_litellm.retrieve_fine_tuning_job(fine_tuning_job_id="fine-current")

    jobs = tracker._storage.query_current_provider_jobs_for_task(str(task.task_id))
    assert len(jobs) == 4
    by_service = {job.service: job for job in jobs}
    assert set(by_service) == {
        "litellm.responses",
        "litellm.videos",
        "litellm.batches",
        "litellm.fine_tuning",
    }
    assert all(job.revision == 2 for job in jobs)
    assert all(job.status == "succeeded" for job in jobs)
    assert by_service["litellm.responses"].task_input_tokens == 15
    assert by_service["litellm.responses"].task_output_tokens == 5
    assert {line.metric for line in by_service["litellm.videos"].usage} == {
        "output_video_count",
        "output_video_seconds",
    }
    assert {line.metric for line in by_service["litellm.batches"].usage} == {
        "batch_input_tokens",
        "batch_output_tokens",
        "batch_request_count",
        "batch_successful_request_count",
        "batch_failed_request_count",
    }
    assert [line.metric for line in by_service["litellm.fine_tuning"].usage] == [
        "training_billable_tokens"
    ]
    serialized = " ".join(str(job) for job in jobs).lower()
    for secret in (
        "private background input",
        "private video prompt",
        "private-batch-file",
        "private-training-file",
    ):
        assert secret not in serialized
