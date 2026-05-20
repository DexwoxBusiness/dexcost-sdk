// cgroup v2 file readers — tests mirror python/tests/test_cgroup_reader.py.
//
// Fail-silent contract: every reader returns (zero, false) on missing or
// malformed input. Tests swap the package-level cgroupRoot via t.TempDir.

package core

import (
	"os"
	"path/filepath"
	"testing"
)

// withCgroupRoot points cgroupRoot at tmp and restores it on cleanup.
func withCgroupRoot(t *testing.T, tmp string) {
	t.Helper()
	old := cgroupRoot
	cgroupRoot = tmp
	t.Cleanup(func() { cgroupRoot = old })
}

func seedFile(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
		t.Fatalf("seed %s: %v", name, err)
	}
}

func TestReadCPUStatParsesUsageUsec(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "cpu.stat",
		"usage_usec 12345\nuser_usec 6000\nsystem_usec 6345\n"+
			"nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n")
	withCgroupRoot(t, tmp)

	s, ok := ReadCPUStat()
	if !ok {
		t.Fatal("ReadCPUStat returned ok=false")
	}
	if s.UsageUsec != 12345 {
		t.Fatalf("UsageUsec = %d, want 12345", s.UsageUsec)
	}
}

func TestReadCPUMaxWithQuota(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "cpu.max", "100000 100000\n")
	withCgroupRoot(t, tmp)

	m, ok := ReadCPUMax()
	if !ok {
		t.Fatal("ReadCPUMax returned ok=false")
	}
	if m.QuotaUS != 100000 || m.PeriodUS != 100000 || m.VCPUCount != 1.0 {
		t.Fatalf("got %+v, want {Quota:100000 Period:100000 VCPUCount:1.0}", m)
	}
}

func TestReadCPUMaxQuotaFraction(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "cpu.max", "25000 100000\n")
	withCgroupRoot(t, tmp)

	m, ok := ReadCPUMax()
	if !ok {
		t.Fatal("ReadCPUMax returned ok=false")
	}
	if m.VCPUCount != 0.25 {
		t.Fatalf("VCPUCount = %v, want 0.25", m.VCPUCount)
	}
}

func TestReadCPUMaxUnlimited(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "cpu.max", "max 100000\n")
	withCgroupRoot(t, tmp)

	m, ok := ReadCPUMax()
	if !ok {
		t.Fatal("ReadCPUMax returned ok=false")
	}
	if m.QuotaUS != 0 {
		t.Fatalf("QuotaUS = %d, want 0 (unlimited sentinel)", m.QuotaUS)
	}
	if m.VCPUCount <= 0 {
		t.Fatalf("VCPUCount = %v, want > 0 (nproc fallback)", m.VCPUCount)
	}
}

func TestReadMemoryPeak(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "memory.peak", "2147483648\n")
	withCgroupRoot(t, tmp)

	v, ok := ReadMemoryPeak()
	if !ok || v != 2147483648 {
		t.Fatalf("got (%d, %v)", v, ok)
	}
}

func TestReadMemoryMaxFinite(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "memory.max", "1073741824\n")
	withCgroupRoot(t, tmp)

	v, ok := ReadMemoryMax()
	if !ok || v != 1073741824 {
		t.Fatalf("got (%d, %v)", v, ok)
	}
}

func TestReadMemoryMaxUnlimited(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "memory.max", "max\n")
	withCgroupRoot(t, tmp)

	if _, ok := ReadMemoryMax(); ok {
		t.Fatal("expected ok=false for 'max'")
	}
}

func TestReadMemoryCurrent(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "memory.current", "1024\n")
	withCgroupRoot(t, tmp)

	v, ok := ReadMemoryCurrent()
	if !ok || v != 1024 {
		t.Fatalf("got (%d, %v)", v, ok)
	}
}

func TestMissingFilesReturnFalse(t *testing.T) {
	tmp := t.TempDir()
	withCgroupRoot(t, tmp)

	if _, ok := ReadCPUStat(); ok {
		t.Fatal("ReadCPUStat: expected ok=false")
	}
	if _, ok := ReadCPUMax(); ok {
		t.Fatal("ReadCPUMax: expected ok=false")
	}
	if _, ok := ReadMemoryPeak(); ok {
		t.Fatal("ReadMemoryPeak: expected ok=false")
	}
	if _, ok := ReadMemoryMax(); ok {
		t.Fatal("ReadMemoryMax: expected ok=false")
	}
	if _, ok := ReadMemoryCurrent(); ok {
		t.Fatal("ReadMemoryCurrent: expected ok=false")
	}
}

func TestMalformedCPUStatReturnsFalse(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "cpu.stat", "garbage\n")
	withCgroupRoot(t, tmp)

	if _, ok := ReadCPUStat(); ok {
		t.Fatal("expected ok=false on malformed cpu.stat")
	}
}

func TestMalformedCPUMaxReturnsFalse(t *testing.T) {
	tmp := t.TempDir()
	seedFile(t, tmp, "cpu.max", "only-one-token\n")
	withCgroupRoot(t, tmp)

	if _, ok := ReadCPUMax(); ok {
		t.Fatal("expected ok=false on malformed cpu.max")
	}
}

func TestMemoryPeakAbsentWhenKernelTooOld(t *testing.T) {
	// Kernel < 5.19 — memory.peak missing; memory.current present.
	// Reader does NOT fabricate; caller decides fallback (capture spec §6 case 6).
	tmp := t.TempDir()
	seedFile(t, tmp, "memory.current", "1024\n")
	withCgroupRoot(t, tmp)

	if _, ok := ReadMemoryPeak(); ok {
		t.Fatal("expected ok=false when memory.peak file absent")
	}
	v, ok := ReadMemoryCurrent()
	if !ok || v != 1024 {
		t.Fatalf("memory.current got (%d, %v)", v, ok)
	}
}
