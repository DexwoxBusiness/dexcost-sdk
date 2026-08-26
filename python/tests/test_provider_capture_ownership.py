"""The outer provider gateway owns one logical operation exactly once."""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest

from dexcost.instruments._capture import (
    current_provider_capture_owner,
    provider_capture_wrapper,
)
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _response() -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = 30
    usage.completion_tokens = 12
    usage.prompt_tokens_details = None
    usage.completion_tokens_details = None
    response = MagicMock()
    response.model = "gpt-4o"
    response.usage = usage
    response._hidden_params = {"custom_llm_provider": "openai"}
    return response


def _install_fake_sdks() -> tuple[types.ModuleType, type[Any], type[Any]]:
    openai_mod = types.ModuleType("openai")
    resources_mod = types.ModuleType("openai.resources")
    chat_mod = types.ModuleType("openai.resources.chat")
    completions_mod = types.ModuleType("openai.resources.chat.completions")

    class Completions:
        @staticmethod
        def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    class AsyncCompletions:
        @staticmethod
        async def create(**kwargs: Any) -> Any:
            raise NotImplementedError

    completions_mod.Completions = Completions  # type: ignore[attr-defined]
    completions_mod.AsyncCompletions = AsyncCompletions  # type: ignore[attr-defined]
    chat_mod.completions = completions_mod  # type: ignore[attr-defined]
    resources_mod.chat = chat_mod  # type: ignore[attr-defined]
    openai_mod.resources = resources_mod  # type: ignore[attr-defined]

    litellm_mod = types.ModuleType("litellm")

    def completion(**kwargs: Any) -> Any:
        return Completions.create(model=kwargs["model"], messages=kwargs["messages"])

    async def acompletion(**kwargs: Any) -> Any:
        return await AsyncCompletions.create(model=kwargs["model"], messages=kwargs["messages"])

    litellm_mod.completion = completion  # type: ignore[attr-defined]
    litellm_mod.acompletion = acompletion  # type: ignore[attr-defined]
    litellm_mod.completion_cost = None  # type: ignore[attr-defined]

    sys.modules["openai"] = openai_mod
    sys.modules["openai.resources"] = resources_mod
    sys.modules["openai.resources.chat"] = chat_mod
    sys.modules["openai.resources.chat.completions"] = completions_mod
    sys.modules["litellm"] = litellm_mod
    return litellm_mod, Completions, AsyncCompletions


@pytest.fixture()
def fake_sdks() -> Generator[tuple[types.ModuleType, type[Any], type[Any]], None, None]:
    prefixes = ("openai", "litellm")
    saved = {
        key: value
        for key, value in sys.modules.items()
        if any(key == prefix or key.startswith(f"{prefix}.") for prefix in prefixes)
    }
    for key in list(saved):
        sys.modules.pop(key, None)
    installed = _install_fake_sdks()
    yield installed
    from dexcost.instruments.litellm import uninstrument_litellm
    from dexcost.instruments.openai import uninstrument_openai

    uninstrument_litellm()
    uninstrument_openai()
    for key in list(sys.modules):
        if any(key == prefix or key.startswith(f"{prefix}.") for prefix in prefixes):
            sys.modules.pop(key, None)
    sys.modules.update(saved)


def test_capture_wrapper_is_outermost_wins_for_sync_calls() -> None:
    recorded: list[str] = []

    def inner_adapter(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        result = wrapped(*args, **kwargs)
        recorded.append("inner")
        return result

    inner = provider_capture_wrapper("inner", inner_adapter)

    def outer_adapter(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        result = inner(wrapped, instance, args, kwargs)
        recorded.append("outer")
        return result

    outer = provider_capture_wrapper("outer", outer_adapter)

    assert outer(lambda: "ok", None, (), {}) == "ok"
    assert recorded == ["outer"]
    assert current_provider_capture_owner() is None


def test_capture_wrapper_preserves_owner_across_await() -> None:
    recorded: list[str] = []

    async def raw() -> str:
        assert current_provider_capture_owner() == "outer"
        return "ok"

    def inner_adapter(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        recorded.append("inner")
        return wrapped(*args, **kwargs)

    inner = provider_capture_wrapper("inner", inner_adapter)

    def outer_adapter(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        recorded.append("outer")
        return inner(wrapped, instance, args, kwargs)

    outer = provider_capture_wrapper("outer", outer_adapter)

    assert asyncio.run(outer(raw, None, (), {})) == "ok"
    assert recorded == ["outer"]
    assert current_provider_capture_owner() is None


def test_litellm_sync_call_suppresses_nested_openai_capture(
    fake_sdks: tuple[types.ModuleType, type[Any], type[Any]], tmp_path: Any
) -> None:
    litellm, completions, _ = fake_sdks
    response = _response()
    completions.create = staticmethod(lambda **kwargs: response)  # type: ignore[method-assign]
    storage = SQLiteStorage(tmp_path / "sync-owner.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    from dexcost.instruments.litellm import instrument_litellm
    from dexcost.instruments.openai import instrument_openai

    try:
        instrument_openai(tracker)
        instrument_litellm(tracker)
        with tracker.task(task_type="nested-litellm") as task:
            assert litellm.completion(model="openai/gpt-4o", messages=[]) is response

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1
        assert events[0].service_name == "litellm"
        assert events[0].details["attribution_dimensions"] == [
            {"key": "gateway", "value": {"type": "string", "value": "litellm"}}
        ]
    finally:
        storage.close()


def test_litellm_async_call_suppresses_nested_openai_capture(
    fake_sdks: tuple[types.ModuleType, type[Any], type[Any]], tmp_path: Any
) -> None:
    litellm, _, async_completions = fake_sdks
    response = _response()

    async def create(**kwargs: Any) -> Any:
        return response

    async_completions.create = staticmethod(create)  # type: ignore[method-assign]
    storage = SQLiteStorage(tmp_path / "async-owner.db")
    tracker = CostTracker(storage=storage, auto_update_pricing=False, auto_instrument=[])
    from dexcost.instruments.litellm import instrument_litellm
    from dexcost.instruments.openai import instrument_openai

    async def run() -> str:
        async with tracker.task(task_type="nested-litellm-async") as task:
            result = await litellm.acompletion(model="openai/gpt-4o", messages=[])
            assert result is response
        return str(task.task_id)

    try:
        instrument_openai(tracker)
        instrument_litellm(tracker)
        task_id = asyncio.run(run())
        events = storage.query_events(task_id=task_id)
        assert len(events) == 1
        assert events[0].service_name == "litellm"
    finally:
        storage.close()
