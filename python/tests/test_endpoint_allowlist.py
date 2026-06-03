"""Endpoint resolution — in-code, explicit config only.

The Control Layer endpoint is resolved ONLY from explicit, in-code
configuration (``init(endpoint=...)`` / ``DexcostConfig
(endpoint_override=...)``), never from the process environment. The
threat model is: an attacker who controls the env (misconfigured CI
runner, hostile container) could set ``DEXCOST_ENDPOINT=http://attacker/``
and silently exfiltrate cost telemetry plus the Bearer API key. Because
the SDK never reads the endpoint from the env, that vector is closed.

The explicit, in-code value is developer-supplied and trusted, so it
intentionally accepts ``http://`` (e.g. ``http://localhost`` for e2e).
Validation is minimal: a value with no ``http(s)://`` scheme is rejected
(warn + fall back to the production default).
"""

from __future__ import annotations

import logging

import pytest

from dexcost.config import _DEFAULT_ENDPOINT, DexcostConfig

_PRODUCTION_DEFAULT = "https://api.dexcost.io"


def test_default_when_no_override() -> None:
    cfg = DexcostConfig()
    assert cfg.endpoint == _PRODUCTION_DEFAULT == _DEFAULT_ENDPOINT


def test_explicit_https_honored() -> None:
    cfg = DexcostConfig(endpoint_override="https://custom.example.com")
    assert cfg.endpoint == "https://custom.example.com"


def test_explicit_http_localhost_accepted() -> None:
    # http:// is intentionally allowed for the explicit, in-code option —
    # safe because it is not env-controllable.
    cfg = DexcostConfig(endpoint_override="http://localhost:3000")
    assert cfg.endpoint == "http://localhost:3000"


def test_explicit_bad_scheme_falls_back_to_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        cfg = DexcostConfig(endpoint_override="javascript:alert(1)")
        assert cfg.endpoint == _PRODUCTION_DEFAULT
    assert any("endpoint" in r.message for r in caplog.records), (
        f"expected warning log, got {[r.message for r in caplog.records]}"
    )


def test_env_var_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even a perfectly-valid-looking env value must be ignored entirely.
    monkeypatch.setenv("DEXCOST_ENDPOINT", "http://attacker.example/")
    cfg = DexcostConfig()
    assert cfg.endpoint == _PRODUCTION_DEFAULT
