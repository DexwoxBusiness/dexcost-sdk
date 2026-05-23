"""All three adapter paths must forward is_internal into the accountant."""

import uuid
from unittest.mock import MagicMock

from dexcost.adapters import http as adapter
from dexcost.adapters.http import (
    _handle_catalog_entry, _handle_domain_rate, _handle_uncataloged,
    clear_domain_rates, register_domain_rate,
)
from dexcost.config import DexcostConfig
from dexcost.context import _current_task, set_current_task
from dexcost.models.task import Task


def _fake_byte_details(is_internal):
    return {
        "protocol": "https",
        "request_bytes": 10,
        "response_bytes": 100,
        "is_internal_traffic": is_internal,
    }


def test_domain_rate_path_records_is_internal_false():
    clear_domain_rates()
    register_domain_rate("api.example.com", cost_usd="0.01")
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)
    try:
        ok = _handle_domain_rate(
            "https://api.example.com/x", "api.example.com",
            track_network=True, bytes_in=100, bytes_out=200,
            byte_details=_fake_byte_details(False),
        )
        assert ok is True
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 200
    finally:
        _current_task.reset(token)
        clear_domain_rates()


def test_domain_rate_path_records_is_internal_true_as_zero_external():
    clear_domain_rates()
    register_domain_rate("api.example.com", cost_usd="0.01")
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)
    try:
        _handle_domain_rate(
            "https://api.example.com/x", "api.example.com",
            track_network=True, bytes_in=100, bytes_out=200,
            byte_details=_fake_byte_details(True),
        )
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 0
        assert snap["bytes_out"] == 200
    finally:
        _current_task.reset(token)
        clear_domain_rates()


def test_catalog_path_records_is_internal_false(monkeypatch):
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)

    fake_catalog = MagicMock()
    fake_entry = MagicMock()
    fake_catalog.lookup.return_value = fake_entry
    fake_catalog.extract_cost.return_value = None
    fake_catalog.catalog_version = "v"
    fake_entry.display_name = "openai"
    monkeypatch.setattr(adapter, "get_catalog", lambda: fake_catalog)

    try:
        ok = _handle_catalog_entry(
            "https://api.openai.com/x", "api.openai.com",
            track_network=True, bytes_in=100, bytes_out=200,
            response_headers={}, response=None,
            byte_details=_fake_byte_details(False),
        )
        assert ok is True
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 200
    finally:
        _current_task.reset(token)


def test_uncataloged_internal_call_records_zero_external():
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)
    cfg = DexcostConfig(storage="local")
    try:
        _handle_uncataloged(
            "http://10.0.0.5/x", "GET", "10.0.0.5",
            bytes_in=100, bytes_out=200, status_code=200, latency_ms=10,
            byte_details=_fake_byte_details(True), cfg=cfg,
        )
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 0
        assert snap["bytes_out"] == 200
    finally:
        _current_task.reset(token)


def test_uncataloged_external_call_records_full_external():
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)
    cfg = DexcostConfig(storage="local")
    try:
        _handle_uncataloged(
            "https://api.example.com/x", "GET", "api.example.com",
            bytes_in=100, bytes_out=200, status_code=200, latency_ms=10,
            byte_details=_fake_byte_details(None), cfg=cfg,
        )
        snap = task._network.finalize()
        assert snap["external_bytes_out"] == 200
    finally:
        _current_task.reset(token)


def test_network_event_carries_cost_pending_marker():
    """Spec §6.4 — at emission, network events ship with cost_pending=True so
    _aggregate_costs can identify them at finalize.
    """
    adapter.clear_recorded_events()
    task = Task(task_id=uuid.uuid4(), task_type="x")
    token = set_current_task(task)
    cfg = DexcostConfig(storage="local")
    try:
        # Use a payload larger than the default 100 KiB threshold so an event emits.
        _handle_uncataloged(
            "https://api.example.com/x", "GET", "api.example.com",
            bytes_in=0, bytes_out=200_000, status_code=200, latency_ms=10,
            byte_details={
                "protocol": "https", "request_bytes": 0,
                "response_bytes": 200_000, "is_internal_traffic": False,
            },
            cfg=cfg,
        )
        events = adapter.get_recorded_events()
        net_events = [e for e in events if e.event_type == "network"]
        assert len(net_events) == 1
        ev = net_events[0]
        assert ev.details.get("cost_pending") is True
    finally:
        _current_task.reset(token)
        adapter.clear_recorded_events()
