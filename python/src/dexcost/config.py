"""SDK configuration and API key infrastructure (US-017)."""

from __future__ import annotations

import json
import logging
import os
import re
from base64 import urlsafe_b64decode
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_ENDPOINT = "https://api.dexcost.io"
_log = logging.getLogger(__name__)
_CATALOG_KEY_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_CATALOG_TRUSTED_KEYS_ENV = "DEXCOST_CATALOG_TRUSTED_KEYS"
_CATALOG_REQUIRE_SIGNATURE_ENV = "DEXCOST_CATALOG_REQUIRE_SIGNATURE"
_CATALOG_PRODUCTION_TRUST_PATH = (
    Path(__file__).parent / "data" / "catalog_production_trust.json"
)


class InvalidAPIKeyError(ValueError):
    """Raised when an API key has an invalid format."""


def _validate_catalog_public_key(key_id: object, value: object) -> str | bytes:
    if not isinstance(key_id, str) or _CATALOG_KEY_ID.fullmatch(key_id) is None:
        raise ValueError("catalog trusted key ID is invalid")
    if isinstance(value, bytes):
        if len(value) != 32:
            raise ValueError(f"catalog trusted key {key_id} has the wrong byte length")
        return value
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError(f"catalog trusted key {key_id} is not unpadded base64url")
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"catalog trusted key {key_id} is not valid base64url") from exc
    if len(decoded) != 32:
        raise ValueError(f"catalog trusted key {key_id} has the wrong byte length")
    return value


def _catalog_keys_from_environment(encoded: str | None) -> dict[str, str | bytes]:
    if encoded is None:
        return {}
    if encoded.strip() == "":
        raise ValueError(f"{_CATALOG_TRUSTED_KEYS_ENV} must not be empty")
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{_CATALOG_TRUSTED_KEYS_ENV} is not valid JSON") from exc
    if not isinstance(value, dict) or not 1 <= len(value) <= 8:
        raise ValueError(f"{_CATALOG_TRUSTED_KEYS_ENV} must contain 1-8 public keys")
    return {
        key_id: str(_validate_catalog_public_key(key_id, public_key))
        for key_id, public_key in value.items()
    }


