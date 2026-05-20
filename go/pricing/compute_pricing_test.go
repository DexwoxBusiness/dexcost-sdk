// Compute pricing — per-billing-model math, degradation ladder, no-float-drift.
// Mirrors python/tests/test_compute_pricing.py.

package pricing

import (
	"path/filepath"
	"testing"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

func resetComputeWarnings(t *testing.T) {
	t.Helper()
	ResetComputeWarningStateForTests()
	t.Cleanup(ResetComputeWarningStateForTests)
}

func newTestEnv(provider, region, instanceType string) cloud.CloudEnv {
	return cloud.CloudEnv{
		Provider:     provider,
		Region:       region,
		Source:       "env",
		InstanceType: instanceType,
	}
}

// ─── Lambda ─────────────────────────────────────────────────────────────

func TestLambdaX86CanonicalCase(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":       "lambda",
		"duration_ms":         100,
		"memory_bytes_limit":  int64(1024) * 1024 * 1024,
		"vcpu_count":          1.0,
		"vcpu_seconds_used":   0,
		"invocation_count":    1,
		"region":              "us-east-1",
		"architecture":        "x86_64",
	}
	cost := eng.ResolveComputeCost(details, newTestEnv("aws", "us-east-1", ""), nil, decimal.Zero)

	gbSeconds := decimal.NewFromInt(int64(1024) * 1024 * 1024).
		Div(decimal.NewFromInt(1_000_000_000)).
		Mul(decimal.NewFromFloat(0.1))
	expected := decimal.RequireFromString("0.0000002").Add(
		gbSeconds.Mul(decimal.RequireFromString("0.0000166667")))
	if !cost.CostUSD.Equal(expected) {
		t.Fatalf("CostUSD = %s, want %s", cost.CostUSD, expected)
	}
	if cost.CostConfidence != "computed" {
		t.Fatalf("Confidence = %s, want computed", cost.CostConfidence)
	}
	if cost.PricingSource != "compute_catalog:aws:lambda:us-east-1:x86_64" {
		t.Fatalf("PricingSource = %s", cost.PricingSource)
	}
}

func TestLambdaARMIsCheaper(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	base := func(arch string) map[string]any {
		return map[string]any{
			"billing_model":      "lambda",
			"duration_ms":        100,
			"memory_bytes_limit": int64(1024) * 1024 * 1024,
			"vcpu_count":         1.0,
			"vcpu_seconds_used":  0,
			"invocation_count":   1,
			"region":             "us-east-1",
			"architecture":       arch,
		}
	}
	x86 := eng.ResolveComputeCost(base("x86_64"), newTestEnv("aws", "us-east-1", ""), nil, decimal.Zero)
	arm := eng.ResolveComputeCost(base("arm64"), newTestEnv("aws", "us-east-1", ""), nil, decimal.Zero)
	if !arm.CostUSD.LessThan(x86.CostUSD) {
		t.Fatalf("arm (%s) must be cheaper than x86 (%s) on Lambda", arm.CostUSD, x86.CostUSD)
	}
}

// ─── Fargate binary GiB divisor pin ─────────────────────────────────────

func TestFargateUsesBinaryGiBDivisorInPricing(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":      "fargate",
		"duration_ms":        60_000,
		"memory_bytes_limit": int64(1024) * 1024 * 1024, // exactly 1 GiB
		"vcpu_count":         0.5,
		"vcpu_seconds_used":  30,
		"invocation_count":   0,
		"region":             "us-east-1",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(details, newTestEnv("aws", "us-east-1", ""), nil, decimal.NewFromInt(60))

	vcpuTerm := decimal.RequireFromString("0.5").
		Mul(decimal.NewFromInt(60)).
		Mul(decimal.RequireFromString("0.0000112444"))
	gibTerm := decimal.NewFromInt(1).
		Mul(decimal.NewFromInt(60)).
		Mul(decimal.RequireFromString("0.0000012347"))
	expected := vcpuTerm.Add(gibTerm)
	if !cost.CostUSD.Equal(expected) {
		t.Fatalf("CostUSD = %s, want %s — regression: Fargate divisor confusion "+
			"(Decision #7 BINARY GiB silently over-attributes ~4.86%%)",
			cost.CostUSD, expected)
	}
}

// ─── Cloud Run ──────────────────────────────────────────────────────────

func TestCloudRunDefaultIsEstimated(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":      "cloud_run_request",
		"duration_ms":        250,
		"memory_bytes_limit": int64(256) * 1024 * 1024,
		"vcpu_count":         0.5,
		"vcpu_seconds_used":  0,
		"invocation_count":   1,
		"region":             "us-central1",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(details, newTestEnv("gcp", "us-central1", ""), nil, decimal.Zero)
	if cost.CostConfidence != "estimated" {
		t.Fatalf("Confidence = %s, want estimated", cost.CostConfidence)
	}
	if cost.PricingSource != "compute_catalog:cloud_run:request_based_default" {
		t.Fatalf("PricingSource = %s", cost.PricingSource)
	}
}

