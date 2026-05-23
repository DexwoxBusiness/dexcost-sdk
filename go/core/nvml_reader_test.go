// Task 1 — NVML library wrapper tests. Mirrors python commit b5424ea.
//
// The Go SDK avoids a hard dep on github.com/NVIDIA/go-nvml so the SDK
// builds and tests on GPU-less hosts (mirrors Python's optional pynvml
// dependency). The reader exposes a pluggable backend so tests can inject
// a mock and the default backend returns "unavailable" off-GPU.

package core

import (
	"strings"
	"testing"
)

func TestNVMLAvailableDefaultsToFalseOffGPU(t *testing.T) {
	resetNVMLForTests()
	t.Cleanup(resetNVMLForTests)
	if NVMLAvailable() {
		t.Fatalf("NVMLAvailable() default = true; want false on GPU-less default backend")
	}
}

func TestInitNVMLReturnsFalseWhenUnavailable(t *testing.T) {
	resetNVMLForTests()
	t.Cleanup(resetNVMLForTests)
	if InitNVML() {
		t.Fatalf("InitNVML() = true on default backend; want false")
	}
}

func TestGetDeviceCountNilWhenUnavailable(t *testing.T) {
	resetNVMLForTests()
	t.Cleanup(resetNVMLForTests)
	n := GetNVMLDeviceCount()
	if n != nil {
		t.Fatalf("GetNVMLDeviceCount() = %v; want nil off-GPU", *n)
	}
}

func TestGetProductNameNilWhenUnavailable(t *testing.T) {
	resetNVMLForTests()
	t.Cleanup(resetNVMLForTests)
	n := GetNVMLProductName(0)
	if n != nil {
		t.Fatalf("GetNVMLProductName(0) = %q; want nil off-GPU", *n)
	}
}

// ─── Decision #4 — NFC normalization + lowercase + whitespace collapse ──

func TestNormalizeProductNameNFCLowercaseCollapse(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		// Trivial.
		{"NVIDIA H100", "nvidia h100"},
		// Non-breaking space U+00A0 → collapsed.
		{"NVIDIA H100", "nvidia h100"},
		// Narrow no-break space U+202F → collapsed.
		{"NVIDIA H100", "nvidia h100"},
		// Multiple internal spaces collapse to one.
		{"NVIDIA   H100   80GB", "nvidia h100 80gb"},
		// Leading + trailing whitespace stripped.
		{"  NVIDIA H100  ", "nvidia h100"},
	}
	for _, c := range cases {
		got := normalizeProductName(c.in)
		if got != c.want {
			t.Errorf("normalizeProductName(%q) = %q; want %q", c.in, got, c.want)
		}
	}
}

// ─── Mock backend allows the rest of the GPU stack to be tested ─────────

func TestMockBackendCanProvideDevices(t *testing.T) {
	resetNVMLForTests()
	t.Cleanup(resetNVMLForTests)
	mock := &MockNVMLBackend{
		Available:   true,
		DeviceCount: 2,
		ProductNames: map[int]string{
			0: "NVIDIA H100 80GB HBM3",
			1: "NVIDIA H100 80GB HBM3",
		},
	}
	SetNVMLBackendForTests(mock.AsBackend())
	if !NVMLAvailable() {
		t.Fatalf("NVMLAvailable() = false; expected true with mock")
	}
	if !InitNVML() {
		t.Fatalf("InitNVML() = false; expected true with mock")
	}
	count := GetNVMLDeviceCount()
	if count == nil || *count != 2 {
		t.Fatalf("device count: got %v; want 2", count)
	}
	name := GetNVMLProductName(0)
	if name == nil || !strings.Contains(*name, "h100") {
		t.Fatalf("product name: got %v; want normalized lowercase containing h100", name)
	}
	// NFC normalization: NBSP collapsed to single space.
	if name == nil || strings.Contains(*name, " ") {
		t.Fatalf("product name still contains NBSP: %q", *name)
	}
}