def _bundled_catalog_keys() -> dict[str, str | bytes]:
    try:
        value = json.loads(_CATALOG_PRODUCTION_TRUST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("bundled catalog production trust is unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "algorithm", "trusted_keys"}
        or value.get("schema_version") != "1"
        or value.get("algorithm") != "ed25519"
        or not isinstance(value.get("trusted_keys"), dict)
    ):
        raise ValueError("bundled catalog production trust has an invalid schema")
    trusted_keys = value["trusted_keys"]
    if len(trusted_keys) > 8:
        raise ValueError("bundled catalog production trust supports at most 8 public keys")
    return {
        key_id: _validate_catalog_public_key(key_id, public_key)
        for key_id, public_key in trusted_keys.items()
    }


def _catalog_signature_requirement_from_environment(encoded: str | None) -> bool | None:
    if encoded is None:
        return None
    normalized = encoded.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{_CATALOG_REQUIRE_SIGNATURE_ENV} must be true or false")


def resolve_catalog_trust_policy(
    trusted_keys: Mapping[str, str | bytes] | None,
    require_signature: bool | None,
) -> tuple[dict[str, str | bytes], bool]:
    """Resolve and validate the catalog trust policy before SDK startup.

    Explicit arguments take precedence over environment configuration. When
    trusted keys are supplied and no signature requirement is specified,
    signatures are required by default. Accepting unsigned releases while
    keys are configured therefore requires an explicit ``False`` migration
    override.
    """
    if trusted_keys is None:
        encoded_environment_keys = os.environ.get(_CATALOG_TRUSTED_KEYS_ENV)
        resolved_keys: dict[str, str | bytes] = (
            _bundled_catalog_keys()
            if encoded_environment_keys is None
            else _catalog_keys_from_environment(encoded_environment_keys)
        )
    else:
        if not isinstance(trusted_keys, Mapping):
            raise TypeError("catalog_trusted_keys must be a mapping")
        if len(trusted_keys) > 8:
            raise ValueError("catalog_trusted_keys supports at most 8 public keys")
        resolved_keys = {
            key_id: _validate_catalog_public_key(key_id, public_key)
            for key_id, public_key in trusted_keys.items()
        }

    if require_signature is None:
        env_requirement = _catalog_signature_requirement_from_environment(
            os.environ.get(_CATALOG_REQUIRE_SIGNATURE_ENV)
        )
        resolved_requirement = (
            bool(resolved_keys) if env_requirement is None else env_requirement
        )
    elif isinstance(require_signature, bool):
        resolved_requirement = require_signature
    else:
        raise TypeError("catalog_require_signature must be a bool or None")

    if resolved_requirement and not resolved_keys:
        raise ValueError("catalog signature verification requires at least one trusted public key")
    return resolved_keys, resolved_requirement


def validate_api_key(key: str | None) -> str | None:
    """Validate API key format.

    Returns ``'live'``, ``'test'``, or ``None`` if *key* is ``None``.
    Raises :class:`InvalidAPIKeyError` for invalid formats.
    """
    if key is None:
        return None
    if key.startswith("dx_live_"):
        return "live"
    if key.startswith("dx_test_"):
        return "test"
    raise InvalidAPIKeyError(
        f"Invalid API key format: key must start with 'dx_live_' or 'dx_test_', "
        f"got '{key[:10]}...'"
    )


@dataclass
class DexcostConfig:
    """Global SDK configuration.

    Resolves API key from explicit arg -> ``DEXCOST_API_KEY`` env var.
    ``storage="local"`` forces local-only mode regardless of key.
    """

    api_key: str | None = None
    storage: str | None = None  # "local" or None (auto-detect)
    endpoint_override: str | None = None  # explicit, in-code Control Layer URL
    batch_size: int = 100
    flush_interval_seconds: float = 5.0
    buffer_path: str | None = None
    # PII fields (US-018 will populate these)
    redact_fields: list[str] = field(default_factory=list)
    hash_customer_id: bool = False
    environment: str | None = None
    # Network capture (spec: 2026-05-19-network-capture-design)
    track_network: bool = True
    network_event_threshold_bytes: int = 102_400  # 100 KiB; combined req+resp
    network_event_on_error: bool = True
    network_event_latency_ms: int = 0  # 0 = latency trigger disabled

    _key_type: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.api_key is None and self.storage != "local":
            self.api_key = os.environ.get("DEXCOST_API_KEY")
        if self.environment is None:
            self.environment = os.environ.get("DEXCOST_ENV")
        self._key_type = validate_api_key(self.api_key)

    @property
    def storage_mode(self) -> str:
        """Return ``'local'`` or ``'cloud'`` based on configuration."""
        if self.storage == "local":
            return "local"
        if self.api_key is not None:
            return "cloud"
        return "local"

    @property
    def key_type(self) -> str | None:
        """Return ``'live'``, ``'test'``, or ``None``."""
        return self._key_type

    @property
    def is_sandbox(self) -> bool:
        """Return ``True`` when using a test/sandbox API key."""
        return self._key_type == "test"

    @property
    def endpoint(self) -> str:
        """Control Layer endpoint. Resolved ONLY from explicit, in-code
        configuration (``init(endpoint=...)`` / ``DexcostConfig
        (endpoint_override=...)``), defaulting to ``_DEFAULT_ENDPOINT``.

        The endpoint is intentionally NOT read from the process
        environment: an attacker who controls the env (misconfigured CI
        runner, hostile container) could otherwise set
        ``DEXCOST_ENDPOINT=http://attacker/`` and silently exfiltrate
        cost telemetry plus the Bearer API key. With no env read, that
        vector is closed.

        Validation is minimal because the value is developer-supplied
        and trusted: if it does not start with ``http://`` or
        ``https://`` we log a warning and fall back to the production
        default. ``http://`` is intentionally accepted (e.g.
        ``http://localhost`` for e2e) — safe precisely because it is not
        env-controllable.
        """
        override = self.endpoint_override
        if not override:
            return _DEFAULT_ENDPOINT
        if not override.startswith(("http://", "https://")):
            _log.warning(
                "dexcost: endpoint=%r rejected — must start with http:// "
                "or https://. Falling back to %s.",
                override, _DEFAULT_ENDPOINT,
            )
            return _DEFAULT_ENDPOINT
        return override

    @property
    def is_dev(self) -> bool:
        """Return True in development mode."""
        return self.environment == "development"
