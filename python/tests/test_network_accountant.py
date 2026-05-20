"""NetworkAccountant — per-task in-process byte accumulator."""

import threading

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
    # is_internal defaults to None → bytes_out attributes as external (v2 §6.1).
    assert hosts["a.com"] == {
        "host": "a.com", "calls": 1, "bytes_in": 100, "bytes_out": 10,
        "external_bytes_out": 10,
    }
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


def test_live_host_count_starts_zero_and_tracks():
    acc = NetworkAccountant()
    assert acc.live_host_count() == 0
    acc.record("x.com", 1, 0)
    acc.record("y.com", 1, 0)
    acc.record("z.com", 1, 0)
    assert acc.live_host_count() == 3


def test_real_other_host_does_not_duplicate_key():
    acc = NetworkAccountant()
    # Record a host literally named "_other" with 7 calls (1 byte each so it
    # lands in the top-FINALIZE_CAP by bytes).
    for _ in range(7):
        acc.record("_other", bytes_in=1000, bytes_out=0)
    real_other_calls = 7

    # Now record FINALIZE_CAP additional distinct hosts so that the overflow
    # bucket is also non-empty (the lightest host will be pushed out of top).
    for i in range(FINALIZE_CAP):
        acc.record(f"extra{i}.com", bytes_in=1, bytes_out=0)
    overflow_calls = 1  # 1 host × 1 call each ends up in synthetic overflow

    snap = acc.finalize()
    host_names = [h["host"] for h in snap["by_host"]["hosts"]]

    # No duplicate keys.
    assert len(host_names) == len(set(host_names)), "duplicate host keys in output"

    # Exactly one "_other" entry.
    assert host_names.count("_other") == 1

    # Its calls == real "_other" calls + the synthetic overflow calls.
    single_other = next(h for h in snap["by_host"]["hosts"] if h["host"] == "_other")
    assert single_other["calls"] == real_other_calls + overflow_calls


def test_negative_bytes_are_clamped():
    acc = NetworkAccountant()
    acc.record("a.com", bytes_in=-50, bytes_out=-5)
    snap = acc.finalize()
    host = next(h for h in snap["by_host"]["hosts"] if h["host"] == "a.com")
    assert host["bytes_in"] == 0
    assert host["bytes_out"] == 0
    assert host["calls"] == 1
    assert snap["bytes_in"] == 0
    assert snap["bytes_out"] == 0
    assert snap["call_count"] == 1


def test_concurrent_record_is_thread_safe():
    acc = NetworkAccountant()
    threads = [
        threading.Thread(target=lambda: [acc.record("shared.com", 1, 1) for _ in range(500)])
        for _ in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = acc.finalize()
    assert snap["call_count"] == 5000
    host = next(h for h in snap["by_host"]["hosts"] if h["host"] == "shared.com")
    assert host["calls"] == 5000
