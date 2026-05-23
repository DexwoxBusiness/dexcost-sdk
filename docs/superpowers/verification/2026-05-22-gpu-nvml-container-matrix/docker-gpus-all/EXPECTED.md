# Docker `--gpus all` (standard NVIDIA Container Toolkit)

**Priority:** P0
**Setup:**
```
docker run --rm --gpus all \
  -v "$PWD/docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/capture.sh:/tmp/capture.sh:ro" \
  nvidia/cuda:12.4.0-base-ubuntu22.04 \
  bash -c 'apt-get update -qq && apt-get install -y python3-pip -qq && pip install -q nvidia-ml-py && bash /tmp/capture.sh /tmp/out && cat /tmp/out/*'
```

## Hypothesis (documentation-based)

**`/proc/self/cgroup` should show `0::/docker/<container_id>`** (cgroup v2, default Docker engine config on Ubuntu 22.04+) — per [Docker docs on cgroup v2](https://docs.docker.com/config/containers/runmetrics/#control-groups-cgroups). On older Docker / systemd-cgroup driver, may show `0::/system.slice/docker-<container_id>.scope`. Both prefixes are listed in Decision #1's classification table as container-scope.

**NVML init should succeed.** The nvidia-container-toolkit injects the NVIDIA driver libraries and `/dev/nvidia*` device nodes into the container — per [NVIDIA Container Toolkit docs](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). `nvmlInit()` returns `NVML_SUCCESS`, `nvmlDeviceGetCount()` returns the count of visible devices (filtered by `NVIDIA_VISIBLE_DEVICES`).

**`nvmlDeviceGetComputeRunningProcesses` should return all PIDs holding the device,** scoped to the host's process namespace. Inside a container the PIDs may not map to anything visible to the container's PID namespace — dexcost should treat them as "external" and not attempt cross-namespace lookups. The Decision #1 cgroup-walk filters by cgroup membership, which resolves this: PIDs in the container's cgroup are attributable; PIDs outside are not.

**`nvmlDeviceGetProcessUtilization`** should return samples for the dexcost test PID once it's run a CUDA kernel. The `productName` from `nvmlDeviceGetName` should match what the host driver reports (e.g. `"NVIDIA H100 80GB HBM3"`, `"Tesla T4"`, `"NVIDIA A10G"`).

## What the spec needs to learn from this capture

1. **Confirm cgroup prefix.** Is it `docker/` (cgroup v2 native) or `system.slice/docker-*.scope` (systemd-cgroup driver)? Both must be in the classification table; if only one observed, the table can defer the other to "untested but expected to work."
2. **Confirm `cgroup.procs` enumerates all container PIDs.** The walk that Decision #1 specifies depends on this.
3. **Confirm the `productName` string format** for alias matching (Decision #4). Does the driver report `"NVIDIA H100 80GB HBM3"` or `"H100 80GB HBM3"` or `"Tesla H100"`? The catalog aliases must match verbatim after NFC normalization.

## Sources

- [Docker cgroup driver docs](https://docs.docker.com/config/containers/runmetrics/#control-groups-cgroups)
- [NVIDIA Container Toolkit install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [NVML reference](https://docs.nvidia.com/deploy/nvml-api/) — `nvmlDeviceGetComputeRunningProcesses`, `nvmlDeviceGetProcessUtilization`
