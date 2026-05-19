"""init() wires the SDK config into the HTTP network adapter."""

import pytest

import dexcost
from dexcost.adapters import http as http_adapter


@pytest.fixture(autouse=True)
def _reset_network_config():
    yield
    http_adapter.set_network_config(None)


def test_init_wires_network_config(tmp_path):
    dexcost.close()
    dexcost.init(
        storage="local",
        buffer_path=str(tmp_path / "b.db"),
        network_event_threshold_bytes=4096,
        network_event_on_error=False,
        network_event_latency_ms=250,
        auto_instrument=[],
    )
    cfg = http_adapter._cfg()
    assert cfg.network_event_threshold_bytes == 4096
    assert cfg.track_network is True
    assert cfg.network_event_on_error is False
    assert cfg.network_event_latency_ms == 250
    dexcost.close()


def test_init_wires_track_network_false(tmp_path):
    dexcost.close()
    dexcost.init(storage="local", buffer_path=str(tmp_path / "b.db"),
                 track_network=False, auto_instrument=[])
    assert http_adapter._cfg().track_network is False
    dexcost.close()
