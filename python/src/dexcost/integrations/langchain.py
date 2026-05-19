"""LangChain-compatible callback handler for dexcost cost tracking.

Implements US-032: a callback handler that captures LLM calls made through
LangChain and records them as dexcost events.  Uses duck typing so that
the ``langchain`` package is **not** required at import time.

Usage::

    from dexcost.integrations import DexcostCallbackHandler
    from dexcost.tracker import CostTracker

    tracker = CostTracker()
    handler = DexcostCallbackHandler(tracker)

    # Pass to LangChain as a callback:
    llm = ChatOpenAI(callbacks=[handler])
"""

from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import Any

from dexcost.context import get_current_task
from dexcost.models.event import Event
from dexcost.pricing import PricingEngine
from dexcost.storage.protocol import StorageBackend

_log = logging.getLogger(__name__)


class DexcostCallbackHandler:
    """LangChain-compatible callback handler for cost tracking.

    This handler implements the same method signatures that LangChain's
    ``BaseCallbackHandler`` uses, but does **not** inherit from it.  This
    allows the handler to work when LangChain is installed (duck typing)
    without requiring LangChain as a dependency.

    Args:
        tracker: A :class:`~dexcost.tracker.CostTracker` instance.  The
            handler uses its storage backend and pricing engine.
    """

    def __init__(self, tracker: Any) -> None:
        self._storage: StorageBackend = tracker._storage
        self._pricing: PricingEngine = tracker._pricing
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # LangChain callback interface
    # ------------------------------------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM starts generating.

        Records the start time and extracts the model name from the
        serialized kwargs so they are available when the call completes.
        """
        key = str(run_id) if run_id is not None else str(uuid.uuid4())
        model = _extract_model(serialized, kwargs)
        self._pending[key] = {
            "start_time": time.perf_counter(),
            "model": model,
        }

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM finishes generating.

        Extracts token usage from the response, computes cost via the
        pricing engine, creates an :class:`Event`, and inserts it into
        storage if a task context is active.
        """
        task = get_current_task()
        if task is None:
            _log.warning(
                "DexcostCallbackHandler.on_llm_end called outside a task "
                "context — skipping event recording."
            )
            self._cleanup_pending(run_id)
            return

        key = str(run_id) if run_id is not None else None
        pending = self._pending.pop(key, {}) if key else {}
        model = pending.get("model", "unknown")
        start_time = pending.get("start_time")
        latency_ms: int | None = None
        if start_time is not None:
            latency_ms = int((time.perf_counter() - start_time) * 1000)

        # Extract token usage from the LLMResult
        input_tokens, output_tokens = _extract_tokens(response)
        has_usage = input_tokens > 0 or output_tokens > 0

        if has_usage:
            try:
                cost_result = self._pricing.get_cost(model, input_tokens, output_tokens)
            except Exception:
                cost_result = None
            if cost_result is not None:
                cost_usd = cost_result.cost_usd
                cost_confidence = "computed"
                pricing_source = cost_result.pricing_source
                pricing_version = cost_result.pricing_version
            else:
                cost_usd = Decimal("0")
                cost_confidence = "unknown"
                pricing_source = "unknown"
                pricing_version = None
        else:
            cost_usd = Decimal("0")
            cost_confidence = "unknown"
            pricing_source = "unknown"
            pricing_version = None

        event = Event(
            task_id=task.task_id,
            event_type="llm_call",
            cost_usd=cost_usd,
            cost_confidence=cost_confidence,
            pricing_source=pricing_source,
            pricing_version=pricing_version,
            provider="langchain",
            model=model,
            input_tokens=input_tokens if has_usage else None,
            output_tokens=output_tokens if has_usage else None,
            latency_ms=latency_ms,
        )
        self._storage.insert_event(event)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: uuid.UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called when an LLM call errors.

        Records a failure event if a task context is active.  Never raises.
        """
        task = get_current_task()
        if task is None:
            _log.warning(
                "DexcostCallbackHandler.on_llm_error called outside a task "
                "context — skipping event recording."
            )
            self._cleanup_pending(run_id)
            return

        key = str(run_id) if run_id is not None else None
        pending = self._pending.pop(key, {}) if key else {}
        model = pending.get("model", "unknown")
        start_time = pending.get("start_time")
        latency_ms: int | None = None
        if start_time is not None:
            latency_ms = int((time.perf_counter() - start_time) * 1000)

        error_type = type(error).__name__
        event = Event(
            task_id=task.task_id,
            event_type="llm_call",
            cost_usd=Decimal("0"),
            cost_confidence="unknown",
            pricing_source="unknown",
            provider="langchain",
            model=model,
            latency_ms=latency_ms,
            details={"error": str(error), "error_type": error_type},
        )
        self._storage.insert_event(event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup_pending(self, run_id: uuid.UUID | None) -> None:
        """Remove pending state for a run without recording an event."""
        key = str(run_id) if run_id is not None else None
        if key:
            self._pending.pop(key, None)


def _extract_model(serialized: dict[str, Any], kwargs: Any) -> str:
    """Extract the model name from LangChain's serialized dict.

    LangChain passes the LLM configuration in ``serialized["kwargs"]``.
    The model name can appear under ``"model"``, ``"model_name"``, or
    ``"model_id"``.
    """
    kw = serialized.get("kwargs", {})
    for key in ("model", "model_name", "model_id"):
        val = kw.get(key)
        if val:
            return str(val)
    # Fallback: check top-level kwargs
    for key in ("model", "model_name", "model_id"):
        val = kwargs.get(key) if isinstance(kwargs, dict) else None
        if val:
            return str(val)
    return "unknown"


def _extract_tokens(response: Any) -> tuple[int, int]:
    """Extract token counts from a LangChain LLMResult.

    LangChain stores usage in ``response.llm_output["token_usage"]`` with
    keys ``prompt_tokens`` and ``completion_tokens``.
    """
    llm_output = getattr(response, "llm_output", None)
    if llm_output is None:
        # Also try dict access for mock objects
        if isinstance(response, dict):
            llm_output = response.get("llm_output")
        if llm_output is None:
            return 0, 0

    if isinstance(llm_output, dict):
        token_usage = llm_output.get("token_usage", {})
    else:
        token_usage = getattr(llm_output, "token_usage", {}) or {}

    if not isinstance(token_usage, dict):
        return 0, 0

    try:
        input_tokens = int(token_usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(token_usage.get("completion_tokens", 0) or 0)
    except (ValueError, TypeError):
        input_tokens = 0
        output_tokens = 0
    return input_tokens, output_tokens
