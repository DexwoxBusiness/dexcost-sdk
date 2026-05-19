package dexcost

import (
	"os"
	"testing"
)

func TestValidateAPIKey_Live(t *testing.T) {
	kt, err := ValidateAPIKey("dx_live_abc123")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if kt != "live" {
		t.Errorf("expected live, got %s", kt)
	}
}

func TestValidateAPIKey_Test(t *testing.T) {
	kt, err := ValidateAPIKey("dx_test_abc123")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if kt != "test" {
		t.Errorf("expected test, got %s", kt)
	}
}

func TestValidateAPIKey_Empty(t *testing.T) {
	kt, err := ValidateAPIKey("")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if kt != "" {
		t.Errorf("expected empty, got %s", kt)
	}
}

func TestValidateAPIKey_Invalid(t *testing.T) {
	_, err := ValidateAPIKey("sk-invalid-key")
	if err == nil {
		t.Fatal("expected error for invalid key")
	}
}

func TestConfig_StorageMode_Cloud(t *testing.T) {
	cfg := Config{APIKey: "dx_live_abc123"}
	if err := cfg.init(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.StorageMode() != "cloud" {
		t.Errorf("expected cloud, got %s", cfg.StorageMode())
	}
}

func TestConfig_StorageMode_LocalForced(t *testing.T) {
	cfg := Config{APIKey: "dx_live_abc123", Storage: "local"}
	if err := cfg.init(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.StorageMode() != "local" {
		t.Errorf("expected local, got %s", cfg.StorageMode())
	}
}

func TestConfig_StorageMode_NoKey(t *testing.T) {
	// Ensure env var is not set for this test
	os.Unsetenv("DEXCOST_API_KEY")
	cfg := Config{}
	if err := cfg.init(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.StorageMode() != "local" {
		t.Errorf("expected local, got %s", cfg.StorageMode())
	}
}

func TestConfig_ResolvedEndpoint_Default(t *testing.T) {
	os.Unsetenv("DEXCOST_ENDPOINT")
	cfg := Config{}
	if cfg.resolvedEndpoint() != "https://api.dexcost.io" {
		t.Errorf("unexpected endpoint: %s", cfg.resolvedEndpoint())
	}
}

func TestConfig_ResolvedEndpoint_EnvOverride(t *testing.T) {
	os.Setenv("DEXCOST_ENDPOINT", "https://custom.api.dev")
	defer os.Unsetenv("DEXCOST_ENDPOINT")
	cfg := Config{}
	if cfg.resolvedEndpoint() != "https://custom.api.dev" {
		t.Errorf("unexpected endpoint: %s", cfg.resolvedEndpoint())
	}
}

func TestConfig_Defaults(t *testing.T) {
	cfg := Config{}
	cfg.applyDefaults()
	if cfg.BatchSize != 100 {
		t.Errorf("unexpected batch_size: %d", cfg.BatchSize)
	}
	if cfg.FlushIntervalSeconds != 5.0 {
		t.Errorf("unexpected flush_interval: %f", cfg.FlushIntervalSeconds)
	}
}

func TestConfig_EnvFallback(t *testing.T) {
	os.Setenv("DEXCOST_API_KEY", "dx_test_from_env")
	defer os.Unsetenv("DEXCOST_API_KEY")
	cfg := Config{}
	if err := cfg.init(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.APIKey != "dx_test_from_env" {
		t.Errorf("expected env key, got %s", cfg.APIKey)
	}
	if cfg.IsSandbox() != true {
		t.Error("expected sandbox=true for test key")
	}
}
