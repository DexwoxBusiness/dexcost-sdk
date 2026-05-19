"""Tests for wrapper client fallback (US-021)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from dexcost.clients import TrackedAnthropic, TrackedOpenAI
from dexcost.context import set_current_task
from dexcost.pricing import PricingEngine
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _mock_openai_response(
    model: str = "gpt-4o",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> SimpleNamespace:
    """Create a mock OpenAI-style response."""
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=0,
    )
    choice = SimpleNamespace(
        message=SimpleNamespace(content="Hello!"),
        finish_reason="stop",
    )
    return SimpleNamespace(
        id="chatcmpl-test",
        model=model,
        usage=usage,
        choices=[choice],
    )


def _mock_anthropic_response(
    model: str = "claude-3-haiku-20240307",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> SimpleNamespace:
    """Create a mock Anthropic-style response."""
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return SimpleNamespace(
        id="msg-test",
        model=model,
        usage=usage,
        content=[SimpleNamespace(text="Hello!", type="text")],
        stop_reason="end_turn",
    )


class TestTrackedOpenAI:
    def test_records_event_in_task_context(self, tmp_path: Any) -> None:
        """TrackedOpenAI records llm_call event when inside a task."""
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        tracker = CostTracker(storage=storage, auto_instrument=[])

        # Mock the underlying OpenAI client
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_openai_response()

        wrapped = TrackedOpenAI(client=mock_client, tracker=tracker)

        with tracker.task(task_type="test_task") as t:
            response = wrapped.chat.completions.create(model="gpt-4o", messages=[])

        # Verify response passed through
        assert response.choices[0].message.content == "Hello!"

        # Verify event was recorded
        events = storage.query_events(task_id=str(t.task_id))
        llm_events = [e for e in events if e.event_type == "llm_call"]
        assert len(llm_events) >= 1
        assert llm_events[0].provider == "openai"
        assert llm_events[0].model == "gpt-4o"
        assert llm_events[0].input_tokens == 100
        assert llm_events[0].output_tokens == 50

        tracker.pricing.close()
        storage.close()

    def test_no_event_without_context(self, tmp_path: Any) -> None:
        """TrackedOpenAI passes through silently without task context."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_openai_response()
        pricing = PricingEngine(auto_update=False)
        wrapped = TrackedOpenAI(client=mock_client, pricing=pricing)

        # No task context
        set_current_task(None)
        response = wrapped.chat.completions.create(model="gpt-4o", messages=[])
        assert response.choices[0].message.content == "Hello!"
        pricing.close()

    def test_preserves_return_value(self, tmp_path: Any) -> None:
        """Return value from underlying client is unchanged."""
        expected = _mock_openai_response(model="gpt-4o-mini")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = expected
        pricing = PricingEngine(auto_update=False)
        wrapped = TrackedOpenAI(client=mock_client, pricing=pricing)

        result = wrapped.chat.completions.create(model="gpt-4o-mini", messages=[])
        assert result is expected
        pricing.close()

    def test_proxies_non_wrapped_attrs(self) -> None:
        """Attributes not explicitly wrapped are proxied to the real client."""
        mock_client = MagicMock()
        mock_client.models.list.return_value = ["gpt-4o"]
        pricing = PricingEngine(auto_update=False)
        wrapped = TrackedOpenAI(client=mock_client, pricing=pricing)

        result = wrapped.models.list()
        assert result == ["gpt-4o"]
        pricing.close()


class TestTrackedAnthropic:
    def test_records_event_in_task_context(self, tmp_path: Any) -> None:
        """TrackedAnthropic records llm_call event when inside a task."""
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        tracker = CostTracker(storage=storage, auto_instrument=[])

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_anthropic_response()

        wrapped = TrackedAnthropic(client=mock_client, tracker=tracker)

        with tracker.task(task_type="test_task") as t:
            response = wrapped.messages.create(
                model="claude-3-haiku-20240307", max_tokens=100, messages=[]
            )

        assert response.content[0].text == "Hello!"

        events = storage.query_events(task_id=str(t.task_id))
        llm_events = [e for e in events if e.event_type == "llm_call"]
        assert len(llm_events) >= 1
        assert llm_events[0].provider == "anthropic"
        assert llm_events[0].input_tokens == 100
        assert llm_events[0].output_tokens == 50

        tracker.pricing.close()
        storage.close()

    def test_no_event_without_context(self, tmp_path: Any) -> None:
        """TrackedAnthropic passes through silently without task context."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_anthropic_response()
        pricing = PricingEngine(auto_update=False)
        wrapped = TrackedAnthropic(client=mock_client, pricing=pricing)

        set_current_task(None)
        response = wrapped.messages.create(
            model="claude-3-haiku-20240307", max_tokens=100, messages=[]
        )
        assert response.content[0].text == "Hello!"
        pricing.close()

    def test_works_with_auto_instrument_disabled(self, tmp_path: Any) -> None:
        """Wrappers work when auto_instrument=[] (fully explicit mode)."""
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        tracker = CostTracker(storage=storage, auto_instrument=[])

        mock_client = MagicMock()
        mock_client.messages.create.return_value = _mock_anthropic_response()
        wrapped = TrackedAnthropic(client=mock_client, tracker=tracker)

        with tracker.task(task_type="explicit_test") as t:
            wrapped.messages.create(model="claude-3-haiku-20240307", max_tokens=50, messages=[])

        events = storage.query_events(task_id=str(t.task_id))
        assert any(e.event_type == "llm_call" for e in events)

        tracker.pricing.close()
        storage.close()

    def test_cache_tokens_recorded(self, tmp_path: Any) -> None:
        """Anthropic cache tokens (creation + read) are summed as cached_tokens."""
        storage = SQLiteStorage(db_path=tmp_path / "test.db")
        tracker = CostTracker(storage=storage, auto_instrument=[])

        mock_resp = SimpleNamespace(
            id="msg-cache-test",
            model="claude-3-haiku-20240307",
            usage=SimpleNamespace(
                input_tokens=200,
                output_tokens=75,
                cache_creation_input_tokens=30,
                cache_read_input_tokens=20,
            ),
            content=[SimpleNamespace(text="cached!", type="text")],
            stop_reason="end_turn",
        )
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp

        wrapped = TrackedAnthropic(client=mock_client, tracker=tracker)

        with tracker.task(task_type="cache_test") as t:
            wrapped.messages.create(model="claude-3-haiku-20240307", max_tokens=100, messages=[])

        events = storage.query_events(task_id=str(t.task_id))
        llm_events = [e for e in events if e.event_type == "llm_call"]
        assert len(llm_events) == 1
        assert llm_events[0].cached_tokens == 50  # 30 + 20

        tracker.pricing.close()
        storage.close()

    def test_proxies_non_wrapped_attrs(self) -> None:
        """Attributes not explicitly wrapped are proxied to the real client."""
        mock_client = MagicMock()
        mock_client.count_tokens.return_value = 42
        pricing = PricingEngine(auto_update=False)
        wrapped = TrackedAnthropic(client=mock_client, pricing=pricing)

        result = wrapped.count_tokens()
        assert result == 42
        pricing.close()
