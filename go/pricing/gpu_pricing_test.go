// Task 5 — GPU pricing engine tests. Mirrors python commit a47c58a.

package pricing

import (
	"testing"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/cloud"
)

func newGPUEngine(t *testing.T) *GpuPricingEngine {
	t.Helper()
	return NewGpuPricingEngine()
}

func TestGPUEngineLoadsCatalogVersion(t *testing.T) {
	e := newGPUEngine(t)
	v := e.CatalogVersion()
	if v == "" || v == "unknown" {
		t.Fatalf("catalog version should be loaded; got %q", v)
	}
}

// ─── per_gpu_second_active — Modal H100 ─────────────────────────────────

func TestPerGpuSecondActiveModalH100(t *testing.T) {
	e := newGPUEngine(t)
	details := map[string]any{
		"billing_model":    "per_gpu_second_active",
		"gpu_sku":          "h100-80gb-sxm5",
		"gpu_count":        1,
		"gpu_seconds_used": 60.0,
		"duration_ms":      60000,
	}
	env := cloud.CloudEnv{Provider: "modal"}
	cost := e.ResolveGPUCost(details, env, decimal.Zero)
	if !cost.CostUSD.IsPositive() {
		t.Fatalf("Modal H100 60s should produce positive cost; got %s", cost.CostUSD)
	}
	if cost.CostConfidence != "computed" {
		t.Errorf("expected computed; got %q", cost.CostConfidence)
	}
}

// ─── per_instance_hour — AWS p4d.24xlarge ───────────────────────────────

func TestPerInstanceHourAWSP4d(t *testing.T) {
	e := newGPUEngine(t)
	details := map[string]any{
		"billing_model":    "per_instance_hour",
		"gpu_count":        8,
		"gpu_seconds_used": 28800.0, // full 1-hr-worth-of-GPU-seconds
		"duration_ms":      3600000, // 1 hr
		"region":           "us-east-1",
		"instance_type":    "p4d.24xlarge",
	}
	env := cloud.CloudEnv{Provider: "aws", Region: "us-east-1", InstanceType: "p4d.24xlarge"}
	cost := e.ResolveGPUCost(details, env, decimal.Zero)
	if !cost.CostUSD.IsPositive() {
		t.Fatalf("AWS p4d 1hr should produce positive cost; got %s", cost.CostUSD)
	}
}

// ─── per_gpu_hour_reserved — Lambda H100 ────────────────────────────────

func TestPerGpuHourReservedLambdaLabsH100(t *testing.T) {
	e := newGPUEngine(t)
	details := map[string]any{
		"billing_model":    "per_gpu_hour_reserved",
		"gpu_sku":          "h100-80gb-sxm5",
		"gpu_count":        1,
		"gpu_seconds_used": 3600.0,
		"duration_ms":      3600000,
	}
	env := cloud.CloudEnv{Provider: "lambda_labs"}
	cost := e.ResolveGPUCost(details, env, decimal.Zero)
	if !cost.CostUSD.IsPositive() {
		t.Fatalf("Lambda H100 1hr should produce positive cost; got %s", cost.CostUSD)
	}
}

// ─── per_vgpu_hour — Azure NV6ads_A10_v5 ────────────────────────────────

func TestPerVGpuHourAzureNV6(t *testing.T) {
	e := newGPUEngine(t)
	details := map[string]any{
		"billing_model":    "per_vgpu_hour",
		"gpu_count":        1,
		"gpu_seconds_used": 3600.0,
		"duration_ms":      3600000,
		"region":           "eastus",
		"instance_type":    "Standard_NV6ads_A10_v5",
	}
	env := cloud.CloudEnv{Provider: "azure", Region: "eastus", InstanceType: "Standard_NV6ads_A10_v5"}
	cost := e.ResolveGPUCost(details, env, decimal.Zero)
	if !cost.CostUSD.IsPositive() {
		t.Fatalf("Azure NV6ads_A10_v5 1hr should produce positive cost; got %s", cost.CostUSD)
	}
}

// ─── Five-tier degradation ──────────────────────────────────────────────

func TestUnsupportedBillingModelReturnsZeroCostUnknown(t *testing.T) {
	e := newGPUEngine(t)
	details := map[string]any{
		"billing_model": "bogus_xyz",
	}
	cost := e.ResolveGPUCost(details, cloud.CloudEnv{}, decimal.Zero)
	if !cost.CostUSD.IsZero() {
		t.Errorf("unsupported billing_model should produce 0; got %s", cost.CostUSD)
	}
	if cost.CostConfidence != "unknown" {
		t.Errorf("expected unknown confidence; got %q", cost.CostConfidence)
	}
}

