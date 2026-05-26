// NVML library wrapper — Phase 2 GPU foundation.
//
// Mirrors python/src/dexcost/nvml_reader.py. The Go SDK avoids a hard
// dep on github.com/NVIDIA/go-nvml so the SDK builds + tests on GPU-less
// hosts; NVML access happens through a pluggable backend interface
// (NVMLBackend). The default backend is a noop that reports "no GPU".
//
// Consumers can register a real backend via SetNVMLBackend(); the GPU
// stack works on default-noop in unit tests with mocked devices.
//
// Decision #4: GetNVMLProductName applies NFC Unicode normalization +
// lowercase + whitespace collapse on the raw productName before returning
// — catalog alias matching depends on byte-level equality post-normalize
// because NVIDIA's productName ships with NBSP/NNBSP/ZWSP quirks across
// driver versions.
//
// Decision #8: GetNVMLProcessUtilization mutates a per-device
// lastSeenTimestamps map in place so callers persist NVML's sample
// buffer state across snapshot calls.
//
// Fail-silent contract (convention §9): every accessor returns nil (or
// false for booleans) on backend errors; the caller (typically
// GpuAccountant) decides the fallback policy.

package core

import (
	"strings"
	"sync"

	"golang.org/x/text/unicode/norm"
)

// NVMLProcessInfo is one compute-running-process record (PID + VRAM bytes).
type NVMLProcessInfo struct {
	PID           int
	UsedGPUMemory int64
}

// NVMLUtilSample is one process-utilization sample.
type NVMLUtilSample struct {
	PID       int
	SMUtil    int   // 0-100; percent of time SMs had ≥1 kernel running
	MemUtil   int   // 0-100; percent of time memory subsystem was busy
	TimeStamp int64 // microseconds since NVML epoch (monotonic)
}

// NVMLMemInfo is device-level memory totals.
type NVMLMemInfo struct {
	UsedBytes  int64
	TotalBytes int64
}

// NVMLBackend abstracts NVML so the SDK doesn't require pynvml/go-nvml at
// build time. The default backend returns "no GPU"; production deployments
// inject a real NVML-backed implementation; tests inject a deterministic
// mock.
type NVMLBackend interface {
	Available() bool
	Init() bool
	Shutdown()
	DeviceCount() (int, bool)
	ProductName(devIdx int) (string, bool)
	MIGMode(devIdx int) bool
	ComputeRunningProcesses(devIdx int) ([]NVMLProcessInfo, bool)
	// ProcessUtilization returns a list of utilization samples per PID
	// observed since `lastSeen[pid]`. Multi-sample-per-PID is required
	// for B2 integration math (Sprint 2 Theme C / §3.1.1, Go port);
	// pre-fix the API collapsed to a single sample and the accountant
	// could not integrate sm_util × dt across the task window.
	ProcessUtilization(devIdx int, lastSeen map[int]int64) (map[int][]NVMLUtilSample, bool)
	MemoryInfo(devIdx int) (NVMLMemInfo, bool)
}

// noopNVMLBackend is the default: reports "no GPU" for every call.
type noopNVMLBackend struct{}

func (noopNVMLBackend) Available() bool                { return false }
func (noopNVMLBackend) Init() bool                     { return false }
func (noopNVMLBackend) Shutdown()                      {}
func (noopNVMLBackend) DeviceCount() (int, bool)       { return 0, false }
func (noopNVMLBackend) ProductName(int) (string, bool) { return "", false }
func (noopNVMLBackend) MIGMode(int) bool               { return false }
func (noopNVMLBackend) ComputeRunningProcesses(int) ([]NVMLProcessInfo, bool) {
	return nil, false
}
func (noopNVMLBackend) ProcessUtilization(int, map[int]int64) (map[int][]NVMLUtilSample, bool) {
	return nil, false
}
func (noopNVMLBackend) MemoryInfo(int) (NVMLMemInfo, bool) { return NVMLMemInfo{}, false }

