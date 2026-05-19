"""End-to-end network capture through the HTTP adapter."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from dexcost.adapters import http as http_adapter
from dexcost.adapters.http import (
    _handle_http_call,
    clear_domain_rates,
    clear_recorded_events,
    get_network_error_count,
    get_recorded_events,
    register_domain_rate,
    reset_network_error_count,
)
from dexcost.context import clear_context, set_current_task, suppress_network_event
from dexcost.models.task import Task
from dexcost.session import reset_session_manager


@pytest.fixture(autouse=True)
def _clean():
    clear_domain_rates()
    clear_recorded_events()
    reset_network_error_count()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    http_adapter.set_catalog(None)
    yield
    clear_domain_rates()
    clear_recorded_events()
    reset_network_error_count()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    http_adapter.set_catalog(None)


class _Resp:
    """Minimal response stand-in: headers dict + status_code."""

    def __init__(self, status_code: int = 200, body_len: int = 0,
                 content_type: str = "application/json"):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type,
                        "Content-Length": str(body_len)}
        self._body_len = body_len

    def json(self):  # noqa: D401 - test stub
        return {}


def _task() -> Task:
    return Task(task_id=uuid.uuid4(), task_type="t",
                started_at=datetime.now(timezone.utc))


def test_bytes_land_on_task_counters():
    task = _task()
    set_current_task(task)
    _handle_http_call("https://api.uncataloged.com/v1/x", method="POST",
                      request_headers={"Content-Type": "application/json"},
                      request_body_len=120, response=_Resp(200, body_len=500),
                      latency_ms=12)
    snap = task._network.finalize()
    assert snap["call_count"] == 1
    assert snap["bytes_in"] > 500   # response body + headers
    assert snap["bytes_out"] > 120  # request body + headers


def test_uncataloged_above_threshold_emits_network_event():
    task = _task()
    set_current_task(task)
    _handle_http_call("https://api.uncataloged.com/big", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Resp(200, body_len=200_000), latency_ms=40)
    events = get_recorded_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "network"
    assert ev.service_name == "api.uncataloged.com"
    assert ev.cost_usd == 0
    assert ev.cost_confidence == "unknown"
    assert ev.details["protocol"] == "https"
    assert ev.details["status_code"] == 200
    assert ev.details["response_bytes"] >= 200_000
    assert ev.details["is_internal_traffic"] is None  # named host


def test_uncataloged_below_threshold_emits_no_event():
    task = _task()
    set_current_task(task)
    _handle_http_call("https://api.uncataloged.com/small", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Resp(200, body_len=300), latency_ms=5)
    assert get_recorded_events() == []          # no event
    assert task._network.finalize()["call_count"] == 1  # counters still updated


def test_uncataloged_error_emits_event_even_when_small():
    task = _task()
    set_current_task(task)
    _handle_http_call("https://api.uncataloged.com/fail", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Resp(503, body_len=80), latency_ms=5)
    events = get_recorded_events()
    assert len(events) == 1
    assert events[0].event_type == "network"
    assert events[0].details["status_code"] == 503


def test_cataloged_domain_rate_stamps_bytes_no_network_event():
    task = _task()
    set_current_task(task)
    register_domain_rate("api.vendor.com", cost_usd="0.01")
    _handle_http_call("https://api.vendor.com/charge", method="POST",
                      request_headers={}, request_body_len=40,
                      response=_Resp(200, body_len=900), latency_ms=8)
    events = get_recorded_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "external_cost"      # not re-typed
    assert ev.details["request_bytes"] >= 40     # bytes stamped in
    assert ev.details["response_bytes"] >= 900
    assert ev.details["protocol"] == "https"


def test_suppressed_call_records_bytes_but_no_network_event():
    task = _task()
    set_current_task(task)
    # Use a domain not in the service catalog so only the uncataloged path runs.
    # Suppression prevents the `network` event that would otherwise fire (body
    # > threshold), but bytes are still recorded into the task counters.
    with suppress_network_event():
        _handle_http_call("https://api.suppressed-vendor.internal/v1/infer", method="POST",
                          request_headers={}, request_body_len=100,
                          response=_Resp(200, body_len=300_000), latency_ms=900)
    assert get_recorded_events() == []                  # no network event
    assert task._network.finalize()["bytes_in"] > 300_000  # bytes still counted


def test_no_active_task_is_noop():
    set_current_task(None)
    # No catalog, no domain rate, no session — must not raise, must not record.
    _handle_http_call("https://api.uncataloged.com/x", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Resp(200, body_len=500_000), latency_ms=10)
    assert get_recorded_events() == []


def test_handler_failure_is_swallowed_and_counted():
    task = _task()
    set_current_task(task)
    # response.headers raising → measurement throws → swallowed + counted.
    class _Bad:
        status_code = 200
        @property
        def headers(self):
            raise RuntimeError("boom")
    _handle_http_call("https://api.uncataloged.com/x", method="GET",
                      request_headers={}, request_body_len=0,
                      response=_Bad(), latency_ms=1)
    assert get_network_error_count() >= 1  # observable, not hidden
