// Task 6 — Per-task GPU accountant tests. Mirrors python commit 0d47371.

package core

import (
	"os"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

// gpuMockWithDevices returns a ready-to-use mock NVML backend with N
// devices and self-PID accumulating SM time. The start sample has
// timeStamp=0; the end sample has timeStamp=1_000_000 (one full second).
// Using os.Getpid() ensures the PID falls into the accountant's cgroup
// PID union (which on non-container hosts degrades to self-PID-only).
func gpuMockWithDevices(deviceCount int) *MockNVMLBackend {
	selfPID := os.Getpid()
	utilStart := map[int]map[int]NVMLUtilSample{}
	utilEnd := map[int]map[int]NVMLUtilSample{}
	productNames := map[int]string{}
	mem := map[int]NVMLMemInfo{}
	for i := 0; i < deviceCount; i++ {
		productNames[i] = "NVIDIA H100 80GB HBM3"
		utilStart[i] = map[int]NVMLUtilSample{
			selfPID: {PID: selfPID, SMUtil: 0, MemUtil: 0, TimeStamp: 0},
		}
		utilEnd[i] = map[int]NVMLUtilSample{
			selfPID: {PID: selfPID, SMUtil: 80, MemUtil: 30, TimeStamp: 1_000_000},
		}
		mem[i] = NVMLMemInfo{UsedBytes: 40 * 1024 * 1024 * 1024, TotalBytes: 80 * 1024 * 1024 * 1024}
	}
	return &MockNVMLBackend{
		Available:    true,
		DeviceCount:  deviceCount,
		ProductNames: productNames,
		Memory:       mem,
		PerCallUtilization: map[int][]map[int]NVMLUtilSample{
			0: {utilStart[0], utilEnd[0]},
		},
	}
}

func TestGpuAccountantSnapshotEndEmitsCostEvent(t *testing.T) {
	resetNVMLForTests()
	mock := gpuMockWithDevices(1)
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)

	a := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	a.SnapshotStart()
	cost, signals := a.SnapshotEndAndBuild(1000)

	if cost == nil {
		t.Fatalf("expected gpu_cost details; got nil")
	}
	if cost["billing_model"] != "per_gpu_second_active" {
		t.Errorf("billing_model = %v; want per_gpu_second_active", cost["billing_model"])
	}
	if cost["cost_pending"] != true {
		t.Errorf("cost_pending should be true on emit")
	}
	if cost["gpu_vendor"] != "nvidia" {
		t.Errorf("gpu_vendor = %v; want nvidia", cost["gpu_vendor"])
	}
	if cost["gpu_count"] != 1 {
		t.Errorf("gpu_count = %v; want 1", cost["gpu_count"])
	}
	if len(signals) != 1 {
		t.Errorf("expected 1 utilization-signal event; got %d", len(signals))
	}
}

func TestGpuAccountantIdempotentFinalize(t *testing.T) {
	resetNVMLForTests()
	mock := gpuMockWithDevices(1)
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)

	a := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	a.SnapshotStart()
	cost1, _ := a.SnapshotEndAndBuild(1000)
	cost2, signals2 := a.SnapshotEndAndBuild(1000)
	if cost1 == nil {
		t.Fatalf("first finalize returned nil")
	}
	if cost2 != nil || signals2 != nil {
		t.Errorf("idempotent second call should return nil/nil; got %v / %v", cost2, signals2)
	}
}

func TestGpuAccountantNoNVMLReturnsNil(t *testing.T) {
	resetNVMLForTests() // noop backend
	t.Cleanup(resetNVMLForTests)
	a := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	a.SnapshotStart()
	cost, signals := a.SnapshotEndAndBuild(1000)
	if cost != nil || signals != nil {
		t.Errorf("no NVML should produce no events; got %v / %v", cost, signals)
	}
}

