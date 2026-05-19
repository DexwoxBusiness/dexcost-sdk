"""Network-capture config fields and their defaults."""

from dexcost.config import DexcostConfig


def test_network_config_defaults():
    c = DexcostConfig(storage="local")
    assert c.track_network is True
    assert c.network_event_threshold_bytes == 102_400
    assert c.network_event_on_error is True
    assert c.network_event_latency_ms == 0


def test_network_config_overrides():
    c = DexcostConfig(storage="local", track_network=False,
                      network_event_threshold_bytes=4096,
                      network_event_on_error=False,
                      network_event_latency_ms=5000)
    assert c.track_network is False
    assert c.network_event_threshold_bytes == 4096
    assert c.network_event_on_error is False
    assert c.network_event_latency_ms == 5000
