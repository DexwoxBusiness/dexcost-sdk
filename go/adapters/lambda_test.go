package adapters_test

import (
	"strings"
	"testing"

	"github.com/DexwoxBusiness/dexcost-sdk/go/adapters"
	"github.com/shopspring/decimal"
)

// TestLambdaCost_BasicUSEast1 mirrors test_basic_us_east_1.
// 128 MB = 0.125 GB. 1s * 0.125 GB * 0.0000166667/GB-s = 0.0000020833375
// + request charge 0.0000002 = 0.0000022833375
func TestLambdaCost_BasicUSEast1(t *testing.T) {
	res, err := adapters.LambdaCost(1000, 128, "us-east-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expected := decimal.RequireFromString("0.0000022833375")
	if !res.CostUSD.Equal(expected) {
		t.Errorf("cost_usd: expected %s, got %s", expected, res.CostUSD)
	}
	if res.Details.Region != "us-east-1" {
		t.Errorf("region: expected us-east-1, got %s", res.Details.Region)
	}
	if res.Details.DurationMs != 1000 {
		t.Errorf("duration_ms: expected 1000, got %d", res.Details.DurationMs)
	}
	if res.Details.MemoryMb != 128 {
		t.Errorf("memory_mb: expected 128, got %d", res.Details.MemoryMb)
	}
	if !res.Details.GBSeconds.Equal(decimal.RequireFromString("0.125")) {
		t.Errorf("gb_seconds: expected 0.125, got %s", res.Details.GBSeconds)
	}
}

// TestLambdaCost_HigherMemory mirrors test_higher_memory.
// 512 MB = 0.5 GB. 3s * 0.5 GB = 1.5 GB-s * 0.0000166667 + 0.0000002.
func TestLambdaCost_HigherMemory(t *testing.T) {
	res, err := adapters.LambdaCost(3000, 512, "us-east-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expectedDuration := decimal.RequireFromString("1.5").Mul(decimal.RequireFromString("0.0000166667"))
	expectedTotal := expectedDuration.Add(decimal.RequireFromString("0.0000002"))
	if !res.CostUSD.Equal(expectedTotal) {
		t.Errorf("cost_usd: expected %s, got %s", expectedTotal, res.CostUSD)
	}
}

// TestLambdaCost_EUCentral1Pricing mirrors test_eu_central_1_pricing.
// 1024 MB = 1 GB. 1s * 1 GB * 0.0000175000 + 0.0000002 = 0.0000177.
func TestLambdaCost_EUCentral1Pricing(t *testing.T) {
	res, err := adapters.LambdaCost(1000, 1024, "eu-central-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expected := decimal.RequireFromString("0.0000177")
	if !res.CostUSD.Equal(expected) {
		t.Errorf("cost_usd: expected %s, got %s", expected, res.CostUSD)
	}
}

// TestLambdaCost_ZeroDuration ensures 0 ms still incurs the request charge.
func TestLambdaCost_ZeroDuration(t *testing.T) {
	res, err := adapters.LambdaCost(0, 128, "us-east-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expected := decimal.RequireFromString("0.0000002")
	if !res.CostUSD.Equal(expected) {
		t.Errorf("cost_usd: expected %s (request charge only), got %s", expected, res.CostUSD)
	}
}

// TestLambdaCost_UnknownRegion ensures unknown regions error out.
func TestLambdaCost_UnknownRegion(t *testing.T) {
	_, err := adapters.LambdaCost(1000, 128, "mars-west-1")
	if err == nil {
		t.Fatal("expected error for unknown region")
	}
	if !strings.Contains(err.Error(), "unknown AWS region") {
		t.Errorf("expected 'unknown AWS region' in error, got %v", err)
	}
}

// TestLambdaCost_NegativeDuration ensures negative durations error out.
func TestLambdaCost_NegativeDuration(t *testing.T) {
	_, err := adapters.LambdaCost(-100, 128, "us-east-1")
	if err == nil {
		t.Fatal("expected error for negative duration")
	}
	if !strings.Contains(err.Error(), "durationMs must be >= 0") {
		t.Errorf("expected 'durationMs must be >= 0', got %v", err)
	}
}

// TestLambdaCost_ZeroMemory ensures zero memory errors out.
func TestLambdaCost_ZeroMemory(t *testing.T) {
	_, err := adapters.LambdaCost(1000, 0, "us-east-1")
	if err == nil {
		t.Fatal("expected error for zero memory")
	}
	if !strings.Contains(err.Error(), "memoryMb must be > 0") {
		t.Errorf("expected 'memoryMb must be > 0', got %v", err)
	}
}

// TestLambdaCost_DetailsBreakdown ensures all breakdown fields are populated.
func TestLambdaCost_DetailsBreakdown(t *testing.T) {
	res, err := adapters.LambdaCost(2000, 256, "us-east-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Details.DurationCostUSD.IsZero() {
		t.Error("duration_cost_usd should not be zero")
	}
	if !res.Details.RequestCostUSD.Equal(decimal.RequireFromString("0.0000002")) {
		t.Errorf("request_cost_usd: expected 0.0000002, got %s", res.Details.RequestCostUSD)
	}
	if res.Details.RatePerGBSecond.IsZero() {
		t.Error("rate_per_gb_second should not be zero")
	}
}

// TestGetSupportedLambdaRegions verifies bundled region list.
func TestGetSupportedLambdaRegions(t *testing.T) {
	regions, err := adapters.GetSupportedLambdaRegions()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(regions) < 10 {
		t.Errorf("expected at least 10 regions, got %d", len(regions))
	}
	// Spot-check known regions.
	want := map[string]bool{"us-east-1": false, "eu-central-1": false}
	for _, r := range regions {
		if _, ok := want[r]; ok {
			want[r] = true
		}
	}
	for r, found := range want {
		if !found {
			t.Errorf("expected region %s in supported list", r)
		}
	}
	// Confirm sorted.
	for i := 1; i < len(regions); i++ {
		if regions[i-1] > regions[i] {
			t.Errorf("regions not sorted: %s > %s", regions[i-1], regions[i])
		}
	}
}

// TestLambdaCost_SubMillisecond verifies a 1 ms duration costs more than the request charge alone.
func TestLambdaCost_SubMillisecond(t *testing.T) {
	res, err := adapters.LambdaCost(1, 128, "us-east-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.CostUSD.GreaterThan(decimal.RequireFromString("0.0000002")) {
		t.Errorf("cost should exceed request charge alone, got %s", res.CostUSD)
	}
}
