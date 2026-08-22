"""Timing-safe DexCost webhook verification with replay protection."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Sequence
from typing import TypeAlias, cast

WebhookSecret: TypeAlias = str | bytes
WebhookHeader: TypeAlias = str | Sequence[str]

_MAX_SECRETS = 8
_MAX_SIGNATURES = 16


class WebhookVerificationError(ValueError):
    """A webhook did not satisfy the signature and freshness contract."""


def _payload_bytes(payload: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("webhook payload must be the unmodified raw bytes")
    return bytes(payload)


def _secret_bytes(secrets: WebhookSecret | Sequence[WebhookSecret]) -> tuple[bytes, ...]:
    raw_secrets: Sequence[WebhookSecret]
    if isinstance(secrets, (str, bytes)):
        raw_secrets = (secrets,)
    elif isinstance(secrets, Sequence):
        raw_secrets = cast("Sequence[WebhookSecret]", secrets)
    else:
        raise TypeError("webhook secrets must be a secret or sequence of secrets")
    if not 1 <= len(raw_secrets) <= _MAX_SECRETS:
        raise ValueError(f"webhook verification supports 1 to {_MAX_SECRETS} secrets")
    encoded: list[bytes] = []
    for secret in raw_secrets:
        value = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not isinstance(value, bytes) or not 1 <= len(value) <= 1024:
            raise ValueError("each webhook secret must contain 1 to 1024 bytes")
        encoded.append(value)
    return tuple(encoded)


def _header_entries(header: WebhookHeader) -> tuple[str, ...]:
    values = (header,) if isinstance(header, str) else header
    if not isinstance(values, Sequence):
        raise TypeError("webhook signature header must be a string or sequence")
    entries: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("webhook signature header values must be strings")
        entries.extend(part.strip() for part in value.split(",") if part.strip())
        if len(entries) > _MAX_SIGNATURES:
            raise ValueError(f"webhook verification supports at most {_MAX_SIGNATURES} signatures")
    return tuple(entries)


def _hex_signatures(header: WebhookHeader, *, versioned: bool) -> tuple[str, ...]:
    signatures: list[str] = []
    for entry in _header_entries(header):
        if "=" in entry:
            scheme, digest = entry.split("=", 1)
            accepted = scheme.lower() == ("v1" if versioned else "sha256")
            if not accepted:
                continue
        elif versioned:
            continue
        else:
            digest = entry
        normalized = digest.lower()
        if len(normalized) == 64 and all(
            character in "0123456789abcdef" for character in normalized
        ):
            signatures.append(normalized)
    return tuple(signatures)


def _timestamp(value: str, tolerance_seconds: float, now: float | None) -> str:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdigit():
        raise ValueError("webhook timestamp must be unix seconds")
    if (
        isinstance(tolerance_seconds, bool)
        or not isinstance(tolerance_seconds, (int, float))
        or not 0 <= tolerance_seconds <= 86_400
    ):
        raise ValueError("webhook tolerance_seconds must be between 0 and 86400")
    signed_at = int(value)
    current = time.time() if now is None else now
    if not isinstance(current, (int, float)) or isinstance(current, bool):
        raise TypeError("webhook now must be unix seconds")
    if abs(float(current) - signed_at) > float(tolerance_seconds):
        raise ValueError("webhook timestamp is outside the accepted tolerance")
    return value


def _matches(message: bytes, signatures: tuple[str, ...], secrets: tuple[bytes, ...]) -> bool:
    if not signatures:
        return False
    for secret in secrets:
        expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
        for received in signatures:
            if hmac.compare_digest(expected, received):
                return True
    return False


def verify_webhook_signature(
    payload: bytes | bytearray | memoryview,
    signature_header: WebhookHeader,
    *,
    timestamp_header: str | None,
    secrets: WebhookSecret | Sequence[WebhookSecret],
    tolerance_seconds: float = 300,
    now: float | None = None,
    allow_legacy: bool = False,
) -> bool:
    """Return whether a raw payload has a fresh valid DexCost signature.

    New deliveries use ``X-Dexcost-Signature-V1`` plus
    ``X-Dexcost-Webhook-Timestamp``. Body-only legacy signatures are accepted
    only when ``allow_legacy=True`` because they cannot prevent replay.
    Malformed or adversarial input returns ``False`` and never raises.
    """
    try:
        raw_payload = _payload_bytes(payload)
        resolved_secrets = _secret_bytes(secrets)
        if timestamp_header is not None:
            timestamp = _timestamp(timestamp_header, tolerance_seconds, now)
            signatures = _hex_signatures(signature_header, versioned=True)
            return _matches(
                timestamp.encode("ascii") + b"." + raw_payload,
                signatures,
                resolved_secrets,
            )
        if not allow_legacy:
            return False
        signatures = _hex_signatures(signature_header, versioned=False)
        return _matches(raw_payload, signatures, resolved_secrets)
    except (TypeError, ValueError, OverflowError):
        return False


def assert_webhook_signature(
    payload: bytes | bytearray | memoryview,
    signature_header: WebhookHeader,
    *,
    timestamp_header: str | None,
    secrets: WebhookSecret | Sequence[WebhookSecret],
    tolerance_seconds: float = 300,
    now: float | None = None,
    allow_legacy: bool = False,
) -> None:
    """Raise one non-oracular error when webhook verification fails."""
    if not verify_webhook_signature(
        payload,
        signature_header,
        timestamp_header=timestamp_header,
        secrets=secrets,
        tolerance_seconds=tolerance_seconds,
        now=now,
        allow_legacy=allow_legacy,
    ):
        raise WebhookVerificationError("invalid or stale DexCost webhook signature")


__all__ = [
    "WebhookHeader",
    "WebhookSecret",
    "WebhookVerificationError",
    "assert_webhook_signature",
    "verify_webhook_signature",
]
