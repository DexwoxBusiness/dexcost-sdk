"""Per-task GPU accountant — Phase 2 v1 capture.

One per dexcost task. Lives on Task as ``_gpu`` (mirrors ``_compute`` /
``_network``). Holds the start cgroup snapshot, the NVML start-snapshot
timestamps (Decision #8 persistent state), and the device handles.

At task finalize, the accountant:

1. Snapshots NVML utilization across all devices via
   :func:`nvml_reader.get_process_utilization` with persisted timestamps.
2. Walks the cgroup PIDs (Decision #1) and accumulates SM-time across
   them per device.
3. Computes the window-averaged ``sm_util_pct`` per Decision #3
   sharpening (NOT a point sample at finalize).
4. Resolves the GPU SKU via NVML productName alias matching (delegated
   to the pricing engine's catalog lookup — accountant just captures
   the productName).
5. Emits **one** ``gpu_cost`` event with ``cost_pending=True`` (pricing
   engine back-fills) AND **one** ``gpu_utilization_signal`` event per
   device that the task's cgroup touched.

Idempotent — second call to ``snapshot_end_and_build`` returns
``(None, None)`` per capture spec §5.3.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from dexcost import cgroup_walker, nvml_reader
from dexcost.cloud_detect import CloudEnv
from dexcost.gpu_runtime import GpuRuntimeKind

_log = logging.getLogger(__name__)

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


# Map GpuRuntimeKind → billing_model discriminator used by the pricing engine.
_BILLING_MODEL_FOR_RUNTIME = {
    GpuRuntimeKind.MODAL:                "per_gpu_second_active",
    GpuRuntimeKind.RUNPOD:               "per_gpu_second_active",
    GpuRuntimeKind.REPLICATE:            "per_gpu_second_active",
    GpuRuntimeKind.LAMBDA_LABS:          "per_gpu_hour_reserved",
    GpuRuntimeKind.COREWEAVE:            "per_gpu_hour_reserved",
    GpuRuntimeKind.GCP_GCE_N1_ATTACHED:  "per_gpu_hour_reserved",
    GpuRuntimeKind.AWS_EC2_GPU:          "per_instance_hour",
    GpuRuntimeKind.GCP_GCE_BUNDLED:      "per_instance_hour",
    GpuRuntimeKind.AZURE_VM_GPU:         "per_instance_hour",
    GpuRuntimeKind.AZURE_VM_VGPU:        "per_vgpu_hour",
}


class GpuAccountant:
    """Per-task GPU accountant. One instance per dexcost task."""

    def __init__(self, runtime: GpuRuntimeKind, cloud_env: CloudEnv) -> None:
        self._lock = threading.Lock()
        self._frozen = False
        self.runtime = runtime
        self.cloud_env = cloud_env
        self._scope: cgroup_walker.CgroupScope | None = None
        self._initial_pids: set[int] = set()
        # Decision #8: per-device-per-PID lastSeenTimeStamp persisted across calls.
        self._initial_timestamps: dict[int, dict[int, int]] = {}
        self._device_handles: list = []
        self._device_product_names: list[str | None] = []
        self._device_mig_modes: list[bool] = []
        # Per-device peak VRAM tracker (sampled at start + end).
        self._vram_total: dict[int, int] = {}
        self._vram_used_peak: dict[int, int] = {}
        # Per-device PID set observed across the task (for process_count signal).
        self._pids_touched_per_device: dict[int, set[int]] = {}

    # ------------------------------------------------------------------
    # Snapshot start
    # ------------------------------------------------------------------

    def snapshot_start(self) -> None:
        """Initialize NVML, snapshot cgroup PIDs, capture baseline NVML timestamps."""
        if not nvml_reader.init_nvml():
            return  # no NVML → no GPU events
        count = nvml_reader.get_device_count() or 0
        if count == 0:
            return
        self._device_handles = [
            nvml_reader.get_device_handle(i) for i in range(count)
        ]
        # Productname + MIG detection per device.
        for i, handle in enumerate(self._device_handles):
            name = nvml_reader.get_product_name(handle)
            self._device_product_names.append(name)
            mig = nvml_reader.get_mig_mode(handle)
            self._device_mig_modes.append(mig)
            if mig:
                _warn_once(
                    f"gpu_mig_detected_full_billing_applied:device{i}",
                    f"NVML reports MIG enabled on device {i} "
                    f"(productName={name!r}); Decision #2 — full-GPU rate "
                    f"applied. details.mig_profile populated for v1.1 "
                    f"forward-compat.",
                )
            mem = nvml_reader.get_memory_info(handle)
            if mem:
                self._vram_total[i] = mem.total_bytes
                self._vram_used_peak[i] = mem.used_bytes
            self._initial_timestamps[i] = {}
            self._pids_touched_per_device[i] = set()
            # Baseline NVML sample — captures the per-PID lastSeenTimeStamp.
            baseline = nvml_reader.get_process_utilization(
                handle, self._initial_timestamps[i],
            )
            if baseline:
                self._pids_touched_per_device[i].update(baseline.keys())
                # B2 (Sprint 2 Theme C / §3.1.1): record per-PID baseline
                # timestamp directly from the returned samples instead of
                # relying on the nvml_reader's in-place mutation of
                # _initial_timestamps. This decouples the accountant from
                # the wrapper's bookkeeping side-effects and makes tests
                # that mock get_process_utilization deterministic.
                for pid, samples_list in baseline.items():
                    if samples_list:
                        self._initial_timestamps[i][pid] = max(
                            s.time_stamp for s in samples_list
                        )

        # Snapshot cgroup PIDs (Decision #1 scope classification).
        self._scope = cgroup_walker.classify_scope()
        pids = cgroup_walker.enumerate_pids(self._scope)
        self._initial_pids = set(pids) if pids is not None else {os.getpid()}

    # ------------------------------------------------------------------
    # Snapshot end + build dual events
    # ------------------------------------------------------------------

    def snapshot_end_and_build(
        self, duration_ms: int,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
        """Build (gpu_cost_event, [gpu_utilization_signal_events]) at task finalize.

        Returns ``(None, None)`` when:
        - already frozen (second call)
        - NVML wasn't available or reported 0 devices at start
        - no devices were touched by the cgroup PIDs during the window
          (the task ran but didn't actually use the GPU — emit nothing
          rather than zero-noise events)
        """
        with self._lock:
            if self._frozen:
                return (None, None)
            self._frozen = True

        if not self._device_handles:
            return (None, None)

        # End-snapshot cgroup walk + Decision #1 fallback label.
        scope = self._scope or cgroup_walker.classify_scope()
        end_pids_list = cgroup_walker.enumerate_pids(scope)
        if end_pids_list is None:
            # cgroup walk denied at end → degrade
            fallback_label = "self_pid_only"
            current_pids = {os.getpid()}
        else:
            fallback_label = cgroup_walker.fallback_label_for(scope)
            current_pids = set(end_pids_list)

        # Union of PIDs seen at start + end (forked workers that exited
        # before end still contributed GPU time captured at start).
        cgroup_pid_union = self._initial_pids | current_pids

        # Per-device: end NVML samples + accumulate SM-time across cgroup PIDs.
        per_device_gpu_seconds: dict[int, float] = {}
        signal_events: list[dict[str, Any]] = []

        # Determine canonical SKU from any device's productName
        # (all devices on one task are assumed homogeneous — same SKU).
        canonical_product_name = next(
            (n for n in self._device_product_names if n), None,
        )
        gpu_sku = self._resolve_sku_from_product_name(canonical_product_name)

        # MIG profile is set from the START snapshot — independent of whether
        # cgroup PIDs ended up touching the device. Decision #2 transparency:
        # if MIG is on, surface it in details regardless of measurement.
        mig_profile: str | None = None
        if any(self._device_mig_modes):
            mig_profile = "mig_detected"  # v1.1 reads exact slice via NVML

        # Decision #3 sub-100ms-degenerate case: if duration is too short to
        # measure utilization meaningfully, emit one signal per device with
        # sm_util_pct=None — surface that the task ran on a GPU even when we
        # can't measure how it used it.
        degenerate_window = duration_ms <= 0

        any_pid_touched = False
        for i, handle in enumerate(self._device_handles):
            # B2 (Sprint 2 Theme C / §3.1.1) — snapshot baseline timestamps
            # BEFORE the end call mutates _initial_timestamps in place.
            # Each PID's first integration-dt is `first_sample.time_stamp -
            # baseline_ts_per_pid[pid]`; reading the mutated dict afterwards
            # would zero out the dt for every PID.
            baseline_ts_per_pid = dict(self._initial_timestamps[i])

            end_samples = nvml_reader.get_process_utilization(
                handle, self._initial_timestamps[i],
            ) or {}
            if end_samples:
                self._pids_touched_per_device[i].update(end_samples.keys())

            # Re-read memory for peak update.
            mem = nvml_reader.get_memory_info(handle)
            if mem:
                self._vram_used_peak[i] = max(
                    self._vram_used_peak.get(i, 0), mem.used_bytes,
                )

            # Filter to cgroup-PID set (Decision #1 boundary). After B2 the
            # NVML wrapper returns dict[pid, list[UtilSample]] — multiple
            # samples per PID covering the task window.
            relevant_samples_by_pid: dict[int, list] = {
                pid: samples_list
                for pid, samples_list in end_samples.items()
                if pid in cgroup_pid_union and samples_list
            }

            if relevant_samples_by_pid:
                any_pid_touched = True
                # B2 (Sprint 2 Theme C / §3.1.1) — integrate SM utilization.
                # The CORRECT formula is sm_seconds = Σ (sm_util[i]/100) ×
                # dt[i], where dt[i] is the wall interval covered by each
                # sample (`sample.time_stamp - prev_ts`, with prev_ts =
                # baseline for the first sample of a PID, or the previous
                # sample's ts thereafter). Pre-fix the accountant used
                # `max_ts - base_ts` directly, which is wall time × 100%
                # utilization — silently inflating cost on underutilized
                # GPUs.
                gpu_seconds_for_device = 0.0
                mem_util_sum = 0.0
                mem_util_n = 0
                # Two semantics for "first sample of a PID with no baseline":
                #  - If the device had ZERO PIDs at snapshot_start, the first
                #    sample is treated as covering [task_start, first_ts].
                #    The PID was running but NVML just hadn't reported yet.
                #  - If OTHER PIDs were active at baseline but this PID was
                #    not, the PID joined mid-task; first-sample dt is 0 (its
                #    own ts is its first observation).
                # Derive task_start_ts from duration_ms + max observed sample
                # timestamp (NVML emits absolute microseconds since epoch).
                device_had_baseline_pids = bool(baseline_ts_per_pid)
                all_ts = [
                    s.time_stamp
                    for sl in relevant_samples_by_pid.values()
                    for s in sl
                ]
                max_sample_ts = max(all_ts) if all_ts else 0
                task_start_ts = max(0, max_sample_ts - duration_ms * 1000)
                for pid, samples_list in relevant_samples_by_pid.items():
                    baseline_ts_for_pid = baseline_ts_per_pid.get(pid)
                    if baseline_ts_for_pid is None:
                        if device_had_baseline_pids:
                            baseline_ts_for_pid = samples_list[0].time_stamp
                        else:
                            baseline_ts_for_pid = task_start_ts
                    prev_ts = baseline_ts_for_pid
                    for s in samples_list:
                        dt_us = max(0, s.time_stamp - prev_ts)
                        gpu_seconds_for_device += (
                            (s.sm_util / 100.0) * (dt_us / 1_000_000.0)
                        )
                        prev_ts = s.time_stamp
                        mem_util_sum += s.mem_util
                        mem_util_n += 1
                per_device_gpu_seconds[i] = gpu_seconds_for_device

                # Decision #3 sharpening: sm_util_pct is TASK-WINDOW-AVERAGED.
                # Now derived from the integrated sm_seconds — exact, not
                # an approximation.
                if duration_ms > 0:
                    window_s = duration_ms / 1000.0
                    sm_util_pct_val: float | None = min(
                        100.0,
                        gpu_seconds_for_device / window_s * 100.0,
                    )
                else:
                    sm_util_pct_val = None  # degenerate window

                mem_util_avg = mem_util_sum / mem_util_n if mem_util_n else 0.0

                signal_events.append({
                    "gpu_index": i,
                    "gpu_sku": gpu_sku,
                    "sm_util_pct": sm_util_pct_val,
                    "mem_util_pct": mem_util_avg,
                    "vram_used_peak_bytes": self._vram_used_peak.get(i, 0),
                    "vram_total_bytes": self._vram_total.get(i, 0),
                    "process_count": len(self._pids_touched_per_device[i]),
                    "sample_count": mem_util_n,
                    "task_duration_ms": duration_ms,
                })
            elif degenerate_window:
                # Sub-100ms / zero-duration task: emit signal with None util.
                signal_events.append({
                    "gpu_index": i,
                    "gpu_sku": gpu_sku,
                    "sm_util_pct": None,
                    "mem_util_pct": None,
                    "vram_used_peak_bytes": self._vram_used_peak.get(i, 0),
                    "vram_total_bytes": self._vram_total.get(i, 0),
                    "process_count": len(self._pids_touched_per_device[i]),
                    "sample_count": 0,
                    "task_duration_ms": duration_ms,
                })

        # Emission rules:
        # - If any PID touched any device → emit cost event + signals
        # - Else if Decision #1 measurement-side fallback in play → emit
        #   zero-cost event so customer sees the attribution attempt
        # - Else if degenerate window (sub-100ms) → emit zero-cost event
        # - Else if MIG detected → emit zero-cost event so transparency
        #   logging has corresponding event surface
        # - Else → no events (task didn't use the GPU)
        should_emit_cost = (
            any_pid_touched
            or fallback_label is not None
            or degenerate_window
            or any(self._device_mig_modes)
        )
        if not should_emit_cost:
            return (None, None)

        total_gpu_seconds = sum(per_device_gpu_seconds.values())
        cost_event = self._build_cost_event(
            duration_ms=duration_ms,
            gpu_sku=gpu_sku,
            gpu_count=len(self._device_handles),
            gpu_seconds_used=total_gpu_seconds,
            mig_profile=mig_profile,
            fallback_label=fallback_label,
        )
        return (cost_event, signal_events if signal_events else None)

    # ------------------------------------------------------------------
    # Event builders
    # ------------------------------------------------------------------

    def _build_cost_event(
        self,
        duration_ms: int,
        gpu_sku: str | None,
        gpu_count: int,
        gpu_seconds_used: float,
        mig_profile: str | None,
        fallback_label: str | None,
    ) -> dict[str, Any]:
        details: dict[str, Any] = {
            "billing_model": _BILLING_MODEL_FOR_RUNTIME.get(
                self.runtime, "per_gpu_second_active",
            ),
            "gpu_vendor": "nvidia",  # Decision #5 — only nvidia in v1
            "gpu_sku": gpu_sku,
            "gpu_count": gpu_count,
            "region": self.cloud_env.region,
            "duration_ms": duration_ms,
            "gpu_seconds_used": gpu_seconds_used,
            "instance_type": self.cloud_env.instance_type,
            "vgpu_profile": self._resolve_vgpu_profile(),
            "mig_profile": mig_profile,
            "cost_pending": True,
        }
        # Pass productName through for Decision #4 device-class fallback in pricing.
        product_name = next(
            (n for n in self._device_product_names if n), None,
        )
        if product_name:
            details["_nvml_product_name_lower"] = product_name
        if fallback_label:
            details["_cgroup_scope_fallback"] = fallback_label
        return details

    def _build_zero_cost_event(
        self, duration_ms, gpu_sku, mig_profile, fallback_label,
    ):
        return self._build_cost_event(
            duration_ms=duration_ms,
            gpu_sku=gpu_sku,
            gpu_count=len(self._device_handles),
            gpu_seconds_used=0.0,
            mig_profile=mig_profile,
            fallback_label=fallback_label,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_sku_from_product_name(product_name_lower: str | None) -> str | None:
        """Best-effort substring → canonical-key mapping.

        The pricing engine does the authoritative catalog-alias lookup;
        this is a coarse hint baked into details.gpu_sku so the pricing
        engine doesn't have to re-walk the catalog. When the productName
        is unknown, returns None and Decision #4 device-class fallback
        kicks in at the pricing layer via _nvml_product_name_lower.
        """
        if not product_name_lower:
            return None
        # Common modern productNames; ordered most-specific-first.
        if "h100" in product_name_lower:
            return "h100-80gb-sxm5"
        if "h200" in product_name_lower:
            return "h200-141gb-sxm5"
        if "a100" in product_name_lower:
            if "40gb" in product_name_lower:
                return "a100-40gb-sxm4"
            return "a100-80gb-sxm4"
        if "a10g" in product_name_lower:
            return "a10g-24gb"
        if "a10-4q" in product_name_lower:
            return "a10-vgpu-1of6"
        if "a10-8q" in product_name_lower:
            return "a10-vgpu-1of3"
        if "a10-12q" in product_name_lower:
            return "a10-vgpu-1of2"
        if "a10-24q" in product_name_lower or "a10" in product_name_lower:
            return "a10"
        if "l40s" in product_name_lower:
            return "l40s-48gb"
        if "l4" in product_name_lower:
            return "l4-24gb"
        if "tesla t4" in product_name_lower or "nvidia t4" in product_name_lower:
            return "t4-16gb"
        if "rtx 6000" in product_name_lower:
            return "rtx-6000-24gb"
        return None  # device_class fallback at pricing layer

    def _resolve_vgpu_profile(self) -> str | None:
        """For Azure NVadsA10 v5: extract vGPU profile from instance type."""
        if self.runtime != GpuRuntimeKind.AZURE_VM_VGPU:
            return None
        instance_type = self.cloud_env.instance_type
        if not instance_type:
            return None
        # Standard_NV{6,12,18,36,72}ads_A10_v5 → profile fraction
        mapping = {
            "Standard_NV6ads_A10_v5":  "1/6 A10",
            "Standard_NV12ads_A10_v5": "1/3 A10",
            "Standard_NV18ads_A10_v5": "1/2 A10",
            "Standard_NV36ads_A10_v5": "full A10",
            "Standard_NV72ads_A10_v5": "2x A10",
        }
        return mapping.get(instance_type)
