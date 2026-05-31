// Cross-runtime regression matrix — one priced event per billing_model value.
// Mirrors python/tests/test_compute_cross_runtime_matrix.py.

package pricing

import (
	"strings"
	"testing"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/cloud"
)

func envFor(provider, region, instanceType string) cloud.CloudEnv {
	return cloud.CloudEnv{
		Provider: provider, Region: region, Source: "env",
		InstanceType: instanceType,
	}
}

func TestDispatchLambda(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "lambda", "duration_ms": 100,
		"memory_bytes_limit": int64(1024) * 1024 * 1024, "vcpu_count": 1.0,
		"vcpu_seconds_used": 0, "invocation_count": 1,
		"region": "us-east-1", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("aws", "us-east-1", ""), nil, decimal.Zero)
	assertDispatch(t, cost, "lambda")
}

func TestDispatchFargate(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "fargate", "duration_ms": 60_000,
		"memory_bytes_limit": int64(1024) * 1024 * 1024, "vcpu_count": 0.5,
		"vcpu_seconds_used": 30, "invocation_count": 0,
		"region": "us-east-1", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("aws", "us-east-1", ""), nil, decimal.NewFromInt(60))
	assertDispatch(t, cost, "fargate")
}

func TestDispatchCloudRunRequest(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "cloud_run_request", "duration_ms": 250,
		"memory_bytes_limit": int64(256) * 1024 * 1024, "vcpu_count": 0.5,
		"vcpu_seconds_used": 0, "invocation_count": 1,
		"region": "us-central1", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("gcp", "us-central1", ""), nil, decimal.Zero)
	assertDispatch(t, cost, "cloud_run")
}

func TestDispatchCloudRunInstanceOverride(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "cloud_run_request", "duration_ms": 0,
		"memory_bytes_limit": int64(256) * 1024 * 1024, "vcpu_count": 0.5,
		"vcpu_seconds_used": 0, "invocation_count": 0,
		"region": "us-central1", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("gcp", "us-central1", ""),
		map[string]string{"cloud_run": "instance"}, decimal.NewFromInt(60))
	if !cost.CostUSD.GreaterThan(decimal.Zero) {
		t.Fatalf("cost = %s, want > 0", cost.CostUSD)
	}
	if !strings.HasSuffix(cost.PricingSource, "instance_override") {
		t.Fatalf("PricingSource = %q, want suffix instance_override", cost.PricingSource)
	}
}

func TestDispatchCloudFunctions(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "cloud_functions", "duration_ms": 250,
		"memory_bytes_limit": int64(256) * 1024 * 1024, "vcpu_count": 0.5,
		"vcpu_seconds_used": 0, "invocation_count": 1,
		"region": "us-central1", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("gcp", "us-central1", ""), nil, decimal.Zero)
	assertDispatch(t, cost, "cloud_functions")
}

func TestDispatchAzureFunctions(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "azure_functions", "duration_ms": 200,
		"memory_bytes_limit": int64(512) * 1000 * 1000, "vcpu_count": 1.0,
		"vcpu_seconds_used": 0, "invocation_count": 1,
		"region": "eastus", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("azure", "eastus", ""), nil, decimal.Zero)
	assertDispatch(t, cost, "azure")
}

func TestDispatchVercelFluid(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "vercel_fluid", "duration_ms": 500,
		"memory_bytes_limit": int64(256) * 1000 * 1000, "vcpu_count": 1.0,
		"vcpu_seconds_used": 0, "invocation_count": 1,
		"region": "", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("", "", ""), nil, decimal.Zero)
	assertDispatch(t, cost, "vercel")
}

func TestDispatchEC2(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "ec2", "duration_ms": 60_000,
		"memory_bytes_limit": 0, "vcpu_count": 4.0,
		"vcpu_seconds_used": 1.0, "invocation_count": 0,
		"region": "us-east-1", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("aws", "us-east-1", "c7g.xlarge"),
		nil, decimal.NewFromInt(60))
	assertDispatch(t, cost, "ec2")
}

func TestDispatchGCE(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "gce", "duration_ms": 60_000,
		"memory_bytes_limit": 0, "vcpu_count": 2.0,
		"vcpu_seconds_used": 0.5, "invocation_count": 0,
		"region": "us-central1", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("gcp", "us-central1", "n2-standard-2"),
		nil, decimal.NewFromInt(60))
	assertDispatch(t, cost, "gce")
}

func TestDispatchAzureVM(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "azure_vm", "duration_ms": 60_000,
		"memory_bytes_limit": 0, "vcpu_count": 2.0,
		"vcpu_seconds_used": 0.5, "invocation_count": 0,
		"region": "eastus", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("azure", "eastus", "Standard_D2s_v3"),
		nil, decimal.NewFromInt(60))
	if !cost.CostUSD.GreaterThan(decimal.Zero) {
		t.Fatalf("cost = %s, want > 0", cost.CostUSD)
	}
	// catalog runtime key is "vm" — pricing_source contains "azure:vm".
	if !strings.Contains(cost.PricingSource, "azure:vm") {
		t.Fatalf("PricingSource = %q, want substring azure:vm", cost.PricingSource)
	}
}

func TestDispatchK8sPod(t *testing.T) {
	eng := NewComputePricingEngine()
	d := map[string]any{
		"billing_model": "k8s_pod", "duration_ms": 60_000,
		"memory_bytes_limit": int64(512) * 1024 * 1024, "vcpu_count": 0.5,
		"vcpu_seconds_used": 0.3, "invocation_count": 0,
		"region": "", "architecture": "x86_64",
	}
	cost := eng.ResolveComputeCost(d, envFor("", "", ""), nil, decimal.NewFromInt(60))
	assertDispatch(t, cost, "k8s_pod")
}

func assertDispatch(t *testing.T, cost ComputeCost, wantSubstr string) {
	t.Helper()
	if !cost.CostUSD.GreaterThan(decimal.Zero) {
		t.Fatalf("cost = %s, want > 0", cost.CostUSD)
	}
	if !strings.Contains(cost.PricingSource, wantSubstr) {
		t.Fatalf("PricingSource = %q, want substring %q", cost.PricingSource, wantSubstr)
	}
}
