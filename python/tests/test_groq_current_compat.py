from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

from dexcost.attribution import to_attribution_observation_v3
from dexcost.instruments.groq import instrument_groq, uninstrument_groq
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


class _Completions:
    def create(self, **kwargs: Any) -> Any:
        response = dict(
            id="groq-native-1",
            model=kwargs["model"],
            choices=[SimpleNamespace(message=SimpleNamespace(executed_tools=[]))],
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 10},
                "total_tokens": 130,
            },
        )
        if not kwargs.get("omit_response_service_tier"):
            response["service_tier"] = kwargs.get("service_tier", "on_demand")
        return SimpleNamespace(**response)


class _AsyncCompletions:
    async def create(self, **kwargs: Any) -> Any:
        return _Completions().create(**kwargs)


def test_native_groq_chat_is_metered_with_disjoint_usage_and_public_lane(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    module = ModuleType("groq.resources.chat.completions")
    module.Completions = _Completions  # type: ignore[attr-defined]
    module.AsyncCompletions = _AsyncCompletions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "groq.resources.chat.completions", module)
    storage = SQLiteStorage(tmp_path / "groq-native.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    instrument_groq(tracker)
    try:
        with tracker.task(task_type="groq-native"):
            _Completions().create(
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": "private"}],
            )

        events = storage.query_events()
        assert len(events) == 1
        event = events[0]
        assert event.provider == "groq"
        assert event.model == "openai/gpt-oss-120b"
        observation = to_attribution_observation_v3(event)
        assert observation is not None
        assert observation["provider"] == {
            "name": "groq",
            "service": "api",
            "record_id": "groq-native-1",
        }
        assert {line["metric"]: line["quantity"] for line in observation["usage"]} == {
            "input_tokens": "80",
            "cache_read_input_tokens": "20",
            "output_tokens": "20",
            "reasoning_output_tokens": "10",
        }
        dimensions = {
            item["key"]: item["value"]["value"]
            for item in observation["usage"][0]["dimensions"]
        }
        assert dimensions == {"gateway": "groq", "groq_pricing_lane": "public_sync"}
        assert "private" not in str(event.to_dict())
    finally:
        uninstrument_groq()
        storage.close()


def test_native_groq_request_tier_fails_open_when_response_omits_it(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    module = ModuleType("groq.resources.chat.completions")
    module.Completions = _Completions  # type: ignore[attr-defined]
    module.AsyncCompletions = _AsyncCompletions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "groq.resources.chat.completions", module)
    storage = SQLiteStorage(tmp_path / "groq-native-request-tier.db")
    tracker = CostTracker(storage=storage, auto_instrument=[])
    instrument_groq(tracker)
    try:
        with tracker.task(task_type="groq-native-performance"):
            _Completions().create(
                model="openai/gpt-oss-120b",
                service_tier="performance",
                omit_response_service_tier=True,
                messages=[],
            )

        observation = to_attribution_observation_v3(storage.query_events()[0])
        assert observation is not None
        dimensions = {
            item["key"]: item["value"]["value"]
            for item in observation["usage"][0]["dimensions"]
        }
        assert dimensions == {"gateway": "groq"}
    finally:
        uninstrument_groq()
        storage.close()
