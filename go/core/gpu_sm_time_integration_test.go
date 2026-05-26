// B2 regression — Sprint 2 Theme C / plan §3.1.1 (Go port of Python d37b6b5).
//
// Pre-fix the Go GPU accountant treated NVML's per-PID timeStamp as if
// it were "accumulated SM-microseconds" and emitted
// `gpu_seconds_used = max_ts - base_ts` per device — reporting wall
// time × 100% utilization instead of integrated SM utilization.
//
// Post-fix the accountant integrates sm_util × dt across the sample
// sequence NVML returns, matching the Python canonical implementation.

package core

import (
	"os"
	"testing"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

func TestGpuSecondsUsed_IsIntegratedSmTimeNotWallTime(t *testing.T) {
	resetNVMLForTests()
	pid := os.Getpid()
	mock := &MockNVMLBackend{
		Available:    true,
		DeviceCount:  1,
		ProductNames: map[int]string{0: "NVIDIA H100 80GB HBM3"},
		Memory: map[int]NVMLMemInfo{
			0: {UsedBytes: 21474836480, TotalBytes: 85899345920},
		},
		// Baseline (empty) → end (two-sample-per-PID).
		// PID at t=20s sm=80% → covers 0..20s → 16 sm-seconds
		// PID at t=60s sm=40% → covers 20..60s → 16 sm-seconds
		// Canonical total: 32 sm-seconds.
		PerCallUtilization: map[int][]map[int][]NVMLUtilSample{
			0: {
				{}, // baseline — no samples yet
				{
					pid: {
						{PID: pid, SMUtil: 80, MemUtil: 10, TimeStamp: 20_000_000},
						{PID: pid, SMUtil: 40, MemUtil: 10, TimeStamp: 60_000_000},
					},
				},
			},
		},
	}
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)

	acc := NewGpuAccountant(GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"})
	acc.SnapshotStart()
	costDetails, _ := acc.SnapshotEndAndBuild(60_000)

	if costDetails == nil {
		t.Fatal("expected cost details, got nil")
	}
	gpuSeconds, ok := costDetails["gpu_seconds_used"].(float64)
	if !ok {
		t.Fatalf("gpu_seconds_used has unexpected type: %T", costDetails["gpu_seconds_used"])
	}
	const want = 32.0
	const tolerance = 0.01
	if gpuSeconds < want-tolerance || gpuSeconds > want+tolerance {
		t.Errorf("expected integrated sm_seconds≈%f, got %f — likely still using wall_dt",
			want, gpuSeconds)
	}
}
