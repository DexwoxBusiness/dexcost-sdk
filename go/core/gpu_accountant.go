// Per-task GPU accountant — Phase 2 v1 capture. Mirrors python commit 0d47371.
//
// Mirrors python/src/dexcost/gpu_accountant.py.
//
// One instance per dexcost task. Lives outside the Task struct in a
// global registry (Go-idiomatic pattern matching the existing
// ComputeAccountant + NetworkAccountant registries). Tracker registers
// on task start, unregisters at finalize.
//
// At task finalize, the accountant:
//
//  1. Snapshots NVML utilization across all devices with persisted
//     timestamps (Decision #8).
//  2. Walks the cgroup PIDs (Decision #1) and accumulates SM-time across
//     them per device.
//  3. Computes window-averaged sm_util_pct per Decision #3 sharpening
//     (NOT a point sample at finalize).
//  4. Resolves the GPU SKU via NVML productName alias matching.
//  5. Emits one gpu_cost event (cost_pending=true; the pricing engine
//     back-fills) AND one gpu_utilization_signal per touched device.
//
// Idempotent — second call to SnapshotEndAndBuild returns nil, nil.

package core

import (
	"os"
	"sync"

	"github.com/DexwoxBusiness/dexcost-go/cloud"
)

// billingModelForGpuRuntime maps a GpuRuntimeKind → billing_model discriminator.
func billingModelForGpuRuntime(r GpuRuntimeKind) string {
	switch r {
	case GpuRuntimeModal, GpuRuntimeRunpod, GpuRuntimeReplicate:
		return "per_gpu_second_active"
	case GpuRuntimeLambdaLabs, GpuRuntimeCoreweave, GpuRuntimeGCPGCEN1Attached:
		return "per_gpu_hour_reserved"
	case GpuRuntimeAWSEC2GPU, GpuRuntimeGCPGCEBundled, GpuRuntimeAzureVMGPU:
		return "per_instance_hour"
	case GpuRuntimeAzureVMVGPU:
		return "per_vgpu_hour"
	}
	return "per_gpu_second_active"
}

// resolveSKUFromProductName is best-effort substring → canonical key mapping.
// The pricing engine does the authoritative catalog-alias lookup; this is a
// coarse hint baked into details.gpu_sku.
func resolveSKUFromProductName(productNameLower string) string {
	if productNameLower == "" {
		return ""
	}
	switch {
	case substringIndexOf(productNameLower, "h100") >= 0:
		return "h100-80gb-sxm5"
	case substringIndexOf(productNameLower, "h200") >= 0:
		return "h200-141gb-sxm5"
	case substringIndexOf(productNameLower, "a100") >= 0:
		if substringIndexOf(productNameLower, "40gb") >= 0 {
			return "a100-40gb-sxm4"
		}
		return "a100-80gb-sxm4"
	case substringIndexOf(productNameLower, "a10g") >= 0:
		return "a10g-24gb"
	case substringIndexOf(productNameLower, "a10-4q") >= 0:
		return "a10-vgpu-1of6"
	case substringIndexOf(productNameLower, "a10-8q") >= 0:
		return "a10-vgpu-1of3"
	case substringIndexOf(productNameLower, "a10-12q") >= 0:
		return "a10-vgpu-1of2"
	case substringIndexOf(productNameLower, "a10-24q") >= 0 || substringIndexOf(productNameLower, "a10") >= 0:
		return "a10"
	case substringIndexOf(productNameLower, "l40s") >= 0:
		return "l40s-48gb"
	case substringIndexOf(productNameLower, "l4") >= 0:
		return "l4-24gb"
	case substringIndexOf(productNameLower, "tesla t4") >= 0 || substringIndexOf(productNameLower, "nvidia t4") >= 0:
		return "t4-16gb"
	case substringIndexOf(productNameLower, "rtx 6000") >= 0:
		return "rtx-6000-24gb"
	}
	return ""
}

func substringIndexOf(s, sub string) int {
	if len(sub) == 0 {
		return 0
	}
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func vgpuProfileForInstance(instanceType string) string {
	switch instanceType {
	case "Standard_NV6ads_A10_v5":
		return "1/6 A10"
	case "Standard_NV12ads_A10_v5":
		return "1/3 A10"
	case "Standard_NV18ads_A10_v5":
		return "1/2 A10"
	case "Standard_NV36ads_A10_v5":
		return "full A10"
	case "Standard_NV72ads_A10_v5":
		return "2x A10"
	}
	return ""
}

// GpuAccountant accumulates per-task GPU state.
type GpuAccountant struct {
	mu sync.Mutex

	Runtime  GpuRuntimeKind
	CloudEnv cloud.CloudEnv

	frozen bool

	scope                    CgroupScope
	scopeSet                 bool
	initialPIDs              map[int]struct{}
	initialTimestamps        map[int]map[int]int64 // devIdx → PID → ts
	deviceProductNames       map[int]string
	deviceMIGModes           map[int]bool
	deviceCount              int
	vramTotal                map[int]int64
	vramUsedPeak             map[int]int64
	pidsTouchedPerDevice     map[int]map[int]struct{}
}

// NewGpuAccountant builds an accountant for the given runtime + cloud env.
func NewGpuAccountant(runtime GpuRuntimeKind, env cloud.CloudEnv) *GpuAccountant {
	return &GpuAccountant{
		Runtime:              runtime,
		CloudEnv:             env,
		initialPIDs:          map[int]struct{}{},
		initialTimestamps:    map[int]map[int]int64{},
		deviceProductNames:   map[int]string{},
		deviceMIGModes:       map[int]bool{},
		vramTotal:            map[int]int64{},
		vramUsedPeak:         map[int]int64{},
		pidsTouchedPerDevice: map[int]map[int]struct{}{},
	}
}

// SetScopeForTests overrides the cgroup scope captured at SnapshotStart.
// Test-only — production accountant reads /proc/self/cgroup.
func (a *GpuAccountant) SetScopeForTests(s CgroupScope) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.scope = s
	a.scopeSet = true
}