func TestDecision4DeviceClassFallback(t *testing.T) {
	// Unknown SKU + productName matching "h100" → hopper class default.
	e := newGPUEngine(t)
	details := map[string]any{
		"billing_model":              "per_gpu_second_active",
		"gpu_sku":                    "unknown-sku-xyz",
		"_nvml_product_name_lower":   "nvidia h100 80gb hbm3",
		"gpu_count":                  1,
		"gpu_seconds_used":           1.0,
		"duration_ms":                1000,
	}
	cost := e.ResolveGPUCost(details, cloud.CloudEnv{Provider: "unknown_cloud"}, decimal.Zero)
	if cost.CostConfidence != "estimated" {
		t.Errorf("device_class fallback should yield estimated confidence; got %q", cost.CostConfidence)
	}
	if cost.PricingSource == "" || !contains(cost.PricingSource, "device_class_fallback:hopper") {
		t.Errorf("PricingSource should include hopper device_class_fallback; got %q", cost.PricingSource)
	}
}

// ─── Decision #1 — _cgroup_scope_fallback suffix ────────────────────────

func TestCgroupScopeFallbackAppendsSuffixAndDropsToEstimated(t *testing.T) {
	e := newGPUEngine(t)
	details := map[string]any{
		"billing_model":             "per_gpu_second_active",
		"gpu_sku":                   "h100-80gb-sxm5",
		"gpu_count":                 1,
		"gpu_seconds_used":          1.0,
		"duration_ms":               1000,
		"_cgroup_scope_fallback":    "self_pid_only",
	}
	env := cloud.CloudEnv{Provider: "modal"}
	cost := e.ResolveGPUCost(details, env, decimal.Zero)
	if cost.CostConfidence != "estimated" {
		t.Errorf("scope fallback should drop confidence to estimated; got %q", cost.CostConfidence)
	}
	if !contains(cost.PricingSource, ":self_pid_only") {
		t.Errorf("PricingSource should end with :self_pid_only; got %q", cost.PricingSource)
	}
}

// ─── Decision #4 device_class detection ─────────────────────────────────

func TestDetectDeviceClassRecognizesAllFourClasses(t *testing.T) {
	cases := []struct{ name, want string }{
		{"nvidia h100 80gb", "hopper"},
		{"nvidia h200 141gb", "hopper"},
		{"nvidia a100 80gb sxm4", "ampere"},
		{"nvidia a10 24gb", "ampere"},
		{"nvidia l4 24gb", "ada_lovelace"},
		{"nvidia l40s 48gb", "ada_lovelace"},
		{"nvidia b100 80gb", "blackwell"},
		{"nvidia gb200 360gb", "blackwell"},
		{"some unknown card", ""},
	}
	for _, c := range cases {
		got := detectDeviceClass(c.name)
		if got != c.want {
			t.Errorf("detectDeviceClass(%q) = %q; want %q", c.name, got, c.want)
		}
	}
}

// ─── Tier-4 hardcoded constants mirror _meta defaults ───────────────────

func TestHardcodedConstantsMirrorMetaDefaults(t *testing.T) {
	data := loadGPUCatalog(t)
	meta := data["_meta"].(map[string]any)
	expect := map[string]string{
		"per_instance_hour":     "default_per_instance_hour_usd",
		"per_gpu_second_active": "default_per_gpu_second_active_usd",
		"per_gpu_hour_reserved": "default_per_gpu_hour_reserved_usd",
		"per_vgpu_hour":         "default_per_vgpu_hour_usd",
	}
	for billing, metaKey := range expect {
		metaVal, _ := meta[metaKey].(string)
		metaDec, err := decimal.NewFromString(metaVal)
		if err != nil {
			t.Errorf("meta %s not Decimal: %v", metaKey, err)
			continue
		}
		hardKey := gpuHardcodedRateKey(billing)
		hcDec := gpuHardcoded[billing][hardKey]
		if !metaDec.Equal(hcDec) {
			t.Errorf("hardcoded[%s][%s] = %s; meta %s = %s — must mirror",
				billing, hardKey, hcDec, metaKey, metaDec)
		}
	}
}

// ─── Malformed catalog → fail-silent ────────────────────────────────────

func TestEngineSurvivesMalformedCatalog(t *testing.T) {
	e := NewGpuPricingEngineFromBytes([]byte("{not valid json"))
	// must still be usable; falls back to hardcoded.
	cost := e.ResolveGPUCost(map[string]any{
		"billing_model":    "per_gpu_second_active",
		"gpu_count":        1,
		"gpu_seconds_used": 1.0,
		"duration_ms":      1000,
	}, cloud.CloudEnv{}, decimal.Zero)
	if !cost.CostUSD.IsPositive() {
		t.Fatalf("hardcoded fallback should still produce non-zero cost; got %s", cost.CostUSD)
	}
	if cost.CostConfidence != "estimated" {
		t.Errorf("hardcoded fallback should be estimated; got %q", cost.CostConfidence)
	}
}

// ─── Helper ─────────────────────────────────────────────────────────────

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (haystack == needle ||
		(len(haystack) > 0 && len(needle) > 0 && substringIndex(haystack, needle) >= 0))
}

func substringIndex(s, sub string) int {
	if len(sub) == 0 {
		return 0
	}
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
