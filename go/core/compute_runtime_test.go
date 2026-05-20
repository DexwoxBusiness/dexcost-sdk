// Compute runtime resolution — env-var cascade + cloud_detect fallback.
//
// Cascade priority (capture spec §5.5):
//  1. Serverless env vars (Lambda, Fargate, Cloud Run, Cloud Functions Gen2,
//     Azure Functions, Vercel)
//  2. KUBERNETES_SERVICE_HOST → k8s_pod (wins over the underlying VM)
//  3. cloud_detect IaaS (EC2 / GCE / Azure VM)
//  4. UNKNOWN
//
// Mirrors python/tests/test_compute_runtime.py.

package core

import (
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

// serverlessEnvVars are scrubbed before every subtest so a leaking env var
// from the host can't taint resolution.
var serverlessEnvVars = []string{
	"AWS_LAMBDA_FUNCTION_NAME",
	"ECS_CONTAINER_METADATA_URI_V4",
	"ECS_CONTAINER_METADATA_URI",
	"K_SERVICE",
	"FUNCTION_TARGET",
	"FUNCTIONS_WORKER_RUNTIME",
	"VERCEL",
	"KUBERNETES_SERVICE_HOST",
}

func scrubRuntimeEnv(t *testing.T) {
	t.Helper()
	for _, v := range serverlessEnvVars {
		t.Setenv(v, "")
	}
	cloud.ResetForTests()
	t.Cleanup(cloud.ResetForTests)
}

func TestLambdaEnvWins(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
	if got := ResolveRuntime(); got != RuntimeLambda {
		t.Fatalf("got %q, want %q", got, RuntimeLambda)
	}
}

func TestFargateEnvWins(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
	if got := ResolveRuntime(); got != RuntimeFargate {
		t.Fatalf("got %q, want %q", got, RuntimeFargate)
	}
}

func TestFargateV3EnvAlsoWorks(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("ECS_CONTAINER_METADATA_URI", "http://169.254.170.2/v3/abc")
	if got := ResolveRuntime(); got != RuntimeFargate {
		t.Fatalf("got %q, want %q", got, RuntimeFargate)
	}
}

func TestCloudRunEnvWins(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("K_SERVICE", "svc")
	if got := ResolveRuntime(); got != RuntimeCloudRun {
		t.Fatalf("got %q, want %q", got, RuntimeCloudRun)
	}
}

func TestCloudFunctionsGen2DisambiguatedFromCloudRun(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("K_SERVICE", "svc")
	t.Setenv("FUNCTION_TARGET", "main")
	if got := ResolveRuntime(); got != RuntimeCloudFunctions {
		t.Fatalf("got %q, want %q", got, RuntimeCloudFunctions)
	}
}

func TestAzureFunctionsEnvWins(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("FUNCTIONS_WORKER_RUNTIME", "python")
	if got := ResolveRuntime(); got != RuntimeAzureFunctions {
		t.Fatalf("got %q, want %q", got, RuntimeAzureFunctions)
	}
}

func TestVercelEnvWins(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("VERCEL", "1")
	if got := ResolveRuntime(); got != RuntimeVercel {
		t.Fatalf("got %q, want %q", got, RuntimeVercel)
	}
}

func TestK8sWinsOverAWSIaaS(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
	cloud.SetResultForTests(cloud.CloudEnv{
		Provider:     "aws",
		Region:       "us-east-1",
		Source:       "dmi",
		InstanceType: "c7g.xlarge",
	})
	if got := ResolveRuntime(); got != RuntimeK8sPod {
		t.Fatalf("got %q, want %q", got, RuntimeK8sPod)
	}
}

func TestFallsThroughToCloudDetectEC2(t *testing.T) {
	scrubRuntimeEnv(t)
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "aws", Region: "us-east-1", Source: "dmi"})
	if got := ResolveRuntime(); got != RuntimeEC2 {
		t.Fatalf("got %q, want %q", got, RuntimeEC2)
	}
}

func TestFallsThroughToCloudDetectGCE(t *testing.T) {
	scrubRuntimeEnv(t)
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "gcp", Region: "us-central1", Source: "imds"})
	if got := ResolveRuntime(); got != RuntimeGCE {
		t.Fatalf("got %q, want %q", got, RuntimeGCE)
	}
}

func TestFallsThroughToCloudDetectAzureVM(t *testing.T) {
	scrubRuntimeEnv(t)
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "azure", Region: "eastus", Source: "imds"})
	if got := ResolveRuntime(); got != RuntimeAzureVM {
		t.Fatalf("got %q, want %q", got, RuntimeAzureVM)
	}
}

func TestUndetectedReturnsUnknown(t *testing.T) {
	scrubRuntimeEnv(t)
	cloud.SetResultForTests(cloud.CloudEnv{Source: "none"})
	if got := ResolveRuntime(); got != RuntimeUnknown {
		t.Fatalf("got %q, want %q", got, RuntimeUnknown)
	}
}

func TestServerlessWinsOverIaaS(t *testing.T) {
	scrubRuntimeEnv(t)
	t.Setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "aws", Region: "us-east-1", Source: "dmi"})
	if got := ResolveRuntime(); got != RuntimeLambda {
		t.Fatalf("got %q, want %q", got, RuntimeLambda)
	}
}

// TestRuntimeKindStringValuesMatchPython pins the wire-format strings used
// in compute_cost event details — cross-SDK event portability depends on
// every SDK emitting the same discriminator strings.
func TestRuntimeKindStringValuesMatchPython(t *testing.T) {
	cases := map[RuntimeKind]string{
		RuntimeLambda:         "lambda",
		RuntimeFargate:        "fargate",
		RuntimeEC2:            "ec2",
		RuntimeCloudRun:       "cloud_run",
		RuntimeCloudFunctions: "cloud_functions",
		RuntimeGCE:            "gce",
		RuntimeAzureFunctions: "azure_functions",
		RuntimeAzureVM:        "azure_vm",
		RuntimeVercel:         "vercel_fluid",
		RuntimeK8sPod:         "k8s_pod",
		RuntimeUnknown:        "unknown",
	}
	for got, want := range cases {
		if string(got) != want {
			t.Errorf("RuntimeKind %v = %q, want %q (Python-compat)", got, string(got), want)
		}
	}
}
