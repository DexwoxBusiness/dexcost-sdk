# Kubernetes pod with NVIDIA Device Plugin

**Priority:** P0
**Setup:** apply this manifest to a GKE / EKS / on-prem K8s cluster with GPU nodes and the `nvidia-device-plugin` DaemonSet installed:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: dexcost-gpu-verify
spec:
  restartPolicy: Never
  containers:
  - name: capture
    image: nvidia/cuda:12.4.0-base-ubuntu22.04
    resources:
      limits:
        nvidia.com/gpu: 1
    command: ["bash", "-lc"]
    args:
    - |
      pip install -q nvidia-ml-py
      apt-get update -qq && apt-get install -y curl -qq
      # capture.sh inlined or fetched from this repo's URL
      bash /tmp/capture.sh /tmp/out
      cat /tmp/out/*
    volumeMounts:
    - name: capture-script
      mountPath: /tmp/capture.sh
      subPath: capture.sh
  volumes:
  - name: capture-script
    configMap:
      name: dexcost-gpu-verify-capture  # kubectl create configmap dexcost-gpu-verify-capture --from-file=capture.sh
```

Then: `kubectl logs dexcost-gpu-verify > k8s-pod-logs.txt` and split the output into the file artifacts below.

## Hypothesis (documentation-based)

**`/proc/self/cgroup` should show `0::/kubepods.slice/kubepods-<QoS>.slice/kubepods-<QoS>-pod<UID>.slice/cri-containerd-<container_id>.scope`** (cgroup v2, modern K8s 1.25+) — per [Kubernetes docs on cgroup v2](https://kubernetes.io/docs/concepts/architecture/cgroups/). The `kubepods.slice` prefix is the unambiguous signal that this is K8s. Decision #1's classification table must accept both `kubepods.slice/` AND the older `kubepods/<QoS>/<pod>/<container>` cgroup v1 layout.

The `<QoS>` token is one of `besteffort`, `burstable`, or `guaranteed` based on the pod's resource specification.

**The container's cgroup contains ONLY the container's PIDs** — not the pod's other containers. This is the [multi-container K8s pod limitation](../../decisions/2026-05-22-gpu-foundation-decisions.md#decision-1--multi-pid-gpu-attribution-the-load-bearing-one) that Decision #1's sharpening flags. The pod cgroup is one level up (`kubepods-<QoS>-pod<UID>.slice/`); reading it requires permission to read other-container `/proc/<pid>/cgroup`, which the standard pod security context denies.

**NVML init should succeed.** The NVIDIA Device Plugin assigns specific GPUs to the pod via `NVIDIA_VISIBLE_DEVICES` and bind-mounts `/dev/nvidia*`. `nvmlDeviceGetCount()` returns the count of assigned GPUs (1 in the manifest above), not the count of all GPUs on the node.

**`nvmlDeviceGetComputeRunningProcesses`** returns PIDs scoped to the host PID namespace. The pod's PID namespace is isolated; cross-namespace PID resolution is denied by default. Same handling as Docker non-root: filter by cgroup membership, treat external PIDs as non-attributable.

## Verification spike specifics

This capture should confirm:

1. **The `kubepods.slice` prefix string** — cgroup v2 layout on K8s 1.25+ matches the [kubelet cgroupfs driver](https://kubernetes.io/docs/setup/production-environment/container-runtimes/#cgroup-drivers) implementation. If the test cluster uses the systemd driver (default on many distros), the layout is `kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod<uid>.slice/<container_runtime>-<container_id>.scope`. Both layouts must be in Decision #1's classification table.

2. **Cross-container cgroup read is denied** for the multi-container case. Add a sidecar container to the manifest above with `securityContext.capabilities.add: ["SYS_PTRACE"]` and verify whether the sidecar CAN read the main container's `/proc/<pid>/cgroup`. If yes, dexcost in the sidecar could in principle attribute the main container's GPU use. If no, the multi-container limitation in Decision #1 is confirmed as a permanent v1 constraint.

3. **NVML productName format** on whatever GPU type the test cluster has (GKE A100/H100 nodes, EKS p3/p4/p5, etc.).

## Sources

- [Kubernetes cgroups concepts](https://kubernetes.io/docs/concepts/architecture/cgroups/)
- [NVIDIA Device Plugin for Kubernetes](https://github.com/NVIDIA/k8s-device-plugin)
- [Kubelet cgroup driver](https://kubernetes.io/docs/setup/production-environment/container-runtimes/#cgroup-drivers)
- [Pod security context](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/) — capabilities and cross-container access
