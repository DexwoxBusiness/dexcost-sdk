"""Fargate ECS task metadata reader.

Hits ``${ECS_CONTAINER_METADATA_URI_V4}/task`` (or v3) once per process and
caches the parsed result. Exposes ``vcpu_count`` (float) and
``memory_bytes_limit`` (int — converted from MiB per Decision #7).

Fail-silent contract (convention §9): unreachable endpoint, malformed JSON,
missing fields all return ``None`` and log once via convention §11.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

_log = logging.getLogger(__name__)

_PROBE_TIMEOUT = 0.25  # seconds

_lock = threading.Lock()
_cached: "FargateTaskMetadata | None" = None
_resolved = False
_warned = False


def _reset_for_tests() -> None:
    """Clear cached state — test fixture helper per convention §11."""
    global _cached, _resolved, _warned
    with _lock:
        _cached = None
        _resolved = False
        _warned = False


@dataclass(frozen=True)
class FargateTaskMetadata:
    vcpu_count: float
    memory_bytes_limit: int


def _endpoint() -> str | None:
    base = (
        os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
        or os.environ.get("ECS_CONTAINER_METADATA_URI")
    )
    if not base:
        return None
    return base.rstrip("/") + "/task"


def fetch_fargate_metadata() -> FargateTaskMetadata | None:
    """Read + cache the ECS task metadata. Idempotent.

    Returns ``None`` when not on Fargate, when the endpoint is unreachable,
    or when the ``Limits`` block is missing / malformed.
    """
    global _cached, _resolved, _warned

    with _lock:
        if _resolved:
            return _cached

    url = _endpoint()
    if url is None:
        with _lock:
            _resolved = True
        return None

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError,
            json.JSONDecodeError) as exc:
        with _lock:
            _resolved = True
            if not _warned:
                _warned = True
                _log.warning(
                    "fargate metadata unreachable (%s); compute cost will "
                    "fall through to default rates", exc,
                )
        return None

    limits = payload.get("Limits", {}) or {}
    try:
        vcpu = float(limits["CPU"])
        mem_mib = int(limits["Memory"])
    except (KeyError, TypeError, ValueError):
        with _lock:
            _resolved = True
        return None

    # Decision #7 — Fargate memory is in MiB (binary), NOT MB. Convert to
    # bytes via the binary divisor (~4.86% silent over-attribution bug if
    # decimal MB is used by mistake).
    memory_bytes = mem_mib * 1024 * 1024

    result = FargateTaskMetadata(
        vcpu_count=vcpu, memory_bytes_limit=memory_bytes,
    )
    with _lock:
        _cached = result
        _resolved = True
    return result
