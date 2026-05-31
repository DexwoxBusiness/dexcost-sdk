// End-to-end: long-running EC2 task auto-emits a compute_cost event with
// cost_pending=true at task finalize, then the pricing engine back-fills
// it. Pins the v1+v2 deferred-cost contract for the compute layer
// (analog of the network v2 §6.4 pattern).
//
// Mirrors python/tests/test_compute_auto_emission_long_running.py.

package core

import (
	"context"
	"testing"
	"time"

	"github.com/shopspring/decimal"

	"github.com/DexwoxBusiness/dexcost-sdk/go/cloud"
)

func TestEC2TaskEmitsAndPrices(t *testing.T) {
	cloud.SetResultForTests(cloud.CloudEnv{
		Provider: "aws", Region: "us-east-1", Source: "imds",
		InstanceType: "c7g.xlarge",
	})
	t.Cleanup(cloud.ResetForTests)
	ResetComputeRegistryForTests()
	t.Cleanup(ResetComputeRegistryForTests)

	// Snapshot-start reads cpu.stat once (returns 0); snapshot-end at
	// finalize returns usage_usec=1_000_000 = 1 vCPU-second used.
	endCalled := false
	restore := SetCgroupReadersForTests(
		func() (CPUStat, bool) {
			if !endCalled {
				return CPUStat{UsageUsec: 0}, true
			}
			return CPUStat{UsageUsec: 1_000_000}, true
		},
		func() (CPUMax, bool) {
			return CPUMax{QuotaUS: 400000, PeriodUS: 100000, VCPUCount: 4.0}, true
		},
		func() (int64, bool) { return int64(512) * 1024 * 1024, true },
		func() (int64, bool) { return int64(8) * 1024 * 1024 * 1024, true },
		func() (int64, bool) { return 0, false },
	)
	defer restore()

	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}

	ctx, tt := tr.StartTask(context.Background(), "x")
	_ = ctx

	accountant := NewComputeAccountant(
		RuntimeEC2, WithRegion("us-east-1"), WithArchitecture("x86_64"),
	)
	accountant.SnapshotStart()
	RegisterComputeAccountant(tt.Task.TaskID.String(), accountant)

	// Force snapshot-end to read 1_000_000 usec.
	endCalled = true

	// Simulate a 60s task.
	tt.Task.StartedAt = time.Now().UTC().Add(-60 * time.Second)
	endedAt := tt.Task.StartedAt.Add(60 * time.Second)
	tt.Task.EndedAt = &endedAt
	tt.Task.Status = TaskStatusSuccess

	// Run aggregateCosts directly (mirrors python tracker._aggregate_costs).
	events, _ := buf.QueryEvents(tt.Task.TaskID.String())
	tt.aggregateCosts(events)

	// Verify one compute_cost event was emitted + back-filled.
	events2, _ := buf.QueryEvents(tt.Task.TaskID.String())
	var computeEvents []Event
	for _, e := range events2 {
		if e.EventType == EventTypeComputeCost {
			computeEvents = append(computeEvents, e)
		}
	}
	if len(computeEvents) != 1 {
		t.Fatalf("compute_cost event count = %d, want 1", len(computeEvents))
	}
	ev := computeEvents[0]
	if !ev.CostUSD.GreaterThan(decimal.Zero) {
		t.Fatalf("CostUSD = %s, want > 0", ev.CostUSD)
	}
	if ev.CostConfidence != CostConfidenceComputed {
		t.Fatalf("CostConfidence = %s, want computed", ev.CostConfidence)
	}
	wantPrefix := "compute_catalog:aws:ec2:"
	gotSrc := string(ev.PricingSource)
	if len(gotSrc) < len(wantPrefix) || gotSrc[:len(wantPrefix)] != wantPrefix {
		t.Fatalf("PricingSource = %q, want prefix %q", gotSrc, wantPrefix)
	}
	if _, pending := ev.Details["cost_pending"]; pending {
		t.Fatal("cost_pending should be stripped after back-fill")
	}
	if !tt.Task.ComputeCostUSD.Equal(ev.CostUSD) {
		t.Fatalf("Task.ComputeCostUSD = %s, want %s", tt.Task.ComputeCostUSD, ev.CostUSD)
	}
}

func TestUnknownRuntimeEmitsNoEvent(t *testing.T) {
	cloud.SetResultForTests(cloud.CloudEnv{Source: "none"})
	t.Cleanup(cloud.ResetForTests)
	ResetComputeRegistryForTests()

	buf := newMockBuffer()
	tr, err := NewTracker(TrackerOptions{Buffer: buf})
	if err != nil {
		t.Fatalf("NewTracker: %v", err)
	}
	_, tt := tr.StartTask(context.Background(), "x")
	endedAt := tt.Task.StartedAt.Add(10 * time.Second)
	tt.Task.EndedAt = &endedAt
	tt.Task.Status = TaskStatusSuccess

	// NO accountant assigned → no event emitted.
	events, _ := buf.QueryEvents(tt.Task.TaskID.String())
	tt.aggregateCosts(events)

	events2, _ := buf.QueryEvents(tt.Task.TaskID.String())
	for _, e := range events2 {
		if e.EventType == EventTypeComputeCost {
			t.Fatalf("expected no compute_cost events; got %+v", e)
		}
	}
}
