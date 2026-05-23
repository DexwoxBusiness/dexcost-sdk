"""NVML library wrapper — fail-silent contract, NFC-normalized productName.

The wrapper guards `import pynvml` so SDK install without the GPU extra
works (the import fails silently and `nvml_available()` returns False).
All NVML calls return None on permission/device/library failures rather
than raising — the caller (GpuAccountant) decides the fallback policy.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


def test_nvml_available_returns_false_when_pynvml_missing(monkeypatch):
    """If pynvml isn't importable, nvml_available() is False and SDK still works."""
    # Force a reload with pynvml hidden.
    monkeypatch.setitem(sys.modules, "pynvml", None)
    sys.modules.pop("dexcost.nvml_reader", None)
    from dexcost import nvml_reader
    assert nvml_reader.nvml_available() is False
    assert nvml_reader.init_nvml() is False
    assert nvml_reader.get_device_count() is None


def test_init_nvml_returns_false_on_driver_not_loaded():
    from dexcost import nvml_reader
    fake = MagicMock()

    class FakeNVMLError(Exception):
        pass

    fake.NVMLError = FakeNVMLError
    fake.NVML_ERROR_DRIVER_NOT_LOADED = 9
    fake.nvmlInit.side_effect = FakeNVMLError("driver not loaded")
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        assert nvml_reader.init_nvml() is False


def test_init_nvml_returns_true_on_success():
    from dexcost import nvml_reader
    fake = MagicMock()
    fake.NVMLError = type("NVMLError", (Exception,), {})
    fake.nvmlInit.return_value = None
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        assert nvml_reader.init_nvml() is True


def test_get_product_name_normalizes_unicode_and_whitespace():
    """NFC normalization + lowercase + collapse whitespace (incl. NBSP)."""
    from dexcost import nvml_reader
    fake = MagicMock()
    fake.NVMLError = type("NVMLError", (Exception,), {})
    # Non-breaking space U+00A0 between words plus mixed case plus
    # double space — common variants across driver versions.
    fake.nvmlDeviceGetName.return_value = "NVIDIA H100  80GB HBM3"
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        name = nvml_reader.get_product_name("fake-handle")
    assert name == "nvidia h100 80gb hbm3"


def test_get_product_name_handles_bytes_input():
    """Older pynvml versions return bytes; wrapper decodes."""
    from dexcost import nvml_reader
    fake = MagicMock()
    fake.NVMLError = type("NVMLError", (Exception,), {})
    fake.nvmlDeviceGetName.return_value = b"NVIDIA A10G"
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        assert nvml_reader.get_product_name("fake-handle") == "nvidia a10g"


def test_get_compute_running_processes_returns_list():
    from dexcost import nvml_reader
    fake = MagicMock()
    fake.NVMLError = type("NVMLError", (Exception,), {})

    class FakeProcessInfo:
        def __init__(self, pid, mem):
            self.pid = pid
            self.usedGpuMemory = mem

    fake.nvmlDeviceGetComputeRunningProcesses.return_value = [
        FakeProcessInfo(1234, 1024 * 1024 * 1024),
        FakeProcessInfo(5678, 512 * 1024 * 1024),
    ]
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        procs = nvml_reader.get_compute_running_processes("h")
    assert procs is not None
    assert len(procs) == 2
    assert procs[0].pid == 1234
    assert procs[0].used_gpu_memory == 1024 * 1024 * 1024


def test_get_compute_running_processes_returns_none_on_permission_denied():
    """Decision #1 load-bearing case: non-root container → NVML denies, caller fallbacks."""
    from dexcost import nvml_reader
    nvml_reader._reset_warning_state()

    class FakeNVMLError(Exception):
        pass

    fake = MagicMock()
    fake.NVMLError = FakeNVMLError
    fake.NVML_ERROR_NO_PERMISSION = 6
    fake.nvmlDeviceGetComputeRunningProcesses.side_effect = FakeNVMLError("no permission")
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        procs = nvml_reader.get_compute_running_processes("h")
    assert procs is None


def test_get_process_utilization_updates_timestamps_in_place():
    """Decision #8: persistent lastSeenTimeStamp state across calls."""
    from dexcost import nvml_reader
    fake = MagicMock()
    fake.NVMLError = type("NVMLError", (Exception,), {})

    class FakeSample:
        def __init__(self, pid, ts, sm, mem):
            self.pid, self.timeStamp, self.smUtil, self.memUtil = pid, ts, sm, mem

    fake.nvmlDeviceGetProcessUtilization.return_value = [
        FakeSample(1234, 1_000_000, 50, 30),
        FakeSample(5678, 1_000_100, 70, 40),
    ]
    timestamps = {}
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        samples = nvml_reader.get_process_utilization("h", timestamps)
    assert 1234 in samples
    assert samples[1234].sm_util == 50
    assert samples[5678].mem_util == 40
    # Timestamps dict was updated to the per-PID last-seen values.
    assert timestamps[1234] == 1_000_000
    assert timestamps[5678] == 1_000_100


def test_get_memory_info_returns_used_and_total():
    from dexcost import nvml_reader
    fake = MagicMock()
    fake.NVMLError = type("NVMLError", (Exception,), {})

    class FakeMemInfo:
        used = 21474836480
        total = 85899345920

    fake.nvmlDeviceGetMemoryInfo.return_value = FakeMemInfo()
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        mem = nvml_reader.get_memory_info("h")
    assert mem.used_bytes == 21474836480
    assert mem.total_bytes == 85899345920


def test_get_mig_mode_returns_true_when_enabled():
    from dexcost import nvml_reader
    fake = MagicMock()
    fake.NVMLError = type("NVMLError", (Exception,), {})
    fake.NVML_DEVICE_MIG_ENABLE = 1
    fake.nvmlDeviceGetMigMode.return_value = (1, 1)  # current_mode, pending_mode
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        assert nvml_reader.get_mig_mode("h") is True


def test_get_mig_mode_returns_false_on_error():
    """Older GPUs without MIG support → fail-silent → False."""
    from dexcost import nvml_reader
    fake = MagicMock()

    class FakeNVMLError(Exception):
        pass

    fake.NVMLError = FakeNVMLError
    fake.nvmlDeviceGetMigMode.side_effect = FakeNVMLError("not supported")
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        assert nvml_reader.get_mig_mode("h") is False


def test_get_device_count_returns_count():
    from dexcost import nvml_reader
    fake = MagicMock()
    fake.NVMLError = type("NVMLError", (Exception,), {})
    fake.nvmlDeviceGetCount.return_value = 8
    with patch.object(nvml_reader, "_pynvml", fake), \
         patch.object(nvml_reader, "_NVML_AVAILABLE", True):
        assert nvml_reader.get_device_count() == 8
