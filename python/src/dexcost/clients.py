"""Wrapper client fallback for environments where monkey-patching won't work (US-021).

Provides ``TrackedOpenAI`` and ``TrackedAnthropic`` -- thin wrappers around
the real SDK clients that auto-record LLM events to the active task context.

Usage::

    from dexcost.clients import TrackedOpenAI

    client = TrackedOpenAI(tracker=tracker)
    # Inside a tracked task, events are auto-recorded:
    response = client.chat.completions.create(model="gpt-4o", messages=[...])
"""

from __future__ import annotations

import logging
from typing import Any

from dexcost.context import get_current_task
from dexcost.models.event import Event
from dexcost.pricing import PricingEngine
from dexcost.storage.protocol import StorageBackend

_log = logging.getLogger(__name__)


def _get_task_id() -> Any:
    """Get the current Task's task_id from context, or None."""
    task = get_current_task()
    if task is None:
        return None
    return task.task_id


class _TrackedOpenAICompletions:
    """Wraps openai chat completions, recording events on each call."""

    def __init__(
        self,
        completions: Any,
        pricing: PricingEngine,
        storage: StorageBackend | None,
    ) -> None:
        self._completions = completions
        self._pricing = pricing
        self._storage = storage

    def create(self, **kwargs: Any) -> Any:
        """Call openai chat.completions.create and record event."""
        response = self._completions.create(**kwargs)
        self._record_event(response, kwargs.get("model", "unknown"))
        return response

    async def acreate(self, **kwargs: Any) -> Any:
        """Call openai async chat.completions.create and record event."""
        response = await self._completions.acreate(**kwargs)
        self._record_event(response, kwargs.get("model", "unknown"))
        return response

    def _record_event(self, response: Any, model: str) -> None:
        task_id = _get_task_id()
        if task_id is None:
            return
        if self._storage is None:
            return

        usage = getattr(response, "usage", None)
        if usage is None:
            return

        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        cached_tokens = getattr(usage, "cached_tokens", 0) or 0

        cost_result = self._pricing.get_cost(model, input_tokens, output_tokens, cached_tokens)

        event = Event(
            task_id=task_id,
            event_type="llm_call",
            cost_usd=cost_result.cost_usd,
            cost_confidence=cost_result.cost_confidence,
            pricing_source=cost_result.pricing_source,
            pricing_version=cost_result.pricing_version,
            provider="openai",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )
        self._storage.insert_event(event)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._completions, name)


class _TrackedOpenAIChat:
    """Wraps openai chat namespace."""

    def __init__(
        self,
        chat: Any,
        pricing: PricingEngine,
        storage: StorageBackend | None,
    ) -> None:
        self._chat = chat
        self.completions = _TrackedOpenAICompletions(chat.completions, pricing, storage)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class TrackedOpenAI:
    """Thin wrapper around ``openai.OpenAI`` that auto-records LLM events.

    Works independently of auto-instrumentation (no monkey-patching).
    Pass an existing client or let the wrapper create one.

    The recommended way is to pass a :class:`~dexcost.tracker.CostTracker`
    via the *tracker* parameter, which provides both pricing and storage::

        from dexcost import CostTracker
        from dexcost.clients import TrackedOpenAI

        tracker = CostTracker(auto_instrument=[])
        client = TrackedOpenAI(tracker=tracker)

    Alternatively, pass *pricing* directly (events are only recorded if
    a tracker with storage is provided).
    """

    def __init__(
        self,
        client: Any = None,
        *,
        tracker: Any = None,
        pricing: PricingEngine | None = None,
        **kwargs: Any,
    ) -> None:
        if client is None:
            import openai

            client = openai.OpenAI(**kwargs)
        self._client = client

        # Resolve pricing and storage from tracker or explicit params
        if tracker is not None:
            self._pricing: PricingEngine = tracker.pricing
            self._storage: StorageBackend | None = tracker.storage
        elif pricing is not None:
            self._pricing = pricing
            self._storage = None
        else:
            self._pricing = PricingEngine(auto_update=False)
            self._storage = None

        self.chat = _TrackedOpenAIChat(client.chat, self._pricing, self._storage)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _TrackedAnthropicMessages:
    """Wraps anthropic messages, recording events on each call."""

    def __init__(
        self,
        messages: Any,
        pricing: PricingEngine,
        storage: StorageBackend | None,
    ) -> None:
        self._messages = messages
        self._pricing = pricing
        self._storage = storage

    def create(self, **kwargs: Any) -> Any:
        """Call anthropic messages.create and record event."""
        response = self._messages.create(**kwargs)
        self._record_event(response, kwargs.get("model", "unknown"))
        return response

    async def acreate(self, **kwargs: Any) -> Any:
        """Call anthropic async messages.create and record event."""
        response = await self._messages.acreate(**kwargs)
        self._record_event(response, kwargs.get("model", "unknown"))
        return response

    def _record_event(self, response: Any, model: str) -> None:
        task_id = _get_task_id()
        if task_id is None:
            return
        if self._storage is None:
            return

        usage = getattr(response, "usage", None)
        if usage is None:
            return

        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        # Anthropic cache tokens
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cached_tokens = cache_creation + cache_read

        cost_result = self._pricing.get_cost(model, input_tokens, output_tokens, cached_tokens)

        event = Event(
            task_id=task_id,
            event_type="llm_call",
            cost_usd=cost_result.cost_usd,
            cost_confidence=cost_result.cost_confidence,
            pricing_source=cost_result.pricing_source,
            pricing_version=cost_result.pricing_version,
            provider="anthropic",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )
        self._storage.insert_event(event)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._messages, name)


class TrackedAnthropic:
    """Thin wrapper around ``anthropic.Anthropic`` that auto-records LLM events.

    Works independently of auto-instrumentation (no monkey-patching).

    The recommended way is to pass a :class:`~dexcost.tracker.CostTracker`
    via the *tracker* parameter::

        from dexcost import CostTracker
        from dexcost.clients import TrackedAnthropic

        tracker = CostTracker(auto_instrument=[])
        client = TrackedAnthropic(tracker=tracker)
    """

    def __init__(
        self,
        client: Any = None,
        *,
        tracker: Any = None,
        pricing: PricingEngine | None = None,
        **kwargs: Any,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(**kwargs)
        self._client = client

        # Resolve pricing and storage from tracker or explicit params
        if tracker is not None:
            self._pricing: PricingEngine = tracker.pricing
            self._storage: StorageBackend | None = tracker.storage
        elif pricing is not None:
            self._pricing = pricing
            self._storage = None
        else:
            self._pricing = PricingEngine(auto_update=False)
            self._storage = None

        self.messages = _TrackedAnthropicMessages(client.messages, self._pricing, self._storage)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
