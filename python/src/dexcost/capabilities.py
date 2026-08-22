"""Async-safe capability attribution context and durable event snapshots."""

from __future__ import annotations

import contextvars
import hashlib
import re
from collections.abc import Generator
from contextlib import contextmanager

from dexcost.models.capability import CapabilityIdentity
from dexcost.models.event import Event

_CAPABILITY_CONTEXT: contextvars.ContextVar[CapabilityIdentity | None] = (
    contextvars.ContextVar("dexcost_capability", default=None)
)
_DETAIL_KEY = "attribution_capability"
_NON_CANONICAL = re.compile(r"[^a-z0-9._-]+")


def get_capability() -> CapabilityIdentity | None:
    """Return the capability active in this sync or async context."""
    return _CAPABILITY_CONTEXT.get()


def set_capability(
    capability: CapabilityIdentity | None,
) -> contextvars.Token[CapabilityIdentity | None]:
    """Set or clear capability attribution and return a restoration token."""
    if capability is not None and not isinstance(capability, CapabilityIdentity):
        raise TypeError("capability must be a CapabilityIdentity or None")
    return _CAPABILITY_CONTEXT.set(capability)


@contextmanager
def capability_context(
    capability: CapabilityIdentity,
) -> Generator[CapabilityIdentity, None, None]:
    """Propagate one capability through nested sync and async SDK capture."""
    token = set_capability(capability)
    try:
        yield capability
    finally:
        _CAPABILITY_CONTEXT.reset(token)


def canonical_tool_capability_name(tool_id: str) -> str:
    """Create a stable canonical capability name without discarding uniqueness."""
    lowered = tool_id.lower().strip()
    normalized = _NON_CANONICAL.sub("-", lowered).lstrip("._-")
    if normalized and normalized == tool_id and len(normalized) <= 128:
        return normalized
    digest = hashlib.sha256(tool_id.encode("utf-8")).hexdigest()[:12]
    base = normalized[:115].rstrip("._-") or "tool"
    return f"{base}-{digest}"


def default_tool_capability(tool_id: str) -> CapabilityIdentity:
    """Return the direct capability identity used outside a richer context."""
    return CapabilityIdentity(
        name=canonical_tool_capability_name(tool_id),
        kind="tool",
        invocation="explicit",
    )


def apply_event_capability(
    event: Event,
    capability: CapabilityIdentity | None = None,
) -> Event:
    """Snapshot explicit, already-durable, or active capability onto an event."""
    resolved = capability
    if resolved is None:
        durable = event.details.get(_DETAIL_KEY)
        if durable is not None:
            if not isinstance(durable, dict):
                raise ValueError("attribution_capability must be a dictionary")
            resolved = CapabilityIdentity.from_dict(durable)
    if resolved is None:
        resolved = get_capability()
    if resolved is not None:
        event.details = {**event.details, _DETAIL_KEY: resolved.to_dict()}
    return event


__all__ = [
    "apply_event_capability",
    "canonical_tool_capability_name",
    "capability_context",
    "default_tool_capability",
    "get_capability",
    "set_capability",
]
