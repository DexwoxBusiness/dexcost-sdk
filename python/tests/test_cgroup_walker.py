"""Cgroup-scope classifier — Decision #1 of Phase 2 GPU foundation.

The classification table (kubepods.slice/docker/containerd/crio →
container; user.slice → bare_metal_user_slice; / → root_cgroup;
multi-line → cgroup_v1) is the load-bearing detail. Walking the wrong
cgroup level silently overcounts (bare-metal hits the systemd user
slice) or silently undercounts (multi-container K8s misses sidecar PIDs).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def _writable_proc(monkeypatch, tmp_path):
    """Override _PROC_SELF_CGROUP to a writable tmp file. Returns a setter."""
    from dexcost import cgroup_walker
    proc = tmp_path / "cgroup"
    monkeypatch.setattr(cgroup_walker, "_PROC_SELF_CGROUP", str(proc))

    def _set(content: str) -> Path:
        proc.write_text(content)
        return proc

    return _set


@pytest.fixture(autouse=True)
def _reset_walker():
    from dexcost import cgroup_walker
    cgroup_walker._reset_warning_state()


# ─── Classification table (Decision #1 verification gate) ──────────────────

@pytest.mark.parametrize("cgroup_content,expected_kind", [
    # cgroup v2 — single line `0::/<path>`
    ("0::/docker/abc123\n",                                          "container"),
    ("0::/kubepods.slice/kubepods-burstable.slice/foo.scope\n",      "container"),
    ("0::/kubepods/burstable/podabc/abc\n",                          "container"),   # legacy K8s
    ("0::/system.slice/docker-abc.scope\n",                          "container"),
    ("0::/system.slice/containerd-abc.scope\n",                      "container"),
    ("0::/system.slice/crio-abc.scope\n",                            "container"),
    ("0::/containerd/abc\n",                                         "container"),
    ("0::/crio/abc\n",                                               "container"),
    # bare-metal — systemd user slice (the silent-overcount case)
    ("0::/user.slice/user-1000.slice/session-2.scope\n",             "bare_metal_user_slice"),
    ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/unit.service\n",
                                                                     "bare_metal_user_slice"),
    # root cgroup — ambiguous (privileged single-tenant host)
    ("0::/\n",                                                       "root_cgroup"),
    # unknown prefix — degrade to self-PID-only
    ("0::/some/unknown/path\n",                                      "unknown"),
])
def test_classify_scope_table(_writable_proc, cgroup_content, expected_kind):
    from dexcost.cgroup_walker import classify_scope
    _writable_proc(cgroup_content)
    scope = classify_scope()
    assert scope.kind == expected_kind


def test_classify_scope_detects_cgroup_v1(_writable_proc):
    """Multi-line cgroup file → v1 layout (multiple controllers)."""
    from dexcost.cgroup_walker import classify_scope
    _writable_proc(
        "12:devices:/docker/abc\n"
        "11:cpuset:/docker/abc\n"
        "10:memory:/docker/abc\n"
    )
    scope = classify_scope()
    assert scope.kind == "cgroup_v1"


def test_classify_scope_missing_file_returns_unknown(monkeypatch):
    from dexcost import cgroup_walker
    monkeypatch.setattr(cgroup_walker, "_PROC_SELF_CGROUP", "/nonexistent/path")
    scope = cgroup_walker.classify_scope()
    assert scope.kind == "unknown"
    assert scope.path is None


def test_container_scope_carries_resolved_path(_writable_proc):
    """For container scope, scope.path holds the unified cgroup path."""
    from dexcost.cgroup_walker import classify_scope
    _writable_proc("0::/kubepods.slice/kubepods-burstable.slice/foo.scope\n")
    scope = classify_scope()
    assert scope.path == "/kubepods.slice/kubepods-burstable.slice/foo.scope"


# ─── enumerate_pids: container walks; non-container returns self-PID only ──

def test_enumerate_pids_container_walks_cgroup_procs(
    _writable_proc, tmp_path, monkeypatch,
):
    """Container scope reads /sys/fs/cgroup/<path>/cgroup.procs."""
    from dexcost import cgroup_walker
    _writable_proc("0::/docker/abc123\n")
    # Stage the cgroup.procs file the walker should read.
    procs_dir = tmp_path / "fake_cgroup_root" / "docker" / "abc123"
    procs_dir.mkdir(parents=True)
    (procs_dir / "cgroup.procs").write_text("1234\n5678\n9012\n")
    monkeypatch.setattr(cgroup_walker, "_CGROUP_ROOT", str(tmp_path / "fake_cgroup_root"))

    scope = cgroup_walker.classify_scope()
    pids = cgroup_walker.enumerate_pids(scope)
    assert pids == [1234, 5678, 9012]


def test_enumerate_pids_bare_metal_returns_self_pid_only(_writable_proc):
    """bare_metal_user_slice → don't walk; return [self_pid]. Silent-overcount guard."""
    from dexcost.cgroup_walker import classify_scope, enumerate_pids
    _writable_proc("0::/user.slice/user-1000.slice/session-2.scope\n")
    scope = classify_scope()
    pids = enumerate_pids(scope)
    assert pids == [os.getpid()]


def test_enumerate_pids_root_cgroup_returns_self_pid_only(_writable_proc):
    """root_cgroup scope is ambiguous — degrade to self-PID-only."""
    from dexcost.cgroup_walker import classify_scope, enumerate_pids
    _writable_proc("0::/\n")
    scope = classify_scope()
    assert enumerate_pids(scope) == [os.getpid()]


def test_enumerate_pids_unknown_scope_returns_self_pid_only(_writable_proc):
    from dexcost.cgroup_walker import classify_scope, enumerate_pids
    _writable_proc("0::/some/unknown/path\n")
    scope = classify_scope()
    assert enumerate_pids(scope) == [os.getpid()]


def test_enumerate_pids_cgroup_v1_returns_self_pid_only(_writable_proc):
    """cgroup v1 walking is deferred to v1.1 — degrade to self-PID-only."""
    from dexcost.cgroup_walker import classify_scope, enumerate_pids
    _writable_proc("12:devices:/docker/abc\n11:cpuset:/docker/abc\n")
    scope = classify_scope()
    assert enumerate_pids(scope) == [os.getpid()]


def test_enumerate_pids_container_walk_denied_returns_none(
    _writable_proc, tmp_path, monkeypatch,
):
    """cgroup.procs unreadable → None + log-once gpu_cgroup_walk_forbidden."""
    from dexcost import cgroup_walker
    _writable_proc("0::/docker/abc123\n")
    monkeypatch.setattr(cgroup_walker, "_CGROUP_ROOT", str(tmp_path / "nonexistent"))
    scope = cgroup_walker.classify_scope()
    pids = cgroup_walker.enumerate_pids(scope)
    assert pids is None


def test_fallback_label_for_scope():
    """Maps scope kind to the pricing_source suffix per Decision #1."""
    from dexcost.cgroup_walker import CgroupScope, fallback_label_for
    assert fallback_label_for(CgroupScope(kind="container", path="/docker/abc")) is None
    assert fallback_label_for(CgroupScope(kind="bare_metal_user_slice", path=None)) == "no_container_scope"
    assert fallback_label_for(CgroupScope(kind="root_cgroup", path=None)) == "no_container_scope"
    assert fallback_label_for(CgroupScope(kind="unknown", path=None)) == "self_pid_only"
    assert fallback_label_for(CgroupScope(kind="cgroup_v1", path=None)) == "self_pid_only"
