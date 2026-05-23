# Docker container WITHOUT `--gpus` flag (NVML init failure case)

**Priority:** P1 — confirms the fail-silent ladder behaves correctly when the customer's container doesn't have GPU access at all.

**Setup:**
```
docker run --rm \
  -v "$PWD/docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/capture.sh:/tmp/capture.sh:ro" \
  nvidia/cuda:12.4.0-base-ubuntu22.04 \
  bash -c 'pip install -q nvidia-ml-py && bash /tmp/capture.sh /tmp/out; cat /tmp/out/*'
```

(Same as `docker-gpus-all/` but omits `--gpus all`.)

## Hypothesis (documentation-based)

**`/proc/self/cgroup`** still shows `0::/docker/<container_id>` — the cgroup classification is identical to the `docker-gpus-all/` case.

**`nvidia-smi`** likely fails with `Failed to initialize NVML: Driver/library version mismatch` or `couldn't communicate with the NVIDIA driver` because `/dev/nvidia*` device nodes aren't mounted.

**NVML init (`nvmlInit()`)** should return `NVML_ERROR_DRIVER_NOT_LOADED` or `NVML_ERROR_LIBRARY_NOT_FOUND`. dexcost-GPU's `compute_runtime` resolver must treat this as "no GPU stack present" and skip GPU capture entirely (no events emitted, no error raised to the customer).

## What this capture confirms

The fail-silent contract from convention §9: dexcost works on any customer host, GPU or not. If NVML can't init, no GPU events emitted — that's the design. This capture pins the exact error code so the spec's error handling can be specific (`NVML_ERROR_DRIVER_NOT_LOADED` → silent no-op + log-once `gpu_no_driver_in_container`).

## Sources

- [NVML error codes](https://docs.nvidia.com/deploy/nvml-api/group__nvmlReturnEnums.html)
