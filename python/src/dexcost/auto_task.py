"""Auto-task creation for auto-instrumented calls without an explicit task."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

# Re-export set_context/get_context for backward compat with tests
from dexcost.context import (
    DexcostContext as DexcostContext,
)
from dexcost.context import (
    clear_context as clear_context,
)
from dexcost.context import (
    get_context,
    get_current_task,
)
from dexcost.context import (
    set_context as set_context,
)
from dexcost.models.event import Event
from dexcost.models.task import Task

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def needs_auto_task() -> bool:
    """Return True if there is no active explicit task."""
    return get_current_task() is None


def create_auto_task(task_type: str) -> Task:
    """Create a task with attribution from the current DexcostContext.

    Agent and workflow identity remain separate from *task_type*. When the
    context contains business identity, the automatic task becomes its own
    canonical root so the identity snapshot is deliverable.
    """
    ctx = get_context()
    task_id = uuid.uuid4()
    has_business_identity = ctx is not None and any(
        (
            ctx.customer_id,
            ctx.project_id,
            ctx.user_id,
            ctx.product_id,
            ctx.agent,
            ctx.workflow_id,
        )
    )
    return Task(
        task_id=task_id,
        task_type=task_type,
        status="pending",
        started_at=datetime.now(timezone.utc),
        customer_id=ctx.customer_id if ctx else None,
        project_id=ctx.project_id if ctx else None,
        user_id=ctx.user_id if ctx else None,
        product_id=ctx.product_id if ctx else None,
        root_task_id=task_id if has_business_identity else None,
        agent_id=ctx.agent if ctx else None,
        agent_version=ctx.agent_version if ctx else None,
        workflow_id=ctx.workflow_id if ctx else None,
        workflow_session_id=ctx.workflow_session_id if ctx else None,
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
