"""NVML library wrapper — Phase 2 GPU foundation.

Wraps NVIDIA's `pynvml` (aka `nvidia-ml-py`) library with the fail-silent
contract from convention §9. Every NVML call returns ``None`` (or ``False``
for boolean accessors) on missing driver / library / permission / device
errors rather than raising — the caller (typically :class:`GpuAccountant`)
decides the fallback policy per Decision #1's classification table.

``pynvml`` is an OPTIONAL dependency (install via ``pip install dexcost[gpu]``).
The top-level ``import pynvml`` is guarded so the SDK works on GPU-less
hosts and customers who haven't opted into the GPU extra.

Per Decision #4: ``get_product_name`` applies NFC Unicode normalization
+ lowercase + whitespace collapse on the raw NVML string before returning
— catalog alias matching depends on byte-level equality after normalization
because NVIDIA's productName carries non-breaking spaces and other Unicode
quirks across driver versions.

Per Decision #8: ``get_process_utilization`` takes a mutable
``last_seen_timestamps`` dict and updates it in place — the caller persists
per-PID timestamps across snapshot calls so NVML's sample buffer doesn't
silently lose intermediate samples.
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from dataclasses import dataclass

_log = logging.getLogger(__name__)

try:
    import pynvml as _pynvml
    _NVML_AVAILABLE = True
except ImportError:
    _pynvml = None  # type: ignore[assignment]
    _NVML_AVAILABLE = False

# Module-level set of warning-mode tokens already logged in this process
# (convention §11 — log-once-per-failure-mode).
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


# ─── Typed return values ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProcessInfo:
    """One NVML compute-running-process record (PID + GPU memory usage)."""

    pid: int
    used_gpu_memory: int  # bytes; may be 0 when NVML reports NOT_AVAILABLE


@dataclass(frozen=True)
class UtilSample:
    """One NVML process-utilization sample for a single PID at one timestamp."""

    pid: int
    sm_util: int      # 0-100 — percent of time SMs had ≥1 kernel running
    mem_util: int     # 0-100 — percent of time memory subsystem was busy
    time_stamp: int   # microseconds since NVML epoch


@dataclass(frozen=True)
class MemInfo:
    """Device-level memory totals."""

    used_bytes: int
    total_bytes: int


# ─── Availability + init ─────────────────────────────────────────────────────


def nvml_available() -> bool:
    """True when ``pynvml`` was importable at SDK load time.

    Returning False here means ``nvidia-ml-py`` isn't installed (no
    ``pip install dexcost[gpu]``). NVML driver presence is a separate
    question — see ``init_nvml()``.
    """
    return _NVML_AVAILABLE and _pynvml is not None


def init_nvml() -> bool:
    """Call ``nvmlInit()``. Returns True on success, False on any failure.

    Fail modes (all silent + log-once):
    - pynvml not importable → ``gpu_pynvml_not_installed``
    - NVIDIA driver not loaded (NVML_ERROR_DRIVER_NOT_LOADED) → ``gpu_no_driver_in_container``
    - Library load failure (NVML_ERROR_LIBRARY_NOT_FOUND) → ``gpu_nvml_library_missing``
    """
    if not nvml_available():
        _warn_once(
            "gpu_pynvml_not_installed",
            "pynvml not installed; GPU capture disabled. "
            "Install with: pip install dexcost[gpu]",
        )
        return False
    try:
        _pynvml.nvmlInit()
        return True
    except _pynvml.NVMLError as exc:
        _warn_once(
            "gpu_nvml_init_failed",
            f"NVML init failed ({exc}); GPU capture disabled for this process",
        )
        return False


def shutdown_nvml() -> None:
    """Best-effort ``nvmlShutdown()``. Silent on any error."""
    if not nvml_available():
        return
    try:
        _pynvml.nvmlShutdown()
    except _pynvml.NVMLError:
        pass


# ─── Device enumeration ──────────────────────────────────────────────────────


def get_device_count() -> int | None:
    """Number of NVIDIA devices visible to NVML. ``None`` on failure."""
    if not nvml_available():
        return None
    try:
        return int(_pynvml.nvmlDeviceGetCount())
    except _pynvml.NVMLError as exc:
        _warn_once(
            "gpu_device_count_failed",
            f"nvmlDeviceGetCount failed ({exc})",
        )
        return None


def get_device_handle(index: int):
    """Opaque device handle (used by other accessors). ``None`` on failure."""
    if not nvml_available():
        return None
    try:
        return _pynvml.nvmlDeviceGetHandleByIndex(index)
    except _pynvml.NVMLError as exc:
        _warn_once(
            f"gpu_device_handle_failed:{index}",
            f"nvmlDeviceGetHandleByIndex({index}) failed ({exc})",
        )
        return None


# ─── Decision #4: NFC-normalized productName ─────────────────────────────────


def _normalize_product_name(raw: str) -> str:
    """NFC → lowercase → whitespace collapse (incl. NBSP / NNBSP / ZWSP)."""
    nfc = unicodedata.normalize("NFC", raw)
    # str.split() with no argument collapses ANY whitespace including
    # non-breaking space U+00A0, narrow no-break space U+202F, zero-width
    # characters that Python treats as whitespace, etc.
    return " ".join(nfc.split()).lower()


def get_product_name(handle) -> str | None:
    """Return the NVML productName, NFC-normalized + lowercased.

    Decision #4 — alias matching against the catalog depends on byte-level
    equality post-normalization.
    """
    if not nvml_available():
        return None
    try:
        raw = _pynvml.nvmlDeviceGetName(handle)
    except _pynvml.NVMLError as exc:
        _warn_once(
            "gpu_product_name_failed",
            f"nvmlDeviceGetName failed ({exc})",
        )
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return _normalize_product_name(raw)


# ─── Per-PID compute processes (Decision #1 measurement-side primitive) ──────


def get_compute_running_processes(handle) -> list[ProcessInfo] | None:
    """List of PIDs holding the GPU + their VRAM usage.

    Returns ``None`` on ``NVML_ERROR_NO_PERMISSION`` — the load-bearing
    non-root-container case from Decision #1. The caller's cgroup walk
    then degrades to self-PID-only.
    """
    if not nvml_available():
        return None
    try:
        raw = _pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
    except _pynvml.NVMLError as exc:
        _warn_once(
            "gpu_nvml_permission_denied",
            f"nvmlDeviceGetComputeRunningProcesses failed ({exc}); "
            "GpuAccountant will degrade to self-PID-only",
        )
        return None
    out: list[ProcessInfo] = []
    for p in raw:
        try:
            mem = int(getattr(p, "usedGpuMemory", 0) or 0)
        except (TypeError, ValueError):
            mem = 0
        out.append(ProcessInfo(pid=int(p.pid), used_gpu_memory=mem))
    return out


# ─── Per-PID utilization with persistent timestamp state (Decision #8) ───────


def get_process_utilization(
    handle, last_seen_timestamps: dict[int, int],
) -> dict[int, list[UtilSample]] | None:
    """Return per-PID utilization samples; UPDATE timestamps dict in place.

    Decision #8 — NVML's ``nvmlDeviceGetProcessUtilization`` returns samples
    accumulated since ``lastSeenTimeStamp``. The caller persists per-PID
    timestamps across snapshot calls so we don't lose samples between calls
    when the internal NVML buffer wraps.

    Returns ``dict[int, list[UtilSample]]`` — multiple samples per PID,
    sorted by ``time_stamp`` ascending. Pre-B2 the wrapper collapsed
    samples to the latest per PID, throwing away the integration data
    the accountant now needs (Sprint 2 Theme C / §3.1.1).

    The earliest baseline call passes ``last_seen_timestamps={}``; each
    subsequent call passes the dict the previous call updated.
    """
    if not nvml_available():
        return None
    # Use the minimum timestamp across known PIDs (or 0 for first call) to
    # capture samples for any new PIDs that joined since last snapshot.
    base_ts = min(last_seen_timestamps.values(), default=0) if last_seen_timestamps else 0
    try:
        raw = _pynvml.nvmlDeviceGetProcessUtilization(handle, base_ts)
    except _pynvml.NVMLError as exc:
        _warn_once(
            "gpu_process_utilization_failed",
            f"nvmlDeviceGetProcessUtilization failed ({exc})",
        )
        return None
    out: dict[int, list[UtilSample]] = {}
    for s in raw:
        pid = int(s.pid)
        ts = int(getattr(s, "timeStamp", 0) or 0)
        sm = int(getattr(s, "smUtil", 0) or 0)
        mem = int(getattr(s, "memUtil", 0) or 0)
        out.setdefault(pid, []).append(
            UtilSample(pid=pid, sm_util=sm, mem_util=mem, time_stamp=ts)
        )
        # last_seen tracks the MAX ts per PID across this batch.
        if ts > last_seen_timestamps.get(pid, 0):
            last_seen_timestamps[pid] = ts
    # Sort each PID's samples by timestamp ascending — the integration
    # in the accountant assumes monotonic ordering.
    for pid in out:
        out[pid].sort(key=lambda s: s.time_stamp)
    return out


# ─── Memory + MIG ────────────────────────────────────────────────────────────


def get_memory_info(handle) -> MemInfo | None:
    """Device-level used / total VRAM bytes. ``None`` on failure."""
    if not nvml_available():
        return None
    try:
        info = _pynvml.nvmlDeviceGetMemoryInfo(handle)
    except _pynvml.NVMLError as exc:
        _warn_once(
            "gpu_memory_info_failed",
            f"nvmlDeviceGetMemoryInfo failed ({exc})",
        )
        return None
    return MemInfo(used_bytes=int(info.used), total_bytes=int(info.total))


def get_mig_mode(handle) -> bool:
    """True when MIG is enabled on this device (Decision #2 detection)."""
    if not nvml_available():
        return False
    try:
        current, _pending = _pynvml.nvmlDeviceGetMigMode(handle)
    except _pynvml.NVMLError:
        # Older GPUs without MIG support → fail-silent (NOT an error)
        return False
    enable_const = getattr(_pynvml, "NVML_DEVICE_MIG_ENABLE", 1)
    return current == enable_const
