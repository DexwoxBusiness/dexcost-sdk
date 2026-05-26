"""Canonical wire-format helpers for the Dexcost Standard Event Schema.

Centralises the timestamp serialisation contract so all SDKs (Python,
Go, TS, Rust) can target byte-identical output. Per plan §4.1.1 (P1):

    occurred_at / started_at / ended_at  →
      RFC3339, microsecond precision (6 fractional digits), "Z" suffix

  e.g. "2026-04-04T10:00:00.123456Z"

Python's `datetime.isoformat()` emits "+00:00" rather than "Z" and
drops the fractional component when microseconds are zero — neither
matches the canonical, so use the helper below at every ``to_dict``
boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso_canonical(dt: datetime) -> str:
    """Serialise a datetime to the canonical RFC3339 microsecond-Z form."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{dt.microsecond:06d}Z"
