"""Tests for the browser cost adapter (US-043).

Uses mocked Playwright — playwright is NOT required to run these tests.
Verifies that track_browser() context manager measures wall-clock time
and records a compute_cost event.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dexcost.adapters.browser import (
    clear_recorded_events,
    get_recorded_events,
    track_browser,
)
from dexcost.context import async_task_context
from dexcost.models.task import Task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Reset adapter state before and after each test."""
    clear_recorded_events()
    yield
    clear_recorded_events()


def _make_task(task_type: str = "browser_scrape") -> Task:
    return Task(task_type=task_type, customer_id="cust-1")


def _make_mock_page() -> MagicMock:
    """Create a mock Playwright Page object."""
    page = MagicMock()
    page.url = "https://example.com"
    page.goto = AsyncMock(return_value=None)
    page.title = AsyncMock(return_value="Example")
    return page


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTrackBrowser:
    """Tests for the track_browser async context manager."""

    def test_records_compute_cost_event(self) -> None:
        """track_browser records a compute_cost event on context exit."""
        page = _make_mock_page()
        task = _make_task()

        async def _run() -> None:
            async with async_task_context(task):
                async with track_browser(page, rate_per_minute=Decimal("0.01")):
                    await asyncio.sleep(0.05)  # 50ms simulated work

        asyncio.run(_run())

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].event_type == "compute_cost"
        assert events[0].service_name == "playwright_browser"
        assert events[0].cost_usd > Decimal("0")
        assert events[0].task_id == task.task_id

    def test_cost_proportional_to_time(self) -> None:
        """Longer browser sessions produce higher costs."""
        page = _make_mock_page()
        task = _make_task()

        async def _run(sleep_time: float) -> Decimal:
            clear_recorded_events()
            async with async_task_context(task):
                async with track_browser(page, rate_per_minute=Decimal("0.60")):
                    await asyncio.sleep(sleep_time)
            events = get_recorded_events()
            return events[0].cost_usd

        cost_short = asyncio.run(_run(0.05))
        cost_long = asyncio.run(_run(0.15))

        assert cost_long > cost_short

    def test_cost_uses_decimal_not_float(self) -> None:
        """cost_usd must be a Decimal."""
        page = _make_mock_page()
        task = _make_task()

        async def _run() -> None:
            async with async_task_context(task):
                async with track_browser(page, rate_per_minute=Decimal("0.01")):
                    pass

        asyncio.run(_run())

        events = get_recorded_events()
        assert isinstance(events[0].cost_usd, Decimal)

    def test_no_task_context_no_event(self) -> None:
        """Outside a task context, no event is recorded."""
        page = _make_mock_page()

        async def _run() -> None:
            async with track_browser(page, rate_per_minute=Decimal("0.01")):
                pass

        asyncio.run(_run())

        events = get_recorded_events()
        assert len(events) == 0

    def test_default_rate_per_minute(self) -> None:
        """Default rate_per_minute is 0.01."""
        page = _make_mock_page()
        task = _make_task()

        async def _run() -> None:
            async with async_task_context(task):
                async with track_browser(page):
                    await asyncio.sleep(0.05)

        asyncio.run(_run())

        events = get_recorded_events()
        assert len(events) == 1
        # With default rate of 0.01/min, 50ms ~= 0.000008333
        assert events[0].cost_usd > Decimal("0")

    def test_event_details_contain_timing(self) -> None:
        """Event details include wall_clock_seconds and rate_per_minute."""
        page = _make_mock_page()
        task = _make_task()

        async def _run() -> None:
            async with async_task_context(task):
                async with track_browser(page, rate_per_minute=Decimal("0.12")):
                    await asyncio.sleep(0.05)

        asyncio.run(_run())

        events = get_recorded_events()
        details = events[0].details
        assert "wall_clock_seconds" in details
        assert "rate_per_minute" in details
        assert details["rate_per_minute"] == "0.12"
        assert float(details["wall_clock_seconds"]) >= 0.04  # some tolerance

    def test_event_details_contain_page_url(self) -> None:
        """Event details include the page URL at time of recording."""
        page = _make_mock_page()
        page.url = "https://app.example.com/dashboard"
        task = _make_task()

        async def _run() -> None:
            async with async_task_context(task):
                async with track_browser(page, rate_per_minute=Decimal("0.01")):
                    pass

        asyncio.run(_run())

        events = get_recorded_events()
        assert events[0].details["page_url"] == "https://app.example.com/dashboard"

    def test_exception_still_records_event(self) -> None:
        """If an exception occurs inside the context, the event is still recorded."""
        page = _make_mock_page()
        task = _make_task()

        async def _run() -> None:
            async with async_task_context(task):
                try:
                    async with track_browser(page, rate_per_minute=Decimal("0.01")):
                        raise RuntimeError("browser crashed")
                except RuntimeError:
                    pass

        asyncio.run(_run())

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].event_type == "compute_cost"

    def test_cost_confidence_is_computed(self) -> None:
        """cost_confidence should be 'computed' since we estimate from wall-clock."""
        page = _make_mock_page()
        task = _make_task()

        async def _run() -> None:
            async with async_task_context(task):
                async with track_browser(page):
                    pass

        asyncio.run(_run())

        events = get_recorded_events()
        assert events[0].cost_confidence == "computed"
        assert events[0].pricing_source == "manual"


class TestStoragePersistence:
    """Browser-captured events must reach durable storage (and thus the
    SyncWorker), not only the in-memory _recorded_events list."""

    def test_recorded_event_persists_to_storage(self, tmp_path: Any) -> None:
        from dexcost.adapters.browser import set_storage
        from dexcost.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
        task = _make_task()
        storage.insert_task(task)  # parent task row must exist first
        set_storage(storage)

        async def _run() -> None:
            async with async_task_context(task):
                page = _make_mock_page()
                async with track_browser(page, rate_per_minute=Decimal("0.60")):
                    await asyncio.sleep(0.02)

        try:
            asyncio.run(_run())
        finally:
            set_storage(None)

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1, "browser event was not persisted to storage"
        assert events[0].event_type == "compute_cost"
        assert events[0].service_name == "playwright_browser"
        # Still available via the in-memory list for lightweight setups.
        assert len(get_recorded_events()) == 1
        storage.close()

    def test_no_storage_keeps_in_memory_only(self, tmp_path: Any) -> None:
        from dexcost.adapters.browser import set_storage
        from dexcost.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
        task = _make_task()
        set_storage(None)  # explicitly detached

        async def _run() -> None:
            async with async_task_context(task):
                page = _make_mock_page()
                async with track_browser(page, rate_per_minute=Decimal("0.60")):
                    await asyncio.sleep(0.02)

        asyncio.run(_run())

        assert storage.query_events(task_id=str(task.task_id)) == []
        assert len(get_recorded_events()) == 1
        storage.close()
