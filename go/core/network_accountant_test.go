package core

import (
	"fmt"
	"testing"
)

// boolPtr is a tiny test helper for nullable bool literals (Go has no
// nullable-bool syntax). Mirrors the adapters/netbytes_test.go helper —
// duplicated here because this file lives in the `core` package.
func boolPtr(b bool) *bool { return &b }

// --- Basic record + finalize -----------------------------------------------

func TestNetworkAccountant_RecordUpdatesCounters(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("a.com", 100, 10, nil)
	a.Record("a.com", 50, 5, nil)
	snap := a.Finalize()
	if snap.BytesIn != 150 {
		t.Fatalf("BytesIn = %d, want 150", snap.BytesIn)
	}
	if snap.BytesOut != 15 {
		t.Fatalf("BytesOut = %d, want 15", snap.BytesOut)
	}
	if snap.CallCount != 2 {
		t.Fatalf("CallCount = %d, want 2", snap.CallCount)
	}
}

func TestNetworkAccountant_FinalizeGroupsByHost(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("a.com", 100, 10, nil)
	a.Record("b.com", 200, 20, nil)
	hosts := a.Finalize().ByHost["hosts"].([]map[string]interface{})
	byName := map[string]map[string]interface{}{}
	for _, h := range hosts {
		byName[h["host"].(string)] = h
	}
	got := byName["a.com"]
	if got["calls"].(int64) != 1 {
		t.Fatalf("a.com calls = %v, want 1", got["calls"])
	}
	if got["bytes_in"].(int64) != 100 {
		t.Fatalf("a.com bytes_in = %v, want 100", got["bytes_in"])
	}
	if got["bytes_out"].(int64) != 10 {
		t.Fatalf("a.com bytes_out = %v, want 10", got["bytes_out"])
	}
	// isInternal=nil → bytes_out attributes as external (v2 §6.1).
	if got["external_bytes_out"].(int64) != 10 {
		t.Fatalf("a.com external_bytes_out = %v, want 10", got["external_bytes_out"])
	}
}

func TestNetworkAccountant_FinalizeCapsToTop20WithOtherBucket(t *testing.T) {
	a := NewNetworkAccountant()
	// 25 hosts; host_i gets i+1 bytes_in so heavy ones are deterministic.
	for i := 0; i < 25; i++ {
		a.Record(fmt.Sprintf("h%02d.com", i), int64(i)+1, 0, nil)
	}
	hosts := a.Finalize().ByHost["hosts"].([]map[string]interface{})
	if len(hosts) != FinalizeCap+1 {
		t.Fatalf("len(hosts) = %d, want %d", len(hosts), FinalizeCap+1)
	}
	names := map[string]bool{}
	for _, h := range hosts {
		names[h["host"].(string)] = true
	}
	if !names["_other"] {
		t.Fatal("_other bucket missing")
	}
	if !names["h24.com"] {
		t.Fatal("heaviest host h24.com missing")
	}
	if names["h00.com"] {
		t.Fatal("lightest host h00.com should be folded into _other")
	}
	var other map[string]interface{}
	for _, h := range hosts {
		if h["host"].(string) == "_other" {
			other = h
			break
		}
	}
	// 5 lightest folded: bytes_in = 1+2+3+4+5 = 15, calls = 5.
	if other["calls"].(int64) != 5 {
		t.Fatalf("_other.calls = %v, want 5", other["calls"])
	}
	if other["bytes_in"].(int64) != 15 {
		t.Fatalf("_other.bytes_in = %v, want 15", other["bytes_in"])
	}
}

func TestNetworkAccountant_EmptyFinalize(t *testing.T) {
	hosts := NewNetworkAccountant().Finalize().ByHost["hosts"].([]map[string]interface{})
	if len(hosts) != 0 {
		t.Fatalf("expected empty hosts, got %d entries", len(hosts))
	}
}

