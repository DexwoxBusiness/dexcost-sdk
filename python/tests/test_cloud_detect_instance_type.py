"""CloudEnv carries instance_type extracted by Phase 2 IMDS probes (Decision #3).

The compute pricing engine reads instance_type at task finalize to resolve
EC2 / GCE / Azure VM SKU rates. Per Decision #3 the instance-type fetch shares
the same Phase 2 background thread that already runs for the region probe —
one probe, two values extracted.
"""

from __future__ import annotations

import json

import pytest


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):  # noqa: D401
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self._body


def test_cloud_env_carries_instance_type_field():
    from dexcost.cloud_detect import CloudEnv

    env = CloudEnv(
        provider="aws", region="us-east-1", source="imds",
        instance_type="c7g.xlarge",
    )
    assert env.instance_type == "c7g.xlarge"


def test_cloud_env_instance_type_defaults_to_none():
    from dexcost.cloud_detect import CloudEnv

    env = CloudEnv(provider=None, region=None, source="none")
    assert env.instance_type is None


def test_aws_probe_returns_instance_type(monkeypatch):
    from dexcost import cloud_detect as cd

    calls: list[str] = []

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if url.endswith("/api/token"):
            return _FakeResp(b"TOKEN")
        if url.endswith("/placement/region"):
            return _FakeResp(b"us-east-1")
        if url.endswith("/meta-data/instance-type"):
            return _FakeResp(b"c7g.xlarge")
        raise OSError(f"unexpected url {url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    env = cd._probe_aws()
    assert env is not None
    assert env.provider == "aws"
    assert env.region == "us-east-1"
    assert env.instance_type == "c7g.xlarge"
    assert any("/meta-data/instance-type" in u for u in calls)


def test_aws_probe_instance_type_failure_does_not_lose_region(monkeypatch):
    """If region succeeds but instance-type fails, region must still come back."""
    from dexcost import cloud_detect as cd

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        if url.endswith("/api/token"):
            return _FakeResp(b"TOKEN")
        if url.endswith("/placement/region"):
            return _FakeResp(b"eu-west-2")
        if url.endswith("/meta-data/instance-type"):
            raise OSError("simulated instance-type 404")
        raise OSError(f"unexpected url {url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    env = cd._probe_aws()
    assert env is not None
    assert env.region == "eu-west-2"
    assert env.instance_type is None


def test_gcp_probe_returns_machine_type(monkeypatch):
    from dexcost import cloud_detect as cd

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        if url.endswith("/instance/region"):
            return _FakeResp(b"projects/123/regions/us-central1")
        if url.endswith("/instance/machine-type"):
            return _FakeResp(b"projects/123/machineTypes/n2-standard-2")
        raise OSError(f"unexpected url {url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    env = cd._probe_gcp()
    assert env is not None
    assert env.region == "us-central1"
    assert env.instance_type == "n2-standard-2"


def test_gcp_probe_machine_type_failure_does_not_lose_region(monkeypatch):
    from dexcost import cloud_detect as cd

    def _fake_urlopen(req, timeout=None):
        url = req.full_url
        if url.endswith("/instance/region"):
            return _FakeResp(b"projects/123/regions/us-central1")
        if url.endswith("/instance/machine-type"):
            raise OSError("simulated 404")
        raise OSError(f"unexpected url {url}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    env = cd._probe_gcp()
    assert env is not None
    assert env.region == "us-central1"
    assert env.instance_type is None


def test_azure_probe_returns_vm_size(monkeypatch):
    from dexcost import cloud_detect as cd

    payload = json.dumps({
        "compute": {"location": "eastus", "vmSize": "Standard_D2s_v3"},
    }).encode("utf-8")

    def _fake_urlopen(req, timeout=None):
        return _FakeResp(payload)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    env = cd._probe_azure()
    assert env is not None
    assert env.region == "eastus"
    assert env.instance_type == "Standard_D2s_v3"


def test_azure_probe_missing_vm_size_returns_none_instance_type(monkeypatch):
    from dexcost import cloud_detect as cd

    payload = json.dumps({"compute": {"location": "eastus"}}).encode("utf-8")

    def _fake_urlopen(req, timeout=None):
        return _FakeResp(payload)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    env = cd._probe_azure()
    assert env is not None
    assert env.region == "eastus"
    assert env.instance_type is None
