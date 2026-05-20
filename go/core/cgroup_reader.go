// Cgroup v2 file readers.
//
// Fail-silent contract (convention §9): every read returns (zero, false) on
// missing or malformed input. Non-Linux hosts, cgroup-v1 kernels, and
// containers without a cgroup mount all silently return (zero, false) —
// the caller decides the fallback.
//
// Backed file layouts (all under /sys/fs/cgroup/):
//
//   - cpu.stat        — multi-line; "usage_usec <N>" is the cumulative CPU
//                       time consumed (microseconds). Read at task start +
//                       end to compute vcpu_seconds_used for long-running
//                       runtimes.
//   - cpu.max         — single line "<quota|"max"> <period>" (both in
//                       microseconds). quota/period is the vCPU count
//                       enforced; "max" means no limit (fall back to
//                       runtime.NumCPU()).
//   - memory.peak     — single integer (bytes); the high-water mark since
//                       cgroup creation. Kernel >= 5.19; absent otherwise.
//   - memory.max      — single integer (bytes) or "max" (unlimited).
//   - memory.current  — single integer (bytes); the current RSS.
//
// Mirrors python/src/dexcost/cgroup_reader.py.

package core

import (
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

// cgroupRoot is the cgroup v2 mount point. Package-level + mutable so tests
// can redirect to t.TempDir().
var cgroupRoot = "/sys/fs/cgroup"

// CPUStat is the cumulative CPU usage at the moment of read.
type CPUStat struct {
	UsageUsec int64
}

// CPUMax is the CPU quota / period as enforced by the cgroup. QuotaUS == 0
// is the unlimited sentinel (the literal "max"); VCPUCount then falls back
// to runtime.NumCPU().
type CPUMax struct {
	QuotaUS   int64
	PeriodUS  int64
	VCPUCount float64
}

// readInt reads a single-integer cgroup file. Returns (0, false) on missing
// file, the literal "max", or any parse error.
func readInt(name string) (int64, bool) {
	raw, err := os.ReadFile(filepath.Join(cgroupRoot, name))
	if err != nil {
		return 0, false
	}
	s := strings.TrimSpace(string(raw))
	if s == "max" {
		return 0, false
	}
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

// Indirection layer — tests swap these to inject deterministic values.
// The exported entry points dispatch through these function vars so the
// accountant can be tested without seeding tmp cgroup files.
var (
	readCPUStatFn       = readCPUStatImpl
	readCPUMaxFn        = readCPUMaxImpl
	readMemoryPeakFn    = readMemoryPeakImpl
	readMemoryMaxFn     = readMemoryMaxImpl
	readMemoryCurrentFn = readMemoryCurrentImpl
)

// SetCgroupReadersForTests replaces the cgroup readers for the duration of
// a test. Pass nil for a reader to leave it unchanged. Returns a cleanup
// function that restores the originals.
func SetCgroupReadersForTests(
	stat func() (CPUStat, bool),
	maxFn func() (CPUMax, bool),
	peak func() (int64, bool),
	max func() (int64, bool),
	cur func() (int64, bool),
) func() {
	oldStat, oldMax, oldPeak, oldMaxF, oldCur := readCPUStatFn, readCPUMaxFn, readMemoryPeakFn, readMemoryMaxFn, readMemoryCurrentFn
	if stat != nil {
		readCPUStatFn = stat
	}
	if maxFn != nil {
		readCPUMaxFn = maxFn
	}
	if peak != nil {
		readMemoryPeakFn = peak
	}
	if max != nil {
		readMemoryMaxFn = max
	}
	if cur != nil {
		readMemoryCurrentFn = cur
	}
	return func() {
		readCPUStatFn, readCPUMaxFn, readMemoryPeakFn, readMemoryMaxFn, readMemoryCurrentFn = oldStat, oldMax, oldPeak, oldMaxF, oldCur
	}
}

// ReadCPUStat parses cpu.stat looking for "usage_usec <N>".
func ReadCPUStat() (CPUStat, bool) { return readCPUStatFn() }

func readCPUStatImpl() (CPUStat, bool) {
	raw, err := os.ReadFile(filepath.Join(cgroupRoot, "cpu.stat"))
	if err != nil {
		return CPUStat{}, false
	}
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.HasPrefix(line, "usage_usec ") {
			parts := strings.Fields(line)
			if len(parts) < 2 {
				return CPUStat{}, false
			}
			v, err := strconv.ParseInt(parts[1], 10, 64)
			if err != nil {
				return CPUStat{}, false
			}
			return CPUStat{UsageUsec: v}, true
		}
	}
	return CPUStat{}, false
}

// ReadCPUMax parses cpu.max — "<quota|max> <period>" in microseconds.
// When the quota is the literal "max", QuotaUS is 0 and VCPUCount falls
// back to runtime.NumCPU().
func ReadCPUMax() (CPUMax, bool) { return readCPUMaxFn() }

func readCPUMaxImpl() (CPUMax, bool) {
	raw, err := os.ReadFile(filepath.Join(cgroupRoot, "cpu.max"))
	if err != nil {
		return CPUMax{}, false
	}
	s := strings.TrimSpace(string(raw))
	parts := strings.Fields(s)
	if len(parts) != 2 {
		return CPUMax{}, false
	}
	periodUS, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil || periodUS <= 0 {
		return CPUMax{}, false
	}
	if parts[0] == "max" {
		nproc := runtime.NumCPU()
		if nproc < 1 {
			nproc = 1
		}
		return CPUMax{QuotaUS: 0, PeriodUS: periodUS, VCPUCount: float64(nproc)}, true
	}
	quotaUS, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return CPUMax{}, false
	}
	return CPUMax{
		QuotaUS:   quotaUS,
		PeriodUS:  periodUS,
		VCPUCount: float64(quotaUS) / float64(periodUS),
	}, true
}

// ReadMemoryPeak returns memory.peak — bytes; kernel >= 5.19. (0, false)
// if file is absent.
func ReadMemoryPeak() (int64, bool) { return readMemoryPeakFn() }

func readMemoryPeakImpl() (int64, bool) { return readInt("memory.peak") }

// ReadMemoryMax returns memory.max — bytes. (0, false) if "max" (unlimited)
// or absent.
func ReadMemoryMax() (int64, bool) { return readMemoryMaxFn() }

func readMemoryMaxImpl() (int64, bool) { return readInt("memory.max") }

// ReadMemoryCurrent returns memory.current — bytes at moment of read.
func ReadMemoryCurrent() (int64, bool) { return readMemoryCurrentFn() }

func readMemoryCurrentImpl() (int64, bool) { return readInt("memory.current") }
