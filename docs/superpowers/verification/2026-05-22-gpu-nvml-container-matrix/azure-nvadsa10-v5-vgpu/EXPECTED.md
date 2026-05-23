# Azure `Standard_NV6ads_A10_v5` (vGPU profile distinction)

**Priority:** P0 — this is the verification spike that [Decision #10](../../decisions/2026-05-22-gpu-foundation-decisions.md#decision-10--vgpu-profile-resolution-azure-nvadsa10-v5) explicitly calls out. The answer determines whether dexcost-GPU catalogs Azure NVadsA10 v5 as 3 separate SKUs (1/6, 1/3, full A10) or as one A10 SKU with an "estimated full-A10 assumption" caveat.

**Setup:** in an Azure subscription, deploy a `Standard_NV6ads_A10_v5` VM (the smallest NV-series vGPU SKU, ~$0.91/hr eastus on-demand). Total cost for this spike: ~$1-2 for 30-60 minutes.

```bash
# Provision (assuming Azure CLI configured)
az group create --name dexcost-gpu-verify --location eastus
az vm create \
  --resource-group dexcost-gpu-verify \
  --name verify-nv6 \
  --image Ubuntu2204 \
  --size Standard_NV6ads_A10_v5 \
  --admin-username azureuser \
  --generate-ssh-keys \
  --public-ip-sku Standard

# SSH in, install nvidia-driver + capture deps
ssh azureuser@<public-ip>
sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common python3-pip
sudo ubuntu-drivers autoinstall  # installs the NVIDIA grid driver
sudo reboot

# After reboot, SSH back in and run capture
ssh azureuser@<public-ip>
pip3 install --user nvidia-ml-py
curl -L https://raw.githubusercontent.com/<repo>/docs/superpowers/verification/2026-05-22-gpu-nvml-container-matrix/capture.sh -o capture.sh
chmod +x capture.sh
./capture.sh ./out
tar czf nv6-capture.tar.gz out/

# Pull artifacts back to repo
scp azureuser@<public-ip>:nv6-capture.tar.gz ./

# Tear down (IMPORTANT — Azure NV-series bills hourly)
az group delete --name dexcost-gpu-verify --yes --no-wait
```

If possible, also provision `Standard_NV12ads_A10_v5` (1/3 A10) and `Standard_NV36ads_A10_v5` (full A10) and capture each separately — costs another $1-2 per SKU. Comparing the three captures answers the central question definitively.

## Hypothesis (documentation-based)

**Azure NVadsA10 v5 sells fractional A10 profiles via NVIDIA vGPU (NVIDIA's data-center virtualization tech).** The profiles are:
- `Standard_NV6ads_A10_v5`  — 1/6 A10 (6 vCPU, 55 GB RAM, ~4 GB vGPU)
- `Standard_NV12ads_A10_v5` — 1/3 A10 (12 vCPU, 110 GB RAM, ~8 GB vGPU)
- `Standard_NV18ads_A10_v5` — 1/2 A10 (18 vCPU, 220 GB RAM, ~12 GB vGPU)
- `Standard_NV36ads_A10_v5` — full A10 (36 vCPU, 440 GB RAM, 24 GB vGPU)
- `Standard_NV36adms_A10_v5` — full A10 with more RAM
- `Standard_NV72ads_A10_v5` — 2× A10 (full GPUs, dual A10)

Per [NVIDIA vGPU documentation](https://docs.nvidia.com/grid/15.0/grid-vgpu-user-guide/index.html), vGPU profiles are exposed to guest VMs as virtual GPU devices. The `nvidia-smi` output inside a vGPU-enabled VM may show:

**Option (A):** `productName = "NVIDIA A10-4Q"` for the 1/6 profile, `"NVIDIA A10-8Q"` for 1/3, `"NVIDIA A10-24Q"` for full. The `-NQ` suffix is NVIDIA's vGPU profile naming convention (Q = compute-capable Quadro profile; A = compute-only). If this is what's observed, **the catalog can distinguish profiles via NVML alone** and Decision #10 resolves to "per-profile SKUs in catalog."

**Option (B):** `productName = "NVIDIA A10"` flat across all profiles, with the profile only discoverable via Azure Instance Metadata Service. If observed, dexcost falls back to the IMDS `vmSize` (already captured in Phase 1 `CloudEnv.instance_type`) to disambiguate. Catalog still carries per-profile SKUs; resolution path is "look up vmSize first, then fall through to NVML productName."

**Option (C):** `productName = "NVIDIA A10"` and vGPU profile not discoverable by any in-VM signal. If observed, Decision #10's "full-A10 assumption with `estimated` confidence" is the locked behavior — the catalog has one A10 SKU and customers on fractional profiles see over-attribution.

## What this capture answers

The hypothesis above lists three possible outcomes; the capture distinguishes them by reading `nvmlDeviceGetName` (the NVML productName) and `/sys/class/nvidia/<dev>/...` if available, on each profile. The answer determines:

- Whether `gpu_prices.json` carries `a10-vgpu-1of6`, `a10-vgpu-1of3`, `a10-vgpu-1of2`, `a10` as separate SKUs (cases A and B), or just `a10` (case C)
- Whether the spec's Azure NV-series detection path looks up vmSize OR NVML productName first
- Whether the customer-facing confidence is `computed` (cases A and B) or `estimated` (case C)

## Sources

- [Azure NVadsA10 v5 series VM SKUs](https://learn.microsoft.com/en-us/azure/virtual-machines/nva10v5-series)
- [Azure NV-series pricing](https://azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/) — filter for NV-series
- [NVIDIA vGPU User Guide](https://docs.nvidia.com/grid/15.0/grid-vgpu-user-guide/index.html) — Q/A profile naming
- [Azure Instance Metadata Service](https://learn.microsoft.com/en-us/azure/virtual-machines/instance-metadata-service) — `compute.vmSize` field
