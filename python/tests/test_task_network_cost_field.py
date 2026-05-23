"""Task model carries network_cost_usd, parallel to the other *_cost_usd fields."""

from decimal import Decimal

from dexcost.models.task import Task


def test_network_cost_usd_defaults_to_zero():
    t = Task(task_type="x")
    assert t.network_cost_usd == Decimal("0")
    assert isinstance(t.network_cost_usd, Decimal)


def test_network_cost_usd_round_trip_through_dict():
    t = Task(task_type="x")
    t.network_cost_usd = Decimal("0.0042")
    d = t.to_dict()
    assert d["network_cost_usd"] == "0.0042"

    t2 = Task.from_dict(d)
    assert t2.network_cost_usd == Decimal("0.0042")


def test_from_dict_defaults_network_cost_usd_for_old_payloads():
    # Old payload without the v2 field — must default to Decimal("0").
    d = Task(task_type="x").to_dict()
    d.pop("network_cost_usd")
    t = Task.from_dict(d)
    assert t.network_cost_usd == Decimal("0")
