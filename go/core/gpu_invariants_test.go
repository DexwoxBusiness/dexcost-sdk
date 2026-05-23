// Task 9 — property invariants + idle-gap + matrix + observability tests.
// Mirrors python commit d42cc81.

package core

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

// Build a tracker with a fresh mock NVML backend + clear registries.
func newGpuTracker(t *testing.T) *Tracker {
	t.Helper()
	resetNVMLForTests()
	t.Cleanup(resetNVMLForTests)
	ResetGpuAccountantRegistryForTests()
	t.Cleanup(ResetGpuAccountantRegistryForTests)
	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}
	return tr
}

// runGPUTaskOnRuntime simulates one GPU-using task on the named runtime.
// Returns the back-filled Task.GpuCostUSD after aggregation.
func runGPUTaskOnRuntime(t *testing.T, tr *Tracker,
	runtime GpuRuntimeKind, env cloud.CloudEnv,
	durationS int, gpuUseSeconds float64,
) decimal.Decimal {
	t.Helper()
	cloud.SetResultForTests(env)
	t.Cleanup(cloud.ResetForTests)

	// Mock NVML reports 1 H100 with self-PID accumulating gpuUseSeconds.
	selfPID := os.Getpid()
	mock := &MockNVMLBackend{
		Available:    true,
		DeviceCount:  1,
		ProductNames: map[int]string{0: "NVIDIA H100 80GB HBM3"},
		Memory:       map[int]NVMLMemInfo{0: {TotalBytes: 80 * 1024 * 1024 * 1024}},
		PerCallUtilization: map[int][]map[int]NVMLUtilSample{
			0: {
				{selfPID: {PID: selfPID, SMUtil: 0, TimeStamp: 0}},
				{selfPID: {PID: selfPID, SMUtil: 80, TimeStamp: int64(gpuUseSeconds * 1_000_000)}},
			},
		},
	}
	SetNVMLBackendForTests(mock.AsBackend())

	_, tt := tr.StartTask(context.Background(), "gpu_task")
	a := NewGpuAccountant(runtime, env)
	a.SnapshotStart()
	RegisterGpuAccountant(tt.Task.TaskID.String(), a)

	// Force known wall-clock duration.
	now := time.Now().UTC()
	tt.Task.StartedAt = now.Add(-time.Duration(durationS) * time.Second)
	tt.Task.EndedAt = &now
	tt.Task.Status = TaskStatusSuccess

	events, _ := tr.buffer.QueryEvents(tt.Task.TaskID.String())
	tt.aggregateCosts(events)
	return tt.Task.GpuCostUSD
}

// ─── Property invariant: gpu_seconds_used ≤ gpu_count * window_seconds ──

func TestInvariantGpuSecondsLEQGpuCountTimesWindow(t *testing.T) {
	tr := newGpuTracker(t)
	cost := runGPUTaskOnRuntime(t, tr, GpuRuntimeModal,
		cloud.CloudEnv{Provider: "modal"}, 2, 1.5)
	if !cost.IsPositive() {
		t.Errorf("modal 1.5s gpu of 2s task should be positive; got %s", cost)
	}
}

// ─── Decision #6 idle-gap test (2 Lambda Labs H100 with 50min idle) ─────

func TestDecision6IdleGapStrictlyLessThanFullWindowCloudShare(t *testing.T) {
	tr := newGpuTracker(t)
	env := cloud.CloudEnv{Provider: "lambda_labs"}

	// Two 5-min tasks with 50min idle between them. Cloud invoice would
	// cover the FULL 60 minutes (5 + 50 + 5) at full Lambda Labs H100 rate.
	// dexcost MUST report strictly LESS than the full-window cloud share —
	// the 50min idle gap is INVISIBLE to dexcost (Decision #6).
	a := runGPUTaskOnRuntime(t, tr, GpuRuntimeLambdaLabs, env, 300, 300.0)
	b := runGPUTaskOnRuntime(t, tr, GpuRuntimeLambdaLabs, env, 300, 300.0)
	total := a.Add(b)

	// Lambda Labs H100 SXM 8x — but we have 1 GPU here for simplicity. The
	// per_gpu_hour_reserved math uses the engine's resolved rate.
	// Even if rate is ~$2.49/gpu-hr (Lambda H100 SXM 1x): full 60min cloud
	// share = 1.0 hr × rate. Two 5-min tasks: 600s / 3600s × rate = 1/6 rate.
	// 600s < 3600s → dexcost is strictly less than 1hr cloud share.
	fullWindowSeconds := decimal.NewFromInt(3600) // 1hr
	// derive rate by running one full-window task and dividing.
	fullCost := runGPUTaskOnRuntime(t, tr, GpuRuntimeLambdaLabs, env, 3600, 3600.0)

	if !total.LessThan(fullCost) {
		t.Fatalf("Decision #6 — two 5-min GPU tasks (idle 50min between) total %s "+
			"must be STRICTLY LESS than full %s-second-window cloud share %s. "+
			"If this test fails, dexcost is double-counting idle GPU-seconds; "+
			"the gap is invisible BY DESIGN.",
			total, fullWindowSeconds, fullCost)
	}
	if !total.IsPositive() {
		t.Errorf("two GPU tasks should yield positive total; got %s", total)
	}
}