var (
	nvmlBackendMu sync.RWMutex
	nvmlBackend   NVMLBackend = noopNVMLBackend{}
)

// SetNVMLBackend swaps the global NVML backend. Used by consumers that
// vendor a real NVML library. nil reverts to the noop default.
func SetNVMLBackend(b NVMLBackend) {
	nvmlBackendMu.Lock()
	defer nvmlBackendMu.Unlock()
	if b == nil {
		nvmlBackend = noopNVMLBackend{}
		return
	}
	nvmlBackend = b
}

// SetNVMLBackendForTests is the test alias for SetNVMLBackend.
func SetNVMLBackendForTests(b NVMLBackend) { SetNVMLBackend(b) }

func getNVMLBackend() NVMLBackend {
	nvmlBackendMu.RLock()
	defer nvmlBackendMu.RUnlock()
	return nvmlBackend
}

// resetNVMLForTests reverts backend + warning state. Mirrors the
// _reset_warning_state helper from Python.
func resetNVMLForTests() {
	SetNVMLBackend(nil)
	nvmlWarnMu.Lock()
	nvmlWarned = map[string]struct{}{}
	nvmlWarnMu.Unlock()
}

// ResetNVMLForTests is the exported alias of resetNVMLForTests so other
// packages (adapters_test, etc.) can reset NVML state.
func ResetNVMLForTests() { resetNVMLForTests() }

// ─── log-once-per-failure-mode (convention §11) ─────────────────────────

var (
	nvmlWarnMu sync.Mutex
	nvmlWarned = map[string]struct{}{}
)

func nvmlWarnOnce(mode string) {
	nvmlWarnMu.Lock()
	defer nvmlWarnMu.Unlock()
	if _, seen := nvmlWarned[mode]; seen {
		return
	}
	nvmlWarned[mode] = struct{}{}
}

// ─── Decision #4 — NFC + lowercase + whitespace collapse ───────────────

// normalizeProductName applies NFC normalization, lowercases, and
// collapses any-and-all Unicode whitespace (including NBSP/NNBSP/ZWSP)
// into single ASCII spaces. Alias matching in the catalog depends on
// byte equality after this step.
func normalizeProductName(raw string) string {
	nfc := norm.NFC.String(raw)
	collapsed := strings.Join(strings.Fields(nfc), " ")
	return strings.ToLower(collapsed)
}

// ─── Public API ─────────────────────────────────────────────────────────

// NVMLAvailable reports whether NVML can be used at all.
func NVMLAvailable() bool {
	return getNVMLBackend().Available()
}

// InitNVML calls nvmlInit() if available. Returns true on success.
func InitNVML() bool {
	b := getNVMLBackend()
	if !b.Available() {
		nvmlWarnOnce("gpu_nvml_unavailable")
		return false
	}
	if !b.Init() {
		nvmlWarnOnce("gpu_nvml_init_failed")
		return false
	}
	return true
}

// ShutdownNVML best-effort shuts down NVML.
func ShutdownNVML() { getNVMLBackend().Shutdown() }

// GetNVMLDeviceCount returns the device count or nil on failure.
func GetNVMLDeviceCount() *int {
	b := getNVMLBackend()
	if !b.Available() {
		return nil
	}
	n, ok := b.DeviceCount()
	if !ok {
		nvmlWarnOnce("gpu_device_count_failed")
		return nil
	}
	return &n
}

// GetNVMLProductName returns the NFC-normalized, lowercased productName
// for device devIdx. nil on failure.
func GetNVMLProductName(devIdx int) *string {
	b := getNVMLBackend()
	if !b.Available() {
		return nil
	}
	raw, ok := b.ProductName(devIdx)
	if !ok {
		nvmlWarnOnce("gpu_product_name_failed")
		return nil
	}
	n := normalizeProductName(raw)
	return &n
}

