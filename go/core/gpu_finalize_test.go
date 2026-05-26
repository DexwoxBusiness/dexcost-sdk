// Task 8 — finalizeGPU wired into tracker tests. Mirrors python commit 56d8d43.

package core

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

// withGpuMockOneDevice sets up NVML to report 1 H100 with self-PID samples.
func withGpuMockOneDevice(t *testing.T) {
	t.Helper()
	resetNVMLForTests()
	selfPID := os.Getpid()
	mock := &MockNVMLBackend{
		Available:    true,
		DeviceCount:  1,
		ProductNames: map[int]string{0: "NVIDIA H100 80GB HBM3"},
		Memory:       map[int]NVMLMemInfo{0: {TotalBytes: 80 * 1024 * 1024 * 1024}},
		PerCallUtilization: map[int][]map[int][]NVMLUtilSample{
			0: {
				{selfPID: {{PID: selfPID, SMUtil: 0, TimeStamp: 0}}},
				{selfPID: {{PID: selfPID, SMUtil: 80, TimeStamp: 1_000_000}}},
			},
		},
	}
	SetNVMLBackendForTests(mock.AsBackend())
	t.Cleanup(resetNVMLForTests)
}

func TestFinalizeGPUBackfillsCostFromPricingEngine(t *testing.T) {
	withGpuMockOneDevice(t)
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "modal"})
	t.Cleanup(cloud.ResetForTests)
	ResetGpuAccountantRegistryForTests()
	t.Cleanup(ResetGpuAccountantRegistryForTests)

	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}

	_, tt := tr.StartTask(context.Background(), "gpu_inference")
	// Register a GPU accountant + snapshot start.
	a := NewGpuAccountant(GpuRuntimeModal, cloud.GetCloudEnv())
	a.SnapshotStart()
	RegisterGpuAccountant(tt.Task.TaskID.String(), a)

	// Build the accountant's end events directly and insert them — emulating
	// what gpu_wrap does at the end of an invocation.
	cost, signals := a.SnapshotEndAndBuild(1000)
	if cost == nil {
		t.Fatalf("accountant should emit gpu_cost details")
	}
	evCost := NewEvent(tt.Task.TaskID, EventTypeGPUCost)
	evCost.CostUSD = decimal.Zero
	evCost.Details = cost
	if err := buf.InsertEvent(evCost); err != nil {
		t.Fatalf("InsertEvent: %v", err)
	}
	for _, sig := range signals {
		evSig := NewEvent(tt.Task.TaskID, EventTypeGPUUtilizationSignal)
		evSig.CostUSD = decimal.Zero
		evSig.Details = sig
		_ = buf.InsertEvent(evSig)
	}

	// Force a known duration.
	tt.Task.StartedAt = time.Now().UTC().Add(-time.Second)
	endedAt := time.Now().UTC()
	tt.Task.EndedAt = &endedAt
	tt.Task.Status = TaskStatusSuccess

	events, _ := buf.QueryEvents(tt.Task.TaskID.String())
	tt.aggregateCosts(events)

	if !tt.Task.GpuCostUSD.IsPositive() {
		t.Fatalf("Task.GpuCostUSD should be back-filled positive; got %s", tt.Task.GpuCostUSD)
	}

	// TotalCostUSD must reflect GpuCostUSD too.
	if tt.Task.TotalCostUSD.LessThan(tt.Task.GpuCostUSD) {
		t.Errorf("TotalCostUSD %s should include GpuCostUSD %s", tt.Task.TotalCostUSD, tt.Task.GpuCostUSD)
	}
}

func TestFinalizeGPUNoAccountantDoesNothing(t *testing.T) {
	cloud.ResetForTests()
	ResetGpuAccountantRegistryForTests()
	t.Cleanup(ResetGpuAccountantRegistryForTests)

	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}
	_, tt := tr.StartTask(context.Background(), "no_gpu")
	tt.Task.Status = TaskStatusSuccess
	endedAt := time.Now().UTC()
	tt.Task.EndedAt = &endedAt
	events, _ := buf.QueryEvents(tt.Task.TaskID.String())
	tt.aggregateCosts(events)
	if !tt.Task.GpuCostUSD.IsZero() {
		t.Fatalf("no accountant → GpuCostUSD should be zero; got %s", tt.Task.GpuCostUSD)
	}
}

// Signal events MUST stay at cost_usd=0 and MUST NOT contribute to totals.
func TestFinalizeGPUSignalEventsNeverAggregatedIntoTotal(t *testing.T) {
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "modal"})
	t.Cleanup(cloud.ResetForTests)
	ResetGpuAccountantRegistryForTests()
	t.Cleanup(ResetGpuAccountantRegistryForTests)
	withGpuMockOneDevice(t)

	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}
	_, tt := tr.StartTask(context.Background(), "obs_test")

	// Insert just a signal event (no gpu_cost) — should not contribute to totals.
	evSig := NewEvent(tt.Task.TaskID, EventTypeGPUUtilizationSignal)
	evSig.CostUSD = decimal.Zero
	evSig.Details = map[string]any{
		"gpu_index":   0,
		"sm_util_pct": 50.0,
	}
	_ = buf.InsertEvent(evSig)

	tt.Task.Status = TaskStatusSuccess
	endedAt := time.Now().UTC()
	tt.Task.EndedAt = &endedAt
	events, _ := buf.QueryEvents(tt.Task.TaskID.String())
	tt.aggregateCosts(events)
	if !tt.Task.GpuCostUSD.IsZero() {
		t.Fatalf("signal-only events should NOT contribute to GpuCostUSD; got %s", tt.Task.GpuCostUSD)
	}
	if !tt.Task.TotalCostUSD.IsZero() {
		t.Fatalf("signal-only events should NOT contribute to TotalCostUSD; got %s", tt.Task.TotalCostUSD)
	}
}
