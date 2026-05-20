// Decisions #9 + #10 — idle compute is invisible to dexcost. THE GAP IS THE DESIGN.
//
// These tests fail fast if a future refactor ever adds synthetic "idle
// pseudo-tasks" or otherwise pushes dexcost_compute_total toward the cloud
// invoice on long-running runtimes. The under-attribution is the customer-
// facing signal for "unaccounted capacity"; surfacing it as a feature
// is mandatory per the decisions log.
//
// Mirrors python/tests/test_compute_idle_gap.py.

package core

import (
	"context"
	"testing"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

// runIdleGapTask creates a task with controlled start/duration, registers
// a ComputeAccountant with mocked cgroup reads, and runs aggregateCosts.
// Returns the Task.ComputeCostUSD.
func runIdleGapTask(t *testing.T, tracker *Tracker,
	startOffsetS, durationS int, cpuUsedSeconds float64,
	runtime RuntimeKind,
) decimal.Decimal {
	t.Helper()
	endCalled := false
	restore := SetCgroupReadersForTests(
		func() (CPUStat, bool) {
			if !endCalled {
				return CPUStat{UsageUsec: 0}, true
			}
			return CPUStat{UsageUsec: int64(cpuUsedSeconds * 1_000_000)}, true
		},
		func() (CPUMax, bool) {
			return CPUMax{QuotaUS: 400000, PeriodUS: 100000, VCPUCount: 4.0}, true
		},
		func() (int64, bool) { return int64(512) * 1024 * 1024, true },
		func() (int64, bool) { return int64(8) * 1024 * 1024 * 1024, true },
		func() (int64, bool) { return 0, false },
	)
	defer restore()

	_, tt := tracker.StartTask(context.Background(), "x")
	accountant := NewComputeAccountant(runtime, WithRegion("us-east-1"), WithArchitecture("x86_64"))
	accountant.SnapshotStart()
	RegisterComputeAccountant(tt.Task.TaskID.String(), accountant)

	endCalled = true

	started := time.Now().UTC().Add(time.Duration(startOffsetS) * time.Second)
	tt.Task.StartedAt = started
	endedAt := started.Add(time.Duration(durationS) * time.Second)
	tt.Task.EndedAt = &endedAt
	tt.Task.Status = TaskStatusSuccess

	events, _ := tracker.buffer.QueryEvents(tt.Task.TaskID.String())
	tt.aggregateCosts(events)
	return tt.Task.ComputeCostUSD
}

func TestEC2IdleBetweenTasksIsInvisibleDecision9(t *testing.T) {
	// Two 60s tasks with 600s idle between them on a 4 vCPU @ $0.1450/hr
	// c7g.xlarge. The cloud bill for the FULL 720s window =
	//   720/3600 * 0.1450 = $0.029.
	// dexcost MUST report STRICTLY LESS — the 600s idle gap is excluded
	// by design (Decision #9).
	cloud.SetResultForTests(cloud.CloudEnv{
		Provider: "aws", Region: "us-east-1", Source: "imds",
		InstanceType: "c7g.xlarge",
	})
	t.Cleanup(cloud.ResetForTests)
	ResetComputeRegistryForTests()
	t.Cleanup(ResetComputeRegistryForTests)

	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}

	a := runIdleGapTask(t, tr, 0, 60, 10, RuntimeEC2)
	b := runIdleGapTask(t, tr, 660, 60, 10, RuntimeEC2)
	total := a.Add(b)

	fullWindowCloudShare := decimal.NewFromInt(720).Div(decimal.NewFromInt(3600)).
		Mul(decimal.RequireFromString("0.1450"))

	if !total.LessThan(fullWindowCloudShare) {
		t.Fatalf("dexcost total %s must be < cloud share %s on long-running runtimes — "+
			"the 600s idle gap is by design (Decision #9). If this test starts "+
			"failing because total grew, check whether a refactor added synthetic "+
			"idle pseudo-tasks.",
			total, fullWindowCloudShare)
	}
	if !total.GreaterThan(decimal.Zero) {
		t.Fatalf("dexcost total = %s, want > 0 (we DO bill the 120s of dexcost-covered time)", total)
	}
}

func TestFargateContainerIdleTailIsInvisibleDecision10(t *testing.T) {
	// 3 Fargate tasks back-to-back, then 50 minutes of container idle tail
	// before container shutdown. The tail is billable Fargate time NOT
	// attributed to any dexcost task — Decision #10.
	cloud.SetResultForTests(cloud.CloudEnv{
		Provider: "aws", Region: "us-east-1", Source: "imds",
	})
	t.Cleanup(cloud.ResetForTests)
	ResetComputeRegistryForTests()
	t.Cleanup(ResetComputeRegistryForTests)

	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}

	a := runIdleGapTask(t, tr, 0, 10, 2, RuntimeFargate)
	b := runIdleGapTask(t, tr, 10, 10, 2, RuntimeFargate)
	c := runIdleGapTask(t, tr, 20, 10, 2, RuntimeFargate)
	total := a.Add(b).Add(c)

	// Total container lifetime = 30s tasks + 3000s idle tail = 3030s.
	// Conservative upper bound: 4.0 vCPU * 3030s * x86 us-east-1 vcpu_second_usd.
	containerLifetimeS := decimal.NewFromInt(3030)
	fullWindowCloudShare := decimal.NewFromInt(4).Mul(containerLifetimeS).
		Mul(decimal.RequireFromString("0.0000111111"))

	if !total.LessThan(fullWindowCloudShare) {
		t.Fatalf("dexcost total %s must be < full-container-lifetime cost %s "+
			"(Decision #10). The 50-minute idle tail is invisible by design.",
			total, fullWindowCloudShare)
	}
	if !total.GreaterThan(decimal.Zero) {
		t.Fatalf("dexcost total = %s, want > 0", total)
	}
}
