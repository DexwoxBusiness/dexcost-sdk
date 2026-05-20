// ComputeAccountant — start/end cgroup snapshots, single event per task,
// fail-silent. Capture §5.3: at most one compute_cost event per task per
// runtime. Mirrors python/tests/test_compute_accountant.py.

package core

import (
	"testing"
)

func TestLongRunningRuntimeEmitsOneEventWithDiff(t *testing.T) {
	// EC2 task: start at usage_usec=1M, end at 4M → 3 vcpu-seconds.
	calls := 0
	restore := SetCgroupReadersForTests(
		func() (CPUStat, bool) {
			calls++
			if calls == 1 {
				return CPUStat{UsageUsec: 1_000_000}, true
			}
			return CPUStat{UsageUsec: 4_000_000}, true
		},
		func() (CPUMax, bool) {
			return CPUMax{QuotaUS: 100000, PeriodUS: 100000, VCPUCount: 1.0}, true
		},
		func() (int64, bool) { return int64(512) * 1024 * 1024, true },
		func() (int64, bool) { return int64(1024) * 1024 * 1024, true },
		func() (int64, bool) { return 0, false },
	)
	defer restore()

	a := NewComputeAccountant(RuntimeEC2)
	a.SnapshotStart()
	details := a.SnapshotEndAndBuild(60_000)
	if details == nil {
		t.Fatal("expected details, got nil")
	}
	if details["billing_model"] != "ec2" {
		t.Fatalf("billing_model = %v", details["billing_model"])
	}
	vsec, _ := details["vcpu_seconds_used"].(float64)
	if vsec < 2.999 || vsec > 3.001 {
		t.Fatalf("vcpu_seconds_used = %v, want ~3.0", vsec)
	}
	if details["memory_bytes_peak"].(int64) != int64(512)*1024*1024 {
		t.Fatalf("memory_bytes_peak = %v", details["memory_bytes_peak"])
	}
	if details["memory_bytes_limit"].(int64) != int64(1024)*1024*1024 {
		t.Fatalf("memory_bytes_limit = %v", details["memory_bytes_limit"])
	}
	if details["vcpu_count"].(float64) != 1.0 {
		t.Fatalf("vcpu_count = %v", details["vcpu_count"])
	}
	if details["cost_pending"] != true {
		t.Fatalf("cost_pending = %v, want true", details["cost_pending"])
	}
}

func TestServerlessLambdaEmitsInvocationEvent(t *testing.T) {
	a := NewComputeAccountant(
		RuntimeLambda,
		WithLambdaMemoryMB(512),
		WithArchitecture("x86_64"),
		WithInitializationType("on-demand"),
		WithRegion("us-east-1"),
	)
	details := a.BuildServerlessEvent(200, int64(400)*1024*1024)
	if details == nil {
		t.Fatal("expected details, got nil")
	}
	if details["billing_model"] != "lambda" {
		t.Fatalf("billing_model = %v", details["billing_model"])
	}
	if details["duration_ms"].(int64) != 200 {
		t.Fatalf("duration_ms = %v", details["duration_ms"])
	}
	if details["invocation_count"].(int) != 1 {
		t.Fatalf("invocation_count = %v", details["invocation_count"])
	}
	// Lambda env var is DECIMAL MB (10^6 bytes).
	if details["memory_bytes_limit"].(int64) != int64(512)*1_000_000 {
		t.Fatalf("memory_bytes_limit = %v, want %d (decimal MB)",
			details["memory_bytes_limit"], int64(512)*1_000_000)
	}
	if details["architecture"] != "x86_64" {
		t.Fatalf("architecture = %v", details["architecture"])
	}
	if details["initialization_type"] != "on-demand" {
		t.Fatalf("initialization_type = %v", details["initialization_type"])
	}
	if details["region"] != "us-east-1" {
		t.Fatalf("region = %v", details["region"])
	}
	if details["cost_pending"] != true {
		t.Fatalf("cost_pending = %v", details["cost_pending"])
	}
}

func TestSecondCallPerTaskNoOps(t *testing.T) {
	// Capture §5.3 — at most one event per task per runtime.
	a := NewComputeAccountant(RuntimeLambda, WithLambdaMemoryMB(128), WithArchitecture("x86_64"))
	first := a.BuildServerlessEvent(10, 0)
	second := a.BuildServerlessEvent(20, 0)
	if first == nil {
		t.Fatal("first call: expected non-nil")
	}
	if second != nil {
		t.Fatal("second call: expected nil (freeze)")
	}
}

