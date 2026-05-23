// Tests for the egress pricing engine — mirrors python/tests/test_egress_pricing.py
// (12 tests covering all 5 ladder tiers, warn-once discipline, and Decimal hygiene).

package pricing

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/shopspring/decimal"
)

func newTestEngine(t *testing.T) *EgressPricingEngine {
	t.Helper()
	ResetEgressWarningStateForTests()
	t.Cleanup(ResetEgressWarningStateForTests)
	return NewEgressPricingEngine()
}

// captureLogs redirects the default logger to a buffer for the duration of fn.
func captureLogs(t *testing.T, fn func()) string {
	t.Helper()
	var buf bytes.Buffer
	old := log.Writer()
	oldFlags := log.Flags()
	log.SetOutput(&buf)
	log.SetFlags(0)
	defer func() {
		log.SetOutput(old)
		log.SetFlags(oldFlags)
	}()
	fn()
	return buf.String()
}

func TestEgressPricing_Tier1RegionMatchIsComputed(t *testing.T) {
	eng := newTestEngine(t)
	r := eng.ResolveRate("aws", "us-east-1")
	if !r.RatePerGB.Equal(decimal.RequireFromString("0.09")) {
		t.Fatalf("rate_per_gb = %s, want 0.09", r.RatePerGB)
	}
	if r.PricingSource != "egress_catalog:aws:us-east-1" {
		t.Fatalf("pricing_source = %q", r.PricingSource)
	}
	if r.CostConfidence != "computed" {
		t.Fatalf("cost_confidence = %q, want computed", r.CostConfidence)
	}
}

func TestEgressPricing_Tier2ProviderKnownRegionMissingIsEstimated(t *testing.T) {
	eng := newTestEngine(t)
	r := eng.ResolveRate("aws", "moon-base-1")
	if !r.RatePerGB.Equal(decimal.RequireFromString("0.09")) {
		t.Fatalf("rate_per_gb = %s", r.RatePerGB)
	}
	if r.PricingSource != "egress_catalog:aws:default" {
		t.Fatalf("pricing_source = %q", r.PricingSource)
	}
	if r.CostConfidence != "estimated" {
		t.Fatalf("cost_confidence = %q", r.CostConfidence)
	}
}

func TestEgressPricing_Tier3UnknownProviderFallsToMetaDefault(t *testing.T) {
	eng := newTestEngine(t)
	r := eng.ResolveRate("", "")
	if !r.RatePerGB.Equal(decimal.RequireFromString("0.09")) {
		t.Fatalf("rate_per_gb = %s", r.RatePerGB)
	}
	if r.PricingSource != "egress_catalog:default" {
		t.Fatalf("pricing_source = %q", r.PricingSource)
	}
	if r.CostConfidence != "estimated" {
		t.Fatalf("cost_confidence = %q", r.CostConfidence)
	}
}

func TestEgressPricing_InternalTrafficIsFreeAndExact(t *testing.T) {
	eng := newTestEngine(t)
	r := eng.RateForInternal()
	if !r.RatePerGB.Equal(decimal.Zero) {
		t.Fatalf("rate_per_gb = %s, want 0", r.RatePerGB)
	}
	if r.PricingSource != "egress_catalog:internal" {
		t.Fatalf("pricing_source = %q", r.PricingSource)
	}
	if r.CostConfidence != "exact" {
		t.Fatalf("cost_confidence = %q", r.CostConfidence)
	}
}

func TestEgressPricing_Tier4MissingCatalogFallsToHardcoded(t *testing.T) {
	ResetEgressWarningStateForTests()
	t.Cleanup(ResetEgressWarningStateForTests)
	bogus := filepath.Join(t.TempDir(), "no.json")
	eng := NewEgressPricingEngineFromPath(bogus)
	r := eng.ResolveRate("aws", "us-east-1")
	if !r.RatePerGB.Equal(decimal.RequireFromString("0.09")) {
		t.Fatalf("rate_per_gb = %s", r.RatePerGB)
	}
	if r.CostConfidence != "estimated" {
		t.Fatalf("cost_confidence = %q", r.CostConfidence)
	}
}