// GetNVMLMIGMode reports whether MIG is currently enabled on device devIdx.
func GetNVMLMIGMode(devIdx int) bool {
	b := getNVMLBackend()
	if !b.Available() {
		return false
	}
	return b.MIGMode(devIdx)
}

// GetNVMLComputeRunningProcesses returns PIDs holding the GPU + VRAM usage.
// Returns nil on permission denied (load-bearing non-root-container case).
func GetNVMLComputeRunningProcesses(devIdx int) []NVMLProcessInfo {
	b := getNVMLBackend()
	if !b.Available() {
		return nil
	}
	out, ok := b.ComputeRunningProcesses(devIdx)
	if !ok {
		nvmlWarnOnce("gpu_nvml_permission_denied")
		return nil
	}
	return out
}

// GetNVMLProcessUtilization returns per-PID utilization samples for device
// devIdx and updates the lastSeen map in place (Decision #8 persistent
// state). Returns nil on failure.
//
// Returns map[pid][]NVMLUtilSample — multiple samples per PID, sorted
// by TimeStamp ascending. Sprint 2 Theme C / §3.1.1 (B2 Go port):
// pre-fix the wrapper collapsed samples to the latest per PID, losing
// the integration data the accountant now needs.
func GetNVMLProcessUtilization(devIdx int, lastSeen map[int]int64) map[int][]NVMLUtilSample {
	if lastSeen == nil {
		return nil
	}
	b := getNVMLBackend()
	if !b.Available() {
		return nil
	}
	out, ok := b.ProcessUtilization(devIdx, lastSeen)
	if !ok {
		nvmlWarnOnce("gpu_process_utilization_failed")
		return nil
	}
	// Record the max timestamp seen per PID — same as Python.
	for pid, samples := range out {
		for _, s := range samples {
			if s.TimeStamp > lastSeen[pid] {
				lastSeen[pid] = s.TimeStamp
			}
		}
	}
	return out
}

// GetNVMLMemoryInfo returns device-level VRAM info; nil on failure.
func GetNVMLMemoryInfo(devIdx int) *NVMLMemInfo {
	b := getNVMLBackend()
	if !b.Available() {
		return nil
	}
	m, ok := b.MemoryInfo(devIdx)
	if !ok {
		nvmlWarnOnce("gpu_memory_info_failed")
		return nil
	}
	return &m
}

// ─── Test backend ───────────────────────────────────────────────────────

// MockNVMLBackend is a deterministic backend for tests. The exported
// fields are configured before the backend is registered via
// SetNVMLBackendForTests; the interface methods are wired below.
type MockNVMLBackend struct {
	// Available toggles every accessor at once. Field is named with a
	// trailing underscore in struct layout to avoid clash with the
	// Available() method required by the NVMLBackend interface.
	Available      bool
	DeviceCount    int
	ProductNames   map[int]string
	MIGModes       map[int]bool
	Procs          map[int][]NVMLProcessInfo
	// Utilizations: map[devIdx][pid][]Sample. Single-element slice gives
	// the legacy single-sample behaviour; multi-element slices exercise
	// B2 integration math.
	Utilizations   map[int]map[int][]NVMLUtilSample
	Memory         map[int]NVMLMemInfo
	ProductNameErr map[int]bool
	DeviceCountErr bool

	// PerCallUtilization, when set, returns the slice element at index
	// invocationCount[devIdx] on each call, allowing tests to model NVML
	// snapshot evolution across start/end calls.
	PerCallUtilization map[int][]map[int][]NVMLUtilSample
	invocationCount    map[int]int

	mu sync.Mutex
}

// nvmlAvailable is the internal helper.
func (m *MockNVMLBackend) nvmlAvailable() bool {
	if m == nil {
		return false
	}
	return m.Available
}

// NVMLBackend interface methods. We use distinct method receiver names
// to avoid the field/method same-name collision.

