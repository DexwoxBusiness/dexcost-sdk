// Active GPU runtime resolver — Phase 2 GPU foundation Task 3.
//
// Mirrors python/src/dexcost/gpu_runtime.py.
//
// Sibling of compute_runtime.go. compute_runtime answers "which compute
// billing model"; gpu_runtime answers "which GPU billing model (if any)".
//
// Cascade priority (capture spec §5.5):
//
//  1. Serverless GPU env vars — MODAL_TASK_ID / RUNPOD_POD_ID /
//     REPLICATE_MODEL win immediately when NVML is available.
//  2. IaaS GPU via cloud_detect — provider+instance_type regex match AND
//     NVML reports ≥1 device.
//  3. Reserved-GPU providers — Lambda Labs / CoreWeave from cloud_detect.
//  4. NONE when NVML isn't available, reports 0 devices, or the runtime
//     isn't covered (Decision #5 — NVIDIA only).

package core

import (
	"os"
	"regexp"

	"github.com/DexwoxBusiness/dexcost-sdk/go/cloud"
)

// GpuRuntimeKind is the active GPU runtime discriminator. String values
// MUST match Python (gpu_runtime.GpuRuntimeKind) byte-for-byte.
type GpuRuntimeKind string

const (
	GpuRuntimeModal            GpuRuntimeKind = "modal"
	GpuRuntimeRunpod           GpuRuntimeKind = "runpod"
	GpuRuntimeReplicate        GpuRuntimeKind = "replicate"
	GpuRuntimeLambdaLabs       GpuRuntimeKind = "lambda_labs"
	GpuRuntimeCoreweave        GpuRuntimeKind = "coreweave"
	GpuRuntimeAWSEC2GPU        GpuRuntimeKind = "aws_ec2_gpu"
	GpuRuntimeGCPGCEBundled    GpuRuntimeKind = "gcp_gce_bundled"
	GpuRuntimeGCPGCEN1Attached GpuRuntimeKind = "gcp_gce_n1_attached"
	GpuRuntimeAzureVMGPU       GpuRuntimeKind = "azure_vm_gpu"
	GpuRuntimeAzureVMVGPU      GpuRuntimeKind = "azure_vm_vgpu"
	GpuRuntimeNone             GpuRuntimeKind = "none"
)

// Instance-family matchers.

var (
	awsGPUFamilyRE        = regexp.MustCompile(`(?i)^(g4|g4dn|g5|g5g|g6|g6e|p3|p4d|p4de|p5|p5e|p5en)\.`)
	gcpBundledGPUFamilyRE = regexp.MustCompile(`(?i)^(a2|a3|a4|g2)-`)
	gcpN1FamilyRE         = regexp.MustCompile(`(?i)^n1-`)
	azureGPUFamilyRE      = regexp.MustCompile(`(?i)^Standard_(ND|NC)`)
	azureVGPUFamilyRE     = regexp.MustCompile(`(?i)^Standard_NV\d+ads_A10_v5`)
)

func isAWSGPUInstance(instanceType string) bool {
	return instanceType != "" && awsGPUFamilyRE.MatchString(instanceType)
}

func isGCPBundledGPUInstance(instanceType string) bool {
	return instanceType != "" && gcpBundledGPUFamilyRE.MatchString(instanceType)
}

func isGCPN1Instance(instanceType string) bool {
	return instanceType != "" && gcpN1FamilyRE.MatchString(instanceType)
}

func isAzureVGPUInstance(instanceType string) bool {
	return instanceType != "" && azureVGPUFamilyRE.MatchString(instanceType)
}

func isAzureGPUInstance(instanceType string) bool {
	return instanceType != "" && azureGPUFamilyRE.MatchString(instanceType)
}

// ResolveGpuRuntime returns the active GPU runtime, or NONE when no GPU.
// Short-circuits on the FIRST positive match. NVML must be available AND
// see ≥1 device for any positive result.
func ResolveGpuRuntime() GpuRuntimeKind {
	if !NVMLAvailable() {
		return GpuRuntimeNone
	}
	count := GetNVMLDeviceCount()
	if count == nil || *count <= 0 {
		return GpuRuntimeNone
	}

	// 1. Serverless env vars — fastest path.
	if os.Getenv("MODAL_TASK_ID") != "" || os.Getenv("MODAL_IMAGE_ID") != "" {
		return GpuRuntimeModal
	}
	if os.Getenv("RUNPOD_POD_ID") != "" || os.Getenv("RUNPOD_POD_HOSTNAME") != "" {
		return GpuRuntimeRunpod
	}
	if os.Getenv("REPLICATE_MODEL") != "" || os.Getenv("REPLICATE_PREDICTION_ID") != "" {
		return GpuRuntimeReplicate
	}

	// 2/3. Cloud_detect IaaS + reserved providers.
	env := cloud.GetCloudEnv()
	provider := env.Provider
	instanceType := env.InstanceType

	if provider == "lambda_labs" {
		return GpuRuntimeLambdaLabs
	}
	if provider == "coreweave" {
		return GpuRuntimeCoreweave
	}

	if provider == "aws" && isAWSGPUInstance(instanceType) {
		return GpuRuntimeAWSEC2GPU
	}

	if provider == "gcp" {
		if isGCPBundledGPUInstance(instanceType) {
			return GpuRuntimeGCPGCEBundled
		}
		if isGCPN1Instance(instanceType) {
			return GpuRuntimeGCPGCEN1Attached
		}
	}

	if provider == "azure" {
		// vGPU first — more specific regex.
		if isAzureVGPUInstance(instanceType) {
			return GpuRuntimeAzureVMVGPU
		}
		if isAzureGPUInstance(instanceType) {
			return GpuRuntimeAzureVMGPU
		}
	}

	return GpuRuntimeNone
}
