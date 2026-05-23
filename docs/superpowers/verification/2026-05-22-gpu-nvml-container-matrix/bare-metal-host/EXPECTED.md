# Bare-metal Python on a GPU host (no container)

**Priority:** P0 — this is the silent-overcount case that Decision #1's classification table guards against. If walked naively, the cgroup at `/proc/self/cgroup` is the systemd user slice (`user.slice/user-1000.slice/...`) which contains every PID in the SSH session, not just dexcost's task.

**Setup:** SSH to any GPU host where you have a personal account (e.g. a workstation, a long-running EC2 GPU instance, a research cluster login node). Then:

```bash
pip install --user nvidia-ml-py
bash docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/capture.sh ./out
tar czf bare-metal-capture.tar.gz out/
```

## Hypothesis (documentation-based)

**`/proc/self/cgroup` should show `0::/user.slice/user-<UID>.slice/user@<UID>.service/...`** or in older systems `0::/user.slice/user-<UID>.slice/session-<N>.scope`. Both indicate **systemd user session scope, NOT a container scope**. Decision #1's classification table flags both as the bare-metal-no-container case that degrades to self-PID-only.

**NVML init should succeed** on any host with a recent NVIDIA driver. No container layer; full access to the device.

**`nvmlDeviceGetComputeRunningProcesses` returns every PID on the host that's currently holding the GPU,** including the SSH user's other Python processes, system daemons (nvidia-persistenced, etc.), and any container workloads using the device. The cgroup-walk filter is what saves dexcost here: by detecting the bare-metal case and degrading to self-PID-only, dexcost attributes only the dexcost-instrumented process and its own children (via `/proc/<self>/task/*` for thread-level, NOT cross-process for the bare-metal case).

## Critical correctness pin

**This capture is the load-bearing one for the silent-overcount case.** If the capture shows `/proc/self/cgroup = /user.slice/...`, the spec MUST include the bare-metal degradation logic. If the capture shows something else (e.g. running under a systemd service unit gives `/system.slice/<unit>.service` — different scope, different semantics), the classification table needs an additional entry.

## What this capture answers

1. **The actual systemd user slice cgroup path** on the test host — confirms Decision #1's `user.slice/` prefix expectation
2. **Whether `nvmlDeviceGetComputeRunningProcesses` returns cross-user PIDs** without elevated privileges — confirms the silent-overcount risk is real (not theoretical)
3. **The driver version + productName** for the host's GPU — populates aliases for whichever SKU is on the test host

## Sources

- [systemd user sessions and cgroup scopes](https://www.freedesktop.org/software/systemd/man/systemd.scope.html)
- [systemd-logind session scoping](https://www.freedesktop.org/software/systemd/man/systemd-logind.service.html)
