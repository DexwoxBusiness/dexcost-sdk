#!/usr/bin/env bash
# GPU NVML × Container-Mode Verification Capture
#
# Run this script inside the target environment (Docker container, K8s pod,
# Modal function, bare-metal SSH session, etc.) and pipe the output into
# the matching environment directory's files.
#
# Usage:
#   curl -L https://raw.githubusercontent.com/.../capture.sh | bash > capture.txt
#   # then split capture.txt into the per-file artifacts manually, OR:
#   bash capture.sh ./out_dir/
#
# The script is read-only — it makes no system changes and only collects
# diagnostic information. Safe to run in any environment.

set -u

OUT_DIR="${1:-.}"
mkdir -p "$OUT_DIR"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
section() { printf '\n========== %s ==========\n' "$1"; }

{
  section "capture metadata"
  echo "captured_at: $(ts)"
  echo "hostname: $(hostname 2>/dev/null || echo unknown)"
  echo "kernel: $(uname -r)"
  echo "uid_gid: $(id 2>/dev/null || echo unknown)"
  echo "container_env_hints:"
  echo "  ECS_CONTAINER_METADATA_URI_V4=${ECS_CONTAINER_METADATA_URI_V4:-}"
  echo "  KUBERNETES_SERVICE_HOST=${KUBERNETES_SERVICE_HOST:-}"
  echo "  MODAL_TASK_ID=${MODAL_TASK_ID:-}"
  echo "  RUNPOD_POD_ID=${RUNPOD_POD_ID:-}"
  echo "  AWS_LAMBDA_FUNCTION_NAME=${AWS_LAMBDA_FUNCTION_NAME:-}"
  echo "  K_SERVICE=${K_SERVICE:-}"
  echo "  NVIDIA_VISIBLE_DEVICES=${NVIDIA_VISIBLE_DEVICES:-}"
} > "$OUT_DIR/metadata.txt"

# /proc/self/cgroup
cat /proc/self/cgroup > "$OUT_DIR/proc-self-cgroup.txt" 2>&1 || true

# mountinfo cgroup lines (distinguishes cgroup v1 vs v2 vs hybrid)
grep -E 'cgroup' /proc/self/mountinfo > "$OUT_DIR/mountinfo-cgroup.txt" 2>&1 || true

# resolved cgroup procs — cgroup v2 path is /proc/self/cgroup with prefix 0::
CGROUP_V2_PATH=$(awk -F'::' '/^0::/{print $2; exit}' /proc/self/cgroup 2>/dev/null)
if [[ -n "$CGROUP_V2_PATH" ]]; then
  {
    echo "# cgroup v2 path: $CGROUP_V2_PATH"
    echo "# resolved to: /sys/fs/cgroup${CGROUP_V2_PATH}/cgroup.procs"
    echo "# contents:"
    cat "/sys/fs/cgroup${CGROUP_V2_PATH}/cgroup.procs" 2>&1
  } > "$OUT_DIR/cgroup-procs.txt"
else
  echo "# cgroup v1 detected — multiple controllers, see proc-self-cgroup.txt" > "$OUT_DIR/cgroup-procs.txt"
fi

# nvidia-smi -q (full NVML query — device list, driver, MIG mode, productName)
nvidia-smi -q > "$OUT_DIR/nvidia-smi-query.txt" 2>&1 || echo "nvidia-smi -q failed: $?" > "$OUT_DIR/nvidia-smi-query.txt"

# nvidia-smi pmon (per-process GPU utilization snapshot — the data NVML reports to dexcost)
nvidia-smi pmon -c 1 > "$OUT_DIR/nvidia-smi-pmon.txt" 2>&1 || echo "nvidia-smi pmon failed: $?" > "$OUT_DIR/nvidia-smi-pmon.txt"

# NVML init via Python (tests whether the binding can actually open the device)
python3 -c "
import pynvml
try:
    pynvml.nvmlInit()
    count = pynvml.nvmlDeviceGetCount()
    print(f'nvmlInit: OK, device count = {count}')
    for i in range(count):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        try:
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes): name = name.decode()
            print(f'  device {i}: productName = {name!r}')
        except pynvml.NVMLError as e:
            print(f'  device {i}: nvmlDeviceGetName failed: {e}')
        try:
            procs = pynvml.nvmlDeviceGetComputeRunningProcesses(h)
            print(f'  device {i}: nvmlDeviceGetComputeRunningProcesses returned {len(procs)} processes')
            for p in procs[:10]:
                print(f'    pid={p.pid} usedMemory={getattr(p, \"usedGpuMemory\", \"n/a\")}')
        except pynvml.NVMLError as e:
            print(f'  device {i}: nvmlDeviceGetComputeRunningProcesses failed: {e}')
        try:
            ts = 0
            utils = pynvml.nvmlDeviceGetProcessUtilization(h, ts)
            print(f'  device {i}: nvmlDeviceGetProcessUtilization returned {len(utils)} samples')
            for u in utils[:10]:
                print(f'    pid={u.pid} smUtil={u.smUtil} memUtil={u.memUtil} timeStamp={u.timeStamp}')
        except pynvml.NVMLError as e:
            print(f'  device {i}: nvmlDeviceGetProcessUtilization failed: {e}')
    pynvml.nvmlShutdown()
except pynvml.NVMLError as e:
    print(f'nvmlInit failed: {e}')
except ImportError:
    print('pynvml not installed; install with: pip install nvidia-ml-py')
" > "$OUT_DIR/nvml-init.log" 2>&1 || true

echo
echo "Captured to $OUT_DIR/"
ls -la "$OUT_DIR/"
