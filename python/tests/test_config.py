"""Tests for API key infrastructure (US-017)."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from dexcost.config import (
    _DEFAULT_ENDPOINT,
    DexcostConfig,
    InvalidAPIKeyError,
    catalog_unsigned_override_requested,
    resolve_catalog_trust_policy,
    validate_api_key,
)

_CATALOG_PUBLIC_KEY = "11qYAYdk9JNu81kOIyRUDn69brTa7WHqmX84xB6sSPA"


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

    def test_endpoint_explicit_override_honored(self) -> None:
        cfg = DexcostConfig(api_key="dx_live_abc", endpoint_override="https://custom.api.dev")
        assert cfg.endpoint == "https://custom.api.dev"

    def test_endpoint_env_var_is_ignored(self) -> None:
        # The endpoint must come ONLY from explicit in-code config. An
        # attacker-controlled DEXCOST_ENDPOINT must never be honored.
        with mock.patch.dict(os.environ, {"DEXCOST_ENDPOINT": "http://evil.example"}):
            cfg = DexcostConfig(api_key="dx_live_abc")
            assert cfg.endpoint == _DEFAULT_ENDPOINT

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


class TestCatalogTrustPolicy:
    def test_no_configuration_requires_signatures_from_bundled_production_trust(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            trusted_keys, require_signature = resolve_catalog_trust_policy(None, None)
            assert set(trusted_keys) == {
                "catalog-prod-2026-08-a",
                "catalog-prod-2026-08-b",
            }
            assert require_signature is True
            assert catalog_unsigned_override_requested(None) is False

    def test_unsigned_catalog_refresh_requires_an_explicit_override(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert catalog_unsigned_override_requested(False) is True
        with mock.patch.dict(
            os.environ,
            {"DEXCOST_CATALOG_REQUIRE_SIGNATURE": "false"},
            clear=True,
        ):
            assert catalog_unsigned_override_requested(None) is True

    def test_environment_keys_require_signatures_by_default(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DEXCOST_CATALOG_TRUSTED_KEYS": (
                    '{"dexcost-prod-2026-01":"' + _CATALOG_PUBLIC_KEY + '"}'
                )
            },
            clear=True,
        ):
            assert resolve_catalog_trust_policy(None, None) == (
                {"dexcost-prod-2026-01": _CATALOG_PUBLIC_KEY},
                True,
            )

    def test_explicit_policy_overrides_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DEXCOST_CATALOG_TRUSTED_KEYS": "not-json",
                "DEXCOST_CATALOG_REQUIRE_SIGNATURE": "true",
            },
            clear=True,
        ):
            assert resolve_catalog_trust_policy({}, False) == ({}, False)

    def test_environment_can_explicitly_allow_unsigned_migration(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DEXCOST_CATALOG_TRUSTED_KEYS": (
                    '{"dexcost-prod-2026-01":"' + _CATALOG_PUBLIC_KEY + '"}'
                ),
                "DEXCOST_CATALOG_REQUIRE_SIGNATURE": "false",
            },
            clear=True,
        ):
            assert resolve_catalog_trust_policy(None, None)[1] is False

    @pytest.mark.parametrize(
        ("environment", "message"),
        [
            ({"DEXCOST_CATALOG_TRUSTED_KEYS": "not-json"}, "not valid JSON"),
            ({"DEXCOST_CATALOG_TRUSTED_KEYS": "{}"}, "1-8 public keys"),
            ({"DEXCOST_CATALOG_TRUSTED_KEYS": "[]"}, "1-8 public keys"),
            (
                {"DEXCOST_CATALOG_TRUSTED_KEYS": '{"BAD KEY":"' + _CATALOG_PUBLIC_KEY + '"}'},
                "key ID is invalid",
            ),
            (
                {"DEXCOST_CATALOG_TRUSTED_KEYS": '{"dexcost-prod":"AAAA"}'},
                "wrong byte length",
            ),
            ({"DEXCOST_CATALOG_REQUIRE_SIGNATURE": "1"}, "must be true or false"),
        ],
    )
    def test_invalid_environment_fails_closed(
        self,
        environment: dict[str, str],
        message: str,
    ) -> None:
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            pytest.raises(ValueError, match=message),
        ):
            resolve_catalog_trust_policy(None, None)

    def test_signature_requirement_with_explicit_empty_trust_fails_closed(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"DEXCOST_CATALOG_REQUIRE_SIGNATURE": "true"},
                clear=True,
            ),
            pytest.raises(ValueError, match="requires at least one"),
        ):
            resolve_catalog_trust_policy({}, None)
