"""Tests for API key infrastructure (US-017)."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from dexcost.config import DexcostConfig, InvalidAPIKeyError, validate_api_key


class TestValidateAPIKey:
    def test_live_key_accepted(self) -> None:
        assert validate_api_key("dx_live_abc123def456") == "live"

    def test_test_key_accepted(self) -> None:
        assert validate_api_key("dx_test_abc123def456") == "test"

    def test_invalid_prefix_rejected(self) -> None:
        with pytest.raises(InvalidAPIKeyError, match="must start with"):
            validate_api_key("sk_live_abc123")

    def test_empty_key_rejected(self) -> None:
        with pytest.raises(InvalidAPIKeyError, match="must start with"):
            validate_api_key("")

    def test_none_returns_none(self) -> None:
        assert validate_api_key(None) is None


class TestDexcostConfig:
    def test_local_mode_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = DexcostConfig()
            assert cfg.storage_mode == "local"
            assert cfg.api_key is None
            assert cfg.key_type is None

    def test_explicit_local_mode(self) -> None:
        cfg = DexcostConfig(storage="local")
        assert cfg.storage_mode == "local"

    def test_api_key_activates_cloud(self) -> None:
        cfg = DexcostConfig(api_key="dx_live_abc123")
        assert cfg.storage_mode == "cloud"
        assert cfg.key_type == "live"

    def test_test_key_sandbox(self) -> None:
        cfg = DexcostConfig(api_key="dx_test_abc123")
        assert cfg.storage_mode == "cloud"
        assert cfg.key_type == "test"
        assert cfg.is_sandbox is True

    def test_env_var_fallback(self) -> None:
        with mock.patch.dict(os.environ, {"DEXCOST_API_KEY": "dx_live_fromenv"}):
            cfg = DexcostConfig()
            assert cfg.api_key == "dx_live_fromenv"
            assert cfg.storage_mode == "cloud"

    def test_explicit_key_overrides_env(self) -> None:
        with mock.patch.dict(os.environ, {"DEXCOST_API_KEY": "dx_live_fromenv"}):
            cfg = DexcostConfig(api_key="dx_test_explicit")
            assert cfg.api_key == "dx_test_explicit"
            assert cfg.key_type == "test"

    def test_storage_local_ignores_key(self) -> None:
        cfg = DexcostConfig(api_key="dx_live_abc", storage="local")
        assert cfg.storage_mode == "local"

    def test_endpoint_default(self) -> None:
        cfg = DexcostConfig(api_key="dx_live_abc")
        assert cfg.endpoint == "https://api.dexcost.io"

    def test_endpoint_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"DEXCOST_ENDPOINT": "https://custom.api.dev"}):
            cfg = DexcostConfig(api_key="dx_live_abc")
            assert cfg.endpoint == "https://custom.api.dev"

    def test_batch_size_default(self) -> None:
        cfg = DexcostConfig()
        assert cfg.batch_size == 100

    def test_flush_interval_default(self) -> None:
        cfg = DexcostConfig()
        assert cfg.flush_interval_seconds == 5.0

    def test_invalid_key_raises(self) -> None:
        with pytest.raises(InvalidAPIKeyError):
            DexcostConfig(api_key="bad_key_format")

    def test_is_sandbox_false_for_live(self) -> None:
        cfg = DexcostConfig(api_key="dx_live_abc")
        assert cfg.is_sandbox is False

    def test_is_sandbox_false_for_none(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = DexcostConfig()
            assert cfg.is_sandbox is False
