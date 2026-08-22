"""Compatibility gates against the installed current LiteLLM package."""

from __future__ import annotations

import importlib
import sys
from decimal import Decimal
from typing import Any

import pytest

from dexcost.instruments.litellm import instrument_litellm, uninstrument_litellm
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
    assert "messages" not in str(event.details).lower()


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
            "together_ai/meta-llama/model",
            "meta-llama/model",
            "together",
            "together_ai/meta-llama/model",
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

    with tracker.task(task_type="litellm.openrouter.stream") as completed_task:
        assert (
            len(list(current_litellm.completion(model="openrouter/openai/gpt-4.1", stream=True)))
            == 2
        )

    completed = tracker._storage.query_events(task_id=str(completed_task.task_id))[0]
    assert completed.details["attribution_operation_status"] == "succeeded"
    assert completed.cost_usd == Decimal("0.0025")
    assert completed.pricing_source == "provider_response"

    with tracker.task(task_type="litellm.openrouter.cancel") as cancelled_task:
        stream = current_litellm.completion(model="openrouter/openai/gpt-4.1", stream=True)
        next(stream)
        stream.close()

    cancelled = tracker._storage.query_events(task_id=str(cancelled_task.task_id))[0]
    assert cancelled.provider == "openrouter"
    assert cancelled.details["attribution_operation_status"] == "cancelled"
    assert cancelled.cost_confidence == "unknown"
