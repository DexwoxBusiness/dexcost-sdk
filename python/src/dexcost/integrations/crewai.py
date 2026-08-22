"""Current CrewAI integration using its documented execution and event APIs."""

from __future__ import annotations

import hashlib
import logging
import threading
import warnings
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from dexcost.capabilities import canonical_tool_capability_name
from dexcost.integrations._framework_runtime import (
    FrameworkExecutionProxy,
    current_framework_invocation,
    resolve_tracker,
)
from dexcost.models.capability import CapabilityIdentity
from dexcost.tracker import CostTracker

_log = logging.getLogger(__name__)

# Public, cost-causing entry points present across current Crew, Agent,
# LiteAgent, and Flow objects. Missing methods are simply delegated normally.
CREWAI_EXECUTION_METHODS = frozenset(
    {
        "aexecute_task",
        "akickoff",
        "akickoff_for_each",
        "aquery_knowledge",
        "ask",
        "astream",
        "execute_task",
        "extract_memories",
        "kickoff",
        "kickoff_async",
        "kickoff_for_each",
        "kickoff_for_each_async",
        "message",
        "query_knowledge",
        "recall",
        "remember",
        "replay",
        "resume",
        "resume_async",
        "stream_events",
        "test",
        "train",
    }
)
_CREWAI_FORCE_STREAM_METHODS = frozenset({"astream", "stream_events"})

_registration_lock = threading.Lock()
_event_handlers_registered = False
_crewai_event_bus: Any = None


def _bounded_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if 1 <= len(stripped) <= 256 else None


def _idempotency_key(kind: str, event_id: str) -> str:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"crewai.{kind}.{digest}"


def _event_id(event: Any) -> str | None:
    return _bounded_string(getattr(event, "event_id", None))


def _tool_capability(tool_id: str) -> CapabilityIdentity:
    return CapabilityIdentity(
        name=canonical_tool_capability_name(tool_id),
        kind="tool",
        namespace="crewai",
        source="other",
        source_id=tool_id,
        invocation="nested",
    )


def _tool_dimensions(event: Any) -> dict[str, str | bool | int]:
    dimensions: dict[str, str | bool | int] = {}
    agent_id = _bounded_string(getattr(event, "agent_id", None))
    task_id = _bounded_string(getattr(event, "task_id", None))
    if agent_id is not None:
        dimensions["agent_id"] = agent_id
    if task_id is not None:
        dimensions["framework_task_id"] = task_id
    from_cache = getattr(event, "from_cache", None)
    if isinstance(from_cache, bool):
        dimensions["from_cache"] = from_cache
    run_attempts = getattr(event, "run_attempts", None)
    if isinstance(run_attempts, int) and not isinstance(run_attempts, bool):
        dimensions["run_attempts"] = max(run_attempts, 0)
    return dimensions


def _duration_ms(started_at: Any, finished_at: Any) -> int:
    if not isinstance(started_at, datetime) or not isinstance(finished_at, datetime):
        return 0
    return max(0, min(int((finished_at - started_at).total_seconds() * 1000), 86_400_000))


def _handle_tool_finished(source: Any, event: Any) -> None:
    del source
    session = current_framework_invocation("crewai")
    if session is None or not session.capture_tool_events:
        return
    tool_id = _bounded_string(getattr(event, "tool_name", None))
    event_id = _event_id(event)
    if tool_id is None or event_id is None:
        return
    key = _idempotency_key("tool", event_id)
    if not session.claim_event(key):
        return
    failure = getattr(event, "failure", None)
    try:
        session.tracked_task.record_tool_call(
            tool_id,
            status="failed" if failure is not None else "succeeded",
            duration_ms=_duration_ms(
                getattr(event, "started_at", None),
                getattr(event, "finished_at", None),
            ),
            provider="crewai",
            provider_record_id=event_id,
            error_type=type(failure).__name__ if failure is not None else None,
            dimensions=_tool_dimensions(event),
            idempotency_key=key,
            capability=_tool_capability(tool_id),
        )
    except Exception:
        _log.debug("dexcost: CrewAI tool completion capture failed", exc_info=True)


