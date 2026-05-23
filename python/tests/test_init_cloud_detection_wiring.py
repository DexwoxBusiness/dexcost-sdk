"""dexcost.init() launches the cloud-detection probe unless track_network=False."""

import time

import dexcost
from dexcost import cloud_detect


def _reset_cloud_detect():
    cloud_detect._result = cloud_detect.CloudEnv(None, None, "none")
    cloud_detect._thread = None


def test_init_launches_detection_under_default(monkeypatch, tmp_path):
    _reset_cloud_detect()
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(dexcost, "_global_tracker", None)
    monkeypatch.setattr(dexcost, "_sync_worker", None)
    monkeypatch.setattr(dexcost, "_global_config", None)
    t0 = time.perf_counter()
    dexcost.init(storage="local", buffer_path=str(tmp_path / "buf.db"),
                 track_http=False, auto_instrument=[])
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5  # never blocks init (probe headroom)
    env = cloud_detect.get_cloud_env()
    assert env.provider == "aws"
    assert env.region == "us-east-1"


def test_init_skips_detection_when_track_network_false(monkeypatch, tmp_path):
    _reset_cloud_detect()
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "x")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(dexcost, "_global_tracker", None)
    monkeypatch.setattr(dexcost, "_sync_worker", None)
    monkeypatch.setattr(dexcost, "_global_config", None)
    dexcost.init(storage="local", buffer_path=str(tmp_path / "buf.db"),
                 track_http=False, auto_instrument=[], track_network=False)
    env = cloud_detect.get_cloud_env()
    assert env.source == "none"
