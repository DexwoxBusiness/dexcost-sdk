// Verify that ComputeBillingOverrides + K8sNodeAware thread through
// dexcost.Init() into the tracker. Mirrors Python tests
// test_compute_billing_overrides_threaded_through_init +
// test_k8s_node_aware_threaded_through_init in test_compute_wrap.py.

package dexcost

import (
	"testing"
)

func TestInit_ComputeBillingOverridesReachable(t *testing.T) {
	dir := t.TempDir()
	Close()
	err := Init(Config{
		Storage:   "local",
		BufferDir: dir,
		ComputeBillingOverrides: map[string]string{
			"cloud_run": "instance",
		},
	})
	if err != nil {
		t.Fatalf("Init: %v", err)
	}
	defer Close()
	// We can't observe the tracker's private field directly, but Init must
	// not error and the override map must round-trip through Config. The
	// behavioural check (override flips Cloud Run math) is covered by
	// pricing tests; this test pins the Init() surface.
}

func TestInit_K8sNodeAwareReachable(t *testing.T) {
	dir := t.TempDir()
	Close()
	err := Init(Config{
		Storage:      "local",
		BufferDir:    dir,
		K8sNodeAware: true,
	})
	if err != nil {
		t.Fatalf("Init: %v", err)
	}
	defer Close()
}
