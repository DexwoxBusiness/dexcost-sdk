"""Task model carries the four network-capture fields."""

from dexcost.models.task import Task


def test_network_field_defaults():
    t = Task(task_type="x")
    assert t.network_bytes_in == 0
    assert t.network_bytes_out == 0
    assert t.network_call_count == 0
    assert t.network_by_host == {"hosts": []}


def test_network_fields_round_trip():
    t = Task(task_type="x")
    t.network_bytes_in = 4096
    t.network_bytes_out = 512
    t.network_call_count = 3
    t.network_by_host = {"hosts": [{"host": "a.com", "calls": 3, "bytes_in": 4096, "bytes_out": 512}]}
    restored = Task.from_dict(t.to_dict())
    assert restored.network_bytes_in == 4096
    assert restored.network_bytes_out == 512
    assert restored.network_call_count == 3
    assert restored.network_by_host == {"hosts": [{"host": "a.com", "calls": 3, "bytes_in": 4096, "bytes_out": 512}]}


def test_network_by_host_absent_in_dict_defaults_empty():
    # A task dict produced before this feature has no network_* keys.
    legacy = Task(task_type="x").to_dict()
    del legacy["network_by_host"]
    del legacy["network_bytes_in"]
    del legacy["network_bytes_out"]
    del legacy["network_call_count"]
    restored = Task.from_dict(legacy)
    assert restored.network_by_host == {"hosts": []}
    assert restored.network_bytes_in == 0
    assert restored.network_bytes_out == 0
    assert restored.network_call_count == 0
