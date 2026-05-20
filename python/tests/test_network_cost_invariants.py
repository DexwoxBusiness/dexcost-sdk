"""§10.3 property invariants — must hold across arbitrary task shapes."""

import itertools
import random
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from dexcost import cloud_detect
from dexcost.models.task import Task
from dexcost.storage.sqlite import SQLiteStorage
from dexcost.tracker import CostTracker


def _params():
    host_counts = (1, 5, 20, 100, 1000)
    classifications = (True, False, None)
    return list(itertools.product(host_counts, classifications))


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cloud_detect, "_result", cloud_detect.CloudEnv("aws", "us-east-1", "env")
    )
    storage = SQLiteStorage(db_path=str(tmp_path / "buf.db"))
    return CostTracker(storage=storage, auto_instrument=[])


@pytest.mark.parametrize("n_hosts,internal", _params())
def test_invariants(tracker, n_hosts, internal):
    rng = random.Random(42 + n_hosts)
    t = Task(task_id=uuid.uuid4(), task_type="x",
             started_at=datetime.now(timezone.utc))
    tracker._storage.insert_task(t)
    for i in range(n_hosts):
        t._network.record(
            f"h{i}.com", bytes_in=rng.randint(0, 5000),
            bytes_out=rng.randint(0, 5000), is_internal=internal,
        )
    # Capture the canonical external scalar BEFORE finalize freezes the
    # accountant — _aggregate_costs calls finalize() internally.
    expected_external = t._network._external_bytes_out
    tracker._aggregate_costs(t)

    # Invariant 1: per-host external == scalar external.
    per_host = sum(h.get("external_bytes_out", 0)
                   for h in t.network_by_host["hosts"])
    assert per_host == expected_external

    # Invariant 2: per-host cost == network_cost_usd.
    per_host_cost = sum(
        Decimal(h.get("egress_cost_usd", "0"))
        for h in t.network_by_host["hosts"]
    )
    assert per_host_cost == t.network_cost_usd

    # Invariant 3: sum(network event cost) ≤ network_cost_usd.
    events = tracker._storage.query_events(task_id=str(t.task_id))
    event_sum = sum(
        (e.cost_usd for e in events if e.event_type == "network"),
        Decimal("0"),
    )
    assert event_sum <= t.network_cost_usd
