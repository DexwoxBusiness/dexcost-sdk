"""Caller-controlled, privacy-safe idempotency for durable event capture."""

from __future__ import annotations

import contextvars
import hashlib
import json
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from dexcost.models.event import Event

_EVENT_NAMESPACE = uuid.UUID("ee9858ce-fc4e-5c97-a803-2ea9df316d5c")
_KEY_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dexcost_idempotency_key", default=None
)
_HASH_DETAIL = "_dexcost_idempotency_sha256"


def _validate_key(key: str) -> str:
    if not isinstance(key, str):
        raise TypeError("idempotency key must be a string")
    if not 1 <= len(key) <= 255:
        raise ValueError("idempotency key must contain 1 to 255 characters")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in key):
        raise ValueError("idempotency key must contain visible ASCII characters only")
    return key


def get_idempotency_key() -> str | None:
    """Return the caller key active in this context, if any."""
    return _KEY_CONTEXT.get()


def set_idempotency_key(
    key: str | None,
) -> contextvars.Token[str | None]:
    """Set or clear the caller key and return a token for precise restoration."""
    return _KEY_CONTEXT.set(None if key is None else _validate_key(key))


@contextmanager
def idempotency_key(key: str) -> Generator[None, None, None]:
    """Scope one stable caller key to captured operations."""
    token = set_idempotency_key(key)
    try:
        yield
    finally:
        _KEY_CONTEXT.reset(token)


def _event_identity(event: Event) -> str:
    details = event.details
    identity: dict[str, Any] = {
        "event_type": event.event_type,
        "provider": event.provider,
        "model": event.model,
        "service_name": event.service_name,
        "operation_name": details.get("attribution_operation_name"),
        "resource_type": details.get("attribution_resource_type"),
        "resource_id": details.get("attribution_resource_id"),
        "capability": details.get("attribution_capability"),
    }
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def apply_event_idempotency(event: Event, key: str | None = None) -> Event:
    """Stamp a deterministic opaque event ID from the active caller key."""
    resolved_key = get_idempotency_key() if key is None else _validate_key(key)
    if resolved_key is None:
        return event
    key_hash = hashlib.sha256(resolved_key.encode("ascii")).hexdigest()
    original_event_id = str(event.event_id)
    event.event_id = uuid.uuid5(
        _EVENT_NAMESPACE,
        "\0".join((str(event.task_id), key_hash, _event_identity(event))),
    )
    event.details = {**event.details, _HASH_DETAIL: key_hash}
    for identity_key in ("attribution_operation_id", "attribution_attempt_id"):
        if event.details.get(identity_key) == original_event_id:
            event.details[identity_key] = str(event.event_id)
    return event


def idempotency_hash(event: Event) -> str | None:
    """Return the opaque key hash stamped on an event, if valid."""
    value = event.details.get(_HASH_DETAIL)
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        return value
    return None


def equivalent_idempotent_event(left: Event, right: Event) -> bool:
    """Compare repeated capture bodies while ignoring their new wall timestamp."""
    if idempotency_hash(left) is None or idempotency_hash(left) != idempotency_hash(right):
        return False
    left_dict = left.to_dict()
    right_dict = right.to_dict()
    left_dict.pop("occurred_at", None)
    right_dict.pop("occurred_at", None)
    return bool(left_dict == right_dict)


__all__ = [
    "apply_event_idempotency",
    "equivalent_idempotent_event",
    "get_idempotency_key",
    "idempotency_hash",
    "idempotency_key",
    "set_idempotency_key",
]
