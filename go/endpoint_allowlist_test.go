// A2 regression — Sprint 1 Theme A / plan §2.1.
//
// DEXCOST_ENDPOINT env var must be rejected if it doesn't start with
// `https://`. An attacker who controls the env (misconfigured CI
// runner, hostile container) could otherwise silently exfiltrate cost
// telemetry to an HTTP collector — we refuse and fall back to the
// production default.

package dexcost

import "testing"

func TestResolvedEndpoint_AcceptsHttps(t *testing.T) {
	t.Setenv("DEXCOST_ENDPOINT", "https://custom.example.com")
	c := &Config{}
	if got := c.resolvedEndpoint(); got != "https://custom.example.com" {
		t.Errorf("expected https://custom.example.com, got %q", got)
	}
}

func TestResolvedEndpoint_RejectsHttpFallsBackToDefault(t *testing.T) {
	t.Setenv("DEXCOST_ENDPOINT", "http://attacker.example/")
	c := &Config{}
	if got := c.resolvedEndpoint(); got != defaultEndpoint {
		t.Errorf("expected %q (default), got %q", defaultEndpoint, got)
	}
}

func TestResolvedEndpoint_RejectsArbitraryScheme(t *testing.T) {
	t.Setenv("DEXCOST_ENDPOINT", "javascript:alert(1)")
	c := &Config{}
	if got := c.resolvedEndpoint(); got != defaultEndpoint {
		t.Errorf("expected %q (default), got %q", defaultEndpoint, got)
	}
}

func TestResolvedEndpoint_DefaultWhenUnset(t *testing.T) {
	t.Setenv("DEXCOST_ENDPOINT", "")
	c := &Config{}
	if got := c.resolvedEndpoint(); got != defaultEndpoint {
		t.Errorf("expected %q (default), got %q", defaultEndpoint, got)
	}
}
