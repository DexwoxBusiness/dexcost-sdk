"""_aggregate_costs computes network_cost_usd from the canonical scalar."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dexcost import cloud_detect
from dexcost.models.event import Event
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


def _make_task(storage, external_bytes_out):
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    storage.insert_task(t)
    t._network.record(
        "api.example.com", bytes_in=0, bytes_out=external_bytes_out,
        is_internal=False,
    )
    return t


def test_network_cost_usd_from_canonical_scalar(tracker):
    # 1 GB external out * $0.09/GB = $0.09
    t = _make_task(tracker._storage, 1_000_000_000)
    tracker._aggregate_costs(t)
    assert t.network_cost_usd == Decimal("0.09")


def test_total_cost_usd_includes_network(tracker):
    t = _make_task(tracker._storage, 1_000_000_000)
    tracker._storage.insert_event(Event(
        task_id=t.task_id, event_type="llm_call",
        cost_usd=Decimal("0.10"), cost_confidence="computed",
    ))
    tracker._aggregate_costs(t)
    assert t.network_cost_usd == Decimal("0.09")
    assert t.llm_cost_usd == Decimal("0.10")
    assert t.total_cost_usd == Decimal("0.19")


def test_per_host_egress_cost_in_by_host(tracker):
    t = _make_task(tracker._storage, 500_000_000)
    tracker._aggregate_costs(t)
    host = t.network_by_host["hosts"][0]
    assert host["host"] == "api.example.com"
    assert "egress_cost_usd" in host
    assert Decimal(host["egress_cost_usd"]) == Decimal("0.045")


def test_internal_host_has_zero_egress_cost(tracker):
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    tracker._storage.insert_task(t)
    t._network.record("10.0.0.5", bytes_in=0, bytes_out=999_999_999,
                       is_internal=True)
    tracker._aggregate_costs(t)
    host = t.network_by_host["hosts"][0]
    assert Decimal(host["egress_cost_usd"]) == Decimal("0")
    assert t.network_cost_usd == Decimal("0")


def test_network_event_cost_stamped_at_finalize(tracker):
    t = _make_task(tracker._storage, 1_000_000_000)
    ev = Event(
        task_id=t.task_id, event_type="network",
        cost_usd=Decimal("0"), cost_confidence="unknown",
        service_name="api.example.com",
        details={"cost_pending": True, "url": "x",
                 "request_bytes": 0, "response_bytes": 1_000_000_000,
                 "is_internal_traffic": False},
    )
    tracker._storage.insert_event(ev)
    tracker._aggregate_costs(t)

    refreshed = tracker._storage.query_events(task_id=str(t.task_id))[0]
    assert refreshed.cost_usd == Decimal("0.09")
    assert refreshed.cost_confidence == "computed"
    assert refreshed.pricing_source == "egress_catalog:aws:us-east-1"
    assert refreshed.pricing_version is not None
    assert refreshed.pricing_version.startswith("egress:")
    assert "cost_pending" not in refreshed.details


def test_below_threshold_uncataloged_bytes_still_priced(tracker):
    # A small call (no network event emitted) still contributes to network_cost_usd.
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    tracker._storage.insert_task(t)
    t._network.record("api.example.com", bytes_in=0, bytes_out=100_000_000,
                       is_internal=False)  # 100 MB, no event
    tracker._aggregate_costs(t)
    assert t.network_cost_usd == Decimal("0.009")


def test_no_cloud_detected_uses_tier3_default(tmp_path, monkeypatch):
    """When CloudEnv is "none", resolver returns _meta default at estimated."""
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv(None, None, "none")
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    tracker = CostTracker(storage=storage, auto_instrument=[])
    t = _make_task(storage, 1_000_000_000)
    tracker._aggregate_costs(t)
    # $0.09/GB is the universal default rate
    assert t.network_cost_usd == Decimal("0.09")


def test_zero_external_bytes_yields_zero_network_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv("aws", "us-east-1", "env")
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    tracker = CostTracker(storage=storage, auto_instrument=[])
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    storage.insert_task(t)
    tracker._aggregate_costs(t)
    assert t.network_cost_usd == Decimal("0")
