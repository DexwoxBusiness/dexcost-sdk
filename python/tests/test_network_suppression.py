"""Context-scoped flag that suppresses the per-call network event."""

from dexcost.context import is_network_event_suppressed, suppress_network_event


def test_default_not_suppressed():
    assert is_network_event_suppressed() is False


def test_suppress_context_manager_sets_and_clears():
    assert is_network_event_suppressed() is False
    with suppress_network_event():
        assert is_network_event_suppressed() is True
    assert is_network_event_suppressed() is False


def test_nested_suppression_restores_outer_state():
    with suppress_network_event():
        with suppress_network_event():
            assert is_network_event_suppressed() is True
        assert is_network_event_suppressed() is True
    assert is_network_event_suppressed() is False
