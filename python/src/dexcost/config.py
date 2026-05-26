"""SDK configuration and API key infrastructure (US-017)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field


_DEFAULT_ENDPOINT = "https://api.dexcost.io"
_log = logging.getLogger(__name__)


class InvalidAPIKeyError(ValueError):
    """Raised when an API key has an invalid format."""


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
        """Control Layer endpoint. Hardcoded default, overridable via
        DEXCOST_ENDPOINT env var. Sprint 1 Theme A / §2.1 (A2): only
        ``https://`` URLs are accepted. An attacker who controls the
        env (misconfigured CI runner, hostile container) could
        otherwise silently exfiltrate cost telemetry to an HTTP
        collector — we refuse and fall back to the production default.
        """
        env_value = os.environ.get("DEXCOST_ENDPOINT")
        if env_value is None:
            return _DEFAULT_ENDPOINT
        if not env_value.startswith("https://"):
            _log.warning(
                "dexcost: DEXCOST_ENDPOINT=%r rejected — only https:// "
                "URLs are accepted. Falling back to %s.",
                env_value, _DEFAULT_ENDPOINT,
            )
            return _DEFAULT_ENDPOINT
        return env_value

    @property
    def is_dev(self) -> bool:
        """Return True in development mode."""
        return self.environment == "development"
