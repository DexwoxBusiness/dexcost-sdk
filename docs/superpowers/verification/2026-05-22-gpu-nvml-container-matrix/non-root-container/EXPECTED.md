# Docker `--gpus all` running as non-root user (NVML permission case)

**Priority:** P0 — this is the case that determines whether Decision #1's fallback ladder uses `try-with-fail-silent` or `if-can-query-then-query` flow control.

**Setup:**
```
docker run --rm --gpus all --user 1000:1000 \
  -v "$PWD/docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/capture.sh:/tmp/capture.sh:ro" \
  nvidia/cuda:12.4.0-base-ubuntu22.04 \
  bash -c 'pip install --user -q nvidia-ml-py 2>/dev/null; bash /tmp/capture.sh /tmp/out 2>&1; cat /tmp/out/* 2>/dev/null'
```

(`--user 1000:1000` runs as UID 1000 inside the container — the case where the calling process doesn't have `CAP_SYS_PTRACE` and may not have read access to other PIDs' `/proc/<pid>/cgroup`.)

## Hypothesis (documentation-based)

**`/proc/self/cgroup` should be readable** by the calling process — its own cgroup info is always self-readable.

**`/sys/fs/cgroup/<path>/cgroup.procs` should be readable** by anyone in the same cgroup — it's not a privileged file. So the cgroup-walk that enumerates the container's PID list should succeed.

**The interesting question: `nvmlDeviceGetComputeRunningProcesses` permission semantics.** Per [NVML API docs](https://docs.nvidia.com/deploy/nvml-api/group__nvmlAccountingStats.html), the function returns processes the calling user has access to query. In a non-root container WITHOUT `CAP_SYS_PTRACE`, the call may:
- (a) Return only the calling PID's entry
- (b) Return all PIDs but with `usedGpuMemory = NVML_VALUE_NOT_AVAILABLE`
- (c) Return `NVML_ERROR_NO_PERMISSION`
- (d) Return all PIDs as if root (NVIDIA Container Toolkit may have already opened the device with root privileges)

**The capture confirms WHICH of these is the actual 2026 behavior.** Decision #1's fallback ladder is written assuming (c) — fail-silent on permission denied, degrade to self-PID-only. If the actual behavior is (a), the ladder still works but the "degraded" path is the default, not a fallback. If (b), the cost math needs a check for `NVML_VALUE_NOT_AVAILABLE` on `usedGpuMemory` (which would only affect the `vram_used_peak_bytes` field of `gpu_utilization_signal`, not the dollar attribution). If (d), the cgroup-walk filter is the only thing that scopes attribution to the dexcost task (no NVML-level permission check to rely on).

**`nvmlDeviceGetProcessUtilization` likely has similar semantics.** Both functions touch per-PID data.

## What the spec needs to learn

This single capture answers the question that shapes the entire spec section on Decision #1's failure handling. Specifically:

- If NVML returns `NVML_ERROR_NO_PERMISSION` for other PIDs: spec uses `try-each-pid-individually-and-skip-failures` flow.
- If NVML silently returns empty for inaccessible PIDs: spec uses `try-all-pids-and-trust-the-filter`.
- If NVML returns everything as root: spec adds the explicit cgroup-membership filter as the authoritative scope (NVML cannot be trusted to self-scope).

## Sources

- [NVML API reference](https://docs.nvidia.com/deploy/nvml-api/group__nvmlAccountingStats.html)
- [Linux capabilities — CAP_SYS_PTRACE](https://man7.org/linux/man-pages/man7/capabilities.7.html)
- [NVIDIA Container Toolkit security model](https://github.com/NVIDIA/nvidia-container-toolkit/blob/main/docs/architecture.md) — describes which capabilities the toolkit injects
