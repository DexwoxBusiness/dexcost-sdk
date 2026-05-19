"""NetworkAccountant — a per-task, in-process accumulator of HTTP byte usage.

One instance lives (un-serialised) on each Task. The HTTP adapter calls
``record()`` per call; ``finalize()`` is called once at task end. After
finalize the accountant is frozen — later ``record()`` calls are no-ops, so
late-arriving bytes never mutate already-shipped task aggregates.
"""

from __future__ import annotations

import threading
from typing import Any

# Hosts kept in the per-task `by_host` array after finalize (plus `_other`).
FINALIZE_CAP = 20
# Distinct hosts tracked live during the task before overflow folds into
# `_other` — bounds mid-task memory for pathological many-host workloads.
LIVE_CAP = 500


class NetworkAccountant:
    """Accumulates bytes in/out, call count, and a per-host breakdown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes_in = 0
        self._bytes_out = 0
        self._call_count = 0
        # host -> [calls, bytes_in, bytes_out]
        self._hosts: dict[str, list[int]] = {}
        # Overflow bucket once LIVE_CAP distinct hosts are tracked.
        self._other = [0, 0, 0]
        self._frozen = False

    def record(self, host: str, bytes_in: int, bytes_out: int) -> None:
        """Add one HTTP call's bytes. No-op once finalized."""
        with self._lock:
            if self._frozen:
                return
            self._bytes_in += bytes_in
            self._bytes_out += bytes_out
            self._call_count += 1
            key = host or "_unknown"
            entry = self._hosts.get(key)
            if entry is not None:
                entry[0] += 1
                entry[1] += bytes_in
                entry[2] += bytes_out
            elif len(self._hosts) < LIVE_CAP:
                self._hosts[key] = [1, bytes_in, bytes_out]
            else:
                self._other[0] += 1
                self._other[1] += bytes_in
                self._other[2] += bytes_out

    def live_host_count(self) -> int:
        """Number of distinct hosts currently tracked (excludes `_other`)."""
        with self._lock:
            return len(self._hosts)

    def finalize(self) -> dict[str, Any]:
        """Freeze the accountant and return the snapshot for the task fields.

        Returns ``{"bytes_in", "bytes_out", "call_count", "by_host"}`` where
        ``by_host`` is ``{"hosts": [...]}`` — the top FINALIZE_CAP hosts by
        total bytes, plus an `_other` bucket summing the rest.
        """
        with self._lock:
            self._frozen = True
            ranked = sorted(
                self._hosts.items(),
                key=lambda kv: kv[1][1] + kv[1][2],
                reverse=True,
            )
            top = ranked[:FINALIZE_CAP]
            overflow = ranked[FINALIZE_CAP:]

            other = list(self._other)  # copy: [calls, bytes_in, bytes_out]
            for _host, (calls, b_in, b_out) in overflow:
                other[0] += calls
                other[1] += b_in
                other[2] += b_out

            hosts: list[dict[str, Any]] = [
                {"host": host, "calls": c, "bytes_in": bi, "bytes_out": bo}
                for host, (c, bi, bo) in top
            ]
            if other[0] > 0:
                hosts.append(
                    {"host": "_other", "calls": other[0],
                     "bytes_in": other[1], "bytes_out": other[2]}
                )
            return {
                "bytes_in": self._bytes_in,
                "bytes_out": self._bytes_out,
                "call_count": self._call_count,
                "by_host": {"hosts": hosts},
            }