// SnapshotStart initializes NVML, snapshots cgroup PIDs, captures baseline
// NVML timestamps. Idempotent.
func (a *GpuAccountant) SnapshotStart() {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.deviceCount > 0 {
		return
	}
	if !InitNVML() {
		return
	}
	count := GetNVMLDeviceCount()
	if count == nil || *count == 0 {
		return
	}
	a.deviceCount = *count
	for i := 0; i < a.deviceCount; i++ {
		if name := GetNVMLProductName(i); name != nil {
			a.deviceProductNames[i] = *name
		}
		a.deviceMIGModes[i] = GetNVMLMIGMode(i)
		if mem := GetNVMLMemoryInfo(i); mem != nil {
			a.vramTotal[i] = mem.TotalBytes
			a.vramUsedPeak[i] = mem.UsedBytes
		}
		a.initialTimestamps[i] = map[int]int64{}
		a.pidsTouchedPerDevice[i] = map[int]struct{}{}
		baseline := GetNVMLProcessUtilization(i, a.initialTimestamps[i])
		for pid := range baseline {
			a.pidsTouchedPerDevice[i][pid] = struct{}{}
		}
	}
	if !a.scopeSet {
		a.scope = ClassifyCgroupScope()
		a.scopeSet = true
	}
	pids := EnumerateCgroupPIDs(a.scope, "")
	if pids == nil {
		// cgroup walk denied at start; degrade to self-PID only.
		a.initialPIDs[os.Getpid()] = struct{}{}
	} else {
		for _, p := range pids {
			a.initialPIDs[p] = struct{}{}
		}
	}
}

