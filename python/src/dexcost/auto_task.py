"""Auto-task creation for auto-instrumented calls without an explicit task."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dexcost.context import get_current_task, get_context
from dexcost.models.event import Event
from dexcost.models.task import Task

# Re-export set_context/get_context for backward compat with tests
from dexcost.context import set_context, clear_context, DexcostContext  # noqa: F401

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def needs_auto_task() -> bool:
    """Return True if there is no active explicit task."""
    return get_current_task() is None


def create_auto_task(task_type: str) -> Task:
    """Create a task with attribution from the current DexcostContext.

    Reads ``customer_id``, ``project_id``, ``metadata``, and ``agent``
    from :func:`set_context`.  When ``agent`` is set in the context it
    overrides the provided *task_type*.
    """
    ctx = get_context()
    effective_task_type = task_type
    if ctx and ctx.agent:
        effective_task_type = ctx.agent
    return Task(
        task_id=uuid.uuid4(),
        task_type=effective_task_type,
        status="pending",
        started_at=datetime.now(timezone.utc),
        customer_id=ctx.customer_id if ctx else None,
        project_id=ctx.project_id if ctx else None,
        metadata=dict(ctx.metadata) if ctx and ctx.metadata else {},
    )


def finalize_auto_task(task: Task, event: Event, status: str = "success") -> None:
    """Finalize an auto-task: aggregate the event's cost and set end time."""
    task.status = status
    task.ended_at = datetime.now(timezone.utc)

    cost = event.cost_usd
    if event.event_type == "llm_call":
        task.llm_cost_usd = cost
        task.total_input_tokens = event.input_tokens or 0
        task.total_output_tokens = event.output_tokens or 0
        task.total_cached_tokens = event.cached_tokens or 0
    elif event.event_type == "external_cost":
        task.external_cost_usd = cost
    elif event.event_type == "compute_cost":
        task.compute_cost_usd = cost
    elif event.event_type == "retry_marker":
        task.retry_count = 1
        task.retry_cost_usd = cost

    task.total_cost_usd = cost

    if event.is_retry:
        task.retry_count = (task.retry_count or 0) + 1
        task.retry_cost_usd = (task.retry_cost_usd or Decimal(0)) + cost

    if status == "failed":
        task.failure_count = 1
