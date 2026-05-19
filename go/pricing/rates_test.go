package pricing

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/shopspring/decimal"
)

func TestRateRegistry_RegisterAndGet(t *testing.T) {
	reg := NewRateRegistry()
	reg.Register("google_maps", "request", decimal.RequireFromString("0.005"))

	entry := reg.Get("google_maps")
	if entry == nil {
		t.Fatal("expected entry")
	}
	if !entry.CostUSD.Equal(decimal.RequireFromString("0.005")) {
		t.Errorf("expected 0.005, got %s", entry.CostUSD)
	}
	if entry.Per != "request" {
		t.Errorf("expected request, got %s", entry.Per)
	}
}

func TestRateRegistry_GetUnknown(t *testing.T) {
	reg := NewRateRegistry()
	if reg.Get("unknown") != nil {
		t.Error("expected nil for unknown service")
	}
}

func TestRateRegistry_VersionChanges(t *testing.T) {
	reg := NewRateRegistry()
	reg.Register("svc_a", "request", decimal.RequireFromString("0.01"))
	v1 := reg.PricingVersion()
	reg.Register("svc_b", "page", decimal.RequireFromString("0.02"))
	v2 := reg.PricingVersion()
	if v1 == v2 {
		t.Error("version should change when rates change")
	}
}

func TestRateRegistry_VersionDeterministic(t *testing.T) {
	reg1 := NewRateRegistry()
	reg1.Register("a", "r", decimal.RequireFromString("0.01"))
	reg1.Register("b", "r", decimal.RequireFromString("0.02"))

	reg2 := NewRateRegistry()
	reg2.Register("b", "r", decimal.RequireFromString("0.02"))
	reg2.Register("a", "r", decimal.RequireFromString("0.01"))

	if reg1.PricingVersion() != reg2.PricingVersion() {
		t.Error("version should be deterministic regardless of insertion order")
	}
}

// TestRateRegistry_LoadCanonicalFormat verifies the Python-compatible
// `rates:`-keyed YAML format is accepted.
func TestRateRegistry_LoadCanonicalFormat(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rates.yaml")
	content := "rates:\n  maps.googleapis.com:\n    per: request\n    cost_usd: \"0.005\"\n"
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
	reg := NewRateRegistry()
	if err := reg.LoadYAML(path); err != nil {
		t.Fatalf("LoadYAML: %v", err)
	}
	entry := reg.Get("maps.googleapis.com")
	if entry == nil || !entry.CostUSD.Equal(decimal.RequireFromString("0.005")) {
		t.Fatalf("expected maps.googleapis.com=0.005, got %+v", entry)
	}
}

// TestRateRegistry_LoadLegacyFlatFormat verifies the pre-parity flat map
// format is still accepted for backward compatibility.
func TestRateRegistry_LoadLegacyFlatFormat(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rates.yaml")
	content := "ocr-api.com:\n  per: page\n  cost_usd: \"0.01\"\n"
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
	reg := NewRateRegistry()
	if err := reg.LoadYAML(path); err != nil {
		t.Fatalf("LoadYAML: %v", err)
	}
	if reg.Get("ocr-api.com") == nil {
		t.Fatal("expected legacy flat-format entry to load")
	}
}

// TestRateRegistry_ExportRoundTrip verifies ExportYAML writes the canonical
// `rates:` format and LoadYAML reads it back.
func TestRateRegistry_ExportRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rates.yaml")
	reg := NewRateRegistry()
	reg.Register("twilio_sms", "message", decimal.RequireFromString("0.0079"))
	if err := reg.ExportYAML(path); err != nil {
		t.Fatalf("ExportYAML: %v", err)
	}
	data, _ := os.ReadFile(path)
	if !strings.Contains(string(data), "rates:") {
		t.Errorf("expected canonical 'rates:' key in export\n%s", data)
	}
	reg2 := NewRateRegistry()
	if err := reg2.LoadYAML(path); err != nil {
		t.Fatalf("LoadYAML round-trip: %v", err)
	}
	if reg2.Get("twilio_sms") == nil {
		t.Fatal("expected round-tripped entry")
	}
}

func TestRateRegistry_VersionIs12Hex(t *testing.T) {
	reg := NewRateRegistry()
	reg.Register("x", "y", decimal.RequireFromString("0.01"))
	v := reg.PricingVersion()
	if len(v) != 12 {
		t.Errorf("expected 12 chars, got %d", len(v))
	}
}
