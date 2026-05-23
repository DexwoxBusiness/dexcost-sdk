# RunPod pod with GPU

**Priority:** P1 — RunPod is the second `per_gpu_second_active` provider after Modal; shares the same NVML-productName-aliases dependency (Decision #4).

**Setup:** create a RunPod account ([runpod.io/console/signup](https://runpod.io/console/signup) — minimum $10 top-up). Deploy the cheapest GPU pod (RTX 3090 community cloud ~$0.20/hr or T4 ~$0.16/hr). SSH or shell in via the RunPod web console, then:

```bash
pip install nvidia-ml-py
curl -L https://raw.githubusercontent.com/<repo>/docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/capture.sh -o capture.sh
bash capture.sh ./out
tar czf runpod-capture.tar.gz out/
# Download via SCP or RunPod's file browser
```

Total cost: ~$0.05-0.10 for 15-30 minutes.

## Hypothesis (documentation-based)

**RunPod runs containers on standard container runtimes** ([RunPod docs](https://docs.runpod.io/pods/overview)), likely Docker or containerd. `/proc/self/cgroup` should show a standard `docker/<id>` or `containerd/...` path, OR a custom RunPod-namespaced path.

**Env vars present:** `RUNPOD_POD_ID`, `RUNPOD_POD_HOSTNAME`, `RUNPOD_DC_ID` (region) — already used by `cloud_detect.py` from Phase 1. **Open question: does RunPod set a `RUNPOD_GPU_TYPE` env var or equivalent?** Research §6b-1 said no, but worth re-checking.

**NVML productName:** for RTX 3090 expects `"NVIDIA GeForce RTX 3090"`; for T4 expects `"Tesla T4"` or `"NVIDIA T4"`. Captures the alias strings for the catalog.

## What this capture answers

1. RunPod's cgroup-path layout — does it fit one of Decision #1's existing prefixes or need a new one?
2. Whether RunPod exposes the GPU SKU via env var (faster than NVML query path)
3. Real NVML productName strings for the SKUs in RunPod's catalog → populate aliases

## Sources

- [RunPod pod docs](https://docs.runpod.io/pods/overview)
- [RunPod env variables](https://docs.runpod.io/pods/references/environment-variables)
- [RunPod GPU pricing](https://www.runpod.io/pricing) — community cloud vs secure cloud
