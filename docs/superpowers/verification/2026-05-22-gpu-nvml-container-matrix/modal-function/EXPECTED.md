# Modal `@app.function(gpu="A10G")`

**Priority:** P0 — Modal is one of the two `per_gpu_second_active` providers (with RunPod) that dexcost depends on detecting correctly; both rely on NVML `productName` → catalog-key aliases (Decision #4) because Modal doesn't set a SKU env var.

**Setup:** create a Modal account ([modal.com/signup](https://modal.com/signup) — $30 free credit covers ~7-8 hours of A10G time, far more than needed for this capture). Then:

```python
# verify_modal.py
import modal

app = modal.App("dexcost-gpu-verify")
image = modal.Image.debian_slim().pip_install("nvidia-ml-py").apt_install("curl")

CAPTURE_SH = open("docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/capture.sh").read()

@app.function(gpu="A10G", image=image, timeout=120)
def capture():
    import os, subprocess, pathlib
    pathlib.Path("/tmp/capture.sh").write_text(CAPTURE_SH)
    os.chmod("/tmp/capture.sh", 0o755)
    subprocess.run(["bash", "/tmp/capture.sh", "/tmp/out"], check=False)
    out = {}
    for p in pathlib.Path("/tmp/out").iterdir():
        out[p.name] = p.read_text(errors="replace")
    return out

if __name__ == "__main__":
    with app.run():
        result = capture.remote()
    for name, content in result.items():
        print(f"\n\n========== {name} ==========\n{content}")
```

Run: `modal run verify_modal.py > modal-capture.txt`, then split into per-file artifacts.

## Hypothesis (documentation-based)

**`/proc/self/cgroup` should show a cgroup path** but Modal's runtime is opaque. Modal containers run on a proprietary container scheduler that may use Firecracker microVMs ([Modal Runtime Architecture](https://modal.com/docs/guide/runtime-architecture)) or containerd or something custom. The cgroup path may be:
- (a) A `kubepods.slice/...` path if Modal runs on K8s underneath
- (b) A `docker/<id>` or `containerd/...` path if Modal uses standard container runtimes
- (c) A custom Modal-namespaced path like `modal/<task_id>` or `firecracker/<vm_id>` if they roll their own
- (d) `0::/` (root cgroup) if running inside a microVM that doesn't expose host cgroup hierarchy

**Decision #1's classification table must accommodate whatever Modal's actual layout is.** Cases (a)/(b) match existing entries. Case (c) needs a new `modal/` or `firecracker/` prefix added. Case (d) is the "bare-metal-no-container" case that degrades to self-PID-only — which is actually fine for Modal because each function invocation gets its own microVM, so self-PID-only captures the whole task.

**NVML init should succeed.** Modal advertises NVIDIA driver and CUDA toolkit availability in their `gpu="A10G"` configs.

**`MODAL_TASK_ID`, `MODAL_IMAGE_ID`, possibly `MODAL_REGION`** env vars should be present. These are how `cloud_detect.py` resolves provider=`modal` (already implemented in Phase 1).

**`nvmlDeviceGetName` should return something like `"NVIDIA A10G"`** based on Modal's A10G config. The exact string is what Decision #4's `aliases` array needs to match.

## What this capture answers

1. **Cgroup classification:** does Modal fit cases (a)/(b)/(c)/(d) above? If (c), Decision #1's table needs a new prefix entry. If (d), document that Modal works fine on self-PID-only because each task is its own microVM.
2. **NVML productName for A10G:** populates the catalog's `aliases` array for the A10G SKU. (`"NVIDIA A10G"` is the predicted value; verify.)
3. **`MODAL_GPU` or equivalent env var:** does Modal expose the GPU SKU via env var? Research §6b-1 said no, but worth re-checking — if yes, dexcost can use the env var as a faster path than NVML lookup.

## Cost

A single `modal run` of this verify function: ~30-60 seconds at $0.000597/sec (A10G) = ~$0.02-0.04. Well within the free credit. Run it 2-3 times to confirm consistency.

## Sources

- [Modal Runtime Architecture](https://modal.com/docs/guide/runtime-architecture)
- [Modal GPU pricing & types](https://modal.com/pricing) — A10G, L4, L40S, A100, H100, B200
- [Modal env variables](https://modal.com/docs/guide/environment-variables) — `MODAL_TASK_ID`, `MODAL_IMAGE_ID`
