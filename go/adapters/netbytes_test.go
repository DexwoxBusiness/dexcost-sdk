package adapters

import "testing"

func boolPtr(b bool) *bool { return &b }

func TestClassifyDestination_PrivateIPv4(t *testing.T) {
	cases := []string{"10.1.2.3", "192.168.0.5", "172.16.9.9"}
	for _, host := range cases {
		got := ClassifyDestination(host)
		if got == nil || *got != true {
			t.Errorf("ClassifyDestination(%q) = %v, want *bool(true)", host, ptrFmt(got))
		}
	}
}

func TestClassifyDestination_LoopbackAndLinkLocal(t *testing.T) {
	cases := []string{"127.0.0.1", "::1", "169.254.10.1"}
	for _, host := range cases {
		got := ClassifyDestination(host)
		if got == nil || *got != true {
			t.Errorf("ClassifyDestination(%q) = %v, want *bool(true)", host, ptrFmt(got))
		}
	}
}

func TestClassifyDestination_PublicIP(t *testing.T) {
	cases := []string{"8.8.8.8", "1.1.1.1"}
	for _, host := range cases {
		got := ClassifyDestination(host)
		if got == nil || *got != false {
			t.Errorf("ClassifyDestination(%q) = %v, want *bool(false)", host, ptrFmt(got))
		}
	}
}

func TestClassifyDestination_NamedHost(t *testing.T) {
	// A hostname (not an IP literal): no DNS lookup, returns nil.
	if got := ClassifyDestination("api.openai.com"); got != nil {
		t.Errorf("ClassifyDestination(api.openai.com) = %v, want nil", ptrFmt(got))
	}
	if got := ClassifyDestination(""); got != nil {
		t.Errorf("ClassifyDestination(\"\") = %v, want nil", ptrFmt(got))
	}
}

func TestClassifyDestination_IPv6ULA(t *testing.T) {
	// fd00::/8 — IPv6 unique-local (RFC 4193).
	got := ClassifyDestination("fd00::1")
	if got == nil || *got != true {
		t.Errorf("ClassifyDestination(fd00::1) = %v, want *bool(true)", ptrFmt(got))
	}
}

func TestClassifyDestination_IPv6LinkLocal(t *testing.T) {
	// fe80::/10 — IPv6 link-local.
	got := ClassifyDestination("fe80::1")
	if got == nil || *got != true {
		t.Errorf("ClassifyDestination(fe80::1) = %v, want *bool(true)", ptrFmt(got))
	}
}

func TestClassifyDestination_CGNATIsPublic(t *testing.T) {
	// 100.64.0.0/10 — RFC 6598 shared address space, NOT covered by
	// net.IP.IsPrivate (mirroring Python ipaddress behaviour).
	got := ClassifyDestination("100.64.1.1")
	if got == nil || *got != false {
		t.Errorf("ClassifyDestination(100.64.1.1) = %v, want *bool(false)", ptrFmt(got))
	}
}

func TestMeasureBytesFromHeaders_ExactTotal(t *testing.T) {
	// Pin the +4/+2/+12 constants against silent regression.
	// Input: method="GET", url="https://a.io/", headers={"X-H": "v"}, body_len=0
	// request_line = len("GET") + len("https://a.io/") + 12 = 3 + 13 + 12 = 28
	// headers: (len("X-H") + len("v") + 4) + 2 = (3 + 1 + 4) + 2 = 10
	// body = 0
	// total = 28 + 10 + 0 = 38
	got := MeasureBytesFromHeaders("GET", "https://a.io/", map[string]string{"X-H": "v"}, 0)
	if got != 38 {
		t.Errorf("MeasureBytesFromHeaders pin = %d, want 38", got)
	}
}

func TestMeasureBytesFromHeaders_IncludesHeadersAndBody(t *testing.T) {
	headers := map[string]string{"Content-Length": "2048", "Content-Type": "application/json"}
	n := MeasureBytesFromHeaders("POST", "https://x.com/v1/y", headers, 2048)
	if n < 2048 {
		t.Errorf("MeasureBytesFromHeaders = %d, want >= 2048", n)
	}
	if n <= 2048 {
		t.Errorf("MeasureBytesFromHeaders = %d, want > 2048 (header bytes count too)", n)
	}
}

func TestMeasureBytesFromHeaders_ZeroBody(t *testing.T) {
	n := MeasureBytesFromHeaders("GET", "https://x.com/", map[string]string{}, 0)
	if n <= 0 {
		t.Errorf("MeasureBytesFromHeaders zero-body = %d, want > 0 (request line still costs bytes)", n)
	}
}

func TestMeasureBytesFromHeaders_NegativeBodyClamped(t *testing.T) {
	// Negative body_len behaves like zero (matches Python max(0, int(body_len))).
	plain := MeasureBytesFromHeaders("GET", "/", map[string]string{}, 0)
	clamped := MeasureBytesFromHeaders("GET", "/", map[string]string{}, -42)
	if plain != clamped {
		t.Errorf("negative body_len not clamped: plain=%d clamped=%d", plain, clamped)
	}
}

// ptrFmt formats a *bool nicely for test failure messages.
func ptrFmt(b *bool) string {
	if b == nil {
		return "nil"
	}
	if *b {
		return "*bool(true)"
	}
	return "*bool(false)"
}
