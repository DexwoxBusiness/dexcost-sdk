"""Tests for the HTTP cost adapter v2 with service catalog integration.

Tests service catalog cost extraction, session auto-grouping, and
the rewritten HTTP adapter behaviour.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from dexcost.adapters.http import (
    _maybe_record_cost,
    clear_domain_rates,
    clear_recorded_events,
    get_recorded_events,
    register_domain_rate,
    set_catalog,
    untrack_http,
)
from dexcost.context import clear_context, set_context, set_current_task, task_context
from dexcost.models.task import Task
from dexcost.session import reset_session_manager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Reset adapter state before and after each test."""
    untrack_http()
    clear_domain_rates()
    clear_recorded_events()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    set_catalog(None)  # Reset to force fresh catalog load
    yield
    untrack_http()
    clear_domain_rates()
    clear_recorded_events()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    set_catalog(None)


def _make_task(task_type: str = "web_query") -> Task:
    return Task(task_type=task_type, customer_id="cust-1")


def _make_response(
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    content_type: str = "application/json",
    content_length: int | None = None,
) -> MagicMock:
    """Create a mock HTTP response."""
    response = MagicMock()

    # Build headers dict
    h: dict[str, str] = {}
    if content_type:
        h["content-type"] = content_type
    if content_length is not None:
        h["content-length"] = str(content_length)
    if headers:
        h.update(headers)
    response.headers = h

    # Set up json() method
    if body is not None:
        response.json.return_value = body
    else:
        response.json.side_effect = ValueError("No JSON")

    return response


# ---------------------------------------------------------------------------
# Service catalog extraction tests
# ---------------------------------------------------------------------------


class TestKnownServiceExtraction:
    """HTTP calls to known services extract cost from response."""

    def test_tavily_cost_from_response_body(self) -> None:
        """Tavily: cost extracted from response_body.usage.credits."""
        task = _make_task()
        response = _make_response(body={"usage": {"credits": 2}, "results": []})

        with task_context(task):
            _maybe_record_cost("https://api.tavily.com/search", response)

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "external_cost"
        # 2 credits * $0.008 = $0.016
        assert event.cost_usd == Decimal("2") * Decimal("0.008")
        assert event.cost_confidence == "computed"
        assert event.pricing_source == "service_catalog"
        assert event.service_name == "Tavily Search"

    def test_pinecone_cost_from_response_body(self) -> None:
        """Pinecone: cost extracted from response_body.usage.readUnits."""
        task = _make_task()
        response = _make_response(
            body={"usage": {"readUnits": 10}, "matches": []},
        )

        with task_context(task):
            _maybe_record_cost(
                "https://my-index.svc.us-east1-gcp.pinecone.io/query",
                response,
            )

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        # 10 * $0.000016 = $0.000160
        assert event.cost_usd == Decimal("10") * Decimal("0.000016")
        assert event.cost_confidence == "computed"
        assert event.service_name == "Pinecone"

    def test_google_maps_endpoint_match(self) -> None:
        """Google Maps Geocoding: fixed cost via endpoint_match."""
        task = _make_task()
        response = _make_response(body={"results": [], "status": "OK"})

        with task_context(task):
            _maybe_record_cost(
                "https://maps.googleapis.com/maps/api/geocode/json?address=foo",
                response,
            )

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.005")
        assert events[0].cost_confidence == "exact"


# ---------------------------------------------------------------------------
# Unknown domain tests
# ---------------------------------------------------------------------------


class TestUnknownDomain:
    """HTTP calls to unknown domains: noise-removal means no event for small calls."""

    def test_unknown_domain_small_response_emits_no_event(self) -> None:
        """Un-cataloged calls with a small body produce no event (noise removal).

        The old ``external_cost $0 / unknown`` event is replaced by nothing
        when the combined bytes are below the 100 KiB threshold and the
        response is successful.  Bytes are still recorded in task counters.
        """
        from dexcost.adapters.http import _handle_http_call

        task = _make_task()
        # response with no Content-Length → body_len=0 → well below threshold
        response = _make_response(body={"data": "hello"})

        with task_context(task):
            _handle_http_call("https://unknown-api.example.com/v1/data",
                              method="GET", request_headers={}, request_body_len=0,
                              response=response, latency_ms=5)

        # No event — small successful call to un-cataloged domain.
        events = get_recorded_events()
        assert len(events) == 0

    def test_unknown_domain_large_response_emits_network_event(self) -> None:
        """Un-cataloged call above the byte threshold emits a ``network`` event."""
        from dexcost.adapters.http import _handle_http_call

        task = _make_task()
        # Simulate a response with Content-Length above the 100 KiB threshold.
        response = _make_response(body={"data": "x"}, content_length=200_000)

        with task_context(task):
            _handle_http_call("https://unknown-api.example.com/v1/bulk",
                              method="GET", request_headers={}, request_body_len=0,
                              response=response, latency_ms=50)

        events = get_recorded_events()
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "network"
        assert event.cost_usd == Decimal("0")
        assert event.cost_confidence == "unknown"
        assert event.service_name == "unknown-api.example.com"


