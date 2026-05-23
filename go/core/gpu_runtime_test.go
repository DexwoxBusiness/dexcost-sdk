// Task 3 — GPU runtime resolver tests. Mirrors python commit 9bcb0c9.

package core

import (
	"os"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

// withNVMLMock sets up a mock NVML backend with N devices and restores
// state at the end of the test.
func withNVMLMock(t *testing.T, devices int) {
	t.Helper()
	resetNVMLForTests()
	mock := &MockNVMLBackend{
		Available:    true,
		DeviceCount:  devices,
		ProductNames: map[int]string{0: "NVIDIA H100 80GB HBM3"},
	}
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)
}

func TestGpuRuntimeKindStringValuesMatchPython(t *testing.T) {
	cases := []struct {
		k    GpuRuntimeKind
		want string
	}{
		{GpuRuntimeModal, "modal"},
		{GpuRuntimeRunpod, "runpod"},
		{GpuRuntimeReplicate, "replicate"},
		{GpuRuntimeLambdaLabs, "lambda_labs"},
		{GpuRuntimeCoreweave, "coreweave"},
		{GpuRuntimeAWSEC2GPU, "aws_ec2_gpu"},
		{GpuRuntimeGCPGCEBundled, "gcp_gce_bundled"},
		{GpuRuntimeGCPGCEN1Attached, "gcp_gce_n1_attached"},
		{GpuRuntimeAzureVMGPU, "azure_vm_gpu"},
		{GpuRuntimeAzureVMVGPU, "azure_vm_vgpu"},
		{GpuRuntimeNone, "none"},
	}
	for _, c := range cases {
		if string(c.k) != c.want {
			t.Errorf("%v = %q; want %q (cross-SDK parity)", c.k, c.k, c.want)
		}
	}
}

func TestResolveGpuRuntimeReturnsNoneWhenNVMLUnavailable(t *testing.T) {
	resetNVMLForTests() // default noop backend
	t.Cleanup(resetNVMLForTests)
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "aws", Region: "us-east-1", InstanceType: "p4d.24xlarge"})
	t.Cleanup(cloud.ResetForTests)
	t.Setenv("MODAL_TASK_ID", "set-but-no-nvml")

	if r := ResolveGpuRuntime(); r != GpuRuntimeNone {
		t.Fatalf("no NVML should yield NONE; got %v", r)
	}
}

func TestResolveGpuRuntimeReturnsNoneWhenZeroDevices(t *testing.T) {
	withNVMLMock(t, 0)
	t.Setenv("MODAL_TASK_ID", "x")
	if r := ResolveGpuRuntime(); r != GpuRuntimeNone {
		t.Fatalf("0 devices should yield NONE; got %v", r)
	}
}

func TestResolveGpuRuntimeServerlessEnvVarsWinFirst(t *testing.T) {
	withNVMLMock(t, 1)
	// Set ALL serverless env vars at once; Modal takes priority.
	t.Setenv("MODAL_TASK_ID", "m1")
	t.Setenv("RUNPOD_POD_ID", "r1")
	t.Setenv("REPLICATE_MODEL", "rep1")
	if r := ResolveGpuRuntime(); r != GpuRuntimeModal {
		t.Fatalf("Modal env var should win; got %v", r)
	}

	os.Unsetenv("MODAL_TASK_ID")
	os.Unsetenv("MODAL_IMAGE_ID")
	if r := ResolveGpuRuntime(); r != GpuRuntimeRunpod {
		t.Fatalf("RunPod should win without Modal; got %v", r)
	}

	os.Unsetenv("RUNPOD_POD_ID")
	if r := ResolveGpuRuntime(); r != GpuRuntimeReplicate {
		t.Fatalf("Replicate should win as last serverless option; got %v", r)
	}
}

func TestResolveGpuRuntimeLambdaLabsByCloudDetect(t *testing.T) {
	withNVMLMock(t, 1)
	os.Unsetenv("MODAL_TASK_ID")
	os.Unsetenv("MODAL_IMAGE_ID")
	os.Unsetenv("RUNPOD_POD_ID")
	os.Unsetenv("RUNPOD_POD_HOSTNAME")
	os.Unsetenv("REPLICATE_MODEL")
	os.Unsetenv("REPLICATE_PREDICTION_ID")
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "lambda_labs"})
	t.Cleanup(cloud.ResetForTests)
	if r := ResolveGpuRuntime(); r != GpuRuntimeLambdaLabs {
		t.Fatalf("lambda_labs cloud should yield LAMBDA_LABS; got %v", r)
	}
}

func TestResolveGpuRuntimeCoreweaveByCloudDetect(t *testing.T) {
	withNVMLMock(t, 1)
	for _, k := range []string{"MODAL_TASK_ID", "MODAL_IMAGE_ID", "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "REPLICATE_MODEL", "REPLICATE_PREDICTION_ID"} {
		os.Unsetenv(k)
	}
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "coreweave"})
	t.Cleanup(cloud.ResetForTests)
	if r := ResolveGpuRuntime(); r != GpuRuntimeCoreweave {
		t.Fatalf("coreweave cloud should yield COREWEAVE; got %v", r)
	}
}

