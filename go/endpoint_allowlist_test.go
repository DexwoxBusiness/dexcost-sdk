// Endpoint validation — security/sdk-endpoint-explicit-config.
//
// The endpoint comes ONLY from explicit in-code Config.Endpoint; the SDK no
// longer reads the DEXCOST_ENDPOINT env var. Config.Endpoint is
// developer-supplied/trusted, so validation is minimal: a non-empty value
// must carry an http:// or https:// scheme (http:// is intentionally allowed
// for local e2e because it is not env-controllable). Anything else falls
// back to the production default.

package dexcost

import "testing"

func TestResolvedEndpoint_ExplicitHttpsAccepted(t *testing.T) {
	c := &Config{Endpoint: "https://custom.example.com"}
	if got := c.resolvedEndpoint(); got != "https://custom.example.com" {
		t.Errorf("expected https://custom.example.com, got %q", got)
	}
}

func TestResolvedEndpoint_ExplicitHttpLocalhostAccepted(t *testing.T) {
	// http:// is allowed for the explicit (trusted) field — e.g. local e2e.
	c := &Config{Endpoint: "http://localhost:3001"}
	if got := c.resolvedEndpoint(); got != "http://localhost:3001" {
		t.Errorf("expected http://localhost:3001, got %q", got)
	}
}

func TestResolvedEndpoint_RejectsSchemelessFallsBackToDefault(t *testing.T) {
	c := &Config{Endpoint: "ftp://x"}
	if got := c.resolvedEndpoint(); got != defaultEndpoint {
		t.Errorf("expected %q (default), got %q", defaultEndpoint, got)
	}
}

func TestResolvedEndpoint_RejectsArbitraryScheme(t *testing.T) {
	c := &Config{Endpoint: "javascript:alert(1)"}
	if got := c.resolvedEndpoint(); got != defaultEndpoint {
		t.Errorf("expected %q (default), got %q", defaultEndpoint, got)
	}
}

func TestResolvedEndpoint_DefaultWhenEmpty(t *testing.T) {
	c := &Config{}
	if got := c.resolvedEndpoint(); got != defaultEndpoint {
		t.Errorf("expected %q (default), got %q", defaultEndpoint, got)
	}
}