func TestNetworkAccountant_LiveCapFoldsOverflowIntoOther(t *testing.T) {
	a := NewNetworkAccountant()
	for i := 0; i < LiveCap+50; i++ {
		a.Record(fmt.Sprintf("host%d.com", i), 0, 1, boolPtr(false))
	}
	// Before finalize, only LiveCap distinct hosts in the map.
	if a.LiveHostCount() != LiveCap {
		t.Fatalf("LiveHostCount = %d, want %d", a.LiveHostCount(), LiveCap)
	}
	hosts := a.Finalize().ByHost["hosts"].([]map[string]interface{})
	var other map[string]interface{}
	for _, h := range hosts {
		if h["host"].(string) == "_other" {
			other = h
			break
		}
	}
	if other == nil {
		t.Fatal("_other bucket missing")
	}
	// 50 hosts that overflowed LiveCap + (LiveCap - FinalizeCap) folded
	// from the top-N cap = (LiveCap + 50 - FinalizeCap) entries.
	want := int64(LiveCap + 50 - FinalizeCap)
	if other["calls"].(int64) != want {
		t.Fatalf("_other.calls = %v, want %d", other["calls"], want)
	}
}

func TestNetworkAccountant_FrozenAfterFinalizeRecordIsNoop(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("a.com", 100, 10, nil)
	snap1 := a.Finalize()
	a.Record("b.com", 999, 999, nil)
	snap2 := a.Finalize()
	if snap1.BytesIn != snap2.BytesIn {
		t.Fatalf("BytesIn changed after freeze: %d → %d", snap1.BytesIn, snap2.BytesIn)
	}
	if snap1.CallCount != snap2.CallCount {
		t.Fatalf("CallCount changed after freeze: %d → %d", snap1.CallCount, snap2.CallCount)
	}
}

func TestNetworkAccountant_EmptyHostFallsBackToUnknown(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("", 10, 0, nil)
	hosts := a.Finalize().ByHost["hosts"].([]map[string]interface{})
	if hosts[0]["host"].(string) != "_unknown" {
		t.Fatalf("empty host got %q, want _unknown", hosts[0]["host"])
	}
}

func TestNetworkAccountant_NegativeBytesClampedToZero(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("a.com", -10, -20, nil)
	snap := a.Finalize()
	if snap.BytesIn != 0 {
		t.Fatalf("negative bytes_in not clamped: %d", snap.BytesIn)
	}
	if snap.BytesOut != 0 {
		t.Fatalf("negative bytes_out not clamped: %d", snap.BytesOut)
	}
}

func TestNetworkAccountant_SyntheticOtherCollidesWithRealHostNamedOther(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("_other", 100, 50, nil)
	a.Record("real.com", 1, 1, nil)
	hosts := a.Finalize().ByHost["hosts"].([]map[string]interface{})
	otherCount := 0
	var other map[string]interface{}
	for _, h := range hosts {
		if h["host"].(string) == "_other" {
			otherCount++
			other = h
		}
	}
	if otherCount != 1 {
		t.Fatalf("expected exactly one _other entry, got %d", otherCount)
	}
	if other["bytes_in"].(int64) != 100 {
		t.Fatalf("_other.bytes_in = %v, want 100 (real-host folded in)", other["bytes_in"])
	}
}

// --- External-byte split (v2) ----------------------------------------------

func TestNetworkAccountant_InternalCallDoesNotContributeToExternal(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("10.0.0.5", 100, 200, boolPtr(true))
	snap := a.Finalize()
	if snap.ExternalBytesOut != 0 {
		t.Fatalf("internal call should not contribute to external: got %d", snap.ExternalBytesOut)
	}
	host := snap.ByHost["hosts"].([]map[string]interface{})[0]
	if host["external_bytes_out"].(int64) != 0 {
		t.Fatalf("per-host external_bytes_out = %v, want 0", host["external_bytes_out"])
	}
	// Raw bytes_out still recorded.
	if host["bytes_out"].(int64) != 200 {
		t.Fatalf("per-host bytes_out = %v, want 200", host["bytes_out"])
	}
}

