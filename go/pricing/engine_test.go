package pricing

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/shopspring/decimal"
)

func minimalDataPath(t *testing.T) string {
	t.Helper()
	data := map[string]interface{}{
		"sample_spec": map[string]interface{}{"input_cost_per_token": 0},
		"gpt-4o": map[string]interface{}{
			"input_cost_per_token":        0.0000025,
			"output_cost_per_token":       0.00001,
			"cache_read_input_token_cost": 0.00000125,
		},
		"gpt-4o-2024-08-06": map[string]interface{}{
			"input_cost_per_token":        0.0000025,
			"output_cost_per_token":       0.00001,
			"cache_read_input_token_cost": 0.00000125,
		},
		"gpt-3.5-turbo": map[string]interface{}{
			"input_cost_per_token":  0.0000005,
			"output_cost_per_token": 0.0000015,
		},
		"claude-test": map[string]interface{}{
			"input_cost_per_token":            0.000003,
			"output_cost_per_token":           0.000015,
			"cache_read_input_token_cost":     0.0000003,
			"cache_creation_input_token_cost": 0.00000375,
			"litellm_provider":                "anthropic",
		},
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "model_cost_map.json")
	raw, _ := json.Marshal(data)
	os.WriteFile(path, raw, 0644)
	return path
}

func TestGetCost_KnownModel(t *testing.T) {
	eng, err := NewEngineFromFile(minimalDataPath(t))
	if err != nil {
		t.Fatal(err)
	}
	result := eng.GetCost("gpt-4o", 1000, 500, 0, 0)
	// input: 1000 * 0.0000025 = 0.0025
	// output: 500 * 0.00001 = 0.005
	expected := decimal.RequireFromString("0.0075")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
	if result.CostConfidence != "computed" {
		t.Errorf("expected computed, got %s", result.CostConfidence)
	}
	if result.PricingSource != "litellm" {
		t.Errorf("expected litellm, got %s", result.PricingSource)
	}
}

func TestGetCost_UnknownModel(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	result := eng.GetCost("nonexistent-xyz", 1000, 500, 0, 0)
	if !result.CostUSD.IsZero() {
		t.Errorf("expected 0, got %s", result.CostUSD)
	}
	if result.CostConfidence != "unknown" {
		t.Errorf("expected unknown, got %s", result.CostConfidence)
	}
	if result.PricingSource != "unknown" {
		t.Errorf("expected unknown, got %s", result.PricingSource)
	}
}

func TestGetCost_CachedTokens(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	result := eng.GetCost("gpt-4o", 1000, 500, 400, 0)
	// non-cached: 600 * 0.0000025 = 0.0015
	// cached: 400 * 0.00000125 = 0.0005
	// output: 500 * 0.00001 = 0.005
	expected := decimal.RequireFromString("0.007")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
}

func TestGetCost_CachedExceedsInput(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	result := eng.GetCost("gpt-4o", 100, 0, 500, 0)
	// clamped: 100 cached * 0.00000125 = 0.000125
	expected := decimal.RequireFromString("0.000125")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
}

func TestGetCost_CacheCreationTokens(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	// Anthropic reports 1000 input, 200 cache-read, and 300 cache-creation
	// as three disjoint buckets. output=500.
	result := eng.GetCost("claude-test", 1000, 500, 200, 300)
	// normal input:  1000 * 0.000003   = 0.003
	// cache-read:     200 * 0.0000003  = 0.00006
	// cache-creation: 300 * 0.00000375 = 0.001125
	// output:         500 * 0.000015   = 0.0075
	expected := decimal.RequireFromString("0.011685")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
}

func TestGetCost_AnthropicCacheBucketsAreNotClampedToInput(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	result := eng.GetCost("claude-test", 100, 0, 80, 500)
	// normal input:   100 * 0.000003   = 0.0003
	// cache-read:     80 * 0.0000003  = 0.000024
	// cache-creation: 500 * 0.00000375 = 0.001875
	expected := decimal.RequireFromString("0.002199")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
}

func TestGetCost_ZeroTokens(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	result := eng.GetCost("gpt-4o", 0, 0, 0, 0)
	if !result.CostUSD.IsZero() {
		t.Errorf("expected 0, got %s", result.CostUSD)
	}
	if result.CostConfidence != "computed" {
		t.Errorf("expected computed, got %s", result.CostConfidence)
	}
}

func TestGetCost_ProviderPrefix(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	result := eng.GetCost("openai/gpt-4o", 1000, 500, 0, 0)
	expected := decimal.RequireFromString("0.0075")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
}

func TestGetCost_DateSuffixFallback(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	result := eng.GetCost("gpt-4o-2099-01-01", 1000, 500, 0, 0)
	expected := decimal.RequireFromString("0.0075")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
}

