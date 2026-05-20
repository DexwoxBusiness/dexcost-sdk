"""Fargate ECS task metadata helper.

Single HTTP call per process, cached. Exposes vcpu_count (float) and
memory_bytes_limit (int — converted from MiB per Decision #7; Fargate uses
BINARY MiB, not decimal MB, which is the ~4.86% silent-over-attribution bug
the conversion table prevents).
"""

from __future__ import annotations

import json


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):  # noqa: D401
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self._body


def _reset(monkeypatch=None):
    from dexcost import fargate_metadata as fm
    fm._reset_for_tests()


def test_returns_vcpu_and_memory(monkeypatch):
    from dexcost.fargate_metadata import (
        FargateTaskMetadata, fetch_fargate_metadata,
    )
    _reset()
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc",
    )

    def _fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps({
            "TaskARN": "arn:aws:ecs:us-east-1:0:task/abc",
            "Limits": {"CPU": 0.5, "Memory": 1024},  # MiB
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    m = fetch_fargate_metadata()
    assert isinstance(m, FargateTaskMetadata)
    assert m.vcpu_count == 0.5
    # 1024 MiB → bytes via binary GiB (Decision #7).
    assert m.memory_bytes_limit == 1024 * 1024 * 1024


def test_no_env_var_returns_none(monkeypatch):
    from dexcost.fargate_metadata import fetch_fargate_metadata
    _reset()
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    assert fetch_fargate_metadata() is None


def test_unreachable_returns_none_and_logs_once(monkeypatch, caplog):
    from dexcost.fargate_metadata import fetch_fargate_metadata
    _reset()
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc",
    )

    def _boom(req, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    import logging
    with caplog.at_level(logging.WARNING):
        assert fetch_fargate_metadata() is None
        assert fetch_fargate_metadata() is None  # no extra log

    messages = [
        r.getMessage() for r in caplog.records
        if "fargate metadata" in r.getMessage().lower()
    ]
    assert len(messages) == 1  # convention §11 log-once-per-mode


def test_cached_after_first_success(monkeypatch):
    from dexcost.fargate_metadata import fetch_fargate_metadata
    _reset()
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc",
    )
    calls = {"n": 0}

    def _fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeResp(json.dumps({
            "Limits": {"CPU": 1, "Memory": 512},
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    a = fetch_fargate_metadata()
    b = fetch_fargate_metadata()
    assert a is b is not None
    assert calls["n"] == 1


def test_malformed_limits_returns_none(monkeypatch):
    from dexcost.fargate_metadata import fetch_fargate_metadata
    _reset()
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc",
    )

    def _fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps({"Limits": {"CPU": "garbage"}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    assert fetch_fargate_metadata() is None


def test_v4_uri_replaced_with_v3_when_set(monkeypatch):
    """ECS_CONTAINER_METADATA_URI (v3, no _V4 suffix) is also valid."""
    from dexcost.fargate_metadata import fetch_fargate_metadata
    _reset()
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    monkeypatch.setenv(
        "ECS_CONTAINER_METADATA_URI", "http://169.254.170.2/v3/abc",
    )

    def _fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps({
            "Limits": {"CPU": 2, "Memory": 4096},
        }).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    m = fetch_fargate_metadata()
    assert m is not None
    assert m.vcpu_count == 2.0
    assert m.memory_bytes_limit == 4096 * 1024 * 1024
