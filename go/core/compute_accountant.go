// Per-task compute accountant.
//
// Holds start cgroup snapshot + runtime context for one dexcost task. At
// task finalize, emits exactly one compute_cost event with cost_pending:
// true — the pricing engine back-fills cost_usd via the deferred-cost
// pattern inherited from network v2 §6.4.
//
// Capture §5.3 invariant: at most one event per task per runtime.
// Idempotent — second call to SnapshotEndAndBuild / BuildServerlessEvent
// returns nil.
//
// Mirrors python/src/dexcost/compute_accountant.py.

package core

import (
	"runtime"
	"strings"
	"sync"
)

// billingModelFor maps a RuntimeKind to the details.billing_model
// discriminator string used by the pricing engine.
func billingModelFor(r RuntimeKind) string {
	switch r {
	case RuntimeLambda:
		return "lambda"
	case RuntimeFargate:
		return "fargate"
	case RuntimeEC2:
		return "ec2"
	case RuntimeGCE:
		return "gce"
	case RuntimeAzureVM:
		return "azure_vm"
	case RuntimeCloudRun:
		return "cloud_run_request"
	case RuntimeCloudFunctions:
		return "cloud_functions"
	case RuntimeAzureFunctions:
		return "azure_functions"
	case RuntimeVercel:
		return "vercel_fluid"
	case RuntimeK8sPod:
		return "k8s_pod"
	}
	return "unknown"
}

// detectArch detects host architecture for Lambda / Fargate / EC2 rate
// selection. Uses runtime.GOARCH (Go's compile-time arch).
func detectArch() string {
	a := strings.ToLower(runtime.GOARCH)
	if a == "arm64" || strings.Contains(a, "aarch64") {
		return "arm64"
	}
	return "x86_64"
}

// ComputeAccountant accumulates compute state for one dexcost task.
// Single-writer; lock-guarded for the freeze flag.
type ComputeAccountant struct {
	mu                 sync.Mutex
	frozen             bool
	Runtime            RuntimeKind
	LambdaMemoryMB     int
	FargateVCPU        float64
	FargateMemoryMiB   int
	Architecture       string
	InitializationType string
	Region             string
	hasFargateVCPU     bool

	startCPUUsec   int64
	startCPULoaded bool
}

// ComputeAccountantOption configures a ComputeAccountant. Go-idiomatic
// functional options.
type ComputeAccountantOption func(*ComputeAccountant)

func WithLambdaMemoryMB(mb int) ComputeAccountantOption {
	return func(a *ComputeAccountant) { a.LambdaMemoryMB = mb }
}
func WithFargateVCPU(v float64) ComputeAccountantOption {
	return func(a *ComputeAccountant) { a.FargateVCPU = v; a.hasFargateVCPU = true }
}
func WithFargateMemoryMiB(mib int) ComputeAccountantOption {
	return func(a *ComputeAccountant) { a.FargateMemoryMiB = mib }
}
func WithArchitecture(arch string) ComputeAccountantOption {
	return func(a *ComputeAccountant) { a.Architecture = arch }
}
func WithInitializationType(t string) ComputeAccountantOption {
	return func(a *ComputeAccountant) { a.InitializationType = t }
}
func WithRegion(r string) ComputeAccountantOption {
	return func(a *ComputeAccountant) { a.Region = r }
}

// NewComputeAccountant builds an accountant for the given runtime. Optional
// knobs are applied left-to-right (functional options).
func NewComputeAccountant(runtime RuntimeKind, opts ...ComputeAccountantOption) *ComputeAccountant {
	a := &ComputeAccountant{Runtime: runtime}
	for _, opt := range opts {
		opt(a)
	}
	if a.Architecture == "" {
		a.Architecture = detectArch()
	}
	return a
}

// SnapshotStart captures the cgroup CPU counter at task start. Idempotent.
func (a *ComputeAccountant) SnapshotStart() {
	a.mu.Lock()
	if a.startCPULoaded {
		a.mu.Unlock()
		return
	}
	a.mu.Unlock()

	s, ok := ReadCPUStat()
	a.mu.Lock()
	if !a.startCPULoaded {
		if ok {
			a.startCPUUsec = s.UsageUsec
		} else {
			a.startCPUUsec = 0
		}
		a.startCPULoaded = true
	}
	a.mu.Unlock()
}