// Decision #3 sharpening: sm_util_pct is task-window-averaged, NOT a point sample.
func TestGpuAccountantSmUtilPctWindowAveraged(t *testing.T) {
	resetNVMLForTests()
	mock := gpuMockWithDevices(1)
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)

	a := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	a.SnapshotStart()
	// duration_ms = 2000 — task ran 2 seconds, but only 1s of GPU activity
	// (TimeStamp delta from 0 → 1_000_000 µs = 1s). So sm_util_pct should
	// be ~50%, NOT 80% (the point-sample value).
	_, signals := a.SnapshotEndAndBuild(2000)
	if len(signals) != 1 {
		t.Fatalf("expected 1 signal; got %d", len(signals))
	}
	sig := signals[0]
	smUtilV, ok := sig["sm_util_pct"]
	if !ok || smUtilV == nil {
		t.Fatalf("sm_util_pct should be set; got %v", smUtilV)
	}
	smUtil, ok := smUtilV.(float64)
	if !ok {
		t.Fatalf("sm_util_pct should be float64; got %T", smUtilV)
	}
	if smUtil < 49 || smUtil > 51 {
		t.Errorf("Decision #3 — sm_util_pct should be window-averaged (~50%%); "+
			"got %v (point-sample bug?)", smUtil)
	}
}

// Decision #3 — sub-100ms degenerate window → sm_util_pct=nil.
func TestGpuAccountantDegenerateWindowEmitsNilSmUtilPct(t *testing.T) {
	resetNVMLForTests()
	mock := &MockNVMLBackend{
		Available:    true,
		DeviceCount:  1,
		ProductNames: map[int]string{0: "NVIDIA H100 80GB HBM3"},
		Memory:       map[int]NVMLMemInfo{0: {TotalBytes: 80 * 1024 * 1024 * 1024}},
		// No utilization samples at all — degenerate window.
		PerCallUtilization: map[int][]map[int]NVMLUtilSample{
			0: {{}, {}},
		},
	}
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)

	a := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	a.SnapshotStart()
	_, signals := a.SnapshotEndAndBuild(0) // 0-duration → degenerate
	if len(signals) != 1 {
		t.Fatalf("degenerate window should still emit one signal per device; got %d", len(signals))
	}
	sm := signals[0]["sm_util_pct"]
	if sm != nil {
		t.Errorf("degenerate sm_util_pct should be nil; got %v", sm)
	}
}

// ─── Decision #2 — MIG detected, full-billing applied ──────────────────

func TestGpuAccountantDetectsMIGAndEmitsTransparencyEvent(t *testing.T) {
	resetNVMLForTests()
	mock := &MockNVMLBackend{
		Available:    true,
		DeviceCount:  1,
		ProductNames: map[int]string{0: "NVIDIA H100 80GB HBM3"},
		MIGModes:     map[int]bool{0: true},
		Memory:       map[int]NVMLMemInfo{0: {TotalBytes: 80 * 1024 * 1024 * 1024}},
	}
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)

	a := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	a.SnapshotStart()
	cost, _ := a.SnapshotEndAndBuild(1000)
	if cost == nil {
		t.Fatalf("MIG-detected task should still emit cost event for transparency")
	}
	if cost["mig_profile"] == nil {
		t.Errorf("Decision #2 — MIG should populate mig_profile; got nil")
	}
}

// ─── Registry pattern (Go SDK chose this over Python's task field) ─────

func TestGpuAccountantRegistryRoundtrip(t *testing.T) {
	ResetGpuAccountantRegistryForTests()
	t.Cleanup(ResetGpuAccountantRegistryForTests)

	a := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	taskID := "task-abc"
	RegisterGpuAccountant(taskID, a)
	got := GetGpuAccountant(taskID)
	if got == nil {
		t.Fatalf("registered accountant should be retrievable")
	}
	removed := UnregisterGpuAccountant(taskID)
	if removed == nil {
		t.Errorf("unregister should return the accountant")
	}
	if GetGpuAccountant(taskID) != nil {
		t.Errorf("post-unregister lookup should return nil")
	}
}

// Decision #1 — sets _cgroup_scope_fallback when scope is not container.
// We can't easily mock /proc/self/cgroup; verify the field plumbs through
// when the accountant is constructed with an explicit cgroup-scope override.
func TestGpuAccountantPropagatesCgroupFallbackLabel(t *testing.T) {
	resetNVMLForTests()
	mock := gpuMockWithDevices(1)
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)

	a := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	// Force a degraded scope so the fallback label is set.
	a.SetScopeForTests(CgroupScope{Kind: CgroupKindBareMetalUserSlice})
	a.SnapshotStart()
	cost, _ := a.SnapshotEndAndBuild(1000)
	if cost == nil {
		t.Fatalf("degraded scope should still emit a zero/observability event")
	}
	if cost["_cgroup_scope_fallback"] != "no_container_scope" {
		t.Errorf("expected fallback label no_container_scope; got %v", cost["_cgroup_scope_fallback"])
	}
}
