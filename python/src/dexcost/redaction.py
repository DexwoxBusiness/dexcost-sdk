"""PII redaction and metadata policy (US-018).

Provides utilities for stripping sensitive fields from event details
and task metadata before storage or cloud push.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

_MAX_DETAILS_BYTES = 10 * 1024  # 10 KB per US-018

# Query parameter names that always strip. Compared case-insensitively.
# Canonical set shared by Go / TypeScript / Rust SDKs — keep in sync.
_SENSITIVE_QUERY_PARAMS = frozenset({
    "api_key", "apikey", "access_token", "token", "auth", "password",
    "secret", "signature", "x-amz-signature", "x-amz-credential",
    "x-amz-security-token", "session",
})

_USERINFO_RE = re.compile(r"^(https?://)([^@/?#]+@)?(.+)$")


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


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>`]+")


def scrub_urls_in_text(text: str) -> str:
    """Run scrub_url over every URL found in ``text``.

    Used to redact URLs embedded in free-form error messages, exception
    strings, and log lines before they are captured into ``details``.
    The URL matcher accepts ``http(s)://`` followed by any non-whitespace,
    non-quote, non-bracket character — broad enough to catch real URLs
    without breaking on punctuation that commonly delimits them in prose.
    """
    if not text:
        return text
    return _URL_IN_TEXT_RE.sub(lambda m: scrub_url(m.group(0)), text)


def scrub_url(url: str) -> str:
    """Strip credentials from a URL before it is captured into an event.

    Removes:
      - userinfo (``user:pass@``) from the authority
      - query parameters whose name (case-insensitive) is in the canonical
        sensitive set OR ends with ``-signature``, ``-credential``, or
        ``-security-token`` (AWS SigV4 surface)

    Preserves scheme, host, port, path, non-sensitive query params, and
    fragment. The shape of every removed query parameter is preserved as
    ``name=REDACTED`` so downstream callers can still see which keys were
    present without leaking the values.

    Canonical algorithm — Go/TS/Rust SDK implementations must produce
    byte-identical output for the same input (enforced by
    fixtures/expected_outputs/security/).
    """
    if not url:
        return url
    m = _USERINFO_RE.match(url)
    if m:
        url = m.group(1) + m.group(3)

    fragment = ""
    if "#" in url:
        url, fragment = url.split("#", 1)
        fragment = "#" + fragment
    if "?" not in url:
        return url + fragment
    base, query = url.split("?", 1)
    kept: list[str] = []
    for part in query.split("&"):
        if "=" in part:
            name, _ = part.split("=", 1)
        else:
            name = part
        lname = name.lower()
        sensitive = (
            lname in _SENSITIVE_QUERY_PARAMS
            or lname.endswith("-signature")
            or lname.endswith("-credential")
            or lname.endswith("-security-token")
        )
        if sensitive:
            kept.append(f"{name}=REDACTED")
        else:
            kept.append(part)
    return f"{base}?{'&'.join(kept)}{fragment}"
