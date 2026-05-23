"""Spec §10.2 — cataloged vendor calls produce ONE event but contribute to BOTH
external_cost_usd and network_cost_usd.  Pins Decision #7 (§2) so a future
refactor cannot silently strip the cloud-egress half of vendor-call cost.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dexcost import cloud_detect
from dexcost.adapters import http as adapter_mod
from dexcost.adapters.http import (
    _handle_domain_rate, clear_domain_rates, register_domain_rate,
)
from dexcost.context import _current_task, set_current_task
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv("aws", "us-east-1", "env")
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    return CostTracker(storage=storage, auto_instrument=[])


def test_dual_invoice_cataloged_vendor_call(tracker, monkeypatch):
    """One HTTP call to a cataloged vendor must yield:
    - exactly one external_cost event (the vendor's per-request charge)
    - task.external_cost_usd == vendor charge
    - task.network_cost_usd == cloud egress on the SAME bytes (Decision #7)
    - the event's own cost_usd unchanged (events carry measurement; the
      task carries derived attribution — §3.3).
    """
    clear_domain_rates()
    register_domain_rate("api.vendor.com", cost_usd="0.01")

    # Wire the HTTP adapter to the test storage so _persist_event lands rows.
    monkeypatch.setattr(adapter_mod, "_storage", tracker._storage)

    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    tracker._storage.insert_task(t)
    token = set_current_task(t)
    try:
        _handle_domain_rate(
            "https://api.vendor.com/x", "api.vendor.com",
            track_network=True, bytes_in=0, bytes_out=500_000_000,  # 0.5 GB
            byte_details={
                "protocol": "https", "request_bytes": 0,
                "response_bytes": 500_000_000,
                "is_internal_traffic": False,
            },
        )
    finally:
        _current_task.reset(token)
        clear_domain_rates()

    t.ended_at = datetime.now(timezone.utc)
    tracker._aggregate_costs(t)

    # (1) Exactly one event for this call, type external_cost — the "one event
    #     per call" invariant from §3.3 holds.
    events = tracker._storage.query_events(task_id=str(t.task_id))
    assert len(events) == 1
    assert events[0].event_type == "external_cost"

    # (2) Vendor's per-request invoice is intact.
    assert t.external_cost_usd == Decimal("0.01")

    # (3) The cloud's egress invoice on those same bytes is captured IN ADDITION.
    #     0.5 GB * $0.09/GB = $0.045
    assert t.network_cost_usd == Decimal("0.045")

    # (4) Total = vendor + egress (no double-count, no silent drop).
    assert t.total_cost_usd == Decimal("0.055")

    # (5) The external_cost event's own cost_usd is unchanged from v1 — no
    #     egress dollars were stamped onto it (events carry measurement,
    #     task carries derived attribution — §3.3).
    assert events[0].cost_usd == Decimal("0.01")
