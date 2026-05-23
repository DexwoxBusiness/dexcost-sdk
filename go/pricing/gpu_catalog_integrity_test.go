// Task 4 — GPU catalog integrity tests. Mirrors python commit 97f736b.
//
// Catalog already shipped at go/pricing/data/gpu_prices.json (synced from
// Python canonical at 79c8745). These tests pin its STRUCTURAL invariants
// so a future refresh can't drift shape; freshness enforces Decision #11
// (90/365-day thresholds — tighter than Phase 1 compute's 180/730).

package pricing

import (
	_ "embed"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/shopspring/decimal"
)

//go:embed data/gpu_prices.json
var embeddedGPUCatalog []byte

func loadGPUCatalog(t *testing.T) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(embeddedGPUCatalog, &m); err != nil {
		t.Fatalf("gpu_prices.json not parseable: %v", err)
	}
	return m
}

func TestGPUCatalogParsesAsJSON(t *testing.T) {
	data := loadGPUCatalog(t)
	if _, ok := data["_meta"]; !ok {
		t.Fatalf("catalog missing _meta")
	}
}

func TestGPUMetaHasRequiredDefaultKeys(t *testing.T) {
	data := loadGPUCatalog(t)
	meta, ok := data["_meta"].(map[string]any)
	if !ok {
		t.Fatalf("_meta not a map")
	}
	required := []string{
		"version", "last_updated", "currency",
		"default_per_instance_hour_usd",
		"default_per_gpu_second_active_usd",
		"default_per_gpu_hour_reserved_usd",
		"default_per_vgpu_hour_usd",
		"description", "notes",
	}
	for _, k := range required {
		v, present := meta[k]
		if !present {
			t.Errorf("_meta missing %s", k)
			continue
		}
		if strings.HasPrefix(k, "default_") && strings.HasSuffix(k, "_usd") {
			s, _ := v.(string)
			if _, err := decimal.NewFromString(s); err != nil {
				t.Errorf("_meta.%s not Decimal-parseable: %q (%v)", k, s, err)
			}
		}
	}
	if cur, _ := meta["currency"].(string); cur != "USD" {
		t.Errorf("currency = %q; want USD", cur)
	}
}

func TestGPUAllEightProvidersPresent(t *testing.T) {
	data := loadGPUCatalog(t)
	expected := []string{"aws", "gcp", "azure", "modal", "runpod",
		"lambda_labs", "coreweave", "replicate"}
	for _, p := range expected {
		if _, ok := data[p]; !ok {
			t.Errorf("catalog missing provider block: %s", p)
		}
	}
}

func TestGPUEveryProviderHasLastVerifiedISO(t *testing.T) {
	data := loadGPUCatalog(t)
	for provider, blk := range data {
		if provider == "_meta" {
			continue
		}
		m, ok := blk.(map[string]any)
		if !ok {
			t.Errorf("%s provider block not a map", provider)
			continue
		}
		s, ok := m["_last_verified"].(string)
		if !ok {
			t.Errorf("%s missing _last_verified", provider)
			continue
		}
		if _, err := time.Parse("2006-01-02", s); err != nil {
			t.Errorf("%s _last_verified %q not ISO-8601: %v", provider, s, err)
		}
	}
}

func TestDecision11HardFailAt365Days(t *testing.T) {
	data := loadGPUCatalog(t)
	today := time.Now().UTC()
	hardLimit := 365 * 24 * time.Hour
	var stale []string
	for provider, blk := range data {
		if provider == "_meta" {
			continue
		}
		m := blk.(map[string]any)
		s := m["_last_verified"].(string)
		verified, _ := time.Parse("2006-01-02", s)
		age := today.Sub(verified)
		if age > hardLimit {
			stale = append(stale, provider)
		}
	}
	if len(stale) > 0 {
		t.Fatalf("GPU catalog entries >365d old (Decision #11 hard fail): %v", stale)
	}
}

// ─── Per-provider block shape ────────────────────────────────────────────

