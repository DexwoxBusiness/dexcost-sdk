"""Replay-resistant DexCost webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from dexcost.webhooks import (
    WebhookVerificationError,
    assert_webhook_signature,
    verify_webhook_signature,
)

_NOW = 1_700_000_000
_PAYLOAD = b'{"event":"statement.generated","id":"evt_42"}'


def _v1(secret: str, timestamp: str = str(_NOW), payload: bytes = _PAYLOAD) -> str:
    digest = hmac.new(
        secret.encode(),
        timestamp.encode("ascii") + b"." + payload,
        hashlib.sha256,
    ).hexdigest()
    return f"v1={digest}"


def _legacy(secret: str, payload: bytes = _PAYLOAD) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_versioned_signature_verifies_raw_body_and_timestamp() -> None:
    assert verify_webhook_signature(
        _PAYLOAD,
        _v1("secret"),
        timestamp_header=str(_NOW),
        secrets="secret",
        now=_NOW,
    )


def test_secret_rotation_and_multi_value_headers_are_supported() -> None:
    wrong = _v1("wrong")
    correct = _v1("new-secret").upper().replace("V1=", "v1=")
    assert verify_webhook_signature(
        _PAYLOAD,
        [wrong, f"unrelated=abc, {correct}"],
        timestamp_header=str(_NOW),
        secrets=["old-secret", "new-secret"],
        now=_NOW,
    )


@pytest.mark.parametrize("offset", [-301, 301])
def test_stale_or_too_far_future_timestamp_is_rejected(offset: int) -> None:
    timestamp = str(_NOW + offset)
    assert not verify_webhook_signature(
        _PAYLOAD,
        _v1("secret", timestamp),
        timestamp_header=timestamp,
        secrets="secret",
        now=_NOW,
    )


def test_tampering_and_wrong_secret_are_rejected() -> None:
    assert not verify_webhook_signature(
        _PAYLOAD + b" ",
        _v1("secret"),
        timestamp_header=str(_NOW),
        secrets="secret",
        now=_NOW,
    )
    assert not verify_webhook_signature(
        _PAYLOAD,
        _v1("secret"),
        timestamp_header=str(_NOW),
        secrets="wrong",
        now=_NOW,
    )


@pytest.mark.parametrize(
    ("payload", "signature", "timestamp", "secrets"),
    [
        ("not-bytes", "v1=abc", str(_NOW), ["secret"]),
        (_PAYLOAD, "v1=xyz", str(_NOW), ["secret"]),
        (_PAYLOAD, _v1("secret"), "1.5", ["secret"]),
        (_PAYLOAD, _v1("secret"), str(_NOW), []),
        (_PAYLOAD, _v1("secret"), str(_NOW), [""]),
        (_PAYLOAD, ",".join([_v1("secret")] * 17), str(_NOW), ["secret"]),
    ],
)
def test_malformed_adversarial_inputs_return_false_without_raising(
    payload: object,
    signature: str,
    timestamp: str,
    secrets: list[str],
) -> None:
    assert not verify_webhook_signature(  # type: ignore[arg-type]
        payload,
        signature,
        timestamp_header=timestamp,
        secrets=secrets,
        now=_NOW,
    )


def test_legacy_body_only_signature_requires_explicit_opt_in() -> None:
    signature = _legacy("secret")
    assert not verify_webhook_signature(
        _PAYLOAD,
        signature,
        timestamp_header=None,
        secrets="secret",
    )
    assert verify_webhook_signature(
        _PAYLOAD,
        signature,
        timestamp_header=None,
        secrets="secret",
        allow_legacy=True,
    )
    assert verify_webhook_signature(
        _PAYLOAD,
        f"sha256={signature}",
        timestamp_header=None,
        secrets="secret",
        allow_legacy=True,
    )


def test_assert_helper_raises_one_generic_error() -> None:
    with pytest.raises(
        WebhookVerificationError,
        match="invalid or stale DexCost webhook signature",
    ):
        assert_webhook_signature(
            _PAYLOAD,
            _v1("wrong"),
            timestamp_header=str(_NOW),
            secrets="secret",
            now=_NOW,
        )