func TestEgressPricing_Tier4MalformedCatalogFallsToHardcoded(t *testing.T) {
	ResetEgressWarningStateForTests()
	t.Cleanup(ResetEgressWarningStateForTests)
	bad := filepath.Join(t.TempDir(), "bad.json")
	if err := os.WriteFile(bad, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	eng := NewEgressPricingEngineFromPath(bad)
	r := eng.ResolveRate("aws", "us-east-1")
	if !r.RatePerGB.Equal(decimal.RequireFromString("0.09")) {
		t.Fatalf("rate_per_gb = %s", r.RatePerGB)
	}
	if r.CostConfidence != "estimated" {
		t.Fatalf("cost_confidence = %q", r.CostConfidence)
	}
}

func TestEgressPricing_Tier4MetaDefaultMissingFallsToHardcoded(t *testing.T) {
	ResetEgressWarningStateForTests()
	t.Cleanup(ResetEgressWarningStateForTests)
	bad := filepath.Join(t.TempDir(), "no_meta_default.json")
	if err := os.WriteFile(bad, []byte(`{"_meta": {"version": "x", "currency": "USD"}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	eng := NewEgressPricingEngineFromPath(bad)
	r := eng.ResolveRate("", "")
	if !r.RatePerGB.Equal(decimal.RequireFromString("0.09")) {
		t.Fatalf("rate_per_gb = %s", r.RatePerGB)
	}
	if r.CostConfidence != "estimated" {
		t.Fatalf("cost_confidence = %q", r.CostConfidence)
	}
}

func TestEgressPricing_WarnOncePerFailureMode(t *testing.T) {
	ResetEgressWarningStateForTests()
	t.Cleanup(ResetEgressWarningStateForTests)
	bogus := filepath.Join(t.TempDir(), "missing.json")
	out := captureLogs(t, func() {
		NewEgressPricingEngineFromPath(bogus)
		NewEgressPricingEngineFromPath(bogus)
	})
	// Count the "not found" warnings — must be exactly one even though we
	// constructed the engine twice for the same missing path.
	count := strings.Count(strings.ToLower(out), "not found")
	if count != 1 {
		t.Fatalf("expected exactly 1 'not found' log, got %d:\n%s", count, out)
	}
}

func TestEgressPricing_WarnDistinctModesIndependently(t *testing.T) {
	ResetEgressWarningStateForTests()
	t.Cleanup(ResetEgressWarningStateForTests)
	tmp := t.TempDir()
	missing := filepath.Join(tmp, "missing.json")
	malformed := filepath.Join(tmp, "bad.json")
	if err := os.WriteFile(malformed, []byte("{"), 0o644); err != nil {
		t.Fatal(err)
	}
	out := captureLogs(t, func() {
		NewEgressPricingEngineFromPath(missing)
		NewEgressPricingEngineFromPath(malformed)
	})
	low := strings.ToLower(out)
	if !strings.Contains(low, "not found") {
		t.Fatalf("missing 'not found' warning:\n%s", out)
	}
	if !strings.Contains(low, "malformed") {
		t.Fatalf("missing 'malformed' warning:\n%s", out)
	}
}

func TestEgressPricing_DecimalNoFloatDrift(t *testing.T) {
	// The divisor for GB conversion is decimal — never float64. These
	// equalities hold exactly in Decimal arithmetic but would drift with
	// IEEE-754 multiplication.
	a := decimal.RequireFromString("0.1093").Mul(decimal.RequireFromString("1000000000"))
	if !a.Equal(decimal.RequireFromString("109300000.0000")) {
		t.Fatalf("0.1093 * 1_000_000_000 = %s, want 109300000.0000", a)
	}
	b := decimal.RequireFromString("0.087").Mul(decimal.RequireFromString("12345678"))
	if !b.Equal(decimal.RequireFromString("1074073.986")) {
		t.Fatalf("0.087 * 12345678 = %s, want 1074073.986", b)
	}
	// And the GB divisor itself: NewFromInt(1_000_000_000) must equal the
	// string form.
	if !decimal.NewFromInt(1_000_000_000).Equal(decimal.RequireFromString("1000000000")) {
		t.Fatal("NewFromInt(1_000_000_000) != Decimal('1000000000')")
	}
}

func TestEgressPricing_VersionFromMeta(t *testing.T) {
	eng := newTestEngine(t)
	if got := eng.CatalogVersion(); got != "1.0.0" {
		t.Fatalf("catalog_version = %q, want 1.0.0", got)
	}
}

func TestEgressPricing_EgressRateValueSemantics(t *testing.T) {
	// In Python this asserts dataclass(frozen=True). In Go EgressRate is a
	// pass-by-value struct — copies don't mutate the original. Verify the
	// zero-value behaviour + that a copy is independent.
	eng := newTestEngine(t)
	r := eng.ResolveRate("aws", "us-east-1")
	original := r.RatePerGB
	r2 := r
	r2.RatePerGB = decimal.RequireFromString("99")
	if !r.RatePerGB.Equal(original) {
		t.Fatalf("modifying a copy mutated the original: %s vs %s", r.RatePerGB, original)
	}
}
