"""NetworkAccountant — external_bytes_out scalar + per-host split."""

from dexcost.network_accountant import LIVE_CAP, NetworkAccountant


def test_internal_call_does_not_contribute_to_external():
    a = NetworkAccountant()
    a.record("10.0.0.5", bytes_in=100, bytes_out=200, is_internal=True)
    snap = a.finalize()
    assert snap["external_bytes_out"] == 0
    host = snap["by_host"]["hosts"][0]
    assert host["external_bytes_out"] == 0
    assert host["bytes_out"] == 200  # raw measurement still recorded


def test_public_call_contributes_to_external():
    a = NetworkAccountant()
    a.record("api.example.com", bytes_in=100, bytes_out=500, is_internal=False)
    snap = a.finalize()
    assert snap["external_bytes_out"] == 500


def test_null_is_internal_is_treated_as_external():
    a = NetworkAccountant()
    a.record("api.example.com", bytes_in=100, bytes_out=500, is_internal=None)
    snap = a.finalize()
    assert snap["external_bytes_out"] == 500


def test_scalar_equals_sum_of_per_host_external():
    a = NetworkAccountant()
    a.record("a.com", 0, 100, is_internal=False)
    a.record("b.com", 0, 200, is_internal=False)
    a.record("10.0.0.1", 0, 999, is_internal=True)
    snap = a.finalize()
    by_host_sum = sum(h["external_bytes_out"] for h in snap["by_host"]["hosts"])
    assert by_host_sum == snap["external_bytes_out"] == 300


def test_other_bucket_carries_external_bytes():
    """LIVE_CAP overflow folds into _other, which still carries external bytes.

    The 501st distinct host bypasses the per-host map and goes directly into
    _other. The top-20 cap then folds 480 of the 500 live hosts into _other
    as well, so _other.external_bytes_out = 480 (the live-overflow folds) +
    555 (the LIVE_CAP-overflow record).
    """
    a = NetworkAccountant()
    for i in range(LIVE_CAP):
        a.record(f"host{i}.com", 0, 1, is_internal=False)
    a.record("overflow.com", 0, 555, is_internal=False)
    snap = a.finalize()
    other = next(h for h in snap["by_host"]["hosts"] if h["host"] == "_other")
    assert other["external_bytes_out"] == 480 + 555


def test_default_is_internal_is_none_external_attributed():
    a = NetworkAccountant()
    a.record("api.example.com", bytes_in=0, bytes_out=100)  # no kwarg
    snap = a.finalize()
    assert snap["external_bytes_out"] == 100


def test_finalize_includes_external_bytes_out_top_level():
    a = NetworkAccountant()
    snap = a.finalize()
    assert "external_bytes_out" in snap
    assert snap["external_bytes_out"] == 0