// SnapshotEndAndBuild returns (cost_event_details, []signal_event_details).
// Returns (nil, nil) on second call (idempotent), when NVML wasn't
// available at start, or when no devices were touched.
func (a *GpuAccountant) SnapshotEndAndBuild(durationMS int64) (map[string]any, []map[string]any) {
	a.mu.Lock()
	if a.frozen {
		a.mu.Unlock()
		return nil, nil
	}
	a.frozen = true
	deviceCount := a.deviceCount
	scope := a.scope
	a.mu.Unlock()

	if deviceCount == 0 {
		return nil, nil
	}

	// End cgroup walk + Decision #1 fallback label.
	var fallbackLabel string
	endPIDs := EnumerateCgroupPIDs(scope, "")
	cgroupPIDUnion := map[int]struct{}{}
	for p := range a.initialPIDs {
		cgroupPIDUnion[p] = struct{}{}
	}
	if endPIDs == nil {
		fallbackLabel = "self_pid_only"
		cgroupPIDUnion[os.Getpid()] = struct{}{}
	} else {
		fallbackLabel = FallbackLabelForScope(scope)
		for _, p := range endPIDs {
			cgroupPIDUnion[p] = struct{}{}
		}
	}

	// Canonical product name + SKU.
	var canonicalProduct string
	for i := 0; i < deviceCount; i++ {
		if n, ok := a.deviceProductNames[i]; ok && n != "" {
			canonicalProduct = n
			break
		}
	}
	gpuSku := resolveSKUFromProductName(canonicalProduct)

	// MIG-profile transparency.
	var migProfile string
	for i := 0; i < deviceCount; i++ {
		if a.deviceMIGModes[i] {
			migProfile = "mig_detected"
			break
		}
	}

	degenerate := durationMS <= 0

	signals := []map[string]any{}
	perDeviceGPUSeconds := map[int]float64{}
	anyPIDTouched := false

	for i := 0; i < deviceCount; i++ {
		// Snapshot baseTS BEFORE the end call mutates initialTimestamps.
		baseTS := int64(0)
		first := true
		for _, ts := range a.initialTimestamps[i] {
			if first || ts < baseTS {
				baseTS = ts
				first = false
			}
		}
		end := GetNVMLProcessUtilization(i, a.initialTimestamps[i])
		for pid := range end {
			a.pidsTouchedPerDevice[i][pid] = struct{}{}
		}
		if mem := GetNVMLMemoryInfo(i); mem != nil {
			if mem.UsedBytes > a.vramUsedPeak[i] {
				a.vramUsedPeak[i] = mem.UsedBytes
			}
		}
		// Filter to cgroup PID set.
		var relevant []NVMLUtilSample
		for pid, s := range end {
			if _, in := cgroupPIDUnion[pid]; in {
				relevant = append(relevant, s)
			}
		}
		if len(relevant) > 0 {
			anyPIDTouched = true
			maxTS := int64(0)
			for _, s := range relevant {
				if s.TimeStamp > maxTS {
					maxTS = s.TimeStamp
				}
			}
			delta := maxTS - baseTS
			if delta < 0 {
				delta = 0
			}
			seconds := float64(delta) / 1_000_000.0
			perDeviceGPUSeconds[i] = seconds

			var smUtilPct interface{}
			if durationMS > 0 {
				ws := float64(durationMS) / 1000.0
				v := seconds / ws * 100.0
				if v > 100.0 {
					v = 100.0
				}
				smUtilPct = v
			} else {
				smUtilPct = nil
			}

			memUtilSum := 0
			for _, s := range relevant {
				memUtilSum += s.MemUtil
			}
			memUtilAvg := float64(memUtilSum) / float64(len(relevant))

			signals = append(signals, map[string]any{
				"gpu_index":             i,
				"gpu_sku":               gpuSku,
				"sm_util_pct":           smUtilPct,
				"mem_util_pct":          memUtilAvg,
				"vram_used_peak_bytes":  a.vramUsedPeak[i],
				"vram_total_bytes":      a.vramTotal[i],
				"process_count":         len(a.pidsTouchedPerDevice[i]),
				"sample_count":          len(relevant),
				"task_duration_ms":      durationMS,
			})
		} else if degenerate {
			signals = append(signals, map[string]any{
				"gpu_index":             i,
				"gpu_sku":               gpuSku,
				"sm_util_pct":           nil,
				"mem_util_pct":          nil,
				"vram_used_peak_bytes":  a.vramUsedPeak[i],
				"vram_total_bytes":      a.vramTotal[i],
				"process_count":         len(a.pidsTouchedPerDevice[i]),
				"sample_count":          0,
				"task_duration_ms":      durationMS,
			})
		}
	}

	anyMIG := migProfile != ""
	shouldEmitCost := anyPIDTouched || fallbackLabel != "" || degenerate || anyMIG
	if !shouldEmitCost {
		return nil, nil
	}

	totalGPUSeconds := 0.0
	for _, s := range perDeviceGPUSeconds {
		totalGPUSeconds += s
	}
	cost := map[string]any{
		"billing_model":    billingModelForGpuRuntime(a.Runtime),
		"gpu_vendor":       "nvidia",
		"gpu_sku":          gpuSku,
		"gpu_count":        deviceCount,
		"region":           a.CloudEnv.Region,
		"duration_ms":      durationMS,
		"gpu_seconds_used": totalGPUSeconds,
		"instance_type":    a.CloudEnv.InstanceType,
		"cost_pending":     true,
	}
	if vp := vgpuProfileForInstance(a.CloudEnv.InstanceType); vp != "" && a.Runtime == GpuRuntimeAzureVMVGPU {
		cost["vgpu_profile"] = vp
	} else {
		cost["vgpu_profile"] = nil
	}
	if migProfile != "" {
		cost["mig_profile"] = migProfile
	} else {
		cost["mig_profile"] = nil
	}
	if canonicalProduct != "" {
		cost["_nvml_product_name_lower"] = canonicalProduct
	}
	if fallbackLabel != "" {
		cost["_cgroup_scope_fallback"] = fallbackLabel
	}

	if len(signals) == 0 {
		return cost, nil
	}
	return cost, signals
}

// ─── Registry ───────────────────────────────────────────────────────────

var (
	gpuRegistryMu sync.RWMutex
	gpuRegistry   = map[string]*GpuAccountant{}
)

// RegisterGpuAccountant attaches a task's GPU accountant.
func RegisterGpuAccountant(taskID string, a *GpuAccountant) {
	gpuRegistryMu.Lock()
	defer gpuRegistryMu.Unlock()
	gpuRegistry[taskID] = a
}

// GetGpuAccountant resolves the task's accountant, or nil.
func GetGpuAccountant(taskID string) *GpuAccountant {
	gpuRegistryMu.RLock()
	defer gpuRegistryMu.RUnlock()
	return gpuRegistry[taskID]
}

// UnregisterGpuAccountant removes + returns the accountant.
func UnregisterGpuAccountant(taskID string) *GpuAccountant {
	gpuRegistryMu.Lock()
	defer gpuRegistryMu.Unlock()
	a := gpuRegistry[taskID]
	delete(gpuRegistry, taskID)
	return a
}

// ResetGpuAccountantRegistryForTests clears the registry.
func ResetGpuAccountantRegistryForTests() {
	gpuRegistryMu.Lock()
	defer gpuRegistryMu.Unlock()
	gpuRegistry = map[string]*GpuAccountant{}
}