def _handle_tool_error(source: Any, event: Any) -> None:
    del source
    session = current_framework_invocation("crewai")
    if session is None or not session.capture_tool_events:
        return
    tool_id = _bounded_string(getattr(event, "tool_name", None))
    event_id = _event_id(event)
    if tool_id is None or event_id is None:
        return
    key = _idempotency_key("tool", event_id)
    if not session.claim_event(key):
        return
    error = getattr(event, "error", None)
    try:
        session.tracked_task.record_tool_call(
            tool_id,
            status="failed",
            duration_ms=0,
            provider="crewai",
            provider_record_id=event_id,
            error_type=type(error).__name__ if error is not None else "CrewAIToolError",
            dimensions=_tool_dimensions(event),
            idempotency_key=key,
            capability=_tool_capability(tool_id),
        )
    except Exception:
        _log.debug("dexcost: CrewAI tool failure capture failed", exc_info=True)


def _integer_token(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value < 0 or int(value) != value:
        return 0
    return int(value)


def _usage_tokens(usage: Any) -> tuple[int, int, int]:
    if not isinstance(usage, Mapping):
        return 0, 0, 0

    def first(*names: str) -> int:
        for name in names:
            if name in usage:
                return _integer_token(usage[name])
        return 0

    input_tokens = first("input_tokens", "prompt_tokens", "prompt_token_count")
    output_tokens = first(
        "output_tokens",
        "completion_tokens",
        "candidates_token_count",
    )
    cached_tokens = first(
        "cached_tokens",
        "cached_prompt_tokens",
        "cache_read_input_tokens",
        "prompt_tokens_cached",
    )
    if cached_tokens == 0:
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, Mapping):
            cached_tokens = _integer_token(prompt_details.get("cached_tokens"))
    return input_tokens, output_tokens, min(cached_tokens, input_tokens)


def _handle_llm_completed(source: Any, event: Any) -> None:
    del source
    session = current_framework_invocation("crewai")
    if session is None or not session.capture_llm_events:
        return
    event_id = _event_id(event)
    model = _bounded_string(getattr(event, "model", None))
    if event_id is None or model is None:
        return
    key = _idempotency_key("llm", event_id)
    if not session.claim_event(key):
        return
    input_tokens, output_tokens, cached_tokens = _usage_tokens(
        getattr(event, "usage", None)
    )
    details: dict[str, Any] = {"framework_event_id": event_id}
    call_id = _bounded_string(getattr(event, "call_id", None))
    response_id = _bounded_string(getattr(event, "response_id", None))
    if call_id is not None:
        details["provider_record_id"] = call_id
    elif response_id is not None:
        details["provider_record_id"] = response_id
    try:
        session.tracked_task.record_llm_call(
            "crewai",
            model,
            input_tokens,
            output_tokens,
            cached_tokens=cached_tokens,
            details=details,
            idempotency_key=key,
            capability=session.capability,
        )
    except Exception:
        _log.debug("dexcost: CrewAI LLM fallback capture failed", exc_info=True)


def _handle_llm_failed(source: Any, event: Any) -> None:
    del source
    session = current_framework_invocation("crewai")
    if session is None or not session.capture_llm_events:
        return
    event_id = _event_id(event)
    model = _bounded_string(getattr(event, "model", None))
    if event_id is None or model is None:
        return
    key = _idempotency_key("llm", event_id)
    if not session.claim_event(key):
        return
    details: dict[str, Any] = {"framework_event_id": event_id}
    call_id = _bounded_string(getattr(event, "call_id", None))
    if call_id is not None:
        details["provider_record_id"] = call_id
    try:
        session.tracked_task.record_llm_call(
            "crewai",
            model,
            0,
            0,
            cost_usd="0",
            cost_confidence="unknown",
            pricing_source="unknown",
            details=details,
            error_type="CrewAILLMError",
            idempotency_key=key,
            capability=session.capability,
        )
    except Exception:
        _log.debug("dexcost: CrewAI LLM failure fallback capture failed", exc_info=True)


