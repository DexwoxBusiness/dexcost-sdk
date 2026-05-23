# CoreWeave Kubernetes pod with GPU

**Priority:** P1 — verifies CoreWeave's node-label namespace ([research §6b-5](../../research/2026-05-22-gpu-foundation-research.md#6b-coreweave-node-label-namespace)). Decision #1's cgroup-walk is K8s-standard (covered by `k8s-nvidia-device-plugin/`); this capture specifically resolves the detection signal for "we're on CoreWeave."

**Setup:** CoreWeave trial account ([coreweave.com/contact-sales](https://www.coreweave.com/contact-sales) — typically a $300-500 trial credit). Apply a similar manifest to the `k8s-nvidia-device-plugin/` one, but also capture node labels:

```bash
# Inside the pod, after running capture.sh:
kubectl get nodes -o yaml > all-nodes.yaml  # if pod has kubectl access
# OR via Downward API in pod spec:
#   env:
#   - name: NODE_NAME
#     valueFrom: { fieldRef: { fieldPath: spec.nodeName } }
# Then from the pod, query the K8s API for that node's labels.

# Specifically grep for namespace prefixes:
grep -E 'coreweave|nvidia\.com|gpu\.product' all-nodes.yaml
```

## Hypothesis (documentation-based)

**Two competing hypotheses** (research §6b-5 couldn't resolve which is actual):

- **(A)** CoreWeave uses standard NVIDIA Device Plugin labels: `nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3`, `nvidia.com/gpu.memory=81920`, etc. If so, detection cascade in dexcost uses the same K8s+NVIDIA labels as GKE / EKS — no CoreWeave-specific code needed.

- **(B)** CoreWeave adds its own `coreweave.cloud/*` namespace labels (e.g. `coreweave.cloud/gpu.sku=h100-hgx-sxm5`, `coreweave.cloud/node.tier=...`). If so, dexcost's detection has a positive CoreWeave signal independent of the generic NVIDIA labels — useful because CoreWeave's per-GPU-hour pricing differs from on-prem K8s.

**The capture confirms which.** Whichever is observed becomes the spec's CoreWeave detection signal in `compute_runtime` (currently `k8s_pod`; may gain a `k8s_pod_coreweave` variant for billing-model dispatch).

## What this capture answers

1. CoreWeave node label namespace (A or B above)
2. Whether the cgroup layout matches the standard K8s case in `k8s-nvidia-device-plugin/EXPECTED.md` (expected yes; CoreWeave uses standard kubelet + containerd)
3. CoreWeave's published GPU SKU canonical names → catalog aliases (e.g. their "H100 HGX" naming vs Lambda's "H100 SXM5" naming for the same physical chip)

## Sources

- [CoreWeave Kubernetes docs](https://docs.coreweave.com/coreweave-kubernetes/getting-started)
- [CoreWeave GPU types](https://docs.coreweave.com/coreweave-kubernetes/node-types)
- [NVIDIA GPU Operator labels](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html) — `nvidia.com/gpu.product` and related
