"""Network event type is registered."""

from dexcost.models.enums import EventType


def test_network_event_type_exists():
    assert EventType.NETWORK.value == "network"


def test_network_in_event_type_members():
    assert "network" in {e.value for e in EventType}