func TestCustomPricing_OverridesBundled(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	eng.SetCustomPricing("gpt-4o", decimal.RequireFromString("0.001"), decimal.RequireFromString("0.002"))
	result := eng.GetCost("gpt-4o", 1000, 1000, 0, 0)
	// input: 1000/1000 * 0.001 = 0.001
	// output: 1000/1000 * 0.002 = 0.002
	expected := decimal.RequireFromString("0.003")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
	if result.PricingSource != "custom" {
		t.Errorf("expected custom, got %s", result.PricingSource)
	}
}

func TestCustomPricing_AnthropicCacheBucketsAreNotDropped(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	eng.SetCustomPricing("my-claude-model", decimal.RequireFromString("0.001"), decimal.RequireFromString("0.002"))
	result := eng.GetCost("my-claude-model", 100, 0, 1000, 500)
	expected := decimal.RequireFromString("0.0016")
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected %s, got %s", expected, result.CostUSD)
	}
	if result.CostConfidence != "unknown" {
		t.Errorf("expected unknown confidence, got %s", result.CostConfidence)
	}
	if result.PricingSource != "custom" {
		t.Errorf("expected custom, got %s", result.PricingSource)
	}
}

func TestPricingVersion_Stable(t *testing.T) {
	eng, _ := NewEngineFromFile(minimalDataPath(t))
	r1 := eng.GetCost("gpt-4o", 1000, 500, 0, 0)
	r2 := eng.GetCost("gpt-4o", 2000, 1000, 0, 0)
	if r1.PricingVersion != r2.PricingVersion {
		t.Error("pricing_version should be stable across calls")
	}
	if len(r1.PricingVersion) != 12 {
		t.Errorf("expected 12-char hex hash, got %d chars", len(r1.PricingVersion))
	}
}

func TestBundledData_Loads(t *testing.T) {
	eng, err := NewEngine()
	if err != nil {
		t.Fatalf("failed to load bundled data: %v", err)
	}
	if eng.ModelCount() < 400 {
		t.Errorf("expected 400+ models, got %d", eng.ModelCount())
	}
}

func TestEngine_RefreshFromServer(t *testing.T) {
	serverData := map[string]interface{}{
		"models": map[string]interface{}{
			"new-model-v1": map[string]interface{}{
				"input_cost_per_token":  0.000005,
				"output_cost_per_token": 0.000015,
			},
		},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/api/pricing-data/latest" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(serverData)
	}))
	defer srv.Close()

	eng, err := NewEngineFromFile(minimalDataPath(t))
	if err != nil {
		t.Fatal(err)
	}
	oldVersion := eng.PricingVersion()

	err = eng.RefreshFromServer(srv.URL)
	if err != nil {
		t.Fatalf("RefreshFromServer returned error: %v", err)
	}

	// New model should now be queryable.
	result := eng.GetCost("new-model-v1", 1000, 1000, 0, 0)
	if result.CostConfidence == "unknown" {
		t.Error("expected new-model-v1 to be found after refresh, got unknown")
	}
	expected := decimal.RequireFromString("0.02") // 1000*0.000005 + 1000*0.000015
	if !result.CostUSD.Equal(expected) {
		t.Errorf("expected cost %s, got %s", expected, result.CostUSD)
	}

	// Version should have changed.
	if eng.PricingVersion() == oldVersion {
		t.Error("expected pricingVersion to change after refresh")
	}
	if len(eng.PricingVersion()) != 12 {
		t.Errorf("expected 12-char version, got %d", len(eng.PricingVersion()))
	}
}

func TestEngine_RefreshFromServer_FailSilent(t *testing.T) {
	eng, err := NewEngineFromFile(minimalDataPath(t))
	if err != nil {
		t.Fatal(err)
	}
	oldVersion := eng.PricingVersion()

	// Connect to a server that is not listening.
	err = eng.RefreshFromServer("http://127.0.0.1:1")
	if err == nil {
		t.Error("expected error when connecting to non-existent server, got nil")
	}

	// Version should be unchanged.
	if eng.PricingVersion() != oldVersion {
		t.Error("expected pricingVersion to be unchanged after failed refresh")
	}
}

func TestEngine_StartStopBackgroundRefresh(t *testing.T) {
	var callCount atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callCount.Add(1)
		data := map[string]interface{}{
			"models": map[string]interface{}{
				"bg-model": map[string]interface{}{
					"input_cost_per_token":  0.000001,
					"output_cost_per_token": 0.000002,
				},
			},
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(data)
	}))
	defer srv.Close()

	eng, err := NewEngineFromFile(minimalDataPath(t))
	if err != nil {
		t.Fatal(err)
	}

	eng.StartBackgroundRefresh(srv.URL, 50*time.Millisecond)
	time.Sleep(200 * time.Millisecond)
	eng.StopBackgroundRefresh()

	got := callCount.Load()
	if got < 2 {
		t.Errorf("expected at least 2 refresh calls, got %d", got)
	}
}