func TestAWSBlockHasEC2GPURegions(t *testing.T) {
	data := loadGPUCatalog(t)
	aws := data["aws"].(map[string]any)
	if _, ok := aws["ec2_gpu"]; !ok {
		t.Fatalf("aws missing ec2_gpu")
	}
	ecg := aws["ec2_gpu"].(map[string]any)
	regions, ok := ecg["regions"].(map[string]any)
	if !ok {
		t.Fatalf("aws.ec2_gpu missing regions")
	}
	if _, ok := regions["us-east-1"]; !ok {
		t.Fatalf("aws.ec2_gpu.regions missing us-east-1")
	}
}

func TestGCPBlockHasAttachedAndBundled(t *testing.T) {
	data := loadGPUCatalog(t)
	gcp := data["gcp"].(map[string]any)
	for _, k := range []string{"gce_gpu_attached", "gce_gpu_bundled"} {
		if _, ok := gcp[k]; !ok {
			t.Errorf("gcp missing %s", k)
		}
	}
}

func TestAzureBlockHasVMGPUAndVMVGPU(t *testing.T) {
	data := loadGPUCatalog(t)
	az := data["azure"].(map[string]any)
	for _, k := range []string{"vm_gpu", "vm_vgpu"} {
		if _, ok := az[k]; !ok {
			t.Errorf("azure missing %s", k)
		}
	}
}

func TestServerlessProvidersHavePerGpuSecondActive(t *testing.T) {
	data := loadGPUCatalog(t)
	for _, p := range []string{"modal", "runpod", "replicate"} {
		block := data[p].(map[string]any)
		if _, ok := block["per_gpu_second_active"]; !ok {
			t.Errorf("%s should have per_gpu_second_active billing model", p)
		}
	}
}

func TestReservedProvidersHavePerGpuHourReserved(t *testing.T) {
	data := loadGPUCatalog(t)
	for _, p := range []string{"lambda_labs", "coreweave"} {
		block := data[p].(map[string]any)
		if _, ok := block["per_gpu_hour_reserved"]; !ok {
			t.Errorf("%s should have per_gpu_hour_reserved billing model", p)
		}
	}
}

// ─── Every *_usd / numeric field Decimal-parseable ───────────────────────

func TestEveryUSDRateIsDecimalParseable(t *testing.T) {
	data := loadGPUCatalog(t)

	var walk func(node any, path string)
	walk = func(node any, path string) {
		switch n := node.(type) {
		case map[string]any:
			for k, v := range n {
				p := path + "." + k
				if s, ok := v.(string); ok {
					if strings.HasSuffix(k, "_usd") ||
						k == "vcpu_count" || k == "gpu_count" ||
						k == "gpu_vram_gb" || k == "memory_gb" {
						if _, err := decimal.NewFromString(s); err != nil {
							t.Errorf("%s not Decimal-parseable: %q (%v)", p, s, err)
						}
					}
				} else {
					walk(v, p)
				}
			}
		case []any:
			for i, item := range n {
				walk(item, path+"["+itoa(i)+"]")
			}
		}
	}
	walk(data, "")
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var s [20]byte
	pos := len(s)
	for i > 0 {
		pos--
		s[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		s[pos] = '-'
	}
	return string(s[pos:])
}

// ─── Cross-provider canonical SKU consistency ────────────────────────────

func TestH100SXM5SKUConsistentAcrossProviders(t *testing.T) {
	data := loadGPUCatalog(t)
	foundProviders := map[string]bool{}

	var walk func(node any, provider string)
	walk = func(node any, provider string) {
		switch n := node.(type) {
		case map[string]any:
			if sku, _ := n["gpu_sku"].(string); sku == "h100-80gb-sxm5" {
				foundProviders[provider] = true
			}
			for _, v := range n {
				walk(v, provider)
			}
		case []any:
			for _, item := range n {
				walk(item, provider)
			}
		}
	}
	for p, blk := range data {
		if p == "_meta" {
			continue
		}
		walk(blk, p)
	}
	// h100-80gb-sxm5 should appear on at least the highest-fidelity providers.
	required := []string{"aws", "modal", "lambda_labs"}
	for _, r := range required {
		if !foundProviders[r] {
			t.Errorf("h100-80gb-sxm5 SKU missing from provider %s", r)
		}
	}
}