# ---------------------------------------------------------------------------
# Auto-session tests
# ---------------------------------------------------------------------------


class TestAutoSession:
    """HTTP calls without explicit task create auto-sessions."""

    def test_creates_session_when_no_task(self) -> None:
        """Without an explicit task, a session task is auto-created."""
        response = _make_response(body={"results": []})

        # No task context active
        _maybe_record_cost("https://api.tavily.com/search", response)

        events = get_recorded_events()
        assert len(events) == 1
        # Event should have a task_id (from the auto-created session)
        assert events[0].task_id is not None

    def test_session_groups_multiple_calls(self) -> None:
        """Multiple calls without explicit task share the same session task."""
        response1 = _make_response(body={"api_credits_used": 1})
        response2 = _make_response(body={"api_credits_used": 2})

        _maybe_record_cost("https://api.tavily.com/search", response1)
        _maybe_record_cost("https://api.tavily.com/search", response2)

        events = get_recorded_events()
        assert len(events) == 2
        # Both events should have the same task_id
        assert events[0].task_id == events[1].task_id


# ---------------------------------------------------------------------------
# User override tests
# ---------------------------------------------------------------------------


class TestDomainRateOverride:
    """register_domain_rate overrides catalog rate."""

    def test_override_takes_precedence(self) -> None:
        register_domain_rate("api.tavily.com", cost_usd="0.50")

        task = _make_task()
        response = _make_response(body={"api_credits_used": 3})

        with task_context(task):
            _maybe_record_cost("https://api.tavily.com/search", response)

        events = get_recorded_events()
        assert len(events) == 1
        # Should use the override rate, not the catalog
        assert events[0].cost_usd == Decimal("0.50")
        assert events[0].pricing_source == "rate_registry"


# ---------------------------------------------------------------------------
# Response body edge cases
# ---------------------------------------------------------------------------


class TestResponseBodyEdgeCases:
    """Edge cases for response body parsing."""

    def test_large_response_body_not_parsed(self) -> None:
        """Responses > 1MB are not parsed for cost extraction."""
        task = _make_task()
        response = _make_response(
            body={"api_credits_used": 5},
            content_length=2_000_000,  # 2MB
        )

        with task_context(task):
            _maybe_record_cost("https://api.tavily.com/search", response)

        events = get_recorded_events()
        assert len(events) == 1
        # Cost should use fallback (body wasn't parsed due to size)
        # Tavily has fallback_credits=1, so: 1 * $0.008 = $0.008
        assert events[0].cost_usd == Decimal("1") * Decimal("0.008")
        assert events[0].cost_confidence == "estimated"

    def test_non_json_response_body_skipped(self) -> None:
        """Non-JSON responses don't attempt body parsing."""
        task = _make_task()
        response = _make_response(
            content_type="text/html",
        )

        with task_context(task):
            _maybe_record_cost("https://api.tavily.com/search", response)

        events = get_recorded_events()
        assert len(events) == 1
        # Body not parsed -> fallback credits used
        assert events[0].cost_usd == Decimal("1") * Decimal("0.008")
        assert events[0].cost_confidence == "estimated"

    def test_json_parse_failure_graceful(self) -> None:
        """If response.json() raises, extraction falls back gracefully."""
        task = _make_task()
        response = _make_response(content_type="application/json")
        response.json.side_effect = ValueError("Broken JSON")

        with task_context(task):
            _maybe_record_cost("https://api.tavily.com/search", response)

        events = get_recorded_events()
        assert len(events) == 1
        # Falls back to fallback_credits
        assert events[0].cost_confidence == "estimated"


# ---------------------------------------------------------------------------
# Event field correctness
# ---------------------------------------------------------------------------


class TestEventFields:
    """Recorded events have correct fields."""

    def test_event_has_url_in_details(self) -> None:
        task = _make_task()
        response = _make_response(body={"results": []})

        with task_context(task):
            _maybe_record_cost("https://api.exa.ai/search?query=test", response)

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].details["url"] == "https://api.exa.ai/search?query=test"

    def test_event_has_task_id_from_context(self) -> None:
        task = _make_task()
        response = _make_response(body={})

        with task_context(task):
            _maybe_record_cost("https://api.exa.ai/search", response)

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].task_id == task.task_id

    def test_catalog_version_in_pricing_version(self) -> None:
        """Events from catalog matches include pricing_version."""
        task = _make_task()
        response = _make_response(body={"results": []})

        with task_context(task):
            _maybe_record_cost("https://api.exa.ai/search", response)

        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].pricing_version is not None
        assert len(events[0].pricing_version) == 16
