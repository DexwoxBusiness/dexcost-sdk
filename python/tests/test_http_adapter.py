"""Tests for the HTTP cost adapter (US-035).

Uses mocks for ``requests`` and ``httpx`` — neither library is required.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dexcost.adapters.http import (
    _domain_rates,
    clear_domain_rates,
    clear_recorded_events,
    get_recorded_events,
    register_domain_rate,
    track_http,
    untrack_http,
)
from dexcost.adapters.http import set_catalog
from dexcost.context import clear_context, set_current_task, task_context
from dexcost.models.task import Task
from dexcost.session import reset_session_manager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Reset adapter state before and after each test."""
    # Clean before
    untrack_http()
    clear_domain_rates()
    clear_recorded_events()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    set_catalog(None)
    yield
    # Clean after
    untrack_http()
    clear_domain_rates()
    clear_recorded_events()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    set_catalog(None)


def _make_task(task_type: str = "web_query") -> Task:
    return Task(task_type=task_type, customer_id="cust-1")


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_fake_requests_module() -> MagicMock:
    """Create a fake ``requests`` module with Session.send."""
    mod = MagicMock()
    mod.Session = MagicMock()
    original_send = MagicMock(return_value=MagicMock(status_code=200))
    mod.Session.send = original_send
    return mod


def _make_fake_httpx_module() -> MagicMock:
    """Create a fake ``httpx`` module with Client.send."""
    mod = MagicMock()
    mod.Client = MagicMock()
    original_send = MagicMock(return_value=MagicMock(status_code=200))
    mod.Client.send = original_send
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTrackHttp:
    """Tests for track_http / untrack_http."""

    def test_track_http_patches_requests(self) -> None:
        """track_http wraps requests.Session.send when requests is available."""
        fake_requests = _make_fake_requests_module()
        original_send = fake_requests.Session.send

        with patch.dict("sys.modules", {"requests": fake_requests}):
            patched = track_http()

        assert "requests" in patched
        # After patching, the send method should differ from the original
        assert fake_requests.Session.send is not original_send

    def test_track_http_patches_httpx(self) -> None:
        """track_http wraps httpx.Client.send when httpx is available."""
        fake_httpx = _make_fake_httpx_module()
        original_send = fake_httpx.Client.send

        with patch.dict("sys.modules", {"httpx": fake_httpx}):
            patched = track_http()

        assert "httpx" in patched
        assert fake_httpx.Client.send is not original_send

    def test_matched_domain_records_event(self) -> None:
        """When a registered domain is called within a task, an event is recorded."""
        register_domain_rate("api.example.com", cost_usd="0.01")

        task = _make_task()
        with task_context(task):
            # Simulate calling _maybe_record_cost directly
            from dexcost.adapters.http import _maybe_record_cost

            _maybe_record_cost("https://api.example.com/v1/query")

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].event_type == "external_cost"
        assert events[0].cost_usd == Decimal("0.01")
        assert events[0].service_name == "api.example.com"

    def test_unmatched_domain_records_unknown(self) -> None:
        """Un-cataloged domains with a small/no-body response emit no event.

        The old behaviour was to record an ``external_cost $0`` with
        ``cost_confidence='unknown'`` for every un-cataloged call.  The new
        behaviour (noise-removal) records bytes into the task counters but only
        emits a ``network`` event when the call is notable (combined bytes >
        threshold, HTTP error, or slow).  A no-body request to an unknown
        domain is below all thresholds → no event.
        """
        register_domain_rate("api.example.com", cost_usd="0.01")

        task = _make_task()
        with task_context(task):
            from dexcost.adapters.http import _handle_http_call

            _handle_http_call("https://unregistered.example.com/v1/query",
                              method="GET", request_headers={}, request_body_len=0,
                              response=None, latency_ms=0)

        # No event — call was small and successful (below threshold).
        events = get_recorded_events()
        assert len(events) == 0

    def test_no_task_context_auto_creates_session(self) -> None:
        """Outside a task context, a session is auto-created and event is recorded."""
        register_domain_rate("api.example.com", cost_usd="0.01")

        # No task context active
        set_current_task(None)

        from dexcost.adapters.http import _maybe_record_cost

        _maybe_record_cost("https://api.example.com/v1/query")

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.01")

    def test_cost_from_registered_rate(self) -> None:
        """Event cost_usd matches the registered rate exactly."""
        register_domain_rate("maps.googleapis.com", cost_usd="0.005", per="request")

        task = _make_task()
        with task_context(task):
            from dexcost.adapters.http import _maybe_record_cost

            _maybe_record_cost("https://maps.googleapis.com/maps/api/geocode")

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.005")

    def test_untrack_restores_original(self) -> None:
        """untrack_http() restores the original send methods."""
        fake_requests = _make_fake_requests_module()
        original_send = fake_requests.Session.send

        with patch.dict("sys.modules", {"requests": fake_requests}):
            track_http()
            assert fake_requests.Session.send is not original_send
            untrack_http()
            assert fake_requests.Session.send is original_send

    def test_no_libraries_noop(self) -> None:
        """track_http() returns [] when no HTTP libraries are installed."""
        import builtins

        _real_import = builtins.__import__

        _blocked = {"requests", "httpx", "aiohttp", "botocore", "botocore.httpsession", "urllib3"}

        def _fail_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name in _blocked:
                raise ImportError(f"No module named '{name}'")
            return _real_import(name, *args, **kwargs)

        try:
            builtins.__import__ = _fail_import  # type: ignore[assignment]
            # Must also remove from sys.modules if cached
            saved: dict[str, Any] = {}
            for key in _blocked:
                if key in sys.modules:
                    saved[key] = sys.modules.pop(key)

            patched = track_http()
            assert patched == []
        finally:
            builtins.__import__ = _real_import  # type: ignore[assignment]
            sys.modules.update(saved)

    def test_register_domain_rate(self) -> None:
        """register_domain_rate stores the rate and it can be looked up."""
        register_domain_rate("api.stripe.com", cost_usd="0.02", per="request")

        rates = _domain_rates
        assert "api.stripe.com" in rates
        assert rates["api.stripe.com"]["cost_usd"] == Decimal("0.02")
        assert rates["api.stripe.com"]["per"] == "request"

    def test_event_has_correct_fields(self) -> None:
        """Recorded event has event_type='external_cost' and service_name=domain."""
        register_domain_rate("ocr.example.com", cost_usd="0.10", per="page")

        task = _make_task()
        with task_context(task):
            from dexcost.adapters.http import _maybe_record_cost

            _maybe_record_cost("https://ocr.example.com/process")

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "external_cost"
        assert event.service_name == "ocr.example.com"
        assert event.task_id == task.task_id
        assert event.cost_confidence == "exact"
        assert event.pricing_source == "rate_registry"
        assert event.details["url"] == "https://ocr.example.com/process"
        assert event.details["per"] == "page"