// SnapshotEndAndBuild captures cgroup CPU/memory at task end and builds the
// event details. Returns nil if already frozen (second call) or runtime is
// unknown.
func (a *ComputeAccountant) SnapshotEndAndBuild(durationMS int64) map[string]any {
	a.mu.Lock()
	if a.frozen {
		a.mu.Unlock()
		return nil
	}
	a.frozen = true
	startCPU := a.startCPUUsec
	a.mu.Unlock()

	end, endOk := ReadCPUStat()
	cpuMax, cpuMaxOk := ReadCPUMax()
	// capture §6 case 6 — memory.peak missing → fall back to memory.current.
	memPeak, peakOk := ReadMemoryPeak()
	if !peakOk {
		if cur, ok := ReadMemoryCurrent(); ok {
			memPeak = cur
		} else {
			memPeak = 0
		}
	}
	memLimit, limitOk := ReadMemoryMax()
	if !limitOk {
		memLimit = 0
	}

	var vcpuSecondsUsed float64
	if endOk && end.UsageUsec >= startCPU {
		vcpuSecondsUsed = float64(end.UsageUsec-startCPU) / 1_000_000.0
	}

	var vcpuCount float64
	if cpuMaxOk {
		vcpuCount = cpuMax.VCPUCount
	} else {
		n := runtime.NumCPU()
		if n < 1 {
			n = 1
		}
		vcpuCount = float64(n)
	}

	return map[string]any{
		"billing_model":       billingModelFor(a.Runtime),
		"duration_ms":         durationMS,
		"memory_bytes_peak":   memPeak,
		"memory_bytes_limit":  memLimit,
		"vcpu_count":          vcpuCount,
		"vcpu_seconds_used":   vcpuSecondsUsed,
		"invocation_count":    0,
		"region":              a.Region,
		"architecture":        a.Architecture,
		"initialization_type": nil,
		"cost_pending":        true,
	}
}

// BuildServerlessEvent builds a per-invocation event for Lambda / Cloud
// Run / Cloud Functions / Azure Functions / Vercel. Idempotent — second
// call returns nil.
func (a *ComputeAccountant) BuildServerlessEvent(durationMS int64, memoryBytesPeak int64) map[string]any {
	a.mu.Lock()
	if a.frozen {
		a.mu.Unlock()
		return nil
	}
	a.frozen = true
	a.mu.Unlock()

	var memLimit int64
	var vcpuCount float64

	switch a.Runtime {
	case RuntimeLambda:
		// Lambda's AWS_LAMBDA_FUNCTION_MEMORY_SIZE is DECIMAL MB (10^6 bytes).
		mb := a.LambdaMemoryMB
		if mb == 0 {
			mb = 128
		}
		memLimit = int64(mb) * 1_000_000
		vcpuCount = vcpuCountFromCgroup()
	case RuntimeFargate:
		memLimit = int64(a.FargateMemoryMiB) * 1024 * 1024
		if a.hasFargateVCPU {
			vcpuCount = a.FargateVCPU
		} else {
			vcpuCount = vcpuCountFromCgroup()
		}
	default:
		// Cloud Run / Cloud Functions / Azure Functions / Vercel — cgroup
		// memory.max is the declared limit.
		if v, ok := ReadMemoryMax(); ok {
			memLimit = v
		} else {
			memLimit = memoryBytesPeak
		}
		vcpuCount = vcpuCountFromCgroup()
	}

	var initType any
	if a.InitializationType != "" {
		initType = a.InitializationType
	}
	return map[string]any{
		"billing_model":       billingModelFor(a.Runtime),
		"duration_ms":         durationMS,
		"memory_bytes_peak":   memoryBytesPeak,
		"memory_bytes_limit":  memLimit,
		"vcpu_count":          vcpuCount,
		"vcpu_seconds_used":   0,
		"invocation_count":    1,
		"region":              a.Region,
		"architecture":        a.Architecture,
		"initialization_type": initType,
		"cost_pending":        true,
	}
}

// vcpuCountFromCgroup is the cgroup-or-nproc shared helper used by the
// serverless paths.
func vcpuCountFromCgroup() float64 {
	if m, ok := ReadCPUMax(); ok {
		return m.VCPUCount
	}
	n := runtime.NumCPU()
	if n < 1 {
		n = 1
	}
	return float64(n)
}

// ─── Registry ────────────────────────────────────────────────────────────
//
// Same pattern as the NetworkAccountant registry — the Task struct is value-
// typed and serialised; the accountant lives outside, indexed by task_id.
// Tracker registers on task start, unregisters at finalize.

var (
	computeRegistryMu sync.RWMutex
	computeRegistry   = map[string]*ComputeAccountant{}
)

// RegisterComputeAccountant registers a task's compute accountant.
func RegisterComputeAccountant(taskID string, a *ComputeAccountant) {
	computeRegistryMu.Lock()
	defer computeRegistryMu.Unlock()
	computeRegistry[taskID] = a
}

// GetComputeAccountant resolves a task's accountant; nil if none.
func GetComputeAccountant(taskID string) *ComputeAccountant {
	computeRegistryMu.RLock()
	defer computeRegistryMu.RUnlock()
	return computeRegistry[taskID]
}

// UnregisterComputeAccountant removes and returns the accountant.
func UnregisterComputeAccountant(taskID string) *ComputeAccountant {
	computeRegistryMu.Lock()
	defer computeRegistryMu.Unlock()
	a := computeRegistry[taskID]
	delete(computeRegistry, taskID)
	return a
}

// ResetComputeRegistryForTests clears the registry.
func ResetComputeRegistryForTests() {
	computeRegistryMu.Lock()
	defer computeRegistryMu.Unlock()
	computeRegistry = map[string]*ComputeAccountant{}
}

// IsLongRunningRuntime returns true for runtimes whose accountant uses the
// start/end snapshot path (vs serverless per-invocation events).
func IsLongRunningRuntime(r RuntimeKind) bool {
	switch r {
	case RuntimeFargate, RuntimeEC2, RuntimeGCE, RuntimeAzureVM, RuntimeK8sPod:
		return true
	}
	return false
}