func TestCloudRunInstanceOverrideIsComputed(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":      "cloud_run_request",
		"duration_ms":        0,
		"memory_bytes_limit": int64(256) * 1024 * 1024,
		"vcpu_count":         0.5,
		"vcpu_seconds_used":  0,
		"invocation_count":   0,
		"region":             "us-central1",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(
		details,
		newTestEnv("gcp", "us-central1", ""),
		map[string]string{"cloud_run": "instance"},
		decimal.NewFromInt(60),
	)
	if cost.CostConfidence != "computed" {
		t.Fatalf("Confidence = %s, want computed", cost.CostConfidence)
	}
	if got := cost.PricingSource; got == "" || got[len(got)-len("instance_override"):] != "instance_override" {
		t.Fatalf("PricingSource = %s, want suffix instance_override", got)
	}
}

// ─── Azure Functions ────────────────────────────────────────────────────

func TestAzureFunctionsCanonical(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":      "azure_functions",
		"duration_ms":        200,
		"memory_bytes_limit": int64(512) * 1000 * 1000,
		"vcpu_count":         1.0,
		"vcpu_seconds_used":  0,
		"invocation_count":   1,
		"region":             "eastus",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(details, newTestEnv("azure", "eastus", ""), nil, decimal.Zero)

	gbSeconds := decimal.NewFromInt(int64(512)*1000*1000).
		Div(decimal.NewFromInt(1_000_000_000)).
		Mul(decimal.RequireFromString("0.2"))
	expected := decimal.RequireFromString("0.0000002").Add(
		gbSeconds.Mul(decimal.RequireFromString("0.000016")))
	if !cost.CostUSD.Equal(expected) {
		t.Fatalf("CostUSD = %s, want %s", cost.CostUSD, expected)
	}
}

// ─── Vercel ─────────────────────────────────────────────────────────────

func TestVercelActiveCPUApproximatesWallDuration(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":      "vercel_fluid",
		"duration_ms":        500,
		"memory_bytes_limit": int64(256) * 1000 * 1000,
		"vcpu_count":         1.0,
		"vcpu_seconds_used":  0,
		"invocation_count":   1,
		"region":             "",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(details, newTestEnv("", "", ""), nil, decimal.Zero)
	if !cost.CostUSD.GreaterThan(decimal.Zero) {
		t.Fatalf("CostUSD = %s, want > 0", cost.CostUSD)
	}
	if cost.CostConfidence != "computed" {
		t.Fatalf("Confidence = %s, want computed", cost.CostConfidence)
	}
}

// ─── EC2 share factor ───────────────────────────────────────────────────

func TestEC2ShareFactorMath(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":      "ec2",
		"duration_ms":        60_000,
		"memory_bytes_limit": 0,
		"vcpu_count":         4.0,
		"vcpu_seconds_used":  1.0,
		"invocation_count":   0,
		"region":             "us-east-1",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(
		details,
		newTestEnv("aws", "us-east-1", "c7g.xlarge"),
		nil,
		decimal.NewFromInt(60),
	)
	share := decimal.NewFromInt(1).Div(decimal.NewFromInt(4).Mul(decimal.NewFromInt(60)))
	hours := share.Mul(decimal.NewFromInt(60).Div(decimal.NewFromInt(3600)))
	expected := hours.Mul(decimal.RequireFromString("0.1450"))
	if !cost.CostUSD.Equal(expected) {
		t.Fatalf("CostUSD = %s, want %s", cost.CostUSD, expected)
	}
}

// ─── K8s pod ────────────────────────────────────────────────────────────

func TestK8sPodLimitsMath(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":      "k8s_pod",
		"duration_ms":        60_000,
		"memory_bytes_limit": int64(512) * 1024 * 1024,
		"vcpu_count":         0.5,
		"vcpu_seconds_used":  0.3,
		"invocation_count":   0,
		"region":             "",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(details, newTestEnv("", "", ""), nil, decimal.NewFromInt(60))
	expected := decimal.RequireFromString("0.5").
		Mul(decimal.NewFromInt(60).Div(decimal.NewFromInt(3600))).
		Mul(decimal.RequireFromString("0.0464"))
	if !cost.CostUSD.Equal(expected) {
		t.Fatalf("CostUSD = %s, want %s", cost.CostUSD, expected)
	}
	if cost.CostConfidence != "computed" {
		t.Fatalf("Confidence = %s, want computed", cost.CostConfidence)
	}
}

// ─── Degradation ladder ─────────────────────────────────────────────────