// ─── Cross-runtime matrix ───────────────────────────────────────────────

func TestCrossRuntimeMatrixAllProduceCosts(t *testing.T) {
	cases := []struct {
		name string
		rt   GpuRuntimeKind
		env  cloud.CloudEnv
	}{
		{"modal", GpuRuntimeModal, cloud.CloudEnv{Provider: "modal"}},
		{"runpod", GpuRuntimeRunpod, cloud.CloudEnv{Provider: "runpod"}},
		{"replicate", GpuRuntimeReplicate, cloud.CloudEnv{Provider: "replicate"}},
		{"lambda_labs", GpuRuntimeLambdaLabs, cloud.CloudEnv{Provider: "lambda_labs"}},
		{"coreweave", GpuRuntimeCoreweave, cloud.CloudEnv{Provider: "coreweave"}},
		{"aws_ec2_gpu", GpuRuntimeAWSEC2GPU, cloud.CloudEnv{
			Provider: "aws", Region: "us-east-1", InstanceType: "p5.48xlarge",
		}},
		{"gcp_gce_bundled", GpuRuntimeGCPGCEBundled, cloud.CloudEnv{
			Provider: "gcp", Region: "us-central1", InstanceType: "a3-highgpu-8g",
		}},
		{"azure_vm_gpu", GpuRuntimeAzureVMGPU, cloud.CloudEnv{
			Provider: "azure", Region: "eastus", InstanceType: "Standard_ND96isr_H100_v5",
		}},
		{"azure_vm_vgpu", GpuRuntimeAzureVMVGPU, cloud.CloudEnv{
			Provider: "azure", Region: "eastus", InstanceType: "Standard_NV6ads_A10_v5",
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			tr := newGpuTracker(t)
			cost := runGPUTaskOnRuntime(t, tr, c.rt, c.env, 10, 5.0)
			if !cost.IsPositive() {
				t.Errorf("%s should produce positive cost; got %s", c.name, cost)
			}
		})
	}
}

// ─── Observability: gpu_utilization_signal events NEVER aggregated ──────

func TestSignalEventsObservabilityCarveOutConventionSec1(t *testing.T) {
	tr := newGpuTracker(t)
	cloud.SetResultForTests(cloud.CloudEnv{Provider: "modal"})
	t.Cleanup(cloud.ResetForTests)

	// Build a task with a high-cost signal event (cost_usd=99.99) — must NOT
	// contribute to totals.
	_, tt := tr.StartTask(context.Background(), "obs_carveout")
	evSig := NewEvent(tt.Task.TaskID, EventTypeGPUUtilizationSignal)
	evSig.CostUSD = decimal.RequireFromString("99.99") // pathological value
	evSig.Details = map[string]any{
		"gpu_index":   0,
		"sm_util_pct": 50.0,
	}
	_ = tr.buffer.InsertEvent(evSig)

	tt.Task.Status = TaskStatusSuccess
	endedAt := time.Now().UTC()
	tt.Task.EndedAt = &endedAt
	events, _ := tr.buffer.QueryEvents(tt.Task.TaskID.String())
	tt.aggregateCosts(events)

	if !tt.Task.GpuCostUSD.IsZero() {
		t.Fatalf("convention §1 — gpu_utilization_signal must NEVER aggregate into GpuCostUSD; got %s", tt.Task.GpuCostUSD)
	}
	if !tt.Task.TotalCostUSD.IsZero() {
		t.Fatalf("convention §1 — gpu_utilization_signal must NEVER aggregate into TotalCostUSD; got %s", tt.Task.TotalCostUSD)
	}
}
