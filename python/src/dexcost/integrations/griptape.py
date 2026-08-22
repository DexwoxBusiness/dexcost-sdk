"""Griptape integration using Structure and context-local EventBus APIs."""

from __future__ import annotations

import functools
import hashlib
import logging
import warnings
from collections.abc import Sequence
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

GRIPTAPE_EXECUTION_METHODS = frozenset({"run", "run_stream"})
_GRIPTAPE_FORCE_STREAM_METHODS = frozenset({"run_stream"})


def _bounded_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if 1 <= len(stripped) <= 256 else None


def _idempotency_key(kind: str, event_id: str, index: int | None = None) -> str:
    identity = event_id if index is None else f"{event_id}\0{index}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"griptape.{kind}.{digest}"


def _tool_capability(tool_id: str) -> CapabilityIdentity:
    return CapabilityIdentity(
        name=canonical_tool_capability_name(tool_id),
        kind="tool",
        namespace="griptape",
        source="other",
        source_id=tool_id,
        invocation="nested",
    )


def _integer_token(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value < 0 or int(value) != value:
        return 0
    return int(value)


def _action_tool_id(action: Any) -> str | None:
    if not isinstance(action, dict):
        return None
    for key in ("name", "path", "tag"):
        value = _bounded_string(action.get(key))
        if value is not None:
            return value
    return None


def _handle_griptape_event(event: Any) -> Any:
    session = current_framework_invocation("griptape")
    if session is None:
        return event
    try:
        from griptape.artifacts import ErrorArtifact
        from griptape.events import (
            FinishActionsSubtaskEvent,
            FinishPromptEvent,
            FinishStructureRunEvent,
        )
    except ImportError:
        return event

    event_id = _bounded_string(getattr(event, "id", None))
    if event_id is None:
        return event
    if isinstance(event, FinishStructureRunEvent):
        if isinstance(getattr(event, "output_task_output", None), ErrorArtifact):
            session.mark_failed()
        return event
    if isinstance(event, FinishPromptEvent):
        if not session.capture_llm_events:
            return event
        key = _idempotency_key("llm", event_id)
        if not session.claim_event(key):
            return event
        model = _bounded_string(getattr(event, "model", None))
        if model is None:
            return event
        try:
            session.tracked_task.record_llm_call(
                "griptape",
                model,
                _integer_token(getattr(event, "input_token_count", None)),
                _integer_token(getattr(event, "output_token_count", None)),
                details={"framework_event_id": event_id},
                idempotency_key=key,
                capability=session.capability,
            )
        except Exception:
            _log.debug("dexcost: Griptape LLM fallback capture failed", exc_info=True)
        return event

    if not isinstance(event, FinishActionsSubtaskEvent) or not session.capture_tool_events:
        return event
    actions = getattr(event, "subtask_actions", None)
    if not isinstance(actions, list):
        return event
    task_id = _bounded_string(getattr(event, "task_id", None))
    output = getattr(event, "task_output", None)
    failed = isinstance(output, ErrorArtifact)
    for index, action in enumerate(actions):
        tool_id = _action_tool_id(action)
        if tool_id is None:
            continue
        provider_record_id = event_id
        key = _idempotency_key("tool", event_id, index)
        if not session.claim_event(key):
            continue
        dimensions = {"framework_task_id": task_id} if task_id is not None else None
        try:
            session.tracked_task.record_tool_call(
                tool_id,
                status="failed" if failed else "succeeded",
                # Griptape exposes the subtask boundary, not an exact duration
                # per parallel action. Reporting zero avoids inflated duration.
                duration_ms=0,
                provider="griptape",
                provider_record_id=provider_record_id,
                error_type="ErrorArtifact" if failed else None,
                dimensions=dimensions,
                idempotency_key=key,
                capability=_tool_capability(tool_id),
            )
        except Exception:
            _log.debug("dexcost: Griptape tool capture failed", exc_info=True)
    return event


@functools.lru_cache(maxsize=1)
def _event_types() -> tuple[type[Any], type[Any], type[Any]]:
    try:
        from griptape.events import (
            FinishActionsSubtaskEvent,
            FinishPromptEvent,
            FinishStructureRunEvent,
        )
    except ImportError as exc:
        raise ImportError(
            "DexCost's Griptape integration requires griptape>=1.0. "
            "Install it with `pip install 'dexcost[griptape]'`."
        ) from exc
    return FinishActionsSubtaskEvent, FinishPromptEvent, FinishStructureRunEvent


def _listener() -> Any:
    from griptape.events import EventListener

    finish_actions, finish_prompt, finish_structure = _event_types()
    return EventListener(
        on_event=_handle_griptape_event,
        event_types=[finish_actions, finish_prompt, finish_structure],
    )


def track_griptape(
    structure: Any,
    tracker: CostTracker | None = None,
    *,
    task_type: str | None = None,
    capability: CapabilityIdentity | None = None,
    capture_llm_events: bool = False,
    capture_tool_events: bool = True,
    additional_methods: Sequence[str] = (),
) -> Any:
    """Wrap a Griptape Structure without replacing its provider drivers.

    ``run`` and ``run_stream`` retain their normal behavior and return values.
    Griptape's context-local EventListener captures privacy-safe action identity.
    Provider SDK instrumentation remains authoritative for LLM costs unless the
    explicit fallback ``capture_llm_events`` option is enabled.
    """
    _event_types()
    methods = GRIPTAPE_EXECUTION_METHODS | frozenset(additional_methods)
    if not any(callable(getattr(structure, name, None)) for name in methods):
        raise TypeError("structure must expose Griptape's run() or run_stream() API")
    if capture_llm_events:
        warnings.warn(
            "capture_llm_events=True is a fallback path; do not combine it with "
            "DexCost provider instrumentation for the same LLM calls.",
            RuntimeWarning,
            stacklevel=2,
        )
    tracker = resolve_tracker(tracker)
    return FrameworkExecutionProxy(
        structure,
        tracker=tracker,
        framework="griptape",
        methods=tuple(methods),
        force_stream_methods=tuple(_GRIPTAPE_FORCE_STREAM_METHODS),
        task_type=task_type,
        capability=capability,
        capture_llm_events=capture_llm_events,
        capture_tool_events=capture_tool_events,
        listener_factory=_listener,
    )


__all__ = ["GRIPTAPE_EXECUTION_METHODS", "track_griptape"]