func (m *MockNVMLBackend) Init() bool                    { return m.nvmlAvailable() }
func (m *MockNVMLBackend) Shutdown()                     {}

func (m *MockNVMLBackend) GetAvailable() bool { return m.nvmlAvailable() }

func (m *MockNVMLBackend) GetDeviceCount() (int, bool) {
	if !m.nvmlAvailable() || m.DeviceCountErr {
		return 0, false
	}
	return m.DeviceCount, true
}

func (m *MockNVMLBackend) GetProductName(devIdx int) (string, bool) {
	if !m.nvmlAvailable() {
		return "", false
	}
	if m.ProductNameErr != nil && m.ProductNameErr[devIdx] {
		return "", false
	}
	n, ok := m.ProductNames[devIdx]
	return n, ok
}

func (m *MockNVMLBackend) GetMIGMode(devIdx int) bool {
	if !m.nvmlAvailable() {
		return false
	}
	if m.MIGModes == nil {
		return false
	}
	return m.MIGModes[devIdx]
}

func (m *MockNVMLBackend) GetComputeRunningProcesses(devIdx int) ([]NVMLProcessInfo, bool) {
	if !m.nvmlAvailable() {
		return nil, false
	}
	if m.Procs == nil {
		return nil, true
	}
	return m.Procs[devIdx], true
}

func (m *MockNVMLBackend) GetProcessUtilization(devIdx int, _ map[int]int64) (map[int][]NVMLUtilSample, bool) {
	if !m.nvmlAvailable() {
		return nil, false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.PerCallUtilization != nil {
		if m.invocationCount == nil {
			m.invocationCount = map[int]int{}
		}
		seq := m.PerCallUtilization[devIdx]
		idx := m.invocationCount[devIdx]
		m.invocationCount[devIdx] = idx + 1
		if idx < len(seq) {
			return seq[idx], true
		}
		if len(seq) > 0 {
			return seq[len(seq)-1], true
		}
		return map[int][]NVMLUtilSample{}, true
	}
	if m.Utilizations == nil {
		return map[int][]NVMLUtilSample{}, true
	}
	return m.Utilizations[devIdx], true
}

func (m *MockNVMLBackend) GetMemoryInfo(devIdx int) (NVMLMemInfo, bool) {
	if !m.nvmlAvailable() {
		return NVMLMemInfo{}, false
	}
	if m.Memory == nil {
		return NVMLMemInfo{}, false
	}
	v, ok := m.Memory[devIdx]
	return v, ok
}

// mockAdapter wraps MockNVMLBackend to satisfy NVMLBackend (whose method
// names clash with the struct's exported field names).
type mockAdapter struct{ m *MockNVMLBackend }

func (a *mockAdapter) Available() bool               { return a.m.GetAvailable() }
func (a *mockAdapter) Init() bool                    { return a.m.Init() }
func (a *mockAdapter) Shutdown()                     { a.m.Shutdown() }
func (a *mockAdapter) DeviceCount() (int, bool)      { return a.m.GetDeviceCount() }
func (a *mockAdapter) ProductName(i int) (string, bool) { return a.m.GetProductName(i) }
func (a *mockAdapter) MIGMode(i int) bool            { return a.m.GetMIGMode(i) }
func (a *mockAdapter) ComputeRunningProcesses(i int) ([]NVMLProcessInfo, bool) {
	return a.m.GetComputeRunningProcesses(i)
}
func (a *mockAdapter) ProcessUtilization(i int, ls map[int]int64) (map[int][]NVMLUtilSample, bool) {
	return a.m.GetProcessUtilization(i, ls)
}
func (a *mockAdapter) MemoryInfo(i int) (NVMLMemInfo, bool) { return a.m.GetMemoryInfo(i) }

// AsBackend exposes the mock as an NVMLBackend (used by tests that
// register via SetNVMLBackendForTests).
func (m *MockNVMLBackend) AsBackend() NVMLBackend { return &mockAdapter{m: m} }
