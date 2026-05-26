"""Per-task compute accountant.

Holds start cgroup snapshot + runtime context for one dexcost task. At task
finalize, emits exactly one ``compute_cost`` event with ``cost_pending: true``
— the pricing engine back-fills ``cost_usd`` via the deferred-cost pattern
inherited from network v2 §6.4.

Capture §5.3 invariant: at most one event per task per runtime. Idempotent —
second call to ``snapshot_end_and_build`` / ``build_serverless_event`` returns
``None``.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from dexcost.cgroup_reader import (
    read_cpu_max, read_cpu_stat, read_memory_current, read_memory_max,
    read_memory_peak,
)
from dexcost.compute_runtime import RuntimeKind


def _billing_model_for(runtime: RuntimeKind) -> str:
    """Map a RuntimeKind to the ``details.billing_model`` discriminator."""
    return {
        RuntimeKind.LAMBDA: "lambda",
        RuntimeKind.FARGATE: "fargate",
        RuntimeKind.EC2: "ec2",
        RuntimeKind.GCE: "gce",
        RuntimeKind.AZURE_VM: "azure_vm",
        RuntimeKind.CLOUD_RUN: "cloud_run_request",
        RuntimeKind.CLOUD_FUNCTIONS: "cloud_functions",
        RuntimeKind.AZURE_FUNCTIONS: "azure_functions",
        RuntimeKind.VERCEL: "vercel_fluid",
        RuntimeKind.K8S_POD: "k8s_pod",
    }.get(runtime, "unknown")


def _detect_arch() -> str:
    """Detect host architecture for Lambda / Fargate / EC2 rate selection."""
    if hasattr(os, "uname"):
        machine = os.uname().machine.lower()
    else:
        machine = ""
    if "aarch64" in machine or "arm64" in machine:
        return "arm64"
    return "x86_64"


class ComputeAccountant:
    """One per dexcost task. Single-writer; lock-guarded for the freeze flag."""

    def __init__(
        self,
        runtime: RuntimeKind,
        lambda_memory_mb: int | None = None,
        fargate_vcpu: float | None = None,
        fargate_memory_mib: int | None = None,
        architecture: str | None = None,
        initialization_type: str | None = None,
        region: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._frozen = False
        self.runtime = runtime
        self.lambda_memory_mb = lambda_memory_mb
        self.fargate_vcpu = fargate_vcpu
        self.fargate_memory_mib = fargate_memory_mib
        self.architecture = architecture or _detect_arch()
        self.initialization_type = initialization_type
        self.region = region
        self._start_cpu_usec: int | None = None
        # Sprint 2 Theme C / §3.1.3 Fix 1: snapshot the cgroup-lifetime
        # memory.peak at task start so back-to-back tasks in a long-lived
        # container report only the peak THIS task pushed, not the
        # accumulated high-water mark inherited from prior workloads.
        self._start_mem_peak: int | None = None

    # ------------------------------------------------------------------
    # Long-running runtimes (Fargate / EC2 / GCE / Azure VM / K8s pod /
    # Cloud Run instance-based)
    # ------------------------------------------------------------------

    def snapshot_start(self) -> None:
        """Capture the cgroup CPU counter + memory peak at task start.

        Idempotent. The memory baseline is the key fix for §3.1.3 Fix 1:
        cgroup v2 ``memory.peak`` is a high-water mark since the cgroup
        was created, so a long-lived container's prior workload would
        otherwise inflate every subsequent task's reported peak.
        """
        with self._lock:
            if self._start_cpu_usec is not None:
                return
        s = read_cpu_stat()
        # B2 (read_memory_peak falls back to read_memory_current if peak
        # file is missing — same pattern as end-of-task read at line 105).
        mp = read_memory_peak()
        if mp is None:
            mp = read_memory_current() or 0
        with self._lock:
            self._start_cpu_usec = s.usage_usec if s else 0
            self._start_mem_peak = int(mp)

    def snapshot_end_and_build(self, duration_ms: int) -> dict[str, Any] | None:
        """Capture cgroup CPU/memory at task end and build the event details.

        Returns ``None`` if already frozen (second call) or runtime is unknown.
        """
        with self._lock:
            if self._frozen:
                return None
            self._frozen = True
            start_cpu = self._start_cpu_usec or 0
            start_mem_peak = self._start_mem_peak or 0

        end = read_cpu_stat()
        cpu_max = read_cpu_max()
        # capture §6 case 6 — memory.peak missing → fall back to memory.current.
        mem_peak_end = read_memory_peak()
        if mem_peak_end is None:
            mem_peak_end = read_memory_current() or 0
        mem_limit = read_memory_max() or 0

        # Sprint 2 Theme C / §3.1.3 Fix 1: subtract start-of-task peak so
        # we report only what THIS task pushed the high-water mark by.
        # Clamp at 0 — peak can't decrease, but the read is racy across
        # cgroup remounts and we'd rather report 0 than negative.
        memory_bytes_peak = max(0, int(mem_peak_end) - start_mem_peak)

        # Sprint 2 Theme C / §3.1.3 Fix 2: a negative CPU delta signals
        # the cgroup counter was reset mid-task (remount, container
        # restart). Report 0 vcpu-seconds AND mark cost_confidence as
        # estimated so the downstream pricer doesn't charge $0 with
        # apparent confidence.
        vcpu_seconds_used = 0.0
        cost_confidence: str | None = None
        if end is not None:
            if end.usage_usec >= start_cpu:
                vcpu_seconds_used = (end.usage_usec - start_cpu) / 1_000_000
            else:
                cost_confidence = "estimated"

        vcpu_count = cpu_max.vcpu_count if cpu_max else float(os.cpu_count() or 1)

        details: dict[str, Any] = {
            "billing_model": _billing_model_for(self.runtime),
            "duration_ms": duration_ms,
            "memory_bytes_peak": memory_bytes_peak,
            "memory_bytes_limit": int(mem_limit),
            "vcpu_count": vcpu_count,
            "vcpu_seconds_used": vcpu_seconds_used,
            "invocation_count": 0,
            "region": self.region,
            "architecture": self.architecture,
            "initialization_type": None,
            "cost_pending": True,
        }
        if cost_confidence is not None:
            details["cost_confidence"] = cost_confidence
        return details

    # ------------------------------------------------------------------
    # Serverless runtimes
    # ------------------------------------------------------------------

    def build_serverless_event(
        self, duration_ms: int, memory_bytes_peak: int,
    ) -> dict[str, Any] | None:
        """Build a per-invocation event for Lambda / Cloud Run / Cloud Functions
        / Azure Functions / Vercel."""
        with self._lock:
            if self._frozen:
                return None
            self._frozen = True

        if self.runtime == RuntimeKind.LAMBDA:
            # Lambda's AWS_LAMBDA_FUNCTION_MEMORY_SIZE is DECIMAL MB (10^6 bytes).
            mem_limit = (self.lambda_memory_mb or 128) * 1_000_000
            vcpu_count = self._vcpu_count_from_cgroup()
        elif self.runtime == RuntimeKind.FARGATE:
            mem_limit = (self.fargate_memory_mib or 0) * 1024 * 1024
            vcpu_count = (
                self.fargate_vcpu
                if self.fargate_vcpu is not None
                else self._vcpu_count_from_cgroup()
            )
        else:
            # Cloud Run / Cloud Functions / Azure Functions / Vercel —
            # cgroup memory.max is the declared limit.
            mem_limit = read_memory_max() or memory_bytes_peak
            vcpu_count = self._vcpu_count_from_cgroup()

        return {
            "billing_model": _billing_model_for(self.runtime),
            "duration_ms": duration_ms,
            "memory_bytes_peak": memory_bytes_peak,
            "memory_bytes_limit": mem_limit,
            "vcpu_count": vcpu_count,
            "vcpu_seconds_used": 0,
            "invocation_count": 1,
            "region": self.region,
            "architecture": self.architecture,
            "initialization_type": self.initialization_type,
            "cost_pending": True,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _vcpu_count_from_cgroup() -> float:
        cpu_max = read_cpu_max()
        if cpu_max is not None:
            return cpu_max.vcpu_count
        return float(os.cpu_count() or 1)
