"""Caller-controlled, privacy-safe idempotency for durable event capture."""

from __future__ import annotations

import contextvars
import hashlib
import json
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from dexcost.models.event import Event

_EVENT_NAMESPACE = uuid.UUID("ee9858ce-fc4e-5c97-a803-2ea9df316d5c")


@dataclass
class _IdempotencyScope:
    key: str
    next_occurrence: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def capture(self) -> CapturedIdempotencyKey:
        with self.lock:
            occurrence = self.next_occurrence
            self.next_occurrence += 1
        return CapturedIdempotencyKey(self.key, occurrence)


@dataclass(frozen=True)
class CapturedIdempotencyKey:
    """One stable occurrence reserved from an ambient caller-key scope."""

    key: str
    occurrence: int


IdempotencyKey = str | CapturedIdempotencyKey


_KEY_CONTEXT: contextvars.ContextVar[_IdempotencyScope | None] = contextvars.ContextVar(
    "dexcost_idempotency_key", default=None
)
_HASH_DETAIL = "_dexcost_idempotency_sha256"
_OCCURRENCE_DETAIL = "_dexcost_idempotency_occurrence"


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
    scope = _KEY_CONTEXT.get()
    return None if scope is None else scope.key


def capture_idempotency_key() -> CapturedIdempotencyKey | None:
    """Reserve one deterministic operation occurrence from the active scope."""
    scope = _KEY_CONTEXT.get()
    return None if scope is None else scope.capture()


def set_idempotency_key(
    key: str | None,
) -> contextvars.Token[Any]:
    """Set or clear the caller key and return a token for precise restoration."""
    return _KEY_CONTEXT.set(None if key is None else _IdempotencyScope(_validate_key(key)))


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


def apply_event_idempotency(event: Event, key: IdempotencyKey | None = None) -> Event:
    """Stamp a deterministic opaque event ID from the active caller key."""
    # Storage backends defensively apply the policy again. Preserve a stamp
    # already reserved by the operation wrapper instead of consuming another
    # occurrence from the ambient scope.
    if idempotency_hash(event) is not None:
        return event
    captured = capture_idempotency_key() if key is None else key
    if captured is None:
        return event
    if isinstance(captured, CapturedIdempotencyKey):
        resolved_key = _validate_key(captured.key)
        occurrence: int | None = captured.occurrence
    else:
        resolved_key = _validate_key(captured)
        occurrence = None
    key_hash = hashlib.sha256(resolved_key.encode("ascii")).hexdigest()
    original_event_id = str(event.event_id)
    identity_parts = [str(event.task_id), key_hash, _event_identity(event)]
    if occurrence is not None:
        identity_parts.insert(2, str(occurrence))
    event.event_id = uuid.uuid5(
        _EVENT_NAMESPACE,
        "\0".join(identity_parts),
    )
    event.details = {
        **event.details,
        _HASH_DETAIL: key_hash,
        **({_OCCURRENCE_DETAIL: occurrence} if occurrence is not None else {}),
    }
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
    "CapturedIdempotencyKey",
    "IdempotencyKey",
    "apply_event_idempotency",
    "capture_idempotency_key",
    "equivalent_idempotent_event",
    "get_idempotency_key",
    "idempotency_hash",
    "idempotency_key",
    "set_idempotency_key",
]