class TestStoragePersistence:
    """HTTP-captured events must reach durable storage (and thus the SyncWorker),
    not only the in-memory _recorded_events list."""

    def test_recorded_event_persists_to_storage(self, tmp_path: Any) -> None:
        from dexcost.adapters.http import _maybe_record_cost, set_storage
        from dexcost.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
        task = _make_task()
        storage.insert_task(task)  # parent task row must exist first
        set_current_task(task)
        register_domain_rate("api.example.com", cost_usd="0.01", per="request")
        set_storage(storage)
        try:
            _maybe_record_cost("https://api.example.com/v1/thing")
        finally:
            set_storage(None)

        events = storage.query_events(task_id=str(task.task_id))
        assert len(events) == 1, "event was not persisted to storage"
        assert events[0].event_type == "external_cost"
        assert events[0].cost_usd == Decimal("0.01")
        assert events[0].pricing_source == "rate_registry"
        # Still available via the in-memory list for lightweight setups.
        assert len(get_recorded_events()) == 1
        storage.close()

    def test_no_storage_keeps_in_memory_only(self, tmp_path: Any) -> None:
        from dexcost.adapters.http import _maybe_record_cost, set_storage
        from dexcost.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path=str(tmp_path / "buffer.db"))
        task = _make_task()
        set_current_task(task)
        register_domain_rate("api.example.com", cost_usd="0.01", per="request")
        set_storage(None)  # explicitly detached
        _maybe_record_cost("https://api.example.com/v1/thing")

        assert storage.query_events(task_id=str(task.task_id)) == []
        assert len(get_recorded_events()) == 1
        storage.close()
