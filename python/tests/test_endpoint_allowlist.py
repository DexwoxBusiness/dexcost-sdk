"""A2 regression — Sprint 1 Theme A / plan §2.1.

DEXCOST_ENDPOINT env var must be rejected if it doesn't start with
`https://`. The threat model is: an attacker sets the env var on a
machine they control (or a misconfigured CI runner) to silently
exfiltrate cost telemetry to an HTTP collector. We refuse any non-
https value, log a warning, and fall back to the production default.
"""

from __future__ import annotations

import logging

import pytest

from dexcost.config import DexcostConfig

_PRODUCTION_DEFAULT = "https://api.dexcost.io"


def test_endpoint_accepts_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEXCOST_ENDPOINT", "https://custom.example.com")
    cfg = DexcostConfig()
    assert cfg.endpoint == "https://custom.example.com"


def test_endpoint_rejects_http_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DEXCOST_ENDPOINT", "http://attacker.example/")
    with caplog.at_level(logging.WARNING):
        cfg = DexcostConfig()
        assert cfg.endpoint == _PRODUCTION_DEFAULT
    assert any("DEXCOST_ENDPOINT" in r.message for r in caplog.records), (
        f"expected warning log, got {[r.message for r in caplog.records]}"
    )


def test_endpoint_rejects_javascript_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive — any non-https value falls back, not just http.
    monkeypatch.setenv("DEXCOST_ENDPOINT", "javascript:alert(1)")
    cfg = DexcostConfig()
    assert cfg.endpoint == _PRODUCTION_DEFAULT


def test_endpoint_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEXCOST_ENDPOINT", raising=False)
    cfg = DexcostConfig()
    assert cfg.endpoint == _PRODUCTION_DEFAULT
