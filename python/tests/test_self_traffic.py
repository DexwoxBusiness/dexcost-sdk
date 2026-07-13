"""Tests for SDK self-traffic exclusion (parity with TS registerInternalHost).

Calls to a registered internal host — the SDK's own pusher / pricing-refresh /
service-catalog endpoints — must be completely invisible to capture: no cost
event, no task resolution, no byte accounting. Without this guard, telemetry
pushed through a patched transport would resolve an ambient session task,
persist it, push it next cycle, and drip forever.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from dexcost.adapters.http import (
    _handle_http_call,
    _reset_internal_hosts_for_tests,
    clear_domain_rates,
    clear_recorded_events,
    get_recorded_events,
    is_internal_host,
    register_domain_rate,
    register_internal_host,
    set_catalog,
)
from dexcost.context import clear_context, set_current_task, task_context
from dexcost.models.task import Task
from dexcost.session import reset_session_manager


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    clear_domain_rates()
    clear_recorded_events()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    set_catalog(None)
    _reset_internal_hosts_for_tests()
    yield
    clear_domain_rates()
    clear_recorded_events()
    set_current_task(None)
    clear_context()
    reset_session_manager()
    set_catalog(None)
    _reset_internal_hosts_for_tests()


def _make_task() -> Task:
    return Task(task_type="web_query", customer_id="cust-1")


class TestInternalHostRegistry:
    def test_default_endpoint_is_internal(self) -> None:
        assert is_internal_host("api.dexcost.io")

    def test_registration_is_case_insensitive(self) -> None:
        register_internal_host("My-Dexcost.Internal")
        assert is_internal_host("my-dexcost.internal")
        assert is_internal_host("MY-DEXCOST.INTERNAL")

    def test_non_internal_host_not_matched(self) -> None:
        assert not is_internal_host("api.openai.com")

    def test_empty_hostname_never_internal(self) -> None:
        assert not is_internal_host("")

    def test_reset_restores_default_only(self) -> None:
        register_internal_host("extra.example.com")
        _reset_internal_hosts_for_tests()
        assert not is_internal_host("extra.example.com")
        assert is_internal_host("api.dexcost.io")


class TestSelfTrafficBypass:
    def test_default_endpoint_call_produces_no_event(self) -> None:
        """A call to api.dexcost.io is dropped even with an active task."""
        task = _make_task()
        with task_context(task):
            _handle_http_call("https://api.dexcost.io/v1/ingest", method="POST")
        assert get_recorded_events() == []

    def test_registered_internal_host_bypasses_even_with_domain_rate(self) -> None:
        """The bypass runs BEFORE domain-rate handling — a rated internal
        host still emits nothing."""
        register_internal_host("telemetry.mycorp.internal")
        register_domain_rate("telemetry.mycorp.internal", cost_usd="0.01")
        task = _make_task()
        with task_context(task):
            _handle_http_call(
                "https://telemetry.mycorp.internal/v1/ingest", method="POST"
            )
        assert get_recorded_events() == []

    def test_non_internal_host_still_captured(self) -> None:
        """Control: a non-internal rated domain is still recorded."""
        register_domain_rate("api.example.com", cost_usd="0.01")
        task = _make_task()
        with task_context(task):
            _handle_http_call("https://api.example.com/search", method="GET")
        events = get_recorded_events()
        assert len(events) == 1
        assert events[0].cost_usd == Decimal("0.01")
