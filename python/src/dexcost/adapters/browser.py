"""Browser cost adapter — automatic cost tracking for Playwright sessions.

Provides an async context manager ``track_browser`` that wraps a Playwright
``Page`` object, measures wall-clock time, and records a ``compute_cost``
event proportional to session duration.

Usage::

    from dexcost.adapters.browser import track_browser

    async with task_context(my_task):
        async with track_browser(page, rate_per_minute=Decimal("0.01")):
            await page.goto("https://example.com")
            data = await page.inner_text("#result")

Implements US-043.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from dexcost.context import get_current_task
from dexcost.models.event import Event
from dexcost.redaction import scrub_url

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level event storage (same pattern as http.py adapter)
# ---------------------------------------------------------------------------

_recorded_events: list[Event] = []

# Storage backend wired by set_storage(). When set, recorded browser cost
# events are persisted durably (and shipped by the SyncWorker) instead of only
# being appended to the in-memory _recorded_events list.
_storage: Any = None


def get_recorded_events() -> list[Event]:
    """Return all events recorded by the browser adapter since last clear."""
    return list(_recorded_events)


def clear_recorded_events() -> None:
    """Clear the recorded events list."""
    _recorded_events.clear()


def set_storage(storage: Any) -> None:
    """Wire the browser adapter to a storage backend.

    Once set, every cost event recorded by :func:`track_browser` is persisted
    via ``storage.insert_event`` so the :class:`SyncWorker` ships browser costs
    to the Control Layer. ``dexcost.init()`` calls this automatically. Pass
    ``None`` to detach (events then stay in-memory only).
    """
    global _storage
    _storage = storage


def _persist_event(event: Event) -> None:
    """Record a captured browser cost event.

    Always appended to the in-memory ``_recorded_events`` list (used by tests
    and lightweight setups) and, when a storage backend is wired via
    :func:`set_storage`, also persisted durably so the SyncWorker ships it.
    """
    _recorded_events.append(event)
    if _storage is not None:
        try:
            _storage.insert_event(event)
        except Exception:
            _log.warning("Failed to persist browser cost event to storage", exc_info=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DEFAULT_RATE_PER_MINUTE = Decimal("0.01")


@asynccontextmanager
async def track_browser(
    page: Any,
    rate_per_minute: Decimal | str = _DEFAULT_RATE_PER_MINUTE,
) -> AsyncGenerator[Any, None]:
    """Async context manager that tracks browser session cost.

    Measures wall-clock time between entry and exit, then records a
    ``compute_cost`` event with ``cost_usd = elapsed_minutes * rate_per_minute``.

    The event is only recorded when there is an active task in the context
    (via :func:`~dexcost.context.get_current_task`).  If no task is active,
    the context manager is a silent pass-through.

    Args:
        page: A Playwright ``Page`` object (or any object with a ``.url``
            attribute).  Not type-checked to avoid requiring Playwright as
            a dependency.
        rate_per_minute: Cost per minute of browser usage in USD.
            Defaults to ``0.01``.  Accepts ``Decimal`` or ``str``.
    """
    rate = Decimal(str(rate_per_minute))
    start = time.monotonic()
    try:
        yield page
    finally:
        elapsed_seconds = time.monotonic() - start
        _record_browser_event(page, elapsed_seconds, rate)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _record_browser_event(
    page: Any,
    elapsed_seconds: float,
    rate_per_minute: Decimal,
) -> None:
    """Record a compute_cost event for the browser session.

    No-op if there is no active task in the context.
    """
    task = get_current_task()
    if task is None:
        return

    elapsed_minutes = Decimal(str(elapsed_seconds)) / Decimal("60")
    cost_usd = elapsed_minutes * rate_per_minute

    page_url: str = ""
    try:
        page_url = scrub_url(str(getattr(page, "url", "")))
    except Exception:
        pass

    event = Event(
        task_id=task.task_id,
        event_type="compute_cost",
        cost_usd=cost_usd,
        cost_confidence="computed",
        pricing_source="rate_per_minute",
        service_name="playwright_browser",
        details={
            "wall_clock_seconds": str(round(elapsed_seconds, 6)),
            "rate_per_minute": str(rate_per_minute),
            "page_url": page_url,
        },
    )

    _persist_event(event)