def _handle_execution_failed(source: Any, event: Any) -> None:
    del source, event
    session = current_framework_invocation("crewai")
    if session is not None:
        session.mark_failed()


def _ensure_event_handlers() -> None:
    global _crewai_event_bus, _event_handlers_registered
    if _event_handlers_registered:
        return
    with _registration_lock:
        if _event_handlers_registered:
            return
        try:
            from crewai.events import (
                AgentExecutionErrorEvent,
                CrewKickoffFailedEvent,
                CrewTestFailedEvent,
                CrewTrainFailedEvent,
                FlowFailedEvent,
                LiteAgentExecutionErrorEvent,
                LLMCallCompletedEvent,
                LLMCallFailedEvent,
                TaskFailedEvent,
                ToolUsageErrorEvent,
                ToolUsageFinishedEvent,
                crewai_event_bus,
            )
        except (ImportError, AttributeError) as exc:
            raise ImportError(
                "DexCost's CrewAI integration requires crewai>=1.0. "
                "Install it with `pip install 'dexcost[crewai]'`."
            ) from exc
        crewai_event_bus.on(ToolUsageFinishedEvent)(_handle_tool_finished)
        crewai_event_bus.on(ToolUsageErrorEvent)(_handle_tool_error)
        crewai_event_bus.on(LLMCallCompletedEvent)(_handle_llm_completed)
        crewai_event_bus.on(LLMCallFailedEvent)(_handle_llm_failed)
        for failure_event in (
            AgentExecutionErrorEvent,
            CrewKickoffFailedEvent,
            CrewTestFailedEvent,
            CrewTrainFailedEvent,
            FlowFailedEvent,
            LiteAgentExecutionErrorEvent,
            TaskFailedEvent,
        ):
            crewai_event_bus.on(failure_event)(_handle_execution_failed)
        _crewai_event_bus = crewai_event_bus
        _event_handlers_registered = True


def _flush_event_handlers() -> None:
    if _crewai_event_bus is not None:
        _crewai_event_bus.flush(timeout=30.0)


def track_crewai(
    execution: Any,
    tracker: CostTracker | None = None,
    *,
    task_type: str | None = None,
    capability: CapabilityIdentity | None = None,
    capture_llm_events: bool = False,
    capture_tool_events: bool = True,
    additional_methods: Sequence[str] = (),
) -> Any:
    """Wrap a CrewAI Crew, Agent, LiteAgent, or Flow execution object.

    Provider SDK instrumentation is authoritative for LLM usage by default.
    Set ``capture_llm_events=True`` only as a fallback for a custom provider
    that DexCost cannot instrument; enabling both paths can double-count calls.

    The wrapper covers current sync, native-async, thread-async, streaming,
    replay, train/test, resume, direct-agent, knowledge, and memory entry points.
    It never subclasses or monkey-patches CrewAI models.
    """
    _ensure_event_handlers()
    methods = CREWAI_EXECUTION_METHODS | frozenset(additional_methods)
    if not any(callable(getattr(execution, name, None)) for name in methods):
        raise TypeError(
            "execution must expose a supported CrewAI execution method such as kickoff()"
        )
    if capture_llm_events:
        warnings.warn(
            "capture_llm_events=True is a fallback path; do not combine it with "
            "DexCost provider instrumentation for the same LLM calls.",
            RuntimeWarning,
            stacklevel=2,
        )
    tracker = resolve_tracker(tracker)
    return FrameworkExecutionProxy(
        execution,
        tracker=tracker,
        framework="crewai",
        methods=tuple(methods),
        force_stream_methods=tuple(_CREWAI_FORCE_STREAM_METHODS),
        task_type=task_type,
        capability=capability,
        capture_llm_events=capture_llm_events,
        capture_tool_events=capture_tool_events,
        before_finalize=_flush_event_handlers,
    )


__all__ = ["CREWAI_EXECUTION_METHODS", "track_crewai"]
