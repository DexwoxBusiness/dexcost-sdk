"""PII redaction and metadata policy (US-018).

Provides utilities for stripping sensitive fields from event details
and task metadata before storage or cloud push.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

_log = logging.getLogger(__name__)

_MAX_DETAILS_BYTES = 10 * 1024  # 10 KB per US-018


def redact_dict(data: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Recursively remove keys matching ``fields`` from a dict.

    Returns a new dict with matching keys stripped at all nesting levels.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key in fields:
            continue
        if isinstance(value, dict):
            result[key] = redact_dict(value, fields)
        else:
            result[key] = value
    return result


def hash_value(value: str) -> str:
    """Return SHA-256 hex digest of *value*."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def enforce_metadata_limit(details: dict[str, Any]) -> dict[str, Any]:
    """Truncate *details* dict if serialized size exceeds 10 KB.

    Returns the original dict if within limit, or a stub dict with
    ``_truncated=True`` and the original byte size.
    """
    try:
        serialized = json.dumps(details, default=str)
    except (TypeError, ValueError):
        return {"_truncated": True, "_error": "unserializable"}
    byte_size = len(serialized.encode("utf-8"))
    if byte_size <= _MAX_DETAILS_BYTES:
        return details
    _log.warning(
        "Event details dict exceeds 10KB limit (%d bytes), truncating",
        byte_size,
    )
    return {"_truncated": True, "_original_size_bytes": byte_size}
