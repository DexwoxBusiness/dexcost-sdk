"""Trace linking helpers for external observability platforms.

Implements US-033: link external traces (Langfuse, LangSmith, etc.) to
the current active dexcost task via metadata.

Usage::

    from dexcost.integrations.traces import link_trace

    # Inside an active task context:
    link_trace("langfuse", "trace-abc-123")
    link_trace("langsmith", "run-def-456")
"""

from __future__ import annotations

from typing import cast

from dexcost.context import get_current_task


def link_trace(provider: str, trace_id: str) -> None:
    """Link an external trace to the current active task.

    Stores the trace reference in the task's ``metadata["_trace_links"]``
    list so that it is preserved across serialisation.

    Args:
        provider: Name of the observability platform (e.g. ``"langfuse"``,
            ``"langsmith"``).
        trace_id: The trace or run identifier from the external platform.

    Raises:
        RuntimeError: If there is no active task context.
    """
    task = get_current_task()
    if task is None:
        raise RuntimeError("No active task context — cannot link trace")
    links = task.metadata.setdefault("_trace_links", [])
    if not isinstance(links, list):
        links = []
        task.metadata["_trace_links"] = links
    links.append({"provider": provider, "trace_id": trace_id})


def get_trace_links() -> list[dict[str, str]]:
    """Return all linked traces for the current active task.

    Returns:
        A list of dicts with ``"provider"`` and ``"trace_id"`` keys.
        Returns an empty list if there is no active task or no traces.
    """
    task = get_current_task()
    if task is None:
        return []
    return cast("list[dict[str, str]]", task.metadata.get("_trace_links", []))