func TestTier2UnknownRegionFallsToRuntimeDefault(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	details := map[string]any{
		"billing_model":      "lambda",
		"duration_ms":        100,
		"memory_bytes_limit": int64(128) * 1000 * 1000,
		"vcpu_count":         1.0,
		"vcpu_seconds_used":  0,
		"invocation_count":   1,
		"region":             "",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(details, newTestEnv("", "", ""), nil, decimal.Zero)
	if cost.CostConfidence != "estimated" {
		t.Fatalf("Confidence = %s, want estimated", cost.CostConfidence)
	}
	if cost.PricingSource != "compute_catalog:aws:lambda:default:x86_64" {
		t.Fatalf("PricingSource = %s", cost.PricingSource)
	}
}

func TestTier4MissingCatalogUsesHardcoded(t *testing.T) {
	resetComputeWarnings(t)
	bogus := filepath.Join(t.TempDir(), "no.json")
	eng, err := NewComputePricingEngineFromPath(bogus)
	if err != nil {
		// Engine returns even on missing — the function should still produce
		// an engine that uses hardcoded fallbacks.
		t.Fatalf("unexpected error: %v", err)
	}
	details := map[string]any{
		"billing_model":      "lambda",
		"duration_ms":        100,
		"memory_bytes_limit": int64(128) * 1000 * 1000,
		"vcpu_count":         1.0,
		"vcpu_seconds_used":  0,
		"invocation_count":   1,
		"region":             "us-east-1",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(details, newTestEnv("aws", "us-east-1", ""), nil, decimal.Zero)
	if !cost.CostUSD.GreaterThan(decimal.Zero) {
		t.Fatalf("CostUSD = %s, want > 0", cost.CostUSD)
	}
	if got := cost.PricingSource; len(got) < len("compute_catalog:hardcoded") ||
		got[:len("compute_catalog:hardcoded")] != "compute_catalog:hardcoded" {
		t.Fatalf("PricingSource = %s, want hardcoded prefix", got)
	}
	if cost.CostConfidence != "estimated" {
		t.Fatalf("Confidence = %s, want estimated", cost.CostConfidence)
	}
}

func TestTier5ComputationFailureReturnsZero(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	bad := map[string]any{
		"billing_model": "lambda",
		"duration_ms":   "not-a-number",
	}
	cost := eng.ResolveComputeCost(bad, newTestEnv("aws", "us-east-1", ""), nil, decimal.Zero)
	if !cost.CostUSD.Equal(decimal.Zero) {
		t.Fatalf("CostUSD = %s, want 0", cost.CostUSD)
	}
}

func TestUnknownBillingModelReturnsZero(t *testing.T) {
	resetComputeWarnings(t)
	eng := NewComputePricingEngine()
	bad := map[string]any{
		"billing_model":      "totally_made_up",
		"duration_ms":        100,
		"memory_bytes_limit": 0,
		"vcpu_count":         1.0,
		"vcpu_seconds_used":  0,
		"invocation_count":   0,
		"region":             "us-east-1",
		"architecture":       "x86_64",
	}
	cost := eng.ResolveComputeCost(bad, newTestEnv("aws", "us-east-1", ""), nil, decimal.Zero)
	if !cost.CostUSD.Equal(decimal.Zero) {
		t.Fatalf("CostUSD = %s, want 0", cost.CostUSD)
	}
}

// ─── No-float-drift ─────────────────────────────────────────────────────

func TestDecimalNoFloatDriftPerConversion(t *testing.T) {
	// Pin Decision #7: divisors stay Decimal.
	binary := decimal.NewFromInt(2 * 1024 * 1024 * 1024).Div(decimal.NewFromInt(1024 * 1024 * 1024))
	if !binary.Equal(decimal.NewFromInt(2)) {
		t.Fatalf("binary divisor = %s, want 2", binary)
	}
	dec := decimal.NewFromInt(2_000_000_000).Div(decimal.NewFromInt(1_000_000_000))
	if !dec.Equal(decimal.NewFromInt(2)) {
		t.Fatalf("decimal divisor = %s, want 2", dec)
	}
	got := decimal.RequireFromString("0.0000166667").Mul(decimal.NewFromInt(1024))
	want := decimal.RequireFromString("0.0170667008")
	if !got.Equal(want) {
		t.Fatalf("multiplication = %s, want %s", got, want)
	}
}

// ─── Warn-once ──────────────────────────────────────────────────────────

func TestWarnOncePerFailureMode(t *testing.T) {
	ResetComputeWarningStateForTests()
	logs := captureComputeWarnLogs(t)
	bogus := filepath.Join(t.TempDir(), "missing.json")
	_, _ = NewComputePricingEngineFromPath(bogus)
	_, _ = NewComputePricingEngineFromPath(bogus)
	if n := *logs; n != 1 {
		t.Fatalf("warn count = %d, want 1 (convention §11 log-once-per-mode)", n)
	}
}

func TestCatalogVersionExposed(t *testing.T) {
	eng := NewComputePricingEngine()
	v := eng.CatalogVersion()
	if len(v) < 2 || v[:2] != "1." {
		t.Fatalf("CatalogVersion = %q, want 1.x", v)
	}
}
