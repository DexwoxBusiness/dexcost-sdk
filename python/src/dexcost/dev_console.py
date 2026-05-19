"""Development mode console output for dexcost.

When DEXCOST_ENV=development or environment="development" is passed to init(),
every recorded event is printed to stderr with a formatted summary.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dexcost.models.event import Event
    from dexcost.models.task import Task

# Module-level flag
_dev_mode: bool = False


def is_dev_mode() -> bool:
    return _dev_mode


def enable_dev_mode() -> None:
    global _dev_mode
    _dev_mode = True
    _print("\033[36m[dexcost]\033[0m dev mode — cloud sync disabled")


def log_event(event: "Event", task_type: str = "") -> None:
    """Print a single event to stderr."""
    if not _dev_mode:
        return

    cost = event.cost_usd
    confidence = event.cost_confidence

    if event.event_type == "llm_call":
        provider = event.provider or "?"
        model = event.model or "?"
        in_tok = event.input_tokens or 0
        out_tok = event.output_tokens or 0
        cached = event.cached_tokens or 0
        retry_tag = "  \033[33m(retry)\033[0m" if event.is_retry else ""
        cache_tag = f"  cached: {cached:,}" if cached > 0 else ""
        _print(
            f"\033[32m✓\033[0m llm_call  {provider}/{model}  "
            f"{in_tok:,} in / {out_tok:,} out{cache_tag}  "
            f"${cost}{retry_tag}"
            f"{_task_tag(task_type)}"
        )
    elif event.event_type in ("external_cost", "compute_cost"):
        service = event.service_name or "unknown"
        if confidence == "unknown" or cost == Decimal("0"):
            _print(
                f"\033[33m⚠\033[0m {event.event_type}  {service}  "
                f"$0.00 \033[33m(no rate configured)\033[0m"
                f"{_task_tag(task_type)}"
            )
        else:
            _print(
                f"\033[32m✓\033[0m {event.event_type}  {service}  "
                f"${cost}{_task_tag(task_type)}"
            )
    elif event.event_type == "retry_marker":
        reason = event.retry_reason or "unknown"
        _print(
            f"\033[33m↻\033[0m retry_marker  reason: {reason}  "
            f"${cost}{_task_tag(task_type)}"
        )


def log_task_complete(task: "Task") -> None:
    """Print task completion summary to stderr."""
    if not _dev_mode:
        return

    retry_info = ""
    if task.retry_count > 0:
        retry_info = (
            f"  retries: {task.retry_count}  "
            f"retry cost: ${task.retry_cost_usd}"
        )

    _print(
        f"\033[36m✓\033[0m task {task.status}  {task.task_type}  "
        f"total: ${task.total_cost_usd}{retry_info}"
    )


def _task_tag(task_type: str) -> str:
    if task_type:
        return f"  \033[90m(task: {task_type})\033[0m"
    return ""


def _print(msg: str) -> None:
    print(f"\033[36m[dexcost]\033[0m {msg}", file=sys.stderr)