func TestFargatePassesExplicitVCPUAndMemory(t *testing.T) {
	a := NewComputeAccountant(
		RuntimeFargate,
		WithFargateVCPU(0.5),
		WithFargateMemoryMiB(1024),
		WithArchitecture("arm64"),
		WithRegion("us-east-1"),
	)
	details := a.BuildServerlessEvent(60_000, int64(600)*1024*1024)
	if details == nil {
		t.Fatal("expected non-nil")
	}
	if details["billing_model"] != "fargate" {
		t.Fatalf("billing_model = %v", details["billing_model"])
	}
	if details["vcpu_count"].(float64) != 0.5 {
		t.Fatalf("vcpu_count = %v", details["vcpu_count"])
	}
	if details["memory_bytes_limit"].(int64) != int64(1024)*1024*1024 {
		t.Fatalf("memory_bytes_limit = %v", details["memory_bytes_limit"])
	}
	if details["architecture"] != "arm64" {
		t.Fatalf("architecture = %v", details["architecture"])
	}
}

func TestNonLinuxFallbackEmitsWithZeroVCPUSeconds(t *testing.T) {
	restore := SetCgroupReadersForTests(
		func() (CPUStat, bool) { return CPUStat{}, false },
		func() (CPUMax, bool) { return CPUMax{}, false },
		func() (int64, bool) { return 0, false },
		func() (int64, bool) { return 0, false },
		func() (int64, bool) { return 0, false },
	)
	defer restore()

	a := NewComputeAccountant(RuntimeEC2)
	a.SnapshotStart()
	details := a.SnapshotEndAndBuild(60_000)
	if details == nil {
		t.Fatal("expected non-nil")
	}
	if v, _ := details["vcpu_seconds_used"].(float64); v != 0 {
		t.Fatalf("vcpu_seconds_used = %v, want 0", v)
	}
	if details["vcpu_count"].(float64) <= 0 {
		t.Fatalf("vcpu_count = %v, want > 0 (nproc fallback)", details["vcpu_count"])
	}
}

func TestMemoryPeakFallsBackToCurrentWhenMissing(t *testing.T) {
	restore := SetCgroupReadersForTests(
		func() (CPUStat, bool) { return CPUStat{UsageUsec: 0}, true },
		func() (CPUMax, bool) {
			return CPUMax{QuotaUS: 100000, PeriodUS: 100000, VCPUCount: 1.0}, true
		},
		func() (int64, bool) { return 0, false }, // peak absent
		func() (int64, bool) { return int64(1024) * 1024 * 1024, true },
		func() (int64, bool) { return int64(256) * 1024 * 1024, true }, // current
	)
	defer restore()

	a := NewComputeAccountant(RuntimeEC2)
	a.SnapshotStart()
	details := a.SnapshotEndAndBuild(60_000)
	if details["memory_bytes_peak"].(int64) != int64(256)*1024*1024 {
		t.Fatalf("memory_bytes_peak = %v, want fallback to current (256 MiB)",
			details["memory_bytes_peak"])
	}
}

func TestArchitectureAutoDetectedFromRuntimeGOARCH(t *testing.T) {
	a := NewComputeAccountant(RuntimeLambda, WithLambdaMemoryMB(128))
	if a.Architecture != "x86_64" && a.Architecture != "arm64" {
		t.Fatalf("Architecture = %q, want x86_64 or arm64", a.Architecture)
	}
}

func TestLongRunningSnapshotFreezeAfterFinalize(t *testing.T) {
	restore := SetCgroupReadersForTests(
		func() (CPUStat, bool) { return CPUStat{UsageUsec: 100}, true },
		func() (CPUMax, bool) {
			return CPUMax{QuotaUS: 100000, PeriodUS: 100000, VCPUCount: 1.0}, true
		},
		func() (int64, bool) { return 0, true },
		func() (int64, bool) { return 0, true },
		func() (int64, bool) { return 0, false },
	)
	defer restore()

	a := NewComputeAccountant(RuntimeEC2)
	a.SnapshotStart()
	first := a.SnapshotEndAndBuild(1000)
	second := a.SnapshotEndAndBuild(2000)
	if first == nil {
		t.Fatal("first: expected non-nil")
	}
	if second != nil {
		t.Fatal("second: expected nil (freeze)")
	}
}
