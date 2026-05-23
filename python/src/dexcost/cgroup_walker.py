"""Cgroup-scope classifier — implements Phase 2 Decision #1.

Reads ``/proc/self/cgroup`` and classifies the cgroup scope by prefix
into one of:

- ``"container"`` — kubepods.slice / docker / containerd / crio / etc.
  The dexcost-task's cgroup IS the right scope to walk: `cgroup.procs`
  enumerates exactly the container's PIDs.
- ``"bare_metal_user_slice"`` — `/user.slice/...` (systemd user session).
  Walking this would capture every PID in the SSH/login session, not
  just dexcost's task. Degrade to self-PID-only at ``estimated``
  confidence with ``pricing_source: :no_container_scope``.
- ``"root_cgroup"`` — `/` (privileged single-tenant host).
  Ambiguous; degrade to self-PID-only.
- ``"cgroup_v1"`` — multi-line file (multiple controllers).
  v1.1 will walk; v1 degrades to self-PID-only.
- ``"unknown"`` — anything else.

The verification matrix at ``docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/``
is the empirical confirmation of this classification table.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

_log = logging.getLogger(__name__)

_PROC_SELF_CGROUP = "/proc/self/cgroup"
_CGROUP_ROOT = "/sys/fs/cgroup"

_warned_modes: set[str] = set()
_warn_lock = threading.Lock()


def _reset_warning_state() -> None:
    """Test-only: clear the warn-once tracking set."""
    with _warn_lock:
        _warned_modes.clear()


def _warn_once(mode: str, message: str) -> None:
    with _warn_lock:
        if mode in _warned_modes:
            return
        _warned_modes.add(mode)
    _log.warning(message)


# ─── Decision #1 prefix table (in classification priority order) ────────────

_CONTAINER_PREFIXES: tuple[str, ...] = (
    "/kubepods.slice/",          # modern K8s with systemd cgroup driver
    "/kubepods/",                # legacy K8s with cgroupfs driver
    "/docker/",
    "/system.slice/docker-",
    "/containerd/",
    "/system.slice/containerd-",
    "/crio/",
    "/system.slice/crio-",
)

_BARE_METAL_PREFIXES: tuple[str, ...] = (
    "/user.slice/",
)


# ─── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CgroupScope:
    """Classified cgroup scope.

    ``kind`` is one of: ``"container"``, ``"bare_metal_user_slice"``,
    ``"root_cgroup"``, ``"cgroup_v1"``, ``"unknown"``.
    ``path`` is the cgroup-v2 unified path for ``container`` scope; ``None``
    for every other kind.
    """

    kind: str
    path: str | None


# ─── Classification ──────────────────────────────────────────────────────────


def classify_scope() -> CgroupScope:
    """Read ``/proc/self/cgroup`` and classify per Decision #1's table."""
    try:
        raw = _read_proc_self_cgroup()
    except OSError:
        return CgroupScope(kind="unknown", path=None)

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return CgroupScope(kind="unknown", path=None)

    # cgroup v1 → multiple controller lines (e.g. "12:devices:/docker/abc")
    # cgroup v2 → single line "0::/path"
    if len(lines) > 1 or not lines[0].startswith("0::"):
        return CgroupScope(kind="cgroup_v1", path=None)

    path = lines[0][3:]  # strip "0::" prefix

    if path == "/" or path == "":
        return CgroupScope(kind="root_cgroup", path=None)

    for prefix in _CONTAINER_PREFIXES:
        if path.startswith(prefix):
            return CgroupScope(kind="container", path=path)

    for prefix in _BARE_METAL_PREFIXES:
        if path.startswith(prefix):
            return CgroupScope(kind="bare_metal_user_slice", path=None)

    return CgroupScope(kind="unknown", path=None)


def _read_proc_self_cgroup() -> str:
    with open(_PROC_SELF_CGROUP, encoding="utf-8") as f:
        return f.read()


# ─── PID enumeration ─────────────────────────────────────────────────────────


def enumerate_pids(scope: CgroupScope) -> list[int] | None:
    """Return the PID set to attribute GPU usage to.

    For ``container`` scope: walks the resolved cgroup's ``cgroup.procs``.
    Returns ``None`` (not an empty list) on read failure — signals the
    caller to log-once ``gpu_cgroup_walk_forbidden`` and fall back.

    For every non-container scope: returns ``[os.getpid()]`` — the
    self-PID-only degradation. This is what makes bare-metal-no-container
    NOT silently overcount: we deliberately don't walk the systemd user
    slice (which would capture unrelated user PIDs).
    """
    if scope.kind != "container" or scope.path is None:
        return [os.getpid()]

    cgroup_procs_path = os.path.join(_CGROUP_ROOT + scope.path, "cgroup.procs")
    try:
        with open(cgroup_procs_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        _warn_once(
            "gpu_cgroup_walk_forbidden",
            f"Could not read {cgroup_procs_path} ({exc}); "
            "GpuAccountant will degrade to self-PID-only",
        )
        return None

    pids: list[int] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


# ─── Decision #1 confidence labelling ────────────────────────────────────────


def fallback_label_for(scope: CgroupScope) -> str | None:
    """Return the pricing_source suffix for this scope, or None if no fallback.

    - ``container`` → no fallback label (full-fidelity attribution)
    - ``bare_metal_user_slice`` / ``root_cgroup`` → ``"no_container_scope"``
    - ``cgroup_v1`` / ``unknown`` → ``"self_pid_only"``
    """
    if scope.kind == "container":
        return None
    if scope.kind in {"bare_metal_user_slice", "root_cgroup"}:
        return "no_container_scope"
    return "self_pid_only"
