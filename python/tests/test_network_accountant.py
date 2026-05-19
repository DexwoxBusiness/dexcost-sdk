"""NetworkAccountant — per-task in-process byte accumulator."""

from dexcost.network_accountant import NetworkAccountant, FINALIZE_CAP, LIVE_CAP


def test_record_accumulates_scalars():
    acc = NetworkAccountant()
    acc.record("a.com", bytes_in=100, bytes_out=10)
    acc.record("a.com", bytes_in=50, bytes_out=5)
    snap = acc.finalize()
    assert snap["bytes_in"] == 150
    assert snap["bytes_out"] == 15
    assert snap["call_count"] == 2


def test_finalize_groups_by_host():
    acc = NetworkAccountant()
    acc.record("a.com", 100, 10)
    acc.record("b.com", 200, 20)
    hosts = {h["host"]: h for h in acc.finalize()["by_host"]["hosts"]}
    assert hosts["a.com"] == {"host": "a.com", "calls": 1, "bytes_in": 100, "bytes_out": 10}
    assert hosts["b.com"]["bytes_in"] == 200


def test_finalize_caps_to_top_20_with_other_bucket():
    acc = NetworkAccountant()
    # 25 hosts; host_i gets i bytes_in so the heavy ones are deterministic.
    for i in range(25):
        acc.record(f"h{i:02d}.com", bytes_in=i + 1, bytes_out=0)
    hosts = acc.finalize()["by_host"]["hosts"]
    assert len(hosts) == FINALIZE_CAP + 1  # 20 + _other
    names = {h["host"] for h in hosts}
    assert "_other" in names
    assert "h24.com" in names  # heaviest survives
    assert "h00.com" not in names  # lightest folded into _other
    other = next(h for h in hosts if h["host"] == "_other")
    # _other holds the 5 lightest (1+2+3+4+5 bytes_in) and their 5 calls.
    assert other["calls"] == 5
    assert other["bytes_in"] == 1 + 2 + 3 + 4 + 5


def test_empty_finalize_is_empty_array():
    assert NetworkAccountant().finalize()["by_host"] == {"hosts": []}


def test_live_cap_folds_overflow_hosts_into_other():
    acc = NetworkAccountant()
    for i in range(LIVE_CAP + 50):
        acc.record(f"h{i}.com", bytes_in=1, bytes_out=0)
    # Live map never exceeds LIVE_CAP tracked hosts (+ the _other bucket).
    assert acc.live_host_count() <= LIVE_CAP
    snap = acc.finalize()
    assert snap["call_count"] == LIVE_CAP + 50  # every call still counted


def test_record_after_finalize_is_noop():
    acc = NetworkAccountant()
    acc.record("a.com", 100, 10)
    acc.finalize()
    acc.record("a.com", 999, 999)  # frozen — must be ignored
    snap = acc.finalize()
    assert snap["bytes_in"] == 100
    assert snap["call_count"] == 1
