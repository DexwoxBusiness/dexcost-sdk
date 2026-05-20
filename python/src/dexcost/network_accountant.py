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
    """Accumulates bytes in/out, call count, and a per-host breakdown.

    v2 adds the *external-bytes-out* split: when ``record(...,
    is_internal=True)`` is called, ``bytes_out`` is still added to the
    raw counters but is NOT added to ``external_bytes_out`` — the basis
    for cloud-egress pricing. ``is_internal=False`` and ``is_internal=None``
    both attribute the bytes as external (the ``None`` case — unresolved
    named host — is the common case and is conservatively billable).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes_in = 0
        self._bytes_out = 0
        self._external_bytes_out = 0
        self._call_count = 0
        # host -> [calls, bytes_in, bytes_out, external_bytes_out]
        self._hosts: dict[str, list[int]] = {}
        # Overflow bucket once LIVE_CAP distinct hosts are tracked.
        self._other = [0, 0, 0, 0]
        self._frozen = False

    def __copy__(self) -> "NetworkAccountant":
        # threading.Lock is not copyable; a copied task must start a fresh,
        # empty accountant — it must not share or inherit frozen state.
        return NetworkAccountant()

    def __deepcopy__(self, memo: dict) -> "NetworkAccountant":
        return NetworkAccountant()

    def record(
        self,
        host: str,
        bytes_in: int,
        bytes_out: int,
        is_internal: bool | None = None,
    ) -> None:
        """Add one HTTP call's bytes. No-op once finalized.

        ``is_internal`` follows the v1 §4.2 three-valued classification:
        - ``True``  → bytes are intra-VPC / loopback → 0 external bytes.
        - ``False`` → confirmed public IP → all of ``bytes_out`` are external.
        - ``None``  → unresolved named host → treated as external
                      (conservative — over-attribute rather than undercount).
        """
        bytes_in = max(0, bytes_in)
        bytes_out = max(0, bytes_out)
        external_out = 0 if is_internal is True else bytes_out
        with self._lock:
            if self._frozen:
                return
            self._bytes_in += bytes_in
            self._bytes_out += bytes_out
            self._external_bytes_out += external_out
            self._call_count += 1
            key = host or "_unknown"
            entry = self._hosts.get(key)
            if entry is not None:
                entry[0] += 1
                entry[1] += bytes_in
                entry[2] += bytes_out
                entry[3] += external_out
            elif len(self._hosts) < LIVE_CAP:
                self._hosts[key] = [1, bytes_in, bytes_out, external_out]
            else:
                self._other[0] += 1
                self._other[1] += bytes_in
                self._other[2] += bytes_out
                self._other[3] += external_out

    def live_host_count(self) -> int:
        """Number of distinct hosts currently tracked (excludes `_other`)."""
        with self._lock:
            return len(self._hosts)

    def finalize(self) -> dict[str, Any]:
        """Freeze the accountant and return the snapshot for the task fields.

        Returns ``{"bytes_in", "bytes_out", "external_bytes_out",
        "call_count", "by_host"}`` where ``by_host`` is
        ``{"hosts": [...]}`` — the top FINALIZE_CAP hosts by total bytes,
        plus an `_other` bucket summing the rest. Each host entry carries
        ``external_bytes_out`` so the v2 per-host egress cost survives the
        top-N cap.
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

            other = list(self._other)  # [calls, bytes_in, bytes_out, external_out]
            for _host, vals in overflow:
                for i in range(4):
                    other[i] += vals[i]

            # If a real host is literally named "_other" it would collide with
            # the synthetic overflow bucket.  Fold it into `other` so the
            # output list never contains two entries with the same host name.
            top_clean = []
            for item in top:
                if item[0] == "_other":
                    for i in range(4):
                        other[i] += item[1][i]
                else:
                    top_clean.append(item)

            hosts: list[dict[str, Any]] = [
                {
                    "host": host,
                    "calls": vals[0],
                    "bytes_in": vals[1],
                    "bytes_out": vals[2],
                    "external_bytes_out": vals[3],
                }
                for host, vals in top_clean
            ]
            if other[0] > 0:
                hosts.append(
                    {
                        "host": "_other",
                        "calls": other[0],
                        "bytes_in": other[1],
                        "bytes_out": other[2],
                        "external_bytes_out": other[3],
                    }
                )
            return {
                "bytes_in": self._bytes_in,
                "bytes_out": self._bytes_out,
                "external_bytes_out": self._external_bytes_out,
                "call_count": self._call_count,
                "by_host": {"hosts": hosts},
            }