func TestNetworkAccountant_PublicCallContributesToExternal(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("api.example.com", 100, 500, boolPtr(false))
	snap := a.Finalize()
	if snap.ExternalBytesOut != 500 {
		t.Fatalf("public call external = %d, want 500", snap.ExternalBytesOut)
	}
}

func TestNetworkAccountant_NilIsInternalTreatedAsExternal(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("api.example.com", 100, 500, nil)
	snap := a.Finalize()
	if snap.ExternalBytesOut != 500 {
		t.Fatalf("nil isInternal external = %d, want 500", snap.ExternalBytesOut)
	}
}

// Property invariant (v2 §10.3 #1): scalar external == sum of per-host external.
func TestNetworkAccountant_ScalarEqualsSumOfPerHostExternal(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("a.com", 0, 100, boolPtr(false))
	a.Record("b.com", 0, 200, boolPtr(false))
	a.Record("10.0.0.1", 0, 999, boolPtr(true))
	snap := a.Finalize()
	var sum int64
	for _, h := range snap.ByHost["hosts"].([]map[string]interface{}) {
		sum += h["external_bytes_out"].(int64)
	}
	if sum != snap.ExternalBytesOut {
		t.Fatalf("sum(per-host external) = %d, scalar = %d", sum, snap.ExternalBytesOut)
	}
	if snap.ExternalBytesOut != 300 {
		t.Fatalf("ExternalBytesOut = %d, want 300", snap.ExternalBytesOut)
	}
}

// Mirror python's "_other carries external bytes through LIVE_CAP + top-20 folds".
func TestNetworkAccountant_OtherBucketCarriesExternalBytes(t *testing.T) {
	a := NewNetworkAccountant()
	for i := 0; i < LiveCap; i++ {
		a.Record(fmt.Sprintf("host%d.com", i), 0, 1, boolPtr(false))
	}
	a.Record("overflow.com", 0, 555, boolPtr(false))
	hosts := a.Finalize().ByHost["hosts"].([]map[string]interface{})
	var other map[string]interface{}
	for _, h := range hosts {
		if h["host"].(string) == "_other" {
			other = h
			break
		}
	}
	if other == nil {
		t.Fatal("_other bucket missing")
	}
	// 555 from the overflow + (LiveCap - FinalizeCap) folded from top-20 cap.
	want := int64(LiveCap-FinalizeCap) + 555
	if other["external_bytes_out"].(int64) != want {
		t.Fatalf("_other.external_bytes_out = %v, want %d", other["external_bytes_out"], want)
	}
}

func TestNetworkAccountant_DefaultIsInternalRoutesBytesAsExternal(t *testing.T) {
	a := NewNetworkAccountant()
	a.Record("api.example.com", 0, 100, nil)
	snap := a.Finalize()
	if snap.ExternalBytesOut != 100 {
		t.Fatalf("default nil → external = %d, want 100", snap.ExternalBytesOut)
	}
}

// --- Registry --------------------------------------------------------------

func TestAccountantRegistry_RegisterAndGet(t *testing.T) {
	ResetAccountantRegistryForTests()
	a := NewNetworkAccountant()
	RegisterAccountant("t-1", a)
	got := GetAccountant("t-1")
	if got != a {
		t.Fatalf("GetAccountant returned a different instance: %p vs %p", got, a)
	}
}

func TestAccountantRegistry_GetMissingReturnsNil(t *testing.T) {
	ResetAccountantRegistryForTests()
	if got := GetAccountant("does-not-exist"); got != nil {
		t.Fatalf("expected nil for missing task, got %p", got)
	}
}

func TestAccountantRegistry_UnregisterReturnsThenRemoves(t *testing.T) {
	ResetAccountantRegistryForTests()
	a := NewNetworkAccountant()
	RegisterAccountant("t-1", a)
	got := UnregisterAccountant("t-1")
	if got != a {
		t.Fatalf("Unregister returned a different instance")
	}
	if GetAccountant("t-1") != nil {
		t.Fatal("Unregister did not remove the entry")
	}
	// Idempotent: a second call returns nil.
	if UnregisterAccountant("t-1") != nil {
		t.Fatal("repeated Unregister did not return nil")
	}
}