func TestResolveGpuRuntimeAWSEC2GPUByInstanceFamily(t *testing.T) {
	withNVMLMock(t, 1)
	for _, k := range []string{"MODAL_TASK_ID", "MODAL_IMAGE_ID", "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "REPLICATE_MODEL", "REPLICATE_PREDICTION_ID"} {
		os.Unsetenv(k)
	}
	families := []string{"g4dn.xlarge", "g5.xlarge", "g6e.4xlarge", "p3.2xlarge", "p4d.24xlarge", "p4de.24xlarge", "p5.48xlarge", "p5e.48xlarge", "p5en.48xlarge"}
	for _, instance := range families {
		cloud.SetResultForTests(cloud.CloudEnv{Provider: "aws", Region: "us-east-1", InstanceType: instance})
		t.Cleanup(cloud.ResetForTests)
		if r := ResolveGpuRuntime(); r != GpuRuntimeAWSEC2GPU {
			t.Errorf("%s should yield AWS_EC2_GPU; got %v", instance, r)
		}
	}
}

func TestResolveGpuRuntimeGCPBundledFamilies(t *testing.T) {
	withNVMLMock(t, 1)
	for _, k := range []string{"MODAL_TASK_ID", "MODAL_IMAGE_ID", "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "REPLICATE_MODEL", "REPLICATE_PREDICTION_ID"} {
		os.Unsetenv(k)
	}
	for _, instance := range []string{"a2-highgpu-1g", "a3-megagpu-8g", "g2-standard-4"} {
		cloud.SetResultForTests(cloud.CloudEnv{Provider: "gcp", Region: "us-central1", InstanceType: instance})
		t.Cleanup(cloud.ResetForTests)
		if r := ResolveGpuRuntime(); r != GpuRuntimeGCPGCEBundled {
			t.Errorf("%s should yield GCP_GCE_BUNDLED; got %v", instance, r)
		}
	}
}

func TestResolveGpuRuntimeGCPN1AttachedDecision9(t *testing.T) {
	withNVMLMock(t, 1)
	for _, k := range []string{"MODAL_TASK_ID", "MODAL_IMAGE_ID", "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "REPLICATE_MODEL", "REPLICATE_PREDICTION_ID"} {
		os.Unsetenv(k)
	}
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "gcp", Region: "us-central1", InstanceType: "n1-standard-8"})
	t.Cleanup(cloud.ResetForTests)
	if r := ResolveGpuRuntime(); r != GpuRuntimeGCPGCEN1Attached {
		t.Fatalf("N1 + NVML should yield GCP_GCE_N1_ATTACHED (Decision #9); got %v", r)
	}
}

func TestResolveGpuRuntimeAzureFamilies(t *testing.T) {
	withNVMLMock(t, 1)
	for _, k := range []string{"MODAL_TASK_ID", "MODAL_IMAGE_ID", "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "REPLICATE_MODEL", "REPLICATE_PREDICTION_ID"} {
		os.Unsetenv(k)
	}
	for _, instance := range []string{"Standard_ND96isr_H100_v5", "Standard_NC24ads_A100_v4"} {
		cloud.SetResultForTests(cloud.CloudEnv{Provider: "azure", Region: "eastus", InstanceType: instance})
		t.Cleanup(cloud.ResetForTests)
		if r := ResolveGpuRuntime(); r != GpuRuntimeAzureVMGPU {
			t.Errorf("%s should yield AZURE_VM_GPU; got %v", instance, r)
		}
	}
}

func TestResolveGpuRuntimeAzureVGPUDecision10(t *testing.T) {
	withNVMLMock(t, 1)
	for _, k := range []string{"MODAL_TASK_ID", "MODAL_IMAGE_ID", "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "REPLICATE_MODEL", "REPLICATE_PREDICTION_ID"} {
		os.Unsetenv(k)
	}
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "azure", Region: "eastus", InstanceType: "Standard_NV6ads_A10_v5"})
	t.Cleanup(cloud.ResetForTests)
	if r := ResolveGpuRuntime(); r != GpuRuntimeAzureVMVGPU {
		t.Fatalf("NVadsA10_v5 should yield AZURE_VM_VGPU (Decision #10); got %v", r)
	}
}

func TestResolveGpuRuntimeUnknownCloudFallsToNone(t *testing.T) {
	withNVMLMock(t, 1)
	for _, k := range []string{"MODAL_TASK_ID", "MODAL_IMAGE_ID", "RUNPOD_POD_ID", "RUNPOD_POD_HOSTNAME", "REPLICATE_MODEL", "REPLICATE_PREDICTION_ID"} {
		os.Unsetenv(k)
	}
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "aws", Region: "us-east-1", InstanceType: "t3.micro"})
	t.Cleanup(cloud.ResetForTests)
	if r := ResolveGpuRuntime(); r != GpuRuntimeNone {
		t.Fatalf("non-GPU AWS instance should yield NONE; got %v", r)
	}
}
