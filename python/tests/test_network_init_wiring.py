"""init() wires the SDK config into the HTTP network adapter."""

import dexcost
from dexcost.adapters import http as http_adapter


def test_init_wires_network_config(tmp_path):
    dexcost.close()
    dexcost.init(storage="local", buffer_path=str(tmp_path / "b.db"),
                 network_event_threshold_bytes=4096)
    cfg = http_adapter._cfg()
    assert cfg.network_event_threshold_bytes == 4096
    assert cfg.track_network is True
    dexcost.close()


def test_init_track_network_false_disables_threshold_path(tmp_path):
    dexcost.close()
    dexcost.init(storage="local", buffer_path=str(tmp_path / "b.db"),
                 track_network=False)
    assert http_adapter._cfg().track_network is False
    dexcost.close()
